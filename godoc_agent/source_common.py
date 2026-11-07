r"""言語に依存しないソース処理（字句の分割・コメント以外の差分検出・抽出結果の型）。

TODO(技術的負債): 字句の分割は C 系の言語に共通する規則（// と /* */ のコメント、
" ' ` の文字列）だけを見る簡易版。C# の @"..."（\ がエスケープにならない）や
TypeScript の正規表現リテラル（/\/*/ など）は正しく分割できないことがある。
"""

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field

# 識別子・数値はひとかたまり、記号は1文字ずつ。"a+b" と "a + b" を同じトークン列にする
_CODE_TOKEN_RE = re.compile(r"\w+|\n|[^\w\s]")


@dataclass(frozen=True)
class SourceFacts:
    """静的解析で抽出した、LLM に渡す「事実」。"""

    language: str  # プロンプトに書く言語名（例: Go）
    package: str | None  # package / namespace（ない言語は None）
    imports: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    # 設計書に必ず記載されるべき公開識別子。何を公開とみなすかは言語ごとに決まる
    exported: list[str] = field(default_factory=list)
    package_label: str = "package"

    def to_prompt(self) -> str:
        def fmt(items: list[str]) -> str:
            return ", ".join(items) if items else "(なし)"

        lines = [f"- {self.package_label}: {self.package}"] if self.package is not None else []
        lines += [
            f"- imports: {fmt(self.imports)}",
            f"- types: {fmt(self.types)}",
            f"- functions/methods: {fmt(self.functions)}",
        ]
        return "\n".join(lines)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scan(source: str) -> Iterator[tuple[str, str, int]]:
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


def strip_comments(source: str) -> str:
    # コメント内の "func Foo(" などを誤検出しないよう除去する（行番号は保つ）
    return "".join("\n" * text.count("\n") if kind == "comment" else text for kind, text, _ in scan(source))


def strip_comments_and_strings(source: str) -> str:
    # 文字列内の "class Foo" なども誤検出しないよう、文字列は中身を空にした "" に置き換える（行番号は保つ）
    def keep(kind: str, text: str) -> str:
        if kind == "code":
            return text
        return ('""' if kind == "string" else "") + "\n" * text.count("\n")

    return "".join(keep(kind, text) for kind, text, _ in scan(source))


def _code_tokens(source: str) -> list[tuple[str, int]]:
    """コメントと空白の違いを無視して比較するための (トークン, 行) の列。

    改行で文末を補う言語（Go・TypeScript）があるため改行だけは残し、連続する改行は1つにまとめる。
    """
    tokens: list[tuple[str, int]] = []

    def newline(line: int) -> None:
        if tokens and tokens[-1][0] != "\n":
            tokens.append(("\n", line))

    for kind, text, line in scan(source):
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
