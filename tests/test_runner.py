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
def skill_file(tmp_path: Path) -> Path:
    path = tmp_path / "skills" / "design-doc-batch" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("# 詳細設計書スキル\n手順...\n", encoding="utf-8")
    return path


@pytest.fixture
def env(monkeypatch, tmp_path: Path, skill_file: Path):
    monkeypatch.setenv("CLAUDE_SKILL_PATH", str(skill_file))
    monkeypatch.delenv("CLAUDE_COMMENT_SKILL_PATH", raising=False)
    monkeypatch.setenv("CHECKPOINT_DB", str(tmp_path / "state" / "cp.sqlite"))
    monkeypatch.setenv("LLM_CONCURRENCY", "1")  # 応答の順序を決定的にする
    # 手元に targets.toml があっても結果が変わらないよう、例の設定（Go の handler 配下）に固定する
    monkeypatch.setenv("TARGETS_CONFIG", str(Path(runner.__file__).resolve().parent.parent / "targets.example.toml"))

    async def no_preflight(settings):
        return None

    monkeypatch.setattr(runner, "preflight", no_preflight)


def use_llm(monkeypatch, llm: ScriptedLLM) -> None:
    monkeypatch.setattr(runner, "build_claude_code", lambda settings, repo, skill_dir: llm)


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


async def test_changed_file_is_revised_from_previous_doc(env, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    use_llm(monkeypatch, ScriptedLLM([reply(good_doc("Health") + "\n履歴 v1.0"), reply(good_doc("GetUser"))]))
    assert await run_batch(repo, out) == 0

    (repo / HEALTH).write_text("package handler\n\nfunc Health() { _ = 1 }\n", encoding="utf-8")
    llm = ScriptedLLM([reply(good_doc("Health") + "\n履歴 v1.0 / v1.1")])
    use_llm(monkeypatch, llm)
    assert await run_batch(repo, out, "--run-id", "r2") == 0

    # 変更されたファイルだけが、前回の設計書（履歴）を添えて再生成される
    assert len(llm.calls) == 1
    assert "履歴 v1.0" in llm.calls[0][-1].content
    assert (out / f"{HEALTH}.md").read_text(encoding="utf-8").endswith("v1.0 / v1.1\n")


async def test_one_failing_file_does_not_stop_the_batch(env, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    # health.go → 失敗、user.go → 成功
    use_llm(monkeypatch, ScriptedLLM([ValueError("boom"), reply(good_doc("GetUser"))]))

    assert await run_batch(repo, out) == 1

    logs = [json.loads(line) for line in (out / "_runs" / "r1.jsonl").read_text(encoding="utf-8").splitlines()]
    statuses = {log["rel_path"]: log["status"] for log in logs}
    assert statuses == {HEALTH: "error", USER: "done"}
    assert "draft" not in logs[0]
    assert not (out / f"{HEALTH}.md").exists()


async def test_rerun_with_same_run_id_resumes_failed_session(env, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    use_llm(monkeypatch, ScriptedLLM([RuntimeError("claude crashed"), reply(good_doc("GetUser"))]))
    assert await run_batch(repo, out) == 1

    # 同じrun_idで再実行すると、失敗したhealth.goだけがチェックポイントから再開される
    llm = ScriptedLLM([reply(good_doc("Health"))])
    use_llm(monkeypatch, llm)
    assert await run_batch(repo, out) == 0
    assert len(llm.calls) == 1
    assert (out / f"{HEALTH}.md").is_file()


def option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


async def test_runs_claude_code_in_repo_and_retries_in_same_session(env, monkeypatch, repo, tmp_path, fake_claude):
    monkeypatch.setenv("CLAUDE_BIN", fake_claude.bin)
    fake_claude.respond(
        claude_result(good_doc("Health"), session_id="s-health"),
        claude_result("識別子の記載なし", session_id="s-user"),
        claude_result(good_doc("GetUser"), session_id="s-user"),
    )
    out = tmp_path / "out"

    assert await run_batch(repo, out) == 0

    assert (out / f"{HEALTH}.md").read_text(encoding="utf-8").startswith("# 詳細設計書")
    assert (out / f"{USER}.md").is_file()
    calls = fake_claude.calls()
    assert len(calls) == 3
    assert all(os.path.samefile(c["cwd"], repo) for c in calls)
    assert "# 詳細設計書スキル" in option(calls[0]["argv"], "--append-system-prompt")
    assert USER in calls[1]["argv"][1]
    # 検証NGの再生成は、同じセッションを --resume して指摘だけを送る
    assert option(calls[2]["argv"], "--resume") == "s-user"


def read_logs(out: Path) -> dict[str, dict]:
    lines = (out / "_runs" / "r1.jsonl").read_text(encoding="utf-8").splitlines()
    return {log["rel_path"]: log for log in map(json.loads, lines)}


async def test_input_near_context_limit_is_warned_without_failing(env, monkeypatch, repo, tmp_path, fake_claude, capsys):
    monkeypatch.setenv("CLAUDE_BIN", fake_claude.bin)
    monkeypatch.setenv("LLM_NUM_CTX", "10000")
    monkeypatch.setenv("LLM_CTX_WARN_RATIO", "0.9")
    fake_claude.respond(
        # health.go: キャッシュ分を含めると 9,500 トークンで 90%（9,000）を超える
        claude_result(good_doc("Health"), usage={"input_tokens": 500, "cache_read_input_tokens": 9000, "output_tokens": 10}),
        # user.go: 8,000 トークンで超えない
        claude_result(good_doc("GetUser"), usage={"input_tokens": 8000, "output_tokens": 10}),
    )
    out = tmp_path / "out"

    # 警告があっても合否は変わらず、出力も保存される
    assert await run_batch(repo, out) == 0

    assert (out / f"{HEALTH}.md").is_file()
    logs = read_logs(out)
    assert logs[HEALTH]["status"] == "done"
    assert logs[HEALTH]["issues"] == []
    assert logs[HEALTH]["llm_calls"][0]["total_input_tokens"] == 9500
    assert len(logs[HEALTH]["warnings"]) == 1
    assert "9500 トークン" in logs[HEALTH]["warnings"][0]
    assert logs[USER]["warnings"] == []
    printed = capsys.readouterr().out
    assert "[警告: " in printed
    assert "入力トークン(1回あたり): 最大 9500 / 平均 8750" in printed


async def test_missing_usage_is_logged_as_null_without_warning(env, monkeypatch, repo, tmp_path, fake_claude):
    monkeypatch.setenv("CLAUDE_BIN", fake_claude.bin)
    no_usage = [claude_result(good_doc(name)) for name in ("Health", "GetUser")]
    for response in no_usage:
        del response["json"]["usage"]
    fake_claude.respond(*no_usage)
    out = tmp_path / "out"

    assert await run_batch(repo, out) == 0

    call = read_logs(out)[HEALTH]["llm_calls"][0]
    assert call["input_tokens"] is None
    assert call["total_input_tokens"] is None
    assert read_logs(out)[HEALTH]["warnings"] == []


def test_summary_shows_input_tokens_only_when_known():
    def result(*totals):
        calls = [{"num_turns": 3, "total_input_tokens": t} for t in totals]
        return runner.FileResult("doc", "a.go", "h", "t", "done", 1.0, llm_calls=calls)

    assert "入力トークン" not in runner.summarize([result(None)], 60)
    summary = runner.summarize([result(1000, None), result(3000)], 60)
    assert "入力トークン(1回あたり): 最大 3000 / 平均 2000（3 回中 2 回で取得）" in summary


@pytest.mark.parametrize("value", ["0", "1.5", "-0.1", "abc", "nan"])
def test_invalid_ctx_warn_ratio_is_rejected(monkeypatch, value):
    monkeypatch.setenv("LLM_CTX_WARN_RATIO", value)

    with pytest.raises(ValueError, match="LLM_CTX_WARN_RATIO"):
        load_settings()


def test_ctx_warn_ratio_accepts_upper_bound(monkeypatch):
    monkeypatch.setenv("LLM_CTX_WARN_RATIO", "1")
    monkeypatch.setenv("LLM_NUM_CTX", "8192")

    assert load_settings().ctx_warn_tokens == 8192


async def test_missing_skill_file_fails_before_scanning(env, monkeypatch, repo, tmp_path):
    monkeypatch.setenv("CLAUDE_SKILL_PATH", str(tmp_path / "skills" / "none" / "SKILL.md"))

    with pytest.raises(FileNotFoundError, match="SKILL.md"):
        await run_batch(repo, tmp_path / "out")


async def test_targets_config_selects_languages_and_folders(env, monkeypatch, repo, tmp_path):
    config = tmp_path / "targets.toml"
    config.write_text(
        """
[[languages]]
name = "go"
extensions = [".go"]
include = ["**/?*_api/pkg/handler/**"]

[[languages]]
name = "typescript"
extensions = [".ts"]
include = ["web/src/**"]
exclude = ["**/*.test.ts"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TARGETS_CONFIG", str(config))
    ts = "web/src/user.ts"
    (repo / ts).parent.mkdir(parents=True)
    (repo / ts).write_text("export function getUser(id: string) { return id }\n", encoding="utf-8")
    (repo / "web/src/user.test.ts").write_text("export function testIt() {}\n", encoding="utf-8")
    out = tmp_path / "out"
    # パス順に Go 2件 → TypeScript 1件。除外した user.test.ts が処理されたら応答が足りずに失敗する
    llm = ScriptedLLM([reply(good_doc("Health")), reply(good_doc("GetUser")), reply(good_doc("getUser"))])
    use_llm(monkeypatch, llm)

    assert await run_batch(repo, out) == 0

    assert (out / f"{ts}.md").is_file()
    system, human = llm.calls[2]
    # 設計書スキルは共通で、言語名だけがプロンプトで切り替わる
    assert "TypeScriptの詳細設計書" in system.content
    assert "TypeScriptファイル" in human.content and ts in human.content
    assert "Goの詳細設計書" in llm.calls[0][0].content


def test_thread_id_is_stable_per_run_task_and_path():
    assert runner.thread_id_for("r1", "doc", "a.go") == runner.thread_id_for("r1", "doc", "a.go")
    assert runner.thread_id_for("r1", "doc", "a.go") != runner.thread_id_for("r1", "doc", "b.go")
    assert runner.thread_id_for("r1", "doc", "a.go") != runner.thread_id_for("r2", "doc", "a.go")
    assert runner.thread_id_for("r1", "doc", "a.go") != runner.thread_id_for("r1", "comment", "a.go")


@pytest.fixture
def comment_skill(monkeypatch, tmp_path: Path) -> Path:
    path = tmp_path / "skills" / "go-comment" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("# コメント追記スキル\n手順...\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_COMMENT_SKILL_PATH", str(path))
    return path


def with_comment(repo: Path, rel_path: str) -> str:
    """コメント追記スキルの出力を模す（コードは変えずに先頭へコメントを足す）。"""
    return "// Package handler はAPIのハンドラー。\n" + (repo / rel_path).read_text(encoding="utf-8")


async def test_runs_doc_and_comment_tasks_in_one_batch(env, comment_skill, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    # 1ファイルごとに 設計書 → コメント追記 の順で処理される（LLM_CONCURRENCY=1）
    llm = ScriptedLLM([
        reply(good_doc("Health")), reply(with_comment(repo, HEALTH)),
        reply(good_doc("GetUser")), reply(with_comment(repo, USER)),
    ])
    use_llm(monkeypatch, llm)

    assert await run_batch(repo, out) == 0

    assert (out / f"{USER}.md").read_text(encoding="utf-8").startswith("# 詳細設計書")
    # コメント追記済みのGoファイルは _commented 配下に出力し、元のリポジトリは書き換えない
    assert (out / "_commented" / USER).read_text(encoding="utf-8") == with_comment(repo, USER)
    assert not (repo / USER).read_text(encoding="utf-8").startswith("//")
    assert set(json.loads((out / "_commented" / ".manifest.json").read_text(encoding="utf-8"))) == {HEALTH, USER}
    assert "# コメント追記スキル" in llm.calls[1][0].content

    # 2回目は両タスクとも変更がないのでスキップされる
    use_llm(monkeypatch, ScriptedLLM([]))
    assert await run_batch(repo, out, "--run-id", "r2") == 0


async def test_comment_that_changes_code_is_not_written(env, comment_skill, monkeypatch, repo, tmp_path):
    monkeypatch.setenv("DOC_MAX_ATTEMPTS", "1")
    out = tmp_path / "out"
    broken = with_comment(repo, HEALTH).replace("func Health()", "func Healthz()")
    use_llm(monkeypatch, ScriptedLLM([
        reply(good_doc("Health")), reply(broken),
        reply(good_doc("GetUser")), reply(with_comment(repo, USER)),
    ]))

    assert await run_batch(repo, out) == 1

    assert not (out / "_commented" / HEALTH).exists()
    assert (out / f"{HEALTH}.md").is_file()  # 同じファイルの設計書は影響を受けない
    logs = [json.loads(line) for line in (out / "_runs" / "r1.jsonl").read_text(encoding="utf-8").splitlines()]
    failed = [log for log in logs if log["status"] != "done"]
    assert [(log["task"], log["rel_path"], log["status"]) for log in failed] == [("comment", HEALTH, "failed_validation")]


async def test_tasks_option_runs_only_selected_task(env, comment_skill, monkeypatch, repo, tmp_path):
    # 設計書スキルが未設定でも、コメント追記だけなら実行できる
    monkeypatch.delenv("CLAUDE_SKILL_PATH")
    out = tmp_path / "out"
    use_llm(monkeypatch, ScriptedLLM([reply(with_comment(repo, HEALTH)), reply(with_comment(repo, USER))]))

    assert await run_batch(repo, out, "--tasks", "comment") == 0

    assert (out / "_commented" / HEALTH).is_file()
    assert not (out / f"{HEALTH}.md").exists()


async def test_comment_task_requires_its_skill_path(env, repo, tmp_path):
    with pytest.raises(ValueError, match="CLAUDE_COMMENT_SKILL_PATH"):
        await run_batch(repo, tmp_path / "out", "--tasks", "doc,comment")


async def test_comment_task_rejects_file_exceeding_output_limit(env, comment_skill, monkeypatch, repo, tmp_path):
    # ファイル全文を出力するため、LLM_NUM_PREDICT から決まる上限を超えるファイルはLLMを呼ばずに弾く
    monkeypatch.setenv("LLM_NUM_PREDICT", "10")
    use_llm(monkeypatch, ScriptedLLM([]))

    assert await run_batch(repo, tmp_path / "out", "--tasks", "comment") == 1

    logs = (tmp_path / "out" / "_runs" / "r1.jsonl").read_text(encoding="utf-8").splitlines()
    assert {json.loads(line)["status"] for line in logs} == {"too_large"}


def test_unknown_task_name_is_rejected():
    with pytest.raises(SystemExit):
        runner.parse_args(["--repo", ".", "--out", "out", "--tasks", "doc,lint"])
