import json
import os
from pathlib import Path

import pytest

from conftest import ScriptedLLM, claude_result, good_doc, reply
from godoc_agent import runner
from godoc_agent.config import load_settings

HEALTH = "user_api/pkg/handler/health.go"
USER = "user_api/pkg/handler/user.go"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    files = {
        HEALTH: "package handler\n\nfunc Health() {}\n",
        USER: "package handler\n\nfunc GetUser(id string) string { return id }\n",
        # 対象外（handler 配下ではない）。処理されたら ScriptedLLM の応答が足りずに失敗する
        "user_api/main.go": "package main\n\nfunc main() {}\n",
    }
    for rel, body in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    return root


@pytest.fixture
def env(monkeypatch, tmp_path: Path, template_file: Path):
    monkeypatch.setenv("DOC_TEMPLATE_PATH", str(template_file))
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "state" / "cp.sqlite"))
    monkeypatch.setenv("LLM_CONCURRENCY", "1")  # 応答の順序を決定的にする

    async def no_preflight(settings):
        return None

    monkeypatch.setattr(runner, "preflight", no_preflight)


def use_llm(monkeypatch, llm: ScriptedLLM) -> None:
    monkeypatch.setattr(runner, "build_llm", lambda settings: llm)


async def run_batch(repo: Path, out: Path, *extra: str) -> int:
    args = runner.parse_args(["--repo", str(repo), "--out", str(out), "--run-id", "r1", *extra])
    return await runner.run(args, load_settings())


async def test_generates_docs_and_skips_unchanged_files_next_time(env, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    use_llm(monkeypatch, ScriptedLLM([reply(good_doc("Health")), reply(good_doc("GetUser"))]))

    assert await run_batch(repo, out) == 0
    assert (out / f"{HEALTH}.md").is_file()
    assert (out / f"{USER}.md").is_file()
    assert set(json.loads((out / ".manifest.json").read_text(encoding="utf-8"))) == {HEALTH, USER}

    # 2回目は変更がないのでLLMは呼ばれない（呼ばれたらScriptedLLMが失敗する）
    use_llm(monkeypatch, ScriptedLLM([]))
    assert await run_batch(repo, out, "--run-id", "r2") == 0


async def test_one_failing_file_does_not_stop_the_batch(env, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    # health.go → 失敗（with_retryは偽LLMには掛からないため1回で失敗扱い）、user.go → 成功
    use_llm(monkeypatch, ScriptedLLM([ValueError("boom"), reply(good_doc("GetUser"))]))

    assert await run_batch(repo, out) == 1

    logs = [json.loads(line) for line in (out / "_runs" / "r1.jsonl").read_text(encoding="utf-8").splitlines()]
    statuses = {log["rel_path"]: log["status"] for log in logs}
    assert statuses == {HEALTH: "error", USER: "done"}
    assert "draft" not in logs[0]
    assert not (out / f"{HEALTH}.md").exists()


async def test_rerun_with_same_run_id_resumes_failed_session(env, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    use_llm(monkeypatch, ScriptedLLM([ConnectionError("down"), reply(good_doc("GetUser"))]))
    assert await run_batch(repo, out) == 1

    # 同じrun_idで再実行すると、失敗したhealth.goだけがチェックポイントから再開される
    llm = ScriptedLLM([reply(good_doc("Health"))])
    use_llm(monkeypatch, llm)
    assert await run_batch(repo, out) == 0
    assert len(llm.calls) == 1
    assert (out / f"{HEALTH}.md").is_file()


@pytest.fixture
def skill_file(tmp_path: Path) -> Path:
    path = tmp_path / "skills" / "go-design-doc" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("# 詳細設計書スキル\n手順...\n", encoding="utf-8")
    return path


def option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


async def test_claude_code_engine_runs_agent_in_repo_and_retries_in_same_session(
    env, monkeypatch, repo, tmp_path, fake_claude, skill_file
):
    monkeypatch.setenv("CLAUDE_BIN", fake_claude.bin)
    monkeypatch.setenv("CLAUDE_SKILL_PATH", str(skill_file))
    fake_claude.respond(
        claude_result(good_doc("Health"), session_id="s-health"),
        claude_result("見出しなし", session_id="s-user"),
        claude_result(good_doc("GetUser"), session_id="s-user"),
    )
    out = tmp_path / "out"

    assert await run_batch(repo, out, "--engine", "claude-code") == 0

    assert (out / f"{HEALTH}.md").read_text(encoding="utf-8").startswith("# 詳細設計書")
    assert (out / f"{USER}.md").is_file()
    calls = fake_claude.calls()
    assert len(calls) == 3
    assert all(os.path.samefile(c["cwd"], repo) for c in calls)
    assert "# 詳細設計書スキル" in option(calls[0]["argv"], "--append-system-prompt")
    assert USER in calls[1]["argv"][1]
    # 検証NGの再生成は、同じセッションを --resume して指摘だけを送る
    assert option(calls[2]["argv"], "--resume") == "s-user"


async def test_claude_code_engine_requires_skill_path(env, monkeypatch, repo, tmp_path):
    monkeypatch.delenv("CLAUDE_SKILL_PATH", raising=False)

    with pytest.raises(ValueError, match="CLAUDE_SKILL_PATH"):
        await run_batch(repo, tmp_path / "out", "--engine", "claude-code")


def test_thread_id_is_stable_per_run_and_path():
    assert runner.thread_id_for("r1", "a.go") == runner.thread_id_for("r1", "a.go")
    assert runner.thread_id_for("r1", "a.go") != runner.thread_id_for("r1", "b.go")
    assert runner.thread_id_for("r1", "a.go") != runner.thread_id_for("r2", "a.go")
