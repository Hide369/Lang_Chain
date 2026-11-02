"""設計書テンプレートの読み込みとプロンプト組み立て。"""

import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from .go_source import GoFacts

_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

SYSTEM_PROMPT = """あなたはGo言語に精通したソフトウェアアーキテクトです。
与えられたGoソースファイル1本について、テンプレートに厳密に従った詳細設計書を日本語のMarkdownで作成してください。

ルール:
- テンプレートの「## 」見出しは、文言を変えずにすべて同じ順序で出力する
- 「抽出済みの事実」に記載された公開識別子は、すべて設計書内で言及する
- ソースに存在しない関数・型・挙動を推測で書かない。不明な点は「不明」と明記する
- 出力は設計書のMarkdown本文のみとし、前置きや```で囲むことはしない
"""


def load_template(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"設計書テンプレートが見つかりません: {path}")
    return path.read_text(encoding="utf-8")


def required_sections(template: str) -> list[str]:
    # HTMLコメント内の説明文を見出しと誤認しないよう除去してから抽出
    without_comments = re.sub(r"<!--.*?-->", "", template, flags=re.DOTALL)
    sections = _SECTION_RE.findall(without_comments)
    if not sections:
        raise ValueError("テンプレートに「## 」で始まる必須セクションがありません")
    return sections


def build_messages(template: str, rel_path: str, source: str, facts: GoFacts) -> list:
    human = f"""## テンプレート
{template}

## 対象ファイル
{rel_path}

## 抽出済みの事実（静的解析の結果。これと矛盾する記述をしないこと）
{facts.to_prompt()}

## ソースコード
```go
{source}
```"""
    return [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=human)]


def build_feedback(issues: list[str]) -> HumanMessage:
    bullet = "\n".join(f"- {i}" for i in issues)
    return HumanMessage(
        content=f"直前の設計書には次の問題があります。修正した設計書の全文を出力し直してください。\n{bullet}"
    )
