import asyncio
import json
import os
from pathlib import Path

import pytest

from conftest import ScriptedLLM, claude_result, good_doc, reply
from godoc_agent import runner
from godoc_agent.config import load_settings
from godoc_agent.runlock import RunLockBusy, run_lock
from godoc_agent.skill_registry import COMMENT_ONLY, DOCUMENT, REWRITE, write_registry

HEALTH = "user_api/pkg/handler/health.go"
USER = "user_api/pkg/handler/user.go"
# 設計書の出力先（user.go → 詳細設計書_user.md）
HEALTH_DOC = "user_api/pkg/handler/詳細設計書_health.md"
USER_DOC = "user_api/pkg/handler/詳細設計書_user.md"


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
    assert (out / HEALTH_DOC).is_file()
    assert (out / USER_DOC).is_file()
    assert set(json.loads((out / ".manifest.json").read_text(encoding="utf-8"))) == {HEALTH, USER}

    # 2回目は変更がないのでLLMは呼ばれない（呼ばれたらScriptedLLMが失敗する）
    use_llm(monkeypatch, ScriptedLLM([]))
    assert await run_batch(repo, out, "--run-id", "r2") == 0


async def test_refuses_to_run_while_another_run_uses_the_same_out_dir(env, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    use_llm(monkeypatch, ScriptedLLM([]))
    # 夜間バッチや Web 画面からの実行が同じ出力先で動いている状態
    with run_lock(out):
        with pytest.raises(RunLockBusy):
            await run_batch(repo, out)


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
    assert (out / HEALTH_DOC).read_text(encoding="utf-8").endswith("v1.0 / v1.1\n")


async def test_one_failing_file_does_not_stop_the_batch(env, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    # health.go → 失敗、user.go → 成功
    use_llm(monkeypatch, ScriptedLLM([ValueError("boom"), reply(good_doc("GetUser"))]))

    assert await run_batch(repo, out) == 1

    logs = [json.loads(line) for line in (out / "_runs" / "r1.jsonl").read_text(encoding="utf-8").splitlines()]
    statuses = {log["rel_path"]: log["status"] for log in logs}
    assert statuses == {HEALTH: "error", USER: "done"}
    assert "draft" not in logs[0]
    assert not (out / HEALTH_DOC).exists()


async def test_rerun_regenerates_only_failed_file_from_scratch(env, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    # health.go は1回目の草稿が検証NG（再生成のため履歴が積まれる）の後に落ちる
    use_llm(monkeypatch, ScriptedLLM([reply("識別子の記載なし"), RuntimeError("claude crashed"), reply(good_doc("GetUser"))]))
    assert await run_batch(repo, out) == 1

    # 再実行では完了済みの user.go は manifest で飛ばされ、health.go だけが最初から生成される
    llm = ScriptedLLM([reply(good_doc("Health"))])
    use_llm(monkeypatch, llm)
    assert await run_batch(repo, out) == 0
    assert len(llm.calls) == 1
    # 前回の会話（草稿・フィードバック）は引き継がず、最初のメッセージ（システム＋依頼）だけを送る
    assert [type(m).__name__ for m in llm.calls[0]] == ["SystemMessage", "HumanMessage"]
    assert (out / HEALTH_DOC).is_file()


def option(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


async def test_runs_claude_code_in_repo_and_retries_in_new_session(env, monkeypatch, repo, tmp_path, fake_claude):
    monkeypatch.setenv("CLAUDE_BIN", fake_claude.bin)
    fake_claude.respond(
        claude_result(good_doc("Health"), session_id="s-health"),
        claude_result("識別子の記載なし", session_id="s-user"),
        claude_result(good_doc("GetUser"), session_id="s-user"),
    )
    out = tmp_path / "out"

    assert await run_batch(repo, out) == 0

    assert (out / HEALTH_DOC).read_text(encoding="utf-8").startswith("# 詳細設計書")
    assert (out / USER_DOC).is_file()
    calls = fake_claude.calls()
    assert len(calls) == 3
    assert all(os.path.samefile(c["cwd"], repo) for c in calls)
    assert "# 詳細設計書スキル" in option(calls[0]["argv"], "--append-system-prompt")
    assert USER in calls[1]["argv"][1]
    # 検証NGの再生成は --resume せず、新しいセッションに元の依頼・直前の出力・指摘をまとめて送る
    retry_argv = calls[2]["argv"]
    assert "--resume" not in retry_argv
    assert "# 詳細設計書スキル" in option(retry_argv, "--append-system-prompt")
    assert USER in retry_argv[1] and "識別子の記載なし" in retry_argv[1] and "GetUser" in retry_argv[1]


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

    assert (out / HEALTH_DOC).is_file()
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

    assert (out / "web/src/詳細設計書_user.md").is_file()
    system, human = llm.calls[2]
    # 設計書スキルは共通で、言語名だけがプロンプトで切り替わる
    assert "TypeScriptの詳細設計書" in system.content
    assert "TypeScriptファイル" in human.content and ts in human.content
    assert "Goの詳細設計書" in llm.calls[0][0].content


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

    assert (out / USER_DOC).read_text(encoding="utf-8").startswith("# 詳細設計書")
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
    assert (out / HEALTH_DOC).is_file()  # 同じファイルの設計書は影響を受けない
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
    assert not (out / HEALTH_DOC).exists()


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


async def test_unknown_task_name_is_rejected(env, repo, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "APP_DIR", tmp_path / "app")
    with pytest.raises(ValueError, match="lint.*登録されていません"):
        await run_batch(repo, tmp_path / "out", "--tasks", "doc,lint")


def test_empty_task_list_is_rejected():
    with pytest.raises(SystemExit):
        runner.parse_args(["--repo", ".", "--out", "out", "--tasks", " , "])


@pytest.fixture
def custom_skills(monkeypatch, tmp_path: Path) -> Path:
    """skills/registry.json で追加した3種類のスキル（文書・コメントだけ変更・コード変更あり）。"""
    app = tmp_path / "app"
    write_registry(app, {"test-spec": DOCUMENT, "jp-comment": COMMENT_ONLY, "fmt": REWRITE})
    for name in ("test-spec", "jp-comment", "fmt"):
        (app / "skills" / name).mkdir(parents=True)
        (app / "skills" / name / "SKILL.md").write_text(f"# {name} スキル\n", encoding="utf-8")
    monkeypatch.setattr(runner, "APP_DIR", app)
    return app


async def test_runs_registered_custom_skills(env, custom_skills, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    health = (repo / HEALTH).read_text(encoding="utf-8")
    llm = ScriptedLLM([
        reply("# テスト仕様書\nHealth の確認\n"),  # test-spec（文書）
        reply("// ヘルスチェック\n" + health),  # jp-comment（コメントだけ）
        reply(health.replace("func Health() {}", "func Health() {\n}")),  # fmt（コードも変更）
    ])
    use_llm(monkeypatch, llm)

    assert await run_batch(repo, out, "--tasks", "test-spec,jp-comment,fmt", "--limit", "3") == 0
    assert (out / "_test-spec" / f"{HEALTH}.md").read_text(encoding="utf-8").startswith("# テスト仕様書")
    assert (out / "_jp-comment" / HEALTH).read_text(encoding="utf-8").startswith("// ヘルスチェック")
    assert "func Health() {\n}" in (out / "_fmt" / HEALTH).read_text(encoding="utf-8")
    # スキルごとに差分スキップの記録を持つ
    for name in ("test-spec", "jp-comment", "fmt"):
        assert HEALTH in json.loads((out / f"_{name}" / ".manifest.json").read_text(encoding="utf-8"))
    # 文書のスキルには SKILL.md の内容と、文書を作る依頼を送る
    first = llm.calls[0]
    assert "# test-spec スキル" in first[0].content
    assert "スキルに従って文書を作成してください" in first[-1].content


async def test_custom_comment_skill_rejects_code_changes(env, custom_skills, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    changed = "package handler\n\nfunc Health() { _ = 1 }\n"
    use_llm(monkeypatch, ScriptedLLM([reply(changed)] * 3))
    assert await run_batch(repo, out, "--tasks", "jp-comment", "--limit", "1") == 1
    assert not (out / "_jp-comment" / HEALTH).exists()


async def test_custom_skill_without_skill_file_is_reported(env, custom_skills, repo, tmp_path):
    (custom_skills / "skills" / "fmt" / "SKILL.md").unlink()
    with pytest.raises(FileNotFoundError, match="SKILL.md"):
        await run_batch(repo, tmp_path / "out", "--tasks", "fmt")


async def test_unreadable_source_is_recorded_and_the_batch_continues(env, monkeypatch, repo, tmp_path):
    # UTF-8 でないファイル（Shift_JIS など）があっても、そのファイルだけを error にして残りを処理する
    (repo / HEALTH).write_bytes("package handler\n// 日本語\n".encode("cp932"))
    out = tmp_path / "out"
    use_llm(monkeypatch, ScriptedLLM([reply(good_doc("GetUser"))]))

    assert await run_batch(repo, out) == 1

    logs = read_logs(out)
    assert logs[HEALTH]["status"] == "error"
    assert "UTF-8" in logs[HEALTH]["error"]
    assert logs[USER]["status"] == "done"
    assert (out / USER_DOC).is_file()
    assert set(json.loads((out / ".manifest.json").read_text(encoding="utf-8"))) == {USER}


async def test_output_that_cannot_be_saved_fails_only_that_file(env, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    # 出力先にフォルダがあって書き込めない状態
    (out / HEALTH_DOC).mkdir(parents=True)
    use_llm(monkeypatch, ScriptedLLM([reply(good_doc("Health")), reply(good_doc("GetUser"))]))

    assert await run_batch(repo, out) == 1

    logs = read_logs(out)
    assert logs[HEALTH]["status"] == "error"
    assert "保存できません" in logs[HEALTH]["error"]
    assert logs[USER]["status"] == "done"
    assert set(json.loads((out / ".manifest.json").read_text(encoding="utf-8"))) == {USER}


async def test_doc_with_old_name_is_renamed_and_kept_up_to_date(env, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    use_llm(monkeypatch, ScriptedLLM([reply(good_doc("Health")), reply(good_doc("GetUser"))]))
    assert await run_batch(repo, out) == 0
    # 名前を変える前の出力（user.go.md）に戻す
    for rel, doc in ((HEALTH, HEALTH_DOC), (USER, USER_DOC)):
        (out / doc).rename(out / f"{rel}.md")

    # 変更がなければ新しい名前に移すだけで、作り直さない（LLM を呼んだら ScriptedLLM が失敗する）
    use_llm(monkeypatch, ScriptedLLM([]))
    assert await run_batch(repo, out, "--run-id", "r2") == 0
    assert (out / HEALTH_DOC).is_file() and (out / USER_DOC).is_file()
    assert not (out / f"{HEALTH}.md").exists()


async def test_sources_sharing_an_output_name_do_not_overwrite_each_other(env, monkeypatch, repo, tmp_path):
    config = tmp_path / "targets.toml"
    config.write_text(
        """
[[languages]]
name = "go"
extensions = [".go"]
include = ["web/**"]

[[languages]]
name = "typescript"
extensions = [".ts"]
include = ["web/**"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TARGETS_CONFIG", str(config))
    (repo / "web").mkdir()
    (repo / "web/user.go").write_text("package web\n\nfunc GetUser() {}\n", encoding="utf-8")
    (repo / "web/user.ts").write_text("export function getUser() {}\n", encoding="utf-8")
    out = tmp_path / "out"
    use_llm(monkeypatch, ScriptedLLM([reply(good_doc("GetUser"))]))

    assert await run_batch(repo, out) == 1

    logs = read_logs(out)
    assert logs["web/user.go"]["status"] == "done"
    assert logs["web/user.ts"]["status"] == "error"
    assert "web/詳細設計書_user.md" in logs["web/user.ts"]["error"]


# --- 同時セッション数・セッションの記録 ------------------------------------------------------

class ConcurrencyProbeLLM:
    """同時に何件の claude が動いているかを数える。どのファイルにも合格する設計書を返す。"""

    def __init__(self):
        self.active = 0
        self.max_active = 0

    async def ainvoke(self, messages, config=None, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.05)
        self.active -= 1
        return reply(good_doc("Health", "GetUser", "A", "B", "C"))


class PerFileLLM:
    """依頼文に含まれる対象ファイルごとに、用意した応答を順番に返す（並列実行でも決定的にする）。"""

    def __init__(self, responses: dict[str, list]):
        self._responses = {path: list(items) for path, items in responses.items()}

    async def ainvoke(self, messages, config=None, **kwargs):
        request = messages[-1].content
        path = next(p for p in self._responses if p in request)
        return self._responses[path].pop(0)


@pytest.fixture
def many_files(repo: Path) -> Path:
    # 既存の2ファイルに加えて、handler 配下に3ファイルを足す（計5件）
    for name in ("A", "B", "C"):
        (repo / f"user_api/pkg/handler/{name.lower()}.go").write_text(f"package handler\n\nfunc {name}() {{}}\n", encoding="utf-8")
    return repo


@pytest.mark.parametrize("sessions", ["1", "3", "10"])
async def test_concurrency_option_limits_simultaneous_sessions(env, monkeypatch, many_files, tmp_path, sessions):
    llm = ConcurrencyProbeLLM()
    use_llm(monkeypatch, llm)

    # .env（LLM_CONCURRENCY=1）より --concurrency を優先する
    assert await run_batch(many_files, tmp_path / "out", "--concurrency", sessions) == 0

    assert llm.max_active == min(int(sessions), 5)


@pytest.mark.parametrize("value", ["0", "11", "x"])
def test_concurrency_option_accepts_only_one_to_ten(value, capsys):
    with pytest.raises(SystemExit):
        runner.parse_args(["--repo", ".", "--out", "out", "--concurrency", value])
    assert "--concurrency" in capsys.readouterr().err


async def test_sessions_file_records_each_session_log(env, monkeypatch, repo, tmp_path, capsys):
    out = tmp_path / "out"
    # health.go は1回目が検証NGで再生成、user.go は1回で合格（並列に動くため、ファイルごとに応答を決める）
    use_llm(monkeypatch, PerFileLLM({
        HEALTH: [reply("識別子の記載なし"), reply(good_doc("Health"))],
        USER: [reply(good_doc("GetUser"))],
    }))

    assert await run_batch(repo, out, "--concurrency", "2") == 0

    data = json.loads((out / "_runs" / "r1.sessions.json").read_text(encoding="utf-8"))
    assert data["running"] is False
    assert data["concurrency"] == 2
    assert (data["active"], data["total"], data["finished"]) == (0, 2, 2)
    assert len(data["sessions"]) == 2
    assert sum(s["handled"] for s in data["sessions"]) == 2
    texts = [e["text"] for s in data["sessions"] for e in s["events"]]
    assert f"開始 doc {HEALTH}" in texts
    assert "生成 1/3 回目を開始" in texts
    assert "生成 2/3 回目を開始" in texts
    assert any(t.startswith("検証NG（") and t.endswith("再生成します") for t in texts)
    assert "検証OK" in texts
    assert sum(t.startswith("終了 done") for t in texts) == 2
    # 同じ出来事は、セッション番号を付けて実行ログ（標準出力）にも出る
    stdout = capsys.readouterr().out
    assert "同時セッション数=2" in stdout
    assert f"] 開始 doc {HEALTH}" in stdout and "[S0" in stdout


async def test_sessions_file_marks_run_finished_even_when_files_fail(env, monkeypatch, repo, tmp_path):
    out = tmp_path / "out"
    use_llm(monkeypatch, ScriptedLLM([ValueError("boom"), reply(good_doc("GetUser"))]))

    assert await run_batch(repo, out) == 1

    data = json.loads((out / "_runs" / "r1.sessions.json").read_text(encoding="utf-8"))
    assert data["running"] is False
    texts = [e["text"] for s in data["sessions"] for e in s["events"]]
    assert any(t.startswith("生成 1 回目が失敗: ValueError: boom") for t in texts)
    assert any(t.startswith("終了 error") and "boom" in t for t in texts)
