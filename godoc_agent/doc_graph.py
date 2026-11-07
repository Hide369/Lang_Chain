"""ソースファイル1本分の出力（設計書・コメント追記済みソース）を生成するグラフ。

1回の実行 = 1ファイル×1タスク。チェックポイントを持たず毎回まっさらな状態から始めるため、
前のファイルや前回の実行のコンテキストが混ざることはない（方式A）。
何を生成し、どう検証するかはタスク（tasks.Task）が決める。

prepare -> generate -> validate -+-> (done)
                ^                |
                +---- retry -----+-> (failed)
"""

import re
import time
from typing import Literal, Protocol, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from .analyzers import extract_facts
from .tasks import Task

# ```markdown / ```go / ```csharp などで全体が囲まれていたら外す
_FENCE_RE = re.compile(r"^```[\w-]*\s*\n(.*)\n```\s*$", re.DOTALL)

Status = Literal["pending", "done", "too_large", "failed_validation"]


class ChatModel(Protocol):
    async def ainvoke(self, messages: list[AnyMessage]) -> AIMessage: ...


class DocState(TypedDict, total=False):
    rel_path: str
    language: str  # 解析に使う言語（analyzers.ANALYZERS のキー）
    source: str
    previous_output: str | None  # 前回の出力（設計書）。履歴を引き継いで改訂するために渡す
    exported: list[str]
    prompt: list[AnyMessage]  # 最初の依頼（システムプロンプト＋依頼文）。再生成のたびにここから組み直す
    messages: list[AnyMessage]  # 次の generate で送るメッセージ
    draft: str
    issues: list[str]
    attempts: int
    llm_calls: list[dict]
    status: Status


def _strip_fence(text: str) -> str:
    m = _FENCE_RE.match(text.strip())
    return m.group(1) if m else text.strip()


_INPUT_TOKEN_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _call_metrics(response: AIMessage, elapsed_s: float) -> dict:
    meta = response.response_metadata or {}
    inputs = {k: meta.get(k) for k in _INPUT_TOKEN_KEYS}
    known = [v for v in inputs.values() if v is not None]
    return {
        "elapsed_s": round(elapsed_s, 2),
        "num_turns": meta.get("num_turns"),
        "output_tokens": meta.get("output_tokens"),
        **inputs,
        # キャッシュ分を含めた入力全体。usage が返らなければ不明(None)とし、0 とは区別する
        "total_input_tokens": sum(known) if known else None,
    }


def _with_feedback(prompt: list[AnyMessage], feedback: str) -> list[AnyMessage]:
    """最初の依頼文の末尾に、直前の出力と問題点を追記したメッセージを作る。

    会話を続けず（--resume しない）毎回新しいセッションで再生成するため、試行を重ねても
    入力は「最初の依頼＋直前の出力1つ分」で頭打ちになり、前の試行でツールが読んだ内容も持ち越さない。
    """
    *head, request = prompt
    return [*head, HumanMessage(content=f"{request.content}\n\n{feedback}")]


def build_doc_graph(llm: ChatModel, task: Task, *, max_attempts: int) -> StateGraph:
    def prepare(state: DocState) -> DocState:
        source = state["source"]
        previous_output = state.get("previous_output")
        # 前回の出力もプロンプトに入るため、ソースと合わせて上限を判定する
        total = len(source) + len(previous_output or "")
        if total > task.max_input_chars:
            return {"status": "too_large", "issues": [f"ソースと前回の出力が計 {total} 文字で上限 {task.max_input_chars} を超えています"]}
        facts = extract_facts(state["language"], source)
        prompt = task.initial_messages(state["rel_path"], source, facts, previous_output)
        return {
            "exported": facts.exported,
            "prompt": prompt,
            "messages": prompt,
            "attempts": 0,
            "llm_calls": [],
            "status": "pending",
        }

    async def generate(state: DocState) -> DocState:
        started = time.perf_counter()
        response = await llm.ainvoke(state["messages"])
        metrics = _call_metrics(response, time.perf_counter() - started)
        return {
            "draft": _strip_fence(response.content),
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
            "messages": _with_feedback(state["prompt"], task.build_feedback(state["draft"], issues)),
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
