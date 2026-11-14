from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from conftest import SAMPLE_GO, ScriptedLLM, good_doc, reply
from godoc_agent.claude_code import ContextOverflowError
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
    return build_doc_graph(llm, task, max_attempts=max_attempts).compile()


async def run(graph, source=SAMPLE_GO, previous_doc=None):
    return await graph.ainvoke({"rel_path": "user/service.go", "language": "go", "source": source, "previous_output": previous_doc})


async def test_valid_draft_finishes_in_one_attempt():
    llm = ScriptedLLM([reply(good_doc(*ALL_EXPORTED))])

    state = await run(compile_graph(llm))

    assert state["status"] == "done"
    assert state["attempts"] == 1
    assert state["llm_calls"][0]["num_turns"] == 3
    assert "NewService" in state["draft"]
    assert [m.content for m in llm.calls[0]] == ["user/service.go の設計書を作成: GetUser, NewService, Repository, User"]


async def test_input_tokens_including_cache_are_recorded_per_call():
    response = reply(good_doc(*ALL_EXPORTED))
    response.response_metadata.update(input_tokens=1000, cache_read_input_tokens=20000, cache_creation_input_tokens=None)
    llm = ScriptedLLM([response])

    state = await run(compile_graph(llm))

    call = state["llm_calls"][0]
    assert call["input_tokens"] == 1000
    assert call["cache_read_input_tokens"] == 20000
    assert call["cache_creation_input_tokens"] is None
    # 返ってきた値だけを合計する（キャッシュ分も入力の一部）
    assert call["total_input_tokens"] == 21000


async def test_unknown_input_tokens_are_recorded_as_none():
    llm = ScriptedLLM([reply(good_doc(*ALL_EXPORTED), input_tokens=None)])

    state = await run(compile_graph(llm))

    assert state["status"] == "done"
    assert state["llm_calls"][0]["total_input_tokens"] is None


async def test_code_fence_around_output_is_removed():
    llm = ScriptedLLM([reply("```markdown\n" + good_doc(*ALL_EXPORTED) + "```")])

    state = await run(compile_graph(llm))

    assert state["draft"].startswith("# 詳細設計書")


async def test_structure_is_left_to_the_skill():
    # 見出し構成は検証しない（SKILL.md に任せる）。公開識別子が揃っていれば合格
    llm = ScriptedLLM([reply("自由な構成の設計書: " + " ".join(ALL_EXPORTED))])

    state = await run(compile_graph(llm))

    assert state["status"] == "done"


async def test_missing_identifiers_are_retried_in_new_session_with_previous_draft():
    first = good_doc("User") + "初版の本文"
    llm = ScriptedLLM([reply(first), reply(good_doc(*ALL_EXPORTED))])

    state = await run(compile_graph(llm))

    assert state["status"] == "done"
    assert state["attempts"] == 2
    initial, retry = llm.calls
    # 会話を続けない（AIMessage を積まない）ので、ClaudeCodeLLM は --resume せず新しいセッションで起動する
    assert not any(isinstance(m, AIMessage) for m in retry)
    assert len(retry) == len(initial)
    # 最初の依頼文に、直前の出力と問題点を追記して送り直す
    assert retry[-1].content.startswith(initial[-1].content)
    assert "初版の本文" in retry[-1].content
    assert "GetUser" in retry[-1].content.removeprefix(initial[-1].content)


async def test_retry_prompt_does_not_grow_with_each_attempt():
    llm = ScriptedLLM([reply("1回目の草稿"), reply("2回目の草稿"), reply(good_doc(*ALL_EXPORTED))])

    state = await run(compile_graph(llm, max_attempts=3))

    assert state["attempts"] == 3
    third = llm.calls[2][-1].content
    # 渡すのは直前の草稿だけで、それより前の試行は持ち越さない
    assert "2回目の草稿" in third
    assert "1回目の草稿" not in third
    assert len(third) == len(llm.calls[1][-1].content)


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


async def test_context_overflow_ends_without_retry():
    # 入力がコンテキストを超えて推論サーバーに断られたら、同じ入力で再生成しても無駄なので再試行しない
    llm = ScriptedLLM([ContextOverflowError("maximum context length is 262144 tokens")])

    state = await run(compile_graph(llm))

    assert state["status"] == "context_overflow"
    assert state["attempts"] == 1
    assert len(llm.calls) == 1
    assert "262144" in state["issues"][0]


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


async def test_each_invocation_starts_with_fresh_context():
    llm = ScriptedLLM([reply(good_doc(*ALL_EXPORTED)), reply(good_doc(*ALL_EXPORTED))])
    graph = compile_graph(llm)

    await run(graph)
    await run(graph)

    # 同じグラフでも2回目に送られるのは最初のメッセージだけで、1回目の履歴は含まれない
    assert len(llm.calls[1]) == 1


async def test_invocation_after_failure_starts_over():
    llm = ScriptedLLM([RuntimeError("claude crashed"), reply(good_doc(*ALL_EXPORTED))])
    graph = compile_graph(llm)

    with pytest.raises(RuntimeError):
        await run(graph)
    state = await run(graph)

    # 失敗した実行の続きではなく、最初のメッセージからやり直す
    assert state["status"] == "done"
    assert state["attempts"] == 1
    assert len(llm.calls[1]) == 1


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
    assert find_comment_issues("  ", SAMPLE_GO, []) == ["ファイルの本文が空です"]
