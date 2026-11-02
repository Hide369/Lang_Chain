"""スキル(SKILL.md)の読み込みとプロンプト組み立て。"""

from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from .go_source import GoFacts

AGENT_SYSTEM_PROMPT = """あなたはGoの詳細設計書を一括生成するバッチ処理の一部として、非対話で動作しています。
<skill> に書かれた詳細設計書スキルの手順と記述ルールに従ってください。

バッチ処理のため、次のルールをスキルの記述より優先します:
- ファイルの作成・編集やコマンドの実行はしない。スキルにファイル保存の手順があっても、保存はバッチ側が行う
- 完成した設計書のMarkdown本文だけを最終応答として出力する。前置き・説明・```での囲みは付けない
- ユーザーへの質問はしない（回答は得られない）。不明な点は設計書内に「不明」と明記する
- スキルが参照している別ファイルは {skill_dir} から Read で読める

<skill>
{skill}
</skill>
"""


def load_skill(path: Path | None) -> str:
    if path is None:
        raise ValueError("環境変数 CLAUDE_SKILL_PATH に SKILL.md のパスを指定してください")
    if not path.is_file():
        raise FileNotFoundError(f"SKILL.md が見つかりません: {path}")
    return path.read_text(encoding="utf-8")


def build_agent_messages(skill: str, skill_dir: Path, rel_path: str, facts: GoFacts) -> list:
    human = f"""次のGoファイルの詳細設計書を作成してください: {rel_path}
（作業ディレクトリがGoリポジトリのルートです。対象ファイルや関連ファイルは Read / Grep / Glob で参照してください）

## 抽出済みの事実（静的解析の結果。これと矛盾する記述をしないこと。公開識別子はすべて設計書内で言及すること）
{facts.to_prompt()}"""
    system = AGENT_SYSTEM_PROMPT.format(skill_dir=skill_dir, skill=skill)
    return [SystemMessage(content=system), HumanMessage(content=human)]


def build_feedback(issues: list[str]) -> HumanMessage:
    bullet = "\n".join(f"- {i}" for i in issues)
    return HumanMessage(
        content=f"直前の設計書には次の問題があります。修正した設計書の全文を出力し直してください。\n{bullet}"
    )
