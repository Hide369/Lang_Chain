"""Goソースの列挙と、LLMに渡す「事実」の抽出。

TODO(技術的負債): 抽出は正規表現による簡易版。ジェネリクスや複数行の宣言を
取りこぼす可能性があるため、本番では go/ast を使う補助ツールに置き換える。
"""

import hashlib
import re
from collections.abc import Iterator
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
# 識別子・数値はひとかたまり、記号は1文字ずつ。"a+b" と "a + b" を同じトークン列にする
_CODE_TOKEN_RE = re.compile(r"\w+|\n|[^\w\s]")


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


def _scan(source: str) -> Iterator[tuple[str, str, int]]:
    """ソースを ("comment" | "string" | "code", 文字列, 開始行) に分ける。

    文字列リテラルを区別するのは、"http://..." の "//" をコメントと誤認しないため。
    """
    i, line, n = 0, 1, len(source)
    while i < n:
        c = source[i]
        if source.startswith("//", i):
            end = source.find("\n", i)
            kind, j = "comment", n if end == -1 else end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            kind, j = "comment", n if end == -1 else end + 2
        elif c == "`":
            end = source.find("`", i + 1)
            kind, j = "string", n if end == -1 else end + 1
        elif c in "\"'":
            j = i + 1
            while j < n and source[j] not in (c, "\n"):
                j += 2 if source[j] == "\\" else 1
            kind, j = "string", min(j + 1, n)
        else:
            j = i + 1
            while j < n and source[j] not in "/`\"'":
                j += 1
            kind = "code"
        text = source[i:j]
        yield kind, text, line
        line += text.count("\n")
        i = j


def _strip_comments(source: str) -> str:
    # コメント内の "func Foo(" などを誤検出しないよう除去する（行番号は保つ）
    return "".join("\n" * text.count("\n") if kind == "comment" else text for kind, text, _ in _scan(source))


def _code_tokens(source: str) -> list[tuple[str, int]]:
    """コメントと空白の違いを無視して比較するための (トークン, 行) の列。

    Go は改行で文末を補うため改行だけは残し、連続する改行は1つにまとめる。
    """
    tokens: list[tuple[str, int]] = []

    def newline(line: int) -> None:
        if tokens and tokens[-1][0] != "\n":
            tokens.append(("\n", line))

    for kind, text, line in _scan(source):
        if kind == "comment":
            if "\n" in text:  # 改行を含むブロックコメントは改行として扱われる
                newline(line)
        elif kind == "string":
            tokens.append((text, line))
        else:
            for m in _CODE_TOKEN_RE.finditer(text):
                tok_line = line + text.count("\n", 0, m.start())
                if m.group() == "\n":
                    newline(tok_line)
                else:
                    tokens.append((m.group(), tok_line))
    if tokens and tokens[-1][0] == "\n":
        tokens.pop()
    return tokens


def first_code_difference(original: str, modified: str) -> int | None:
    """コメントと空白以外に違いがあれば、元のソースでの最初の差分の行番号を返す。"""
    before, after = _code_tokens(original), _code_tokens(modified)
    for (b, line), (a, _) in zip(before, after):
        if a != b:
            return line
    if len(before) > len(after):
        # 改行トークンは前の行に属するため、削られた最初のコードの行を示す
        return next((line for tok, line in before[len(after):] if tok != "\n"), before[-1][1])
    if len(before) < len(after):
        return before[-1][1] if before else 1
    return None


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
