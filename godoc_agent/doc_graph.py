"""Goファイル1本分の設計書を生成するグラフ。

1回の実行 = 1ファイル = 1 thread_id。状態にはそのファイルの情報しか入らないため、
前のファイルのコンテキストが混ざることはない（方式A）。

prepare -> generate -> validate -+-> (done)
                ^                |
                +---- retry -----+-> (failed)
"""

import re
import time
from collections.abc import Callable
from functools import partial
from typing import Literal, TypedDict

from langchain_core.runnables import Runnable
from langchain_core.messages import AIMessage, AnyMessage
from langgraph.graph import END, START, StateGraph

from .go_source import GoFacts, extract_facts
from .prompts import build_feedback, build_messages, required_sections

_FENCE_RE = re.compile(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", re.DOTALL)

# Ollamaは入力がnum_ctxを超えると先頭を黙って切り捨てる。プロンプトのトークン数が
# 上限のこの割合に達していたら、切り捨てが起きた可能性が高いとみなす。
_CTX_OVERFLOW_RATIO = 0.98

# (rel_path, source, facts) -> 最初に送るメッセージ。エンジンごとにプロンプトの組み立て方が異なる
InitialMessages = Callable[[str, str, GoFacts], list[AnyMessage]]

Status = Literal["pending", "done", "too_large", "context_overflow", "failed_validation"]


class DocState(TypedDict, total=False):
    rel_path: str
    source: str
    exported: list[str]
    messages: list[AnyMessage]
    draft: str
    issues: list[str]
    attempts: int
    llm_calls: list[dict]
    status: Status


def _strip_fence(text: str) -> str:
    m = _FENCE_RE.match(text.strip())
    return m.group(1) if m else text.strip()


def _call_metrics(response: AIMessage, elapsed_s: float) -> dict:
    meta = response.response_metadata or {}
    eval_count = meta.get("eval_count") or 0
    eval_ns = meta.get("eval_duration") or 0
    return {
        "elapsed_s": round(elapsed_s, 2),
        "done_reason": meta.get("done_reason"),
        "prompt_tokens": meta.get("prompt_eval_count"),
        "output_tokens": eval_count,
        "tokens_per_s": round(eval_count / (eval_ns / 1e9), 1) if eval_ns else None,
        "num_turns": meta.get("num_turns"),  # Claude Code のみ（エージェントのターン数）
    }


def find_issues(draft: str, sections: list[str], exported: list[str]) -> list[str]:
    issues = []
    headings = [h.strip() for h in re.findall(r"^##\s+(.+?)\s*$", draft, re.MULTILINE)]
    missing = [s for s in sections if s not in headings]
    if missing:
        issues.append(f"必須セクションが不足しています: {', '.join(missing)}")
    # 単語境界で判定し、"Get" が "GetUser" の中で一致してしまうのを防ぐ
    unmentioned = [n for n in exported if not re.search(rf"\b{re.escape(n)}\b", draft)]
    if unmentioned:
        issues.append(f"次の公開識別子が設計書に記載されていません: {', '.join(unmentioned)}")
    return issues


def build_doc_graph(
    llm: Runnable,
    template: str,
    *,
    num_ctx: int,
    max_source_chars: int,
    max_attempts: int,
    initial_messages: InitialMessages | None = None,
) -> StateGraph:
    sections = required_sections(template)
    if initial_messages is None:
        initial_messages = partial(build_messages, template)

    def prepare(state: DocState) -> DocState:
        source = state["source"]
        if len(source) > max_source_chars:
            return {"status": "too_large", "issues": [f"ソースが {len(source)} 文字で上限 {max_source_chars} を超えています"]}
        facts = extract_facts(source)
        return {
            "exported": facts.exported,
            "messages": initial_messages(state["rel_path"], source, facts),
            "attempts": 0,
            "llm_calls": [],
            "status": "pending",
        }

    async def generate(state: DocState) -> DocState:
        started = time.perf_counter()
        response = await llm.ainvoke(state["messages"])
        metrics = _call_metrics(response, time.perf_counter() - started)
        draft = _strip_fence(response.content)
        # 応答メタデータ（Claude Code のセッションIDなど）を残したまま履歴に積み、再生成時に引き継ぐ
        reply = AIMessage(content=draft, response_metadata=response.response_metadata)
        return {
            "draft": draft,
            "messages": state["messages"] + [reply],
            "attempts": state["attempts"] + 1,
            "llm_calls": state["llm_calls"] + [metrics],
        }

    def validate(state: DocState) -> DocState:
        last_call = state["llm_calls"][-1]
        prompt_tokens = last_call["prompt_tokens"] or 0
        if prompt_tokens >= num_ctx * _CTX_OVERFLOW_RATIO:
            # 入力が切り捨てられているので、再試行しても直らない
            return {"status": "context_overflow", "issues": [f"プロンプトが {prompt_tokens} トークンで num_ctx={num_ctx} に達しました"]}

        issues = find_issues(state["draft"], sections, state["exported"])
        if last_call["done_reason"] == "length":
            issues.append("出力がトークン上限で途中終了しています。簡潔に書き直してください")

        if not issues:
            return {"status": "done", "issues": []}
        if state["attempts"] >= max_attempts:
            return {"status": "failed_validation", "issues": issues}
        return {
            "issues": issues,
            "messages": state["messages"] + [build_feedback(issues)],
        }

    def generate_or_end(state: DocState) -> str:
        return "generate" if state["status"] == "pending" else END

    graph = StateGraph(DocState)
    graph.add_node("prepare", prepare)
    graph.add_node("generate", generate)
    graph.add_node("validate", validate)
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges("prepare", generate_or_end, ["generate", END])
    graph.add_edge("generate", "validate")
    graph.add_conditional_edges("validate", generate_or_end, ["generate", END])
    return graph
