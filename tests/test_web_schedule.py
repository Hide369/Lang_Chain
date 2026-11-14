"""Web 画面の定期実行と、実行前の git pull のテスト。フィクスチャは test_web.py のものを使う。"""

import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from godoc_agent.runlock import run_lock
from godoc_agent.skill_registry import COMMENT
from godoc_agent.web import pull_and_run
from godoc_agent.web.git_repo import redact_url
from test_web import (  # noqa: F401  フィクスチャを読み込む
    CSRF, admin, app_dir, b64, client, jobs, login, make_client, out_dir, password_hash, repo_dir, root,
    set_workspace, skills, start_run, wait_finished,
)


class FakeClock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 8, 1, 0))


@pytest.fixture
def sched_client(make_client, clock, repo_dir, out_dir) -> TestClient:
    client = make_client(clock=clock)
    assert login(client).status_code == 200
    assert set_workspace(client, repo_dir, out_dir).status_code == 200
    return client


def scheduler(client: TestClient):
    return client.app.state.scheduler


def save_schedule(client: TestClient, **body):
    payload = {"enabled": True, "time": "02:00", "tasks": ["doc"], "git_pull": False, **body}
    return client.put("/api/schedule", json=payload, headers=CSRF)


def log_text(client: TestClient, run_id: str) -> str:
    return client.get(f"/api/runs/{run_id}/log").json()["text"]


# --- 設定 ---------------------------------------------------------------------------------


def test_viewer_can_see_schedule_but_only_admin_can_change_it(make_client):
    client = make_client()
    res = client.get("/api/schedule")
    assert res.status_code == 200
    assert res.json()["enabled"] is False
    assert res.json()["next_run_at"] is None
    assert save_schedule(client).status_code == 401


@pytest.mark.parametrize("body", [{"time": "24:00"}, {"time": "2:00x"}, {"tasks": []}, {"tasks": ["unknown"]}])
def test_invalid_schedule_is_rejected(sched_client, body):
    assert save_schedule(sched_client, **body).status_code in (400, 422)
    assert sched_client.get("/api/schedule").json()["enabled"] is False


def test_next_run_is_today_before_the_time_and_tomorrow_after_it(sched_client, clock):
    assert save_schedule(sched_client).json()["next_run_at"] == "2026-11-08T02:00"
    # 時刻を過ぎてから保存すると、保存した途端には動かず、翌日から
    clock.now = datetime(2026, 11, 8, 3, 0)
    assert save_schedule(sched_client).json()["next_run_at"] == "2026-11-09T02:00"
    assert scheduler(sched_client).tick() is None


# --- 定期実行 -------------------------------------------------------------------------------


def test_runs_once_a_day_at_the_scheduled_time(sched_client, jobs, clock, repo_dir, out_dir):
    save_schedule(sched_client)
    sch = scheduler(sched_client)

    clock.now = datetime(2026, 11, 8, 1, 59)
    assert sch.tick() is None

    clock.now = datetime(2026, 11, 8, 2, 0)
    job = sch.tick()
    assert job is not None
    assert job.run_id.startswith("scheduled-")
    wait_finished(jobs)
    status = sched_client.get("/api/status").json()["job"]
    assert status["trigger"] == "scheduled"
    args = log_text(sched_client, job.run_id)
    assert f"--repo {repo_dir.resolve()}" in args
    assert f"--out {out_dir.resolve()}" in args
    assert "--tasks doc" in args

    schedule = sched_client.get("/api/schedule").json()
    assert schedule["last_run"]["run_id"] == job.run_id
    assert schedule["next_run_at"] == "2026-11-09T02:00"

    # 同じ日にはもう実行しない
    clock.now = datetime(2026, 11, 8, 23, 0)
    assert sch.tick() is None
    # 翌日の時刻になったら、また実行する
    clock.now = datetime(2026, 11, 9, 2, 0)
    assert sch.tick() is not None


def test_schedule_keeps_folders_of_the_person_who_saved_it(sched_client, jobs, clock, repo_dir, out_dir, root):
    # 複数人がそれぞれ別のフォルダを選べるため、定期実行は保存したときのフォルダで動く（後で誰かが選び直しても変わらない）
    save_schedule(sched_client)
    assert sched_client.get("/api/schedule").json()["out"] == str(out_dir.resolve())
    assert set_workspace(sched_client, repo_dir, root / "other_out").status_code == 200

    clock.now = datetime(2026, 11, 8, 2, 0)
    job = scheduler(sched_client).tick()
    assert job is not None
    assert job.out_dir == out_dir.resolve()


def test_scheduled_run_does_not_wait_for_runs_on_other_outputs(sched_client, jobs, clock, repo_dir, root, monkeypatch):
    save_schedule(sched_client)
    monkeypatch.setenv("FAKE_RUNNER_SLEEP", "30")
    # 別の人が別の出力先で実行中でも、定期実行は始まる（セッション数はサーバー全体で抑えられる）
    assert set_workspace(sched_client, repo_dir, root / "other_out").status_code == 200
    assert start_run(sched_client).status_code == 202
    clock.now = datetime(2026, 11, 8, 2, 0)
    assert scheduler(sched_client).tick() is not None


def test_missed_run_is_executed_when_server_starts_later_that_day(sched_client, clock):
    save_schedule(sched_client)
    # 2:00 に Web 画面が止まっていて、9:00 に起動した
    clock.now = datetime(2026, 11, 8, 9, 0)
    assert scheduler(sched_client).tick() is not None


def test_waits_for_running_job_then_runs(sched_client, jobs, clock, monkeypatch):
    save_schedule(sched_client)
    monkeypatch.setenv("FAKE_RUNNER_SLEEP", "30")
    start_run(sched_client)  # 手動の実行が動いている
    clock.now = datetime(2026, 11, 8, 2, 0)
    assert scheduler(sched_client).tick() is None
    assert sched_client.get("/api/schedule").json()["next_run_at"] == "2026-11-08T02:00"

    sched_client.post("/api/runs/stop", headers=CSRF)
    wait_finished(jobs)
    monkeypatch.setenv("FAKE_RUNNER_SLEEP", "0")
    clock.now = datetime(2026, 11, 8, 2, 1)
    assert scheduler(sched_client).tick() is not None


def test_waits_while_nightly_or_cli_run_holds_the_lock(sched_client, clock, out_dir):
    save_schedule(sched_client)
    clock.now = datetime(2026, 11, 8, 2, 0)
    with run_lock(out_dir.resolve()):
        assert scheduler(sched_client).tick() is None
    assert scheduler(sched_client).tick() is not None


def test_records_why_it_could_not_start_and_retries_next_day(make_client, clock, repo_dir, out_dir):
    client = make_client(clock=clock)
    login(client)
    save_schedule(client)  # フォルダが未設定
    clock.now = datetime(2026, 11, 8, 2, 0)
    assert scheduler(client).tick() is None
    schedule = client.get("/api/schedule").json()
    assert "未設定" in schedule["last_run"]["error"]
    assert schedule["next_run_at"] == "2026-11-09T02:00"

    set_workspace(client, repo_dir, out_dir)
    clock.now = datetime(2026, 11, 9, 2, 0)
    assert scheduler(client).tick() is not None


def test_tasks_without_registered_skill_are_skipped(sched_client, jobs, clock):
    files = [{"path": "SKILL.md", "content_b64": b64("# コメント追記\n")}]
    assert sched_client.put("/api/skills/comment", json={"files": files}, headers=CSRF).status_code == 200
    assert save_schedule(sched_client, tasks=["doc", COMMENT]).status_code == 200
    # 保存した後で登録を外したスキルは、実行時に除く
    sched_client.delete("/api/skills/comment", headers=CSRF)
    clock.now = datetime(2026, 11, 8, 2, 0)
    job = scheduler(sched_client).tick()
    assert job.tasks == ["doc"]


def test_disabled_schedule_does_not_run(sched_client, clock):
    save_schedule(sched_client, enabled=False)
    clock.now = datetime(2026, 11, 8, 2, 0)
    assert scheduler(sched_client).tick() is None


def test_scheduler_runs_in_background_while_app_is_running(make_client, clock, jobs, repo_dir, out_dir):
    clock.now = datetime(2026, 11, 8, 1, 0)
    client = make_client(clock=clock)
    login(client)
    set_workspace(client, repo_dir, out_dir)
    save_schedule(client)
    clock.now = datetime(2026, 11, 8, 2, 0)
    # TestClient を with で使うと、アプリの起動・終了処理（定期実行の開始・停止）が動く
    with client:
        deadline = time.monotonic() + 10
        while jobs.current is None and time.monotonic() < deadline:
            time.sleep(0.05)
    assert jobs.current is not None
    assert jobs.current.trigger == "scheduled"


# --- 実行前の git pull ------------------------------------------------------------------------

GIT_USER = ("-c", "user.name=t", "-c", "user.email=t@example.com")


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=path)
    git(*GIT_USER, "commit", "-q", "--allow-empty", "-m", "first", cwd=path)
    return path


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "origin")


@pytest.fixture
def cloned_repo(root: Path, tmp_path: Path, origin: Path) -> Path:
    """origin から clone したリポジトリ。origin には clone 後のコミットがある。"""
    repo = root / "repos" / "cloned"
    git("clone", "-q", str(origin), str(repo), cwd=tmp_path)
    (origin / "new.go").write_text("package x\n", encoding="utf-8")
    git("add", "new.go", cwd=origin)
    git(*GIT_USER, "commit", "-q", "-m", "second", cwd=origin)
    return repo


def test_git_pull_updates_repo_before_running(admin, jobs, cloned_repo, origin, out_dir):
    set_workspace(admin, cloned_repo, out_dir)
    run_id = start_run(admin, git_pull=True).json()["run_id"]
    wait_finished(jobs)
    text = log_text(admin, run_id)
    # pull の前に、pull 先（URL・追跡ブランチ）とリポジトリをログに出す
    assert "pull 先:" in text
    assert "origin/main" in text
    assert origin.name in text
    assert "commit:" in text
    assert "args:" in text  # runner が起動した
    assert (cloned_repo / "new.go").is_file()
    assert admin.get("/api/status").json()["job"]["exit_code"] == 0


def test_git_pull_is_refused_for_folder_that_is_not_a_repo(admin, repo_dir):
    res = start_run(admin, git_pull=True)
    assert res.status_code == 400
    assert "git リポジトリではありません" in res.json()["detail"]


def test_git_pull_is_refused_for_subfolder_of_clone(admin, cloned_repo, out_dir):
    sub = cloned_repo / "pkg"
    sub.mkdir()
    set_workspace(admin, sub, out_dir)
    res = start_run(admin, git_pull=True)
    assert res.status_code == 400
    assert "最上位フォルダではありません" in res.json()["detail"]


def test_git_pull_is_refused_for_folder_inside_unrelated_repo(admin, root, out_dir):
    # git 管理されていない親フォルダ（apis）が、上位の別のリポジトリ（parent）の中にある
    parent = init_repo(root / "repos" / "parent")
    apis = parent / "apis"
    apis.mkdir()
    set_workspace(admin, apis, out_dir)
    res = start_run(admin, git_pull=True)
    assert res.status_code == 400
    assert "最上位フォルダではありません" in res.json()["detail"]
    assert str(parent.resolve().name) in res.json()["detail"]


def test_git_pull_is_refused_without_upstream(admin, root, out_dir):
    local = init_repo(root / "repos" / "local-only")  # リモートなし
    set_workspace(admin, local, out_dir)
    res = start_run(admin, git_pull=True)
    assert res.status_code == 400
    assert "追跡ブランチ" in res.json()["detail"]


def test_schedule_with_git_pull_is_checked_when_saved(sched_client):
    # sched_client の対象フォルダ（repo_dir）は git リポジトリではない
    res = save_schedule(sched_client, git_pull=True)
    assert res.status_code == 400
    assert "git pull" in res.json()["detail"]
    # git pull をオフにすれば保存できる
    assert save_schedule(sched_client, git_pull=False).status_code == 200


def test_runner_is_not_started_when_git_pull_fails(admin, jobs, cloned_repo, tmp_path, out_dir):
    set_workspace(admin, cloned_repo, out_dir)
    # 確認は通るが、pull 先に接続できない（リモートの URL の先がない）
    git("remote", "set-url", "origin", str(tmp_path / "missing-origin"), cwd=cloned_repo)
    run_id = start_run(admin, git_pull=True).json()["run_id"]
    wait_finished(jobs)
    text = log_text(admin, run_id)
    assert "git pull に失敗" in text
    assert "args:" not in text
    assert admin.get("/api/status").json()["job"]["exit_code"] == 2


def test_pull_and_run_checks_repo_again_at_run_time(root, capsys):
    """画面での確認の後にフォルダの状態が変わっても、実行時に確かめて pull しない。"""
    parent = init_repo(root / "repos" / "parent")
    apis = parent / "apis"
    apis.mkdir()
    code = pull_and_run.main([str(apis), "--", sys.executable, "-c", "print('runner started')"])
    out = capsys.readouterr().out
    assert code == 2
    assert "最上位フォルダではありません" in out
    assert "runner started" not in out


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://oauth2:glpat-secret@gitlab.example.com/group/app.git", "https://***@gitlab.example.com/group/app.git"),
        ("https://user@gitlab.example.com/group/app.git", "https://***@gitlab.example.com/group/app.git"),
        ("git@gitlab.example.com:group/app.git", "git@gitlab.example.com:group/app.git"),
        ("https://gitlab.example.com/group/app.git", "https://gitlab.example.com/group/app.git"),
    ],
)
def test_credentials_in_remote_url_are_hidden_in_logs(url, expected):
    assert redact_url(url) == expected
