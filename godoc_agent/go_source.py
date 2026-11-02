"""Goソースの列挙と、LLMに渡す「事実」の抽出。

TODO(技術的負債): 抽出は正規表現による簡易版。ジェネリクスや複数行の宣言を
取りこぼす可能性があるため、本番では go/ast を使う補助ツールに置き換える。
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

_EXCLUDED_DIRS = {"vendor", "testdata", ".git", "node_modules"}
# 設計書の対象は <xx>_api/pkg/handler 配下（サブディレクトリを含む）だけ
_API_DIR_SUFFIX = "_api"
_HANDLER_SUBPATH = ("pkg", "handler")

_PACKAGE_RE = re.compile(r"^package\s+(\w+)", re.MULTILINE)
# func Name( / func (r *Recv) Name( / func Name[T any](
_FUNC_RE = re.compile(r"^func\s+(?:\(\s*\w*\s*\*?\s*([\w.]+)[^)]*\)\s*)?(\w+)\s*[\[(]", re.MULTILINE)
_TYPE_RE = re.compile(r"^type\s+(\w+)\b", re.MULTILINE)
_TYPE_BLOCK_RE = re.compile(r"^type\s*\((.*?)^\)", re.MULTILINE | re.DOTALL)
# type ( ... ) ブロック内の宣言。1段目のインデントだけを見て、構造体のフィールドを拾わない
_TYPE_BLOCK_ENTRY_RE = re.compile(r"^[ \t](\w+)\b", re.MULTILINE)
_IMPORT_BLOCK_RE = re.compile(r"^import\s*\((.*?)\)", re.MULTILINE | re.DOTALL)
_IMPORT_LINE_RE = re.compile(r'^import\s+(?:\w+\s+)?"([^"]+)"', re.MULTILINE)
_QUOTED_RE = re.compile(r'"([^"]+)"')


@dataclass(frozen=True)
class GoFacts:
    package: str
    imports: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)  # メソッドは "Recv.Name"
    types: list[str] = field(default_factory=list)

    @property
    def exported(self) -> list[str]:
        """設計書に必ず記載されるべき公開識別子（メソッドは名前部分で判定）。"""
        names = [n.split(".")[-1] for n in self.functions + self.types]
        return sorted({n for n in names if n[:1].isupper()})

    def to_prompt(self) -> str:
        def fmt(items: list[str]) -> str:
            return ", ".join(items) if items else "(なし)"

        return (
            f"- package: {self.package}\n"
            f"- imports: {fmt(self.imports)}\n"
            f"- types: {fmt(self.types)}\n"
            f"- functions/methods: {fmt(self.functions)}"
        )


def _is_handler_dir(dir_parts: tuple[str, ...]) -> bool:
    n = len(_HANDLER_SUBPATH)
    for i in range(len(dir_parts) - n):
        api_dir = dir_parts[i]
        if (
            api_dir.endswith(_API_DIR_SUFFIX)
            and len(api_dir) > len(_API_DIR_SUFFIX)
            and dir_parts[i + 1 : i + 1 + n] == _HANDLER_SUBPATH
        ):
            return True
    return False


def discover_go_files(repo: Path) -> list[Path]:
    files = []
    for path in repo.rglob("*.go"):
        rel_parts = path.relative_to(repo).parts
        if any(part in _EXCLUDED_DIRS or part.startswith(".") for part in rel_parts[:-1]):
            continue
        # --repo に xx_api 自体を指定した場合も判定できるよう、リポジトリ名を含めて調べる
        if not _is_handler_dir((repo.name, *rel_parts[:-1])):
            continue
        # テストコードは詳細設計書の対象外（運用上の決定。オプションで含めることもできない）
        if path.name.endswith("_test.go"):
            continue
        files.append(path)
    return sorted(files)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _strip_comments(source: str) -> str:
    # コメント内の "func Foo(" などを誤検出しないよう、行コメントとブロックコメントを除去
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", source)


def extract_facts(source: str) -> GoFacts:
    code = _strip_comments(source)
    pkg = _PACKAGE_RE.search(code)

    imports = _IMPORT_LINE_RE.findall(code)
    for block in _IMPORT_BLOCK_RE.findall(code):
        imports.extend(_QUOTED_RE.findall(block))

    functions = []
    for recv, name in _FUNC_RE.findall(code):
        functions.append(f"{recv.split('.')[-1]}.{name}" if recv else name)

    types = set(_TYPE_RE.findall(code))
    for block in _TYPE_BLOCK_RE.findall(code):
        # ブロック内の構造体フィールドは2段目以降のインデントなので除外される
        types.update(_TYPE_BLOCK_ENTRY_RE.findall(block))

    return GoFacts(
        package=pkg.group(1) if pkg else "(unknown)",
        imports=sorted(set(imports)),
        functions=functions,
        types=sorted(types),
    )
