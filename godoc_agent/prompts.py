"""スキル(SKILL.md)の読み込みとプロンプト組み立て。"""

from datetime import date
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
- 今日の日付は {today} とする。スキルで今日の日付（作成日・更新日など）を記述する箇所にはこの日付を使い、書式はスキルの指定に従う

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


NEW_DOC_SECTION = """## 前回の設計書
なし（新規作成）。スキルの履歴（バージョン管理）の手順に従い、初版として記載してください。"""

REVISION_SECTION = """## 前回の設計書（改訂）
このファイルの設計書は前回生成済みです。現在のソースに合わせて改訂してください。
- 履歴はスキルのバージョン管理の手順に従う。前回までの履歴の行は変更・削除せずすべて残し、今回の改訂を1件追加する
- 今回の改訂内容は、前回の設計書と現在のソースを比べて分かる変更点を記載する
- ソースに変更がない箇所の記述は、前回の設計書の内容を踏襲する

<previous_doc>
{previous_doc}
</previous_doc>"""


def build_agent_messages(
    skill: str, skill_dir: Path, today: date, rel_path: str, facts: GoFacts, previous_doc: str | None
) -> list:
    history = REVISION_SECTION.format(previous_doc=previous_doc) if previous_doc else NEW_DOC_SECTION
    human = f"""次のGoファイルの詳細設計書を作成してください: {rel_path}
（作業ディレクトリがGoリポジトリのルートです。対象ファイルや関連ファイルは Read / Grep / Glob で参照してください）

## 抽出済みの事実（静的解析の結果。これと矛盾する記述をしないこと。公開識別子はすべて設計書内で言及すること）
{facts.to_prompt()}

{history}"""
    system = AGENT_SYSTEM_PROMPT.format(skill_dir=skill_dir, today=today.isoformat(), skill=skill)
    return [SystemMessage(content=system), HumanMessage(content=human)]


def build_feedback(issues: list[str]) -> HumanMessage:
    bullet = "\n".join(f"- {i}" for i in issues)
    return HumanMessage(
        content=f"直前の設計書には次の問題があります。修正した設計書の全文を出力し直してください。\n{bullet}"
    )
