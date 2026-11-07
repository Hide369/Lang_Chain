"""Go ソースから LLM に渡す「事実」を抽出する。

TODO(技術的負債): 抽出は正規表現による簡易版。ジェネリクスや複数行の宣言を
取りこぼす可能性があるため、本番では go/ast を使う補助ツールに置き換える。
"""

import re

from .source_common import SourceFacts, strip_comments

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


def extract_facts(source: str) -> SourceFacts:
    code = strip_comments(source)
    pkg = _PACKAGE_RE.search(code)

    imports = _IMPORT_LINE_RE.findall(code)
    for block in _IMPORT_BLOCK_RE.findall(code):
        imports.extend(_QUOTED_RE.findall(block))

    functions = []
    for recv, name in _FUNC_RE.findall(code):
        functions.append(f"{recv.split('.')[-1]}.{name}" if recv else name)  # メソッドは "Recv.Name"

    types = set(_TYPE_RE.findall(code))
    for block in _TYPE_BLOCK_RE.findall(code):
        # ブロック内の構造体フィールドは2段目以降のインデントなので除外される
        types.update(_TYPE_BLOCK_ENTRY_RE.findall(block))

    # Go は大文字で始まる識別子が公開。メソッドは名前部分で判定する
    names = [n.split(".")[-1] for n in functions + sorted(types)]
    return SourceFacts(
        language="Go",
        package=pkg.group(1) if pkg else "(unknown)",
        imports=sorted(set(imports)),
        functions=functions,
        types=sorted(types),
        exported=sorted({n for n in names if n[:1].isupper()}),
    )
