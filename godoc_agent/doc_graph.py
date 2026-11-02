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
from typing import Literal, Protocol, TypedDict

from langchain_core.messages import AIMessage, AnyMessage
from langgraph.graph import END, START, StateGraph

from .go_source import GoFacts, extract_facts
from .prompts import build_feedback

_FENCE_RE = re.compile(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", re.DOTALL)

# (rel_path, facts) -> 最初に送るメッセージ
InitialMessages = Callable[[str, GoFacts], list[AnyMessage]]

Status = Literal["pending", "done", "too_large", "failed_validation"]


class ChatModel(Protocol):
    async def ainvoke(self, messages: list[AnyMessage]) -> AIMessage: ...


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
    return {
        "elapsed_s": round(elapsed_s, 2),
        "num_turns": meta.get("num_turns"),
        "output_tokens": meta.get("output_tokens"),
    }


def find_issues(draft: str, exported: list[str]) -> list[str]:
    # 設計書の構成は SKILL.md に任せ、ここでは静的解析で確実に分かる記載漏れだけを見る
    if not draft.strip():
        return ["設計書の本文が空です"]
    # 単語境界で判定し、"Get" が "GetUser" の中で一致してしまうのを防ぐ
    unmentioned = [n for n in exported if not re.search(rf"\b{re.escape(n)}\b", draft)]
    if unmentioned:
        return [f"次の公開識別子が設計書に記載されていません: {', '.join(unmentioned)}"]
    return []


def build_doc_graph(
    llm: ChatModel,
    initial_messages: InitialMessages,
    *,
    max_source_chars: int,
    max_attempts: int,
) -> StateGraph:
    def prepare(state: DocState) -> DocState:
        source = state["source"]
        if len(source) > max_source_chars:
            return {"status": "too_large", "issues": [f"ソースが {len(source)} 文字で上限 {max_source_chars} を超えています"]}
        facts = extract_facts(source)
        return {
            "exported": facts.exported,
            "messages": initial_messages(state["rel_path"], facts),
            "attempts": 0,
            "llm_calls": [],
            "status": "pending",
        }

    async def generate(state: DocState) -> DocState:
        started = time.perf_counter()
        response = await llm.ainvoke(state["messages"])
        metrics = _call_metrics(response, time.perf_counter() - started)
        draft = _strip_fence(response.content)
        # 応答メタデータ（セッションID）を残したまま履歴に積み、再生成時に同じセッションを継続する
        reply = AIMessage(content=draft, response_metadata=response.response_metadata)
        return {
            "draft": draft,
            "messages": state["messages"] + [reply],
            "attempts": state["attempts"] + 1,
            "llm_calls": state["llm_calls"] + [metrics],
        }

    def validate(state: DocState) -> DocState:
        issues = find_issues(state["draft"], state["exported"])
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
