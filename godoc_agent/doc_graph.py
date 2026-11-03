"""Goファイル1本分の出力（設計書・コメント追記済みソース）を生成するグラフ。

1回の実行 = 1ファイル×1タスク = 1 thread_id。状態にはそのファイルの情報しか入らないため、
前のファイルのコンテキストが混ざることはない（方式A）。
何を生成し、どう検証するかはタスク（tasks.Task）が決める。

prepare -> generate -> validate -+-> (done)
                ^                |
                +---- retry -----+-> (failed)
"""

import re
import time
from typing import Literal, Protocol, TypedDict

from langchain_core.messages import AIMessage, AnyMessage
from langgraph.graph import END, START, StateGraph

from .go_source import extract_facts
from .tasks import Task

# ```markdown / ```go などで全体が囲まれていたら外す
_FENCE_RE = re.compile(r"^```[\w-]*\s*\n(.*)\n```\s*$", re.DOTALL)

Status = Literal["pending", "done", "too_large", "failed_validation"]


class ChatModel(Protocol):
    async def ainvoke(self, messages: list[AnyMessage]) -> AIMessage: ...


class DocState(TypedDict, total=False):
    rel_path: str
    source: str
    previous_output: str | None  # 前回の出力（設計書）。履歴を引き継いで改訂するために渡す
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


def build_doc_graph(llm: ChatModel, task: Task, *, max_attempts: int) -> StateGraph:
    def prepare(state: DocState) -> DocState:
        source = state["source"]
        previous_output = state.get("previous_output")
        # 前回の出力もプロンプトに入るため、ソースと合わせて上限を判定する
        total = len(source) + len(previous_output or "")
        if total > task.max_input_chars:
            return {"status": "too_large", "issues": [f"ソースと前回の出力が計 {total} 文字で上限 {task.max_input_chars} を超えています"]}
        facts = extract_facts(source)
        return {
            "exported": facts.exported,
            "messages": task.initial_messages(state["rel_path"], source, facts, previous_output),
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
        issues = task.find_issues(state["draft"], state["source"], state["exported"])
        if not issues:
            return {"status": "done", "issues": []}
        if state["attempts"] >= max_attempts:
            return {"status": "failed_validation", "issues": issues}
        return {
            "issues": issues,
            "messages": state["messages"] + [task.build_feedback(issues)],
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
