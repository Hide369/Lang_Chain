"""Goファイル1本に対して行う処理（タスク）の定義。

タスクごとにスキル・プロンプト・検証・出力先が異なり、グラフ（doc_graph）と
バッチ（runner）はこの定義に従って同じ流れで処理する。
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from langchain_core.messages import AnyMessage, HumanMessage

from .go_source import GoFacts, first_code_difference
from .prompts import build_agent_messages, build_comment_feedback, build_comment_messages, build_doc_feedback

DOC = "doc"
COMMENT = "comment"
TASK_NAMES = (DOC, COMMENT)

# (rel_path, source, facts, previous_output) -> 最初に送るメッセージ
InitialMessages = Callable[[str, str, GoFacts, str | None], list[AnyMessage]]
# (draft, source, exported) -> 問題点（なければ空）
FindIssues = Callable[[str, str, list[str]], list[str]]


@dataclass(frozen=True)
class Task:
    name: str
    run_name: str  # LangSmith の実行名の接頭辞
    initial_messages: InitialMessages
    find_issues: FindIssues
    build_feedback: Callable[[list[str]], HumanMessage]
    max_input_chars: int
    out_subdir: str  # --out からの相対フォルダ（"" は --out 直下）
    out_suffix: str  # 出力ファイル名 = Goファイル名 + out_suffix
    uses_previous_output: bool  # 前回の出力を渡して改訂させるか

    def output_path(self, out_dir: Path, rel_path: str) -> Path:
        return out_dir / self.out_subdir / f"{rel_path}{self.out_suffix}"

    def manifest_path(self, out_dir: Path) -> Path:
        return out_dir / self.out_subdir / ".manifest.json"


def find_doc_issues(draft: str, source: str, exported: list[str]) -> list[str]:
    # 設計書の構成は SKILL.md に任せ、ここでは静的解析で確実に分かる記載漏れだけを見る
    if not draft.strip():
        return ["設計書の本文が空です"]
    # 単語境界で判定し、"Get" が "GetUser" の中で一致してしまうのを防ぐ
    unmentioned = [n for n in exported if not re.search(rf"\b{re.escape(n)}\b", draft)]
    if unmentioned:
        return [f"次の公開識別子が設計書に記載されていません: {', '.join(unmentioned)}"]
    return []


def find_comment_issues(draft: str, source: str, exported: list[str]) -> list[str]:
    # コメントの書き方は SKILL.md に任せ、ここではコードが壊されていないことだけを保証する
    if not draft.strip():
        return ["Goファイルの本文が空です"]
    line = first_code_difference(source, draft)
    if line is not None:
        return [
            f"コメント以外のコードが変更されています（元のファイルの {line} 行目付近）。"
            "コードは元のまま残し、コメントの追加・修正だけを行ってください"
        ]
    return []


def design_doc_task(skill: str, skill_dir: Path, today: date, *, max_input_chars: int) -> Task:
    return Task(
        name=DOC,
        run_name="design-doc",
        initial_messages=lambda rel_path, source, facts, previous: build_agent_messages(
            skill, skill_dir, today, rel_path, facts, previous
        ),
        find_issues=find_doc_issues,
        build_feedback=build_doc_feedback,
        max_input_chars=max_input_chars,
        out_subdir="",
        out_suffix=".md",
        uses_previous_output=True,
    )


def comment_task(skill: str, skill_dir: Path, today: date, *, max_input_chars: int) -> Task:
    return Task(
        name=COMMENT,
        run_name="go-comment",
        initial_messages=lambda rel_path, source, facts, previous: build_comment_messages(
            skill, skill_dir, today, rel_path, source, facts
        ),
        find_issues=find_comment_issues,
        build_feedback=build_comment_feedback,
        max_input_chars=max_input_chars,
        # 元のリポジトリは書き換えず、コメントを追記したファイルを別フォルダに出力する
        out_subdir="_commented",
        out_suffix="",
        # Goリポジトリ側のソースは変わらないため、毎回元のソースから追記させる
        uses_previous_output=False,
    )
