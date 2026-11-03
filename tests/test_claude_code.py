import os
from datetime import date
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from conftest import claude_result
from godoc_agent.claude_code import ClaudeCodeError, ClaudeCodeLLM, build_claude_code
from godoc_agent.config import load_settings
from godoc_agent.go_source import extract_facts
from godoc_agent.prompts import build_agent_messages, load_skill

MODEL = "qwen3-coder-next"
BASE_URL = "http://127.0.0.1:11434"


def make_llm(fake_claude, cwd: Path, *, timeout_s: float = 30) -> ClaudeCodeLLM:
    return ClaudeCodeLLM(
        fake_claude.command,
        model=MODEL,
        ollama_base_url=BASE_URL,
        cwd=cwd,
        add_dirs=[cwd / "skill"],
        max_turns=7,
        max_output_tokens=8192,
        timeout_s=timeout_s,
    )


def option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


async def test_first_call_starts_session_with_skill_and_read_only_tools(fake_claude, tmp_path):
    fake_claude.respond(claude_result("# 設計書", session_id="s1"))

    reply = await make_llm(fake_claude, tmp_path).ainvoke([SystemMessage("スキル本文"), HumanMessage("作成して")])

    assert reply.content == "# 設計書"
    assert reply.response_metadata["session_id"] == "s1"
    assert reply.response_metadata["num_turns"] == 3
    call = fake_claude.calls()[0]
    argv = call["argv"]
    assert argv[:2] == ["-p", "作成して"]
    assert option(argv, "--append-system-prompt") == "スキル本文"
    assert option(argv, "--tools") == "Read,Glob,Grep"
    assert option(argv, "--max-turns") == "7"
    assert option(argv, "--model") == MODEL
    assert option(argv, "--add-dir") == str(tmp_path / "skill")
    assert "--resume" not in argv
    assert os.path.samefile(call["cwd"], tmp_path)
    # モデルへの接続先はローカルのOllamaだけで、補助モデルもローカルモデルに向いている
    assert call["base_url"] == BASE_URL
    assert call["haiku_model"] == MODEL
    assert call["nonessential_traffic"] == "1"


async def test_retry_resumes_previous_session_with_feedback_only(fake_claude, tmp_path):
    fake_claude.respond(claude_result("# 修正版", session_id="s1"))
    messages = [
        SystemMessage("スキル本文"),
        HumanMessage("作成して"),
        AIMessage("# 初版", response_metadata={"session_id": "s1"}),
        HumanMessage("見出しが足りません"),
    ]

    reply = await make_llm(fake_claude, tmp_path).ainvoke(messages)

    assert reply.content == "# 修正版"
    argv = fake_claude.calls()[0]["argv"]
    assert argv[:2] == ["-p", "見出しが足りません"]
    assert option(argv, "--resume") == "s1"
    assert "--append-system-prompt" not in argv


async def test_error_result_raises_with_subtype(fake_claude, tmp_path):
    fake_claude.respond(claude_result("", is_error=True, subtype="error_max_turns") | {"exit": 1})

    with pytest.raises(ClaudeCodeError, match="error_max_turns"):
        await make_llm(fake_claude, tmp_path).ainvoke([HumanMessage("作成して")])


async def test_unparsable_output_raises_with_stderr(fake_claude, tmp_path):
    fake_claude.respond({"stdout": "not json", "stderr": "API Error: connection refused", "exit": 1})

    with pytest.raises(ClaudeCodeError, match="connection refused"):
        await make_llm(fake_claude, tmp_path).ainvoke([HumanMessage("作成して")])


async def test_hung_process_is_killed_after_timeout(fake_claude, tmp_path):
    fake_claude.respond(claude_result("# 設計書") | {"sleep": 30})

    with pytest.raises(ClaudeCodeError, match="終了しませんでした"):
        await make_llm(fake_claude, tmp_path, timeout_s=0.5).ainvoke([HumanMessage("作成して")])


async def test_last_message_must_be_human(fake_claude, tmp_path):
    with pytest.raises(ValueError):
        await make_llm(fake_claude, tmp_path).ainvoke([AIMessage("x")])


def test_missing_claude_command_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_BIN", "no-such-claude-command")

    with pytest.raises(RuntimeError, match="CLAUDE_BIN"):
        build_claude_code(load_settings(), tmp_path, tmp_path)


def test_load_skill_requires_existing_path(tmp_path):
    with pytest.raises(ValueError, match="CLAUDE_SKILL_PATH"):
        load_skill(None)
    with pytest.raises(FileNotFoundError):
        load_skill(tmp_path / "SKILL.md")


def test_agent_messages_embed_skill_and_target_file(tmp_path):
    facts = extract_facts("package h\n\nfunc GetUser() {}\n")

    system, human = build_agent_messages(
        "スキル本文 {placeholder}", tmp_path, date(2026, 11, 3), "user_api/pkg/handler/user.go", facts, None
    )

    # スキル内の波括弧はそのまま残る（format の置換対象にならない）
    assert "スキル本文 {placeholder}" in system.content
    assert str(tmp_path) in system.content
    assert "2026-11-03" in system.content
    assert "user_api/pkg/handler/user.go" in human.content
    assert "GetUser" in human.content
    assert "新規作成" in human.content


def test_agent_messages_carry_over_previous_doc_for_history(tmp_path):
    facts = extract_facts("package h\n\nfunc GetUser() {}\n")
    previous = "# 設計書\n## 履歴\n| 1.0 | 2026-11-01 | 初版 |\n"

    _, human = build_agent_messages("スキル", tmp_path, date(2026, 11, 3), "h/user.go", facts, previous)

    assert previous in human.content
    assert "履歴" in human.content
    assert "新規作成" not in human.content
