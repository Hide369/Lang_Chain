import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from conftest import SAMPLE_GO, TEMPLATE, ScriptedLLM, good_doc, reply
from godoc_agent.doc_graph import build_doc_graph, find_issues

ALL_EXPORTED = ("GetUser", "NewService", "Repository", "User")


def compile_graph(llm, *, max_attempts=3, max_source_chars=100_000, num_ctx=65536, initial_messages=None):
    graph = build_doc_graph(
        llm, TEMPLATE, num_ctx=num_ctx, max_source_chars=max_source_chars, max_attempts=max_attempts,
        initial_messages=initial_messages,
    )
    return graph.compile(checkpointer=InMemorySaver())


async def run(graph, thread_id="t1", source=SAMPLE_GO):
    config = {"configurable": {"thread_id": thread_id}}
    return await graph.ainvoke({"rel_path": "user/service.go", "source": source}, config)


async def test_valid_draft_finishes_in_one_attempt():
    llm = ScriptedLLM([reply(good_doc(*ALL_EXPORTED))])

    state = await run(compile_graph(llm))

    assert state["status"] == "done"
    assert state["attempts"] == 1
    assert state["llm_calls"][0]["tokens_per_s"] == 50.0
    assert "NewService" in state["draft"]


async def test_code_fence_around_output_is_removed():
    llm = ScriptedLLM([reply("```markdown\n" + good_doc(*ALL_EXPORTED) + "```")])

    state = await run(compile_graph(llm))

    assert state["draft"].startswith("# 詳細設計書")


async def test_invalid_draft_is_retried_with_feedback():
    llm = ScriptedLLM([reply(good_doc("User")), reply(good_doc(*ALL_EXPORTED))])

    state = await run(compile_graph(llm))

    assert state["status"] == "done"
    assert state["attempts"] == 2
    retry_messages = llm.calls[1]
    assert isinstance(retry_messages[-2], AIMessage)
    assert isinstance(retry_messages[-1], HumanMessage)
    assert "GetUser" in retry_messages[-1].content


async def test_retry_keeps_reply_metadata_for_session_resume():
    first = reply(good_doc("User"))
    first.response_metadata["session_id"] = "s1"
    llm = ScriptedLLM([first, reply(good_doc(*ALL_EXPORTED))])

    await run(compile_graph(llm))

    # Claude Code はこのメタデータのセッションIDで --resume する
    assert llm.calls[1][-2].response_metadata["session_id"] == "s1"


async def test_custom_initial_messages_replace_default_prompt():
    llm = ScriptedLLM([reply(good_doc(*ALL_EXPORTED))])

    def agent_prompt(rel_path, source, facts):
        return [HumanMessage(f"{rel_path} を読んで作成: {', '.join(facts.exported)}")]

    await run(compile_graph(llm, initial_messages=agent_prompt))

    assert [m.content for m in llm.calls[0]] == ["user/service.go を読んで作成: GetUser, NewService, Repository, User"]


async def test_gives_up_after_max_attempts():
    llm = ScriptedLLM([reply("見出しなし")] * 2)

    state = await run(compile_graph(llm, max_attempts=2))

    assert state["status"] == "failed_validation"
    assert len(llm.calls) == 2
    assert any("必須セクション" in i for i in state["issues"])


async def test_truncated_output_triggers_retry():
    llm = ScriptedLLM([reply(good_doc(*ALL_EXPORTED), done_reason="length"), reply(good_doc(*ALL_EXPORTED))])

    state = await run(compile_graph(llm))

    assert state["status"] == "done"
    assert state["attempts"] == 2


async def test_context_overflow_stops_without_retry():
    llm = ScriptedLLM([reply(good_doc(*ALL_EXPORTED), prompt_tokens=8192)])

    state = await run(compile_graph(llm, num_ctx=8192))

    assert state["status"] == "context_overflow"
    assert len(llm.calls) == 1


async def test_oversized_source_is_rejected_before_calling_llm():
    llm = ScriptedLLM([])

    state = await run(compile_graph(llm, max_source_chars=10))

    assert state["status"] == "too_large"
    assert llm.calls == []


async def test_each_thread_starts_with_fresh_context():
    llm = ScriptedLLM([reply(good_doc(*ALL_EXPORTED)), reply(good_doc(*ALL_EXPORTED))])
    graph = compile_graph(llm)

    await run(graph, thread_id="file-a")
    await run(graph, thread_id="file-b")

    # 2本目のファイルに送られるのは system + human の2通だけで、1本目の履歴は含まれない
    assert len(llm.calls[1]) == 2


async def test_session_resumes_from_checkpoint_after_failure():
    llm = ScriptedLLM([ConnectionError("ollama down"), reply(good_doc(*ALL_EXPORTED))])
    graph = compile_graph(llm)
    config = {"configurable": {"thread_id": "resume"}}

    with pytest.raises(ConnectionError):
        await graph.ainvoke({"rel_path": "user/service.go", "source": SAMPLE_GO}, config)
    assert (await graph.aget_state(config)).next == ("generate",)

    state = await graph.ainvoke(None, config)

    assert state["status"] == "done"


def test_find_issues_uses_word_boundaries():
    issues = find_issues("## 1. 概要\n## 2. 関数・メソッド仕様\nGetUser", ["1. 概要", "2. 関数・メソッド仕様"], ["Get"])

    assert issues == ["次の公開識別子が設計書に記載されていません: Get"]
