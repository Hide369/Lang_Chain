"""C# ソースから LLM に渡す「事実」を抽出する。

TODO(技術的負債): 抽出は正規表現による簡易版で、1行に収まった宣言だけを拾う。
インターフェースのメンバー（修飾子なしで公開になる）や、宣言の途中で改行したものは取りこぼす。
本番では Roslyn などの構文解析に置き換える。
"""

import re

from .source_common import SourceFacts, strip_comments_and_strings

_NAMESPACE_RE = re.compile(r"^\s*namespace\s+([\w.]+)", re.MULTILINE)
# using System; / using static System.Math; / using IO = System.IO;（using (...) 文や using var は除く）
_USING_RE = re.compile(r"^\s*(?:global\s+)?using\s+(?:static\s+)?(?:\w+\s*=\s*)?([\w.]+)\s*;", re.MULTILINE)
_MODIFIERS = (
    "public|private|protected|internal|static|abstract|sealed|partial|async|virtual|override|"
    "readonly|new|unsafe|extern|const|required|volatile|file"
)
# 属性（[HttpGet] など）に続く、修飾子付きの宣言の行
_DECL_RE = re.compile(rf"^[ \t]*(?:\[[^\]\n]*\][ \t]*)*((?:(?:{_MODIFIERS})\s+)+)(.*)$", re.MULTILINE)
_TYPE_DECL_RE = re.compile(r"^(?:class|struct|interface|enum|record(?:\s+(?:class|struct))?)\s+(\w+)")
_DELEGATE_RE = re.compile(r"^delegate\s+.*?(\w+)\s*(?:<[^()]*>)?\s*\(")
# メソッド・コンストラクター・プロパティ・フィールド・イベント。名前は ( { = ; => の直前の識別子
_MEMBER_RE = re.compile(r"^(?:[\w.<>\[\],?\s]+?\s+)?(\w+)\s*(?:<[^()]*?>)?\s*(\(|\{|=>|=|;)")
_TYPE_ANYWHERE_RE = re.compile(r"\b(?:class|struct|interface|enum|record(?:\s+(?:class|struct))?)\s+(\w+)")


def extract_facts(source: str) -> SourceFacts:
    code = strip_comments_and_strings(source)
    namespace = _NAMESPACE_RE.search(code)

    functions: list[str] = []
    exported: set[str] = set()
    for modifiers, rest in _DECL_RE.findall(code):
        is_public = re.search(r"\bpublic\b", modifiers) is not None
        rest = rest.strip()
        if m := _TYPE_DECL_RE.match(rest):
            name = m.group(1)
        elif m := _DELEGATE_RE.match(rest):
            name = m.group(1)
        elif m := _MEMBER_RE.match(rest):
            name = m.group(1)
            if m.group(2) == "(":
                functions.append(name)
        else:
            continue
        if is_public:
            exported.add(name)

    return SourceFacts(
        language="C#",
        package=namespace.group(1) if namespace else "(global)",
        package_label="namespace",
        imports=sorted(set(_USING_RE.findall(code))),
        functions=functions,
        types=sorted(set(_TYPE_ANYWHERE_RE.findall(code))),
        exported=sorted(exported),
    )
