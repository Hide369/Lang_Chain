"""スキル(SKILL.md)の読み込みとプロンプト組み立て（設計書・コメント追記）。"""

from datetime import date
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from .source_common import SourceFacts

AGENT_SYSTEM_PROMPT = """あなたは{language}の詳細設計書を一括生成するバッチ処理の一部として、非対話で動作しています。
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


def load_skill(path: Path | None, env_name: str = "CLAUDE_SKILL_PATH") -> str:
    if path is None:
        raise ValueError(f"環境変数 {env_name} に SKILL.md のパスを指定してください")
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
    skill: str, skill_dir: Path, today: date, rel_path: str, facts: SourceFacts, previous_doc: str | None
) -> list:
    history = REVISION_SECTION.format(previous_doc=previous_doc) if previous_doc else NEW_DOC_SECTION
    human = f"""次の{facts.language}ファイルの詳細設計書を作成してください: {rel_path}
（作業ディレクトリがリポジトリのルートです。対象ファイルや関連ファイルは Read / Grep / Glob で参照してください）

## 抽出済みの事実（静的解析の結果。これと矛盾する記述をしないこと。公開識別子はすべて設計書内で言及すること）
{facts.to_prompt()}

{history}"""
    system = AGENT_SYSTEM_PROMPT.format(language=facts.language, skill_dir=skill_dir, today=today.isoformat(), skill=skill)
    return [SystemMessage(content=system), HumanMessage(content=human)]


# 再生成は新しいセッションで行う（会話を続けない）ため、直前の出力そのものを依頼文に添えて渡す
RETRY_SECTION = """## 直前の試行で見つかった問題（再生成）
直前に生成した{target}には次の問題がありました。<previous_attempt> をもとに問題を修正し、{target}の全文を出力し直してください。
{issues}

<previous_attempt>
{draft}
</previous_attempt>"""


def _retry_section(target: str, draft: str, issues: list[str]) -> str:
    return RETRY_SECTION.format(target=target, issues="\n".join(f"- {i}" for i in issues), draft=draft)


def build_doc_feedback(draft: str, issues: list[str]) -> str:
    return _retry_section("設計書", draft, issues)


COMMENT_SYSTEM_PROMPT = """あなたは{language}ファイルへのコメント追記を一括で行うバッチ処理の一部として、非対話で動作しています。
<skill> に書かれたコメント追記スキルの手順と記述ルールに従ってください。

バッチ処理のため、次のルールをスキルの記述より優先します:
- ファイルの作成・編集やコマンドの実行はしない。スキルにファイルを編集する手順があっても、書き出しはバッチ側が行う
- コメントを追記した{language}ファイルの全文だけを最終応答として出力する。前置き・説明・```での囲みは付けない
- コメント以外のコードは変更しない。識別子・式・文字列・import・宣言の順序などはそのまま残し、変えてよいのはコメントと空白だけとする
  （コメント以外の差分はバッチ側で機械的に検出され、やり直しになる）
- ユーザーへの質問はしない（回答は得られない）
- スキルが参照している別ファイルは {skill_dir} から Read で読める
- 今日の日付は {today} とする。スキルで今日の日付を記述する箇所にはこの日付を使い、書式はスキルの指定に従う

<skill>
{skill}
</skill>
"""


def build_comment_messages(
    skill: str, skill_dir: Path, today: date, rel_path: str, source: str, facts: SourceFacts
) -> list:
    # 出力はファイルの全文なので、読み違いが起きないよう現在の内容をそのまま渡す
    human = f"""次の{facts.language}ファイルにコメントを追記してください: {rel_path}
（作業ディレクトリがリポジトリのルートです。関連ファイルは Read / Grep / Glob で参照してください）

## 抽出済みの事実（静的解析の結果）
{facts.to_prompt()}

## 現在のファイルの内容
<source>
{source}
</source>"""
    system = COMMENT_SYSTEM_PROMPT.format(language=facts.language, skill_dir=skill_dir, today=today.isoformat(), skill=skill)
    return [SystemMessage(content=system), HumanMessage(content=human)]


def build_comment_feedback(draft: str, issues: list[str]) -> str:
    return _retry_section("ファイル", draft, issues)
