from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from conftest import SAMPLE_GO, ScriptedLLM, good_doc, reply
from godoc_agent.doc_graph import build_doc_graph
from godoc_agent.tasks import comment_task, design_doc_task, find_comment_issues, find_doc_issues

ALL_EXPORTED = ("GetUser", "NewService", "Repository", "User")


def initial_messages(rel_path, source, facts, previous_output):
    revision = f" 前回: {previous_output}" if previous_output else ""
    return [HumanMessage(f"{rel_path} の設計書を作成: {', '.join(facts.exported)}{revision}")]


def compile_graph(llm, *, max_attempts=3, max_source_chars=100_000, task=None):
    task = task or replace(
        design_doc_task("スキル", Path("skills"), date(2026, 11, 3), max_input_chars=max_source_chars),
        initial_messages=initial_messages,
    )
    return build_doc_graph(llm, task, max_attempts=max_attempts).compile(checkpointer=InMemorySaver())


async def run(graph, thread_id="t1", source=SAMPLE_GO, previous_doc=None):
    config = {"configurable": {"thread_id": thread_id}}
    return await graph.ainvoke({"rel_path": "user/service.go", "source": source, "previous_output": previous_doc}, config)


async def test_valid_draft_finishes_in_one_attempt():
    llm = ScriptedLLM([reply(good_doc(*ALL_EXPORTED))])

    state = await run(compile_graph(llm))

    assert state["status"] == "done"
    assert state["attempts"] == 1
    assert state["llm_calls"][0]["num_turns"] == 3
    assert "NewService" in state["draft"]
    assert [m.content for m in llm.calls[0]] == ["user/service.go の設計書を作成: GetUser, NewService, Repository, User"]


async def test_code_fence_around_output_is_removed():
    llm = ScriptedLLM([reply("```markdown\n" + good_doc(*ALL_EXPORTED) + "```")])

    state = await run(compile_graph(llm))

    assert state["draft"].startswith("# 詳細設計書")


async def test_structure_is_left_to_the_skill():
    # 見出し構成は検証しない（SKILL.md に任せる）。公開識別子が揃っていれば合格
    llm = ScriptedLLM([reply("自由な構成の設計書: " + " ".join(ALL_EXPORTED))])

    state = await run(compile_graph(llm))

    assert state["status"] == "done"


async def test_missing_identifiers_are_retried_with_feedback_in_same_session():
    llm = ScriptedLLM([reply(good_doc("User"), session_id="s1"), reply(good_doc(*ALL_EXPORTED), session_id="s1")])

    state = await run(compile_graph(llm))

    assert state["status"] == "done"
    assert state["attempts"] == 2
    retry_messages = llm.calls[1]
    assert isinstance(retry_messages[-2], AIMessage)
    # ClaudeCodeLLM はこのセッションIDで --resume する
    assert retry_messages[-2].response_metadata["session_id"] == "s1"
    assert isinstance(retry_messages[-1], HumanMessage)
    assert "GetUser" in retry_messages[-1].content


async def test_gives_up_after_max_attempts():
    llm = ScriptedLLM([reply("")] * 2)

    state = await run(compile_graph(llm, max_attempts=2))

    assert state["status"] == "failed_validation"
    assert len(llm.calls) == 2
    assert state["issues"] == ["設計書の本文が空です"]


async def test_oversized_source_is_rejected_before_calling_llm():
    llm = ScriptedLLM([])

    state = await run(compile_graph(llm, max_source_chars=10))

    assert state["status"] == "too_large"
    assert llm.calls == []


async def test_previous_doc_is_passed_to_initial_messages():
    llm = ScriptedLLM([reply(good_doc(*ALL_EXPORTED))])

    await run(compile_graph(llm), previous_doc="前回の設計書 v1.0")

    assert "前回の設計書 v1.0" in llm.calls[0][0].content


async def test_previous_doc_counts_toward_size_limit():
    # 前回の設計書もプロンプトに入るため、ソースと合わせて上限を判定する
    llm = ScriptedLLM([])

    state = await run(compile_graph(llm, max_source_chars=len(SAMPLE_GO) + 5), previous_doc="x" * 10)

    assert state["status"] == "too_large"
    assert llm.calls == []


async def test_each_thread_starts_with_fresh_context():
    llm = ScriptedLLM([reply(good_doc(*ALL_EXPORTED)), reply(good_doc(*ALL_EXPORTED))])
    graph = compile_graph(llm)

    await run(graph, thread_id="file-a")
    await run(graph, thread_id="file-b")

    # 2本目のファイルに送られるのは最初のメッセージだけで、1本目の履歴は含まれない
    assert len(llm.calls[1]) == 1


async def test_session_resumes_from_checkpoint_after_failure():
    llm = ScriptedLLM([RuntimeError("claude crashed"), reply(good_doc(*ALL_EXPORTED))])
    graph = compile_graph(llm)
    config = {"configurable": {"thread_id": "resume"}}

    with pytest.raises(RuntimeError):
        await graph.ainvoke({"rel_path": "user/service.go", "source": SAMPLE_GO}, config)
    assert (await graph.aget_state(config)).next == ("generate",)

    state = await graph.ainvoke(None, config)

    assert state["status"] == "done"


def test_find_doc_issues_uses_word_boundaries():
    assert find_doc_issues("GetUser の説明", "", ["Get"]) == ["次の公開識別子が設計書に記載されていません: Get"]
    assert find_doc_issues("Get と GetUser の説明", "", ["Get", "GetUser"]) == []


def commented(source: str) -> str:
    """コメント追記スキルの出力を模して、宣言の前にコメントを入れる（コードは変えない）。"""
    return source.replace("func NewService", "// NewService はサービスを作る。\nfunc NewService").replace(
        "type User struct", "// User は利用者を表す。\ntype User struct"
    )


def compile_comment_graph(llm, *, max_attempts=3):
    task = comment_task("コメントスキル", Path("skills"), date(2026, 11, 3), max_input_chars=100_000)
    return compile_graph(llm, max_attempts=max_attempts, task=task)


async def test_comment_task_accepts_source_with_only_comments_added():
    llm = ScriptedLLM([reply("```go\n" + commented(SAMPLE_GO) + "```")])

    state = await run(compile_comment_graph(llm))

    assert state["status"] == "done"
    assert state["draft"] == commented(SAMPLE_GO).strip()
    # 出力はファイル全文なので、元のソースをそのままプロンプトに渡す
    assert SAMPLE_GO in llm.calls[0][-1].content


async def test_comment_task_retries_when_code_is_changed():
    broken = commented(SAMPLE_GO).replace('errors.New("empty id")', 'errors.New("id is empty")')
    llm = ScriptedLLM([reply(broken), reply(commented(SAMPLE_GO))])

    state = await run(compile_comment_graph(llm))

    assert state["status"] == "done"
    assert state["attempts"] == 2
    feedback = llm.calls[1][-1].content
    assert "コメント以外のコードが変更されています" in feedback
    assert "22 行目付近" in feedback


async def test_comment_task_does_not_receive_previous_output():
    llm = ScriptedLLM([reply(commented(SAMPLE_GO))])

    await run(compile_comment_graph(llm))

    assert "前回" not in llm.calls[0][-1].content


def test_find_comment_issues_rejects_empty_output():
    assert find_comment_issues("  ", SAMPLE_GO, []) == ["Goファイルの本文が空です"]
