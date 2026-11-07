"""TypeScript ソースから LLM に渡す「事実」を抽出する。

TODO(技術的負債): 抽出は正規表現による簡易版。export const { a, b } = ... のような分割代入や、
export const a = 1, b = 2 の2つ目以降は公開識別子として拾わない。
本番では TypeScript Compiler API などの構文解析に置き換える。
"""

import re

from .source_common import SourceFacts, strip_comments, strip_comments_and_strings

# import ... from "x" / export ... from "x" / import "x" / require("x")
_FROM_RE = re.compile(r"""\bfrom\s*["']([^"'\n]+)["']""")
_SIDE_EFFECT_IMPORT_RE = re.compile(r"""^\s*import\s*["']([^"'\n]+)["']""", re.MULTILINE)
_REQUIRE_RE = re.compile(r"""\brequire\s*\(\s*["']([^"'\n]+)["']\s*\)""")

_EXPORT_DECL_RE = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(?:function\s*\*?|class|interface|type|enum|const\s+enum|const|let|var|namespace|module)\s+(\w+)",
    re.MULTILINE,
)
# export { a, b as c } / export type { T } / export { x } from "./x"
_EXPORT_LIST_RE = re.compile(r"\bexport\s+(?:type\s+)?\{([^}]*)\}")

_FUNCTION_RE = re.compile(r"\bfunction\s*\*?\s*(\w+)\s*(?:<[^()]*>)?\s*\(")
_ARROW_RE = re.compile(r"\b(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|\w+)\s*(?::[^=]+)?=>")
# 行頭の宣言だけを見る（export { type User } や import type { User } を型の宣言と誤認しない）
_TYPE_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:const\s+)?(?:class|interface|type|enum)\s+(\w+)",
    re.MULTILINE,
)


def _export_list_names(body: str) -> list[str]:
    names = []
    for item in body.split(","):
        words = item.split()
        if not words:
            continue
        name = words[-1] if "as" in words else words[0]  # "a as b" は b の名前で公開される
        if name == "type" and len(words) > 1:  # export { type T }
            name = words[1]
        if name != "default" and re.fullmatch(r"\w+", name):
            names.append(name)
    return names


def extract_facts(source: str) -> SourceFacts:
    with_strings = strip_comments(source)  # import 先は文字列で書かれるので、文字列を残したまま探す
    code = strip_comments_and_strings(source)

    imports = _FROM_RE.findall(with_strings) + _SIDE_EFFECT_IMPORT_RE.findall(with_strings)
    imports += _REQUIRE_RE.findall(with_strings)

    exported = set(_EXPORT_DECL_RE.findall(code))
    for body in _EXPORT_LIST_RE.findall(code):
        exported.update(_export_list_names(body))

    functions = list(dict.fromkeys(_FUNCTION_RE.findall(code) + _ARROW_RE.findall(code)))
    return SourceFacts(
        language="TypeScript",
        package=None,  # TypeScript にはパッケージ宣言がない（ファイルが1つのモジュール）
        imports=sorted(set(imports)),
        functions=functions,
        types=sorted(set(_TYPE_RE.findall(code))),
        exported=sorted(exported),
    )
