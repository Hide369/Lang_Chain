import json
from pathlib import Path

import pytest

from conftest import ScriptedLLM, good_doc, reply
from godoc_agent import runner
from godoc_agent.config import load_settings


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")
    (root / "pkg" / "calc.go").write_text("package pkg\n\nfunc Add(a, b int) int { return a + b }\n", encoding="utf-8")
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
    use_llm(monkeypatch, ScriptedLLM([reply(good_doc("main")), reply(good_doc("Add"))]))

    assert await run_batch(repo, out) == 0
    assert (out / "main.go.md").is_file()
    assert (out / "pkg" / "calc.go.md").is_file()
    assert set(json.loads((out / ".manifest.json").read_text(encoding="utf-8"))) == {"main.go", "pkg/calc.go"}

    # 2回目は変更がないのでLLMは呼ばれない（呼ばれたらScriptedLLMが失敗する）
    use_llm(monkeypatch, ScriptedLLM([]))
    assert await run_batch(repo, out, "--run-id", "r2") == 0


async def test_one_failing_file_does_not_stop_the_batch(env, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    # main.go → 失敗（with_retryは偽LLMには掛からないため1回で失敗扱い）、pkg/calc.go → 成功
    use_llm(monkeypatch, ScriptedLLM([ValueError("boom"), reply(good_doc("Add"))]))

    assert await run_batch(repo, out) == 1

    logs = [json.loads(line) for line in (out / "_runs" / "r1.jsonl").read_text(encoding="utf-8").splitlines()]
    statuses = {log["rel_path"]: log["status"] for log in logs}
    assert statuses == {"main.go": "error", "pkg/calc.go": "done"}
    assert "draft" not in logs[0]
    assert not (out / "main.go.md").exists()


async def test_rerun_with_same_run_id_resumes_failed_session(env, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    use_llm(monkeypatch, ScriptedLLM([ConnectionError("down"), reply(good_doc("Add"))]))
    assert await run_batch(repo, out) == 1

    # 同じrun_idで再実行すると、失敗したmain.goだけがチェックポイントから再開される
    llm = ScriptedLLM([reply(good_doc("main"))])
    use_llm(monkeypatch, llm)
    assert await run_batch(repo, out) == 0
    assert len(llm.calls) == 1
    assert (out / "main.go.md").is_file()


def test_thread_id_is_stable_per_run_and_path():
    assert runner.thread_id_for("r1", "a.go") == runner.thread_id_for("r1", "a.go")
    assert runner.thread_id_for("r1", "a.go") != runner.thread_id_for("r1", "b.go")
    assert runner.thread_id_for("r1", "a.go") != runner.thread_id_for("r2", "a.go")
