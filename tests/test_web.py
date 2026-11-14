import base64
import shutil
import sys
import tomllib
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from godoc_agent.runlock import run_lock
from godoc_agent.sessions import SessionBoard, sessions_path
from godoc_agent.skill_registry import COMMENT, DOC, read_registry
from godoc_agent.web.app import CSRF_HEADER, CSRF_VALUE, create_app
from godoc_agent.web.auth import LoginThrottle, hash_password, verify_password
from godoc_agent.web.jobs import JobManager
from godoc_agent.web.settings import WebSettings, parse_browse_roots
from godoc_agent.web.skills import SkillStore
from godoc_agent.web.targets_editor import to_toml

PASSWORD = "correct horse battery staple"
FAKE_RUNNER = Path(__file__).resolve().parent / "fake_runner.py"
EXAMPLE_TARGETS = Path(__file__).resolve().parent.parent / "targets.example.toml"
CSRF = {CSRF_HEADER: CSRF_VALUE}


@pytest.fixture(scope="module")
def password_hash() -> str:
    # テストを速くするため、計算量を下げたハッシュを使う
    return hash_password(PASSWORD, n=2**10)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """フォルダ選択の起点。対象・出力先はこの下に作る。"""
    path = tmp_path / "root"
    path.mkdir()
    return path


@pytest.fixture
def repo_dir(root: Path) -> Path:
    path = root / "repos" / "user_api"
    for rel in ("pkg/handler/user.go", "pkg/handler/user_test.go", "pkg/model/user.go"):
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text("package x\n", encoding="utf-8")
    return path


@pytest.fixture
def out_dir(root: Path) -> Path:
    return root / "out"


@pytest.fixture
def app_dir(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "app"
    path.mkdir()
    shutil.copy(EXAMPLE_TARGETS, path / "targets.example.toml")
    monkeypatch.delenv("TARGETS_CONFIG", raising=False)
    return path


@pytest.fixture
def skills(app_dir: Path, monkeypatch) -> SkillStore:
    # 組み込みのスキルは、テスト用のアプリのフォルダ（skills/design-doc-batch・skills/comment-batch）に置く
    monkeypatch.delenv("CLAUDE_SKILL_PATH", raising=False)
    monkeypatch.delenv("CLAUDE_COMMENT_SKILL_PATH", raising=False)
    store = SkillStore(app_dir)
    skill = skill_dir(store, DOC) / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# 設計書スキル\n", encoding="utf-8")
    return store


@pytest.fixture
def jobs(monkeypatch) -> JobManager:
    monkeypatch.setenv("FAKE_RUNNER_SLEEP", "0")
    monkeypatch.delenv("FAKE_RUNNER_EXIT", raising=False)
    manager = JobManager(runner_cmd=[sys.executable, str(FAKE_RUNNER)])
    yield manager
    for job in manager.running_jobs():
        job.proc.kill()
        job.proc.wait(timeout=10)


def skill_dir(store: SkillStore, name: str) -> Path:
    return store.get(name).skill_dir


def make_settings(password_hash: str, root: Path, state_dir: Path, **overrides) -> WebSettings:
    values = dict(
        admin_password_hash=password_hash, browse_roots=(root.resolve(),), state_dir=state_dir,
        host="127.0.0.1", port=4000, session_ttl_s=3600, cookie_secure=False, view_requires_login=False,
    )
    values.update(overrides)
    return WebSettings(**values)


@pytest.fixture
def make_client(password_hash, root, tmp_path, jobs, app_dir, skills):
    def make(throttle: LoginThrottle | None = None, clock=datetime.now, **overrides) -> TestClient:
        settings = make_settings(password_hash, root, tmp_path / "state", **overrides)
        return TestClient(create_app(settings, jobs, throttle, app_dir=app_dir, skills=skills, clock=clock))

    return make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


@pytest.fixture
def make_app(password_hash, root, tmp_path, jobs, app_dir, skills):
    """同じ Web 画面に、別々のブラウザ（Cookie が別）からつなぐためのアプリ。"""
    settings = make_settings(password_hash, root, tmp_path / "state")
    return create_app(settings, jobs, app_dir=app_dir, skills=skills)


def login(client: TestClient, password: str = PASSWORD):
    return client.post("/api/login", json={"password": password}, headers=CSRF)


def set_workspace(client: TestClient, repo: Path, out: Path):
    return client.put("/api/workspace", json={"repo": str(repo), "out": str(out)}, headers=CSRF)


@pytest.fixture
def admin(client, repo_dir, out_dir) -> TestClient:
    """ログイン済みで、対象・出力先フォルダを設定した状態。"""
    assert login(client).status_code == 200
    assert set_workspace(client, repo_dir, out_dir).status_code == 200
    return client


def start_run(client: TestClient, **body):
    return client.post("/api/runs", json={"tasks": ["doc"], **body}, headers=CSRF)


def wait_finished(jobs: JobManager) -> None:
    jobs.current.proc.wait(timeout=20)


def task_names(client: TestClient) -> list[str]:
    return [t["name"] for t in client.get("/api/config").json()["tasks"]]


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# --- ログイン ------------------------------------------------------------------------------


def test_password_hash_roundtrip(password_hash):
    assert verify_password(PASSWORD, password_hash)
    assert not verify_password("wrong", password_hash)
    assert not verify_password(PASSWORD, "scrypt:broken")


def test_server_refuses_to_start_with_malformed_password_hash(root, tmp_path):
    with pytest.raises(ValueError, match="WEB_ADMIN_PASSWORD_HASH"):
        create_app(make_settings("plain-text-password", root, tmp_path / "state"))


def test_login_and_logout(client):
    assert client.get("/api/me").json()["admin"] is False
    assert login(client, "wrong").status_code == 401
    assert login(client).status_code == 200
    assert client.get("/api/me").json()["admin"] is True
    assert client.post("/api/logout", headers=CSRF).status_code == 200
    assert client.get("/api/me").json()["admin"] is False


def test_login_is_locked_after_repeated_failures(make_client):
    client = make_client(LoginThrottle(max_failures=3))
    for _ in range(3):
        assert login(client, "wrong").status_code == 401
    # ロック中は正しいパスワードでも受け付けない
    assert login(client).status_code == 429


def test_state_changing_requests_require_csrf_header(client, repo_dir, out_dir):
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 403
    login(client)
    assert client.put("/api/workspace", json={"repo": str(repo_dir), "out": str(out_dir)}).status_code == 403
    assert client.post("/api/runs", json={}).status_code == 403


def test_view_can_require_login(make_client):
    client = make_client(view_requires_login=True)
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/outputs").status_code == 401
    login(client)
    assert client.get("/api/status").status_code == 200


# --- 管理者だけの操作 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path",
    [
        ("post", "/api/runs"), ("post", "/api/runs/stop"), ("put", "/api/workspace"), ("get", "/api/fs"),
        ("get", "/api/skills"), ("put", "/api/skills/doc"), ("delete", "/api/skills/comment"),
        ("put", "/api/targets"), ("post", "/api/targets/preview"),
    ],
)
def test_admin_only_endpoints_reject_viewers(client, method, path):
    res = getattr(client, method)(path, headers=CSRF) if method in ("get", "delete") else getattr(client, method)(path, json={}, headers=CSRF)
    assert res.status_code == 401


def test_viewer_can_see_status_config_and_targets(client, monkeypatch):
    # LLM_CONCURRENCY は廃止した（.env に残っていても使わない）。既定はサーバー全体の上限と同じ 10
    monkeypatch.setenv("LLM_CONCURRENCY", "3")
    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.json()["server"] == {"active_sessions": 0, "max_sessions": 10}
    assert client.get("/api/config").json() == {
        "tasks": [{"name": "doc", "label": "設計書", "type": "document"}],
        "workspace": {"repo": None, "out": None},
        "concurrency": {"default": 10, "max": 10},
    }
    assert client.get("/api/targets").status_code == 200


# --- フォルダ ------------------------------------------------------------------------------


def test_browse_lists_folders_only_within_roots(client, root, repo_dir, tmp_path):
    login(client)
    top = client.get("/api/fs").json()
    assert [d["path"] for d in top["dirs"]] == [str(root.resolve())]
    res = client.get("/api/fs", params={"path": str(repo_dir)}).json()
    assert [d["name"] for d in res["dirs"]] == ["pkg"]
    assert res["parent"] == str(repo_dir.parent.resolve())
    # 起点で「上へ」を押すと起点の一覧に戻る
    assert client.get("/api/fs", params={"path": str(root)}).json()["parent"] is None
    assert client.get("/api/fs", params={"path": str(tmp_path)}).status_code == 403
    assert client.get("/api/fs", params={"path": str(root / ".." / "app")}).status_code == 403


def test_workspace_is_saved_and_validated(client, root, repo_dir, out_dir, tmp_path):
    login(client)
    res = set_workspace(client, repo_dir, out_dir)
    assert res.status_code == 200
    assert client.get("/api/config").json()["workspace"] == {"repo": str(repo_dir.resolve()), "out": str(out_dir.resolve())}

    assert set_workspace(client, tmp_path, out_dir).status_code == 400            # 起点の外
    assert set_workspace(client, root / "missing", out_dir).status_code == 400    # 対象がない
    assert set_workspace(client, repo_dir, repo_dir / "out").status_code == 400   # 出力先が対象の中
    assert set_workspace(client, repo_dir, root / "a" / "b").status_code == 400   # 出力先の親がない
    # 失敗しても保存済みの値は変わらない
    assert client.get("/api/config").json()["workspace"]["repo"] == str(repo_dir.resolve())


# --- 実行 --------------------------------------------------------------------------------


def test_admin_runs_runner_on_selected_folders(admin, jobs, out_dir, repo_dir):
    res = start_run(admin, limit=3, force=True)
    assert res.status_code == 202
    run_id = res.json()["run_id"]
    assert run_id.startswith("web-")
    wait_finished(jobs)

    status = admin.get("/api/status").json()
    assert status["job"]["running"] is False
    assert status["job"]["exit_code"] == 0

    log = admin.get(f"/api/runs/{run_id}/log").json()
    assert log["eof"] is True
    assert "処理中: 日本語の出力" in log["text"]
    # 画面で選んだフォルダと指定が runner の引数として渡る
    args_line = next(line for line in log["text"].splitlines() if line.startswith("args:"))
    assert f"--repo {repo_dir.resolve()}" in args_line
    assert f"--out {out_dir.resolve()}" in args_line
    assert f"--run-id {run_id}" in args_line
    assert "--tasks doc --limit 3 --force" in args_line

    # 続きから読むと、新しい内容はない
    assert admin.get(f"/api/runs/{run_id}/log", params={"offset": log["offset"]}).json()["text"] == ""
    assert [r["run_id"] for r in admin.get("/api/runs").json()] == [run_id]


def test_run_requires_folders_and_design_skill(client, skills, repo_dir, out_dir):
    login(client)
    res = start_run(client)
    assert res.status_code == 400
    assert "フォルダ" in res.json()["detail"]

    set_workspace(client, repo_dir, out_dir)
    (skill_dir(skills, DOC) / "SKILL.md").unlink()
    res = start_run(client)
    assert res.status_code == 400
    assert "登録されていないスキル" in res.json()["detail"]


def test_comment_task_needs_registered_skill(admin):
    res = start_run(admin, tasks=["comment"])
    assert res.status_code == 400
    assert "登録されていないスキル" in res.json()["detail"]


def test_second_run_is_rejected_while_running(admin, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_SLEEP", "30")
    assert start_run(admin).status_code == 202
    res = start_run(admin)
    assert res.status_code == 409
    assert "実行中" in res.json()["detail"]


def test_shared_settings_cannot_change_while_running(admin, jobs, repo_dir, root, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_SLEEP", "30")
    run_id = start_run(admin).json()["run_id"]
    # フォルダはブラウザごとなので選び直せる。実行は起動したときの出力先のまま続く
    assert set_workspace(admin, repo_dir, root / "other_out").status_code == 200
    assert jobs.get(run_id).out_dir == (root / "out").resolve()
    assert admin.put("/api/skills/comment", json={"files": [{"path": "SKILL.md", "content_b64": b64("x")}]}, headers=CSRF).status_code == 409
    assert admin.put("/api/targets", json={"config": {}}, headers=CSRF).status_code == 409


def test_stop_terminates_running_job(admin, jobs, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_SLEEP", "30")
    start_run(admin)
    res = admin.post("/api/runs/stop", headers=CSRF)
    assert res.status_code == 200
    assert res.json()["stop_requested"] is True
    wait_finished(jobs)
    job = admin.get("/api/status").json()["job"]
    assert job["running"] is False
    assert job["exit_code"] != 0
    # 止めた後は、すぐに次の実行を受け付ける
    monkeypatch.setenv("FAKE_RUNNER_SLEEP", "0")
    assert start_run(admin).status_code == 202


def test_run_is_rejected_while_nightly_or_cli_run_holds_the_lock(admin, out_dir):
    with run_lock(out_dir.resolve()):
        assert admin.get("/api/status").json()["busy_elsewhere"] is True
        res = start_run(admin)
        assert res.status_code == 409
        assert "夜間バッチ" in res.json()["detail"]
    assert admin.get("/api/status").json()["busy_elsewhere"] is False


def test_failed_run_reports_exit_code(admin, jobs, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_EXIT", "1")
    start_run(admin)
    wait_finished(jobs)
    assert admin.get("/api/status").json()["job"]["exit_code"] == 1


@pytest.mark.parametrize("body", [{"tasks": ["unknown"]}, {"limit": -1}, {"concurrency": 0}, {"concurrency": 11}])
def test_invalid_run_requests_are_rejected(admin, body):
    assert admin.post("/api/runs", json=body, headers=CSRF).status_code in (400, 422)


# --- スキル -------------------------------------------------------------------------------


def test_register_skill_folder_and_enable_comment_task(admin, skills):
    files = [
        {"path": "SKILL.md", "content_b64": b64("# コメント追記\n")},
        {"path": "references/rules.md", "content_b64": b64("ルール\n")},
    ]
    res = admin.put("/api/skills/comment", json={"files": files}, headers=CSRF)
    assert res.status_code == 200
    assert [f["path"] for f in res.json()["files"]] == ["SKILL.md", "references/rules.md"]
    assert (skill_dir(skills, COMMENT) / "references" / "rules.md").read_text(encoding="utf-8") == "ルール\n"
    assert admin.get("/api/skills/comment/content").text == "# コメント追記\n"
    # 登録するとコメント追記を選べるようになる
    assert task_names(admin) == ["doc", "comment"]

    # 登録を外すと選べなくなり、直前に戻すと元に戻る
    assert admin.delete("/api/skills/comment", headers=CSRF).status_code == 200
    assert task_names(admin) == ["doc"]
    assert admin.post("/api/skills/comment/restore", headers=CSRF).json()["registered"] is True


def test_replacing_skill_keeps_previous_one_for_restore(admin, skills):
    res = admin.put("/api/skills/doc", json={"files": [{"path": "SKILL.md", "content_b64": b64("# 新しいスキル\n")}]}, headers=CSRF)
    assert res.json()["has_backup"] is True
    assert admin.get("/api/skills/doc/content").text == "# 新しいスキル\n"
    admin.post("/api/skills/doc/restore", headers=CSRF)
    assert admin.get("/api/skills/doc/content").text == "# 設計書スキル\n"


def upload_custom(client: TestClient, name: str, type_: str | None, text: str = "# 追加スキル\n"):
    body = {"files": [{"path": "SKILL.md", "content_b64": b64(text)}], "type": type_}
    return client.put(f"/api/skills/{name}", json=body, headers=CSRF)


def test_custom_skill_can_be_added_run_and_removed(admin, jobs, app_dir):
    res = upload_custom(admin, "test-spec", "document")
    assert res.status_code == 200
    assert res.json()["type"] == "document"
    assert res.json()["out_subdir"] == "_test-spec"
    assert read_registry(app_dir) == {"test-spec": "document"}
    assert (app_dir / "skills" / "test-spec" / "SKILL.md").is_file()

    # 実行・定期実行で選べるようになり、runner には --tasks で名前が渡る
    assert task_names(admin) == ["doc", "test-spec"]
    run_id = start_run(admin, tasks=["doc", "test-spec"]).json()["run_id"]
    wait_finished(jobs)
    assert "--tasks doc,test-spec" in admin.get(f"/api/runs/{run_id}/log").json()["text"]

    listed = admin.get("/api/skills").json()
    assert [s["name"] for s in listed["skills"]] == ["doc", "comment", "test-spec"]
    assert {t["type"] for t in listed["types"]} == {"document", "comment", "rewrite"}

    # 登録を外すと一覧からも消える
    assert admin.delete("/api/skills/test-spec", headers=CSRF).status_code == 200
    assert read_registry(app_dir) == {}
    assert task_names(admin) == ["doc"]


def test_custom_skill_keeps_type_when_replaced(admin, app_dir):
    upload_custom(admin, "fmt", "rewrite")
    # 入れ替えでは種類を省略でき、前の種類のまま
    res = upload_custom(admin, "fmt", None, "# 新しい版\n")
    assert res.status_code == 200
    assert res.json()["type"] == "rewrite"
    assert res.json()["has_backup"] is True


@pytest.mark.parametrize(
    "name, type_, error",
    [
        ("new-skill", None, "出力の種類"),
        ("new-skill", "unknown", "出力の種類"),
        ("Bad_Name", "document", "スキル名"),
        ("design-doc-batch", "document", "予約"),
        ("runs", "document", "予約"),
    ],
)
def test_invalid_custom_skill_is_rejected(admin, app_dir, name, type_, error):
    res = upload_custom(admin, name, type_)
    assert res.status_code == 400
    assert error in res.json()["detail"]
    assert read_registry(app_dir) == {}


def test_builtin_skill_type_cannot_be_changed(admin):
    res = upload_custom(admin, "doc", "rewrite")
    assert res.status_code == 400


def test_outputs_of_custom_skill_are_labeled_with_its_name(outputs, out_dir):
    (out_dir / "_test-spec" / "user_api").mkdir(parents=True)
    (out_dir / "_test-spec" / "user_api" / "user.go.md").write_text("# 仕様\n", encoding="utf-8")
    kinds = {i["path"]: i["kind"] for i in outputs.get("/api/outputs").json()}
    assert kinds["_test-spec/user_api/user.go.md"] == "test-spec"


def test_design_skill_cannot_be_removed(admin):
    assert admin.delete("/api/skills/doc", headers=CSRF).status_code == 400


@pytest.mark.parametrize(
    "files, error",
    [
        ([{"path": "README.md", "content_b64": b64("x")}], "SKILL.md"),
        ([{"path": "SKILL.md", "content_b64": b64("  \n")}], "空"),
        ([{"path": "SKILL.md", "content_b64": b64("x")}, {"path": "../evil.md", "content_b64": b64("x")}], "パス"),
        ([{"path": "SKILL.md", "content_b64": b64("x")}, {"path": "/etc/x", "content_b64": b64("x")}], "パス"),
        ([{"path": "SKILL.md", "content_b64": "not base64!"}], "正しく送られて"),
    ],
)
def test_invalid_skill_uploads_are_rejected_and_keep_current_skill(admin, skills, files, error):
    res = admin.put("/api/skills/doc", json={"files": files}, headers=CSRF)
    assert res.status_code == 400
    assert error in res.json()["detail"]
    assert (skill_dir(skills, DOC) / "SKILL.md").read_text(encoding="utf-8") == "# 設計書スキル\n"
    assert not (skill_dir(skills, DOC).parent / "evil.md").exists()


# --- 対象の設定（targets.toml） ------------------------------------------------------------


def handler_only_config() -> dict:
    return {
        "exclude": ["**/vendor/**"],
        "languages": [
            {"name": "go", "enabled": True, "extensions": [".go"], "include": ["**/pkg/handler/**"], "exclude": ["**/*_test.go"]},
            {"name": "csharp", "enabled": False, "extensions": [], "include": [], "exclude": []},
        ],
    }


def test_targets_show_example_with_all_languages(client):
    res = client.get("/api/targets").json()
    assert res["is_example"] is True
    assert res["path"].endswith("targets.example.toml")
    assert res["write_path"].endswith("targets.toml")
    assert [lang["name"] for lang in res["config"]["languages"]] == ["go", "csharp", "typescript"]


def test_targets_preview_lists_matching_files(admin):
    res = admin.post("/api/targets/preview", json={"config": handler_only_config()}, headers=CSRF)
    assert res.status_code == 200
    assert res.json()["files"] == [{"path": "pkg/handler/user.go", "language": "go"}]


def test_targets_are_saved_to_targets_toml_used_by_runner(admin, app_dir):
    res = admin.put("/api/targets", json={"config": handler_only_config()}, headers=CSRF)
    assert res.status_code == 200
    assert res.json()["is_example"] is False
    saved = tomllib.loads((app_dir / "targets.toml").read_text(encoding="utf-8"))
    assert saved["languages"][0]["include"] == ["**/pkg/handler/**"]
    assert admin.get("/api/targets").json()["path"] == str(app_dir / "targets.toml")


@pytest.mark.parametrize(
    "change, error",
    [
        (lambda c: c["languages"][0].update(include=[]), "include"),
        (lambda c: c["languages"][0].update(extensions=["go"]), "拡張子"),
        (lambda c: c["languages"][0].update(include=["/abs/**"]), "相対パス"),
        (lambda c: c["languages"][0].update(name="java"), "name"),
        (lambda c: c["languages"][0].update(enabled=False), "有効"),
        (lambda c: c["languages"][0].update(unknown=1), "不明な項目"),
    ],
)
def test_invalid_targets_are_not_saved(admin, app_dir, change, error):
    config = handler_only_config()
    change(config)
    res = admin.put("/api/targets", json={"config": config}, headers=CSRF)
    assert res.status_code == 400
    assert error in res.json()["detail"]
    assert not (app_dir / "targets.toml").exists()


def test_to_toml_escapes_strings():
    config = handler_only_config()
    config["exclude"] = ['**/a"b/**', "**/日本語/**"]
    assert tomllib.loads(to_toml(config))["exclude"] == ['**/a"b/**', "**/日本語/**"]


# --- 生成物の閲覧 ---------------------------------------------------------------------------


@pytest.fixture
def outputs(admin, out_dir: Path) -> TestClient:
    files = {
        "user_api/pkg/handler/詳細設計書_user.md": "# 設計書\n<script>alert(1)</script>\n",
        "_commented/user_api/pkg/handler/user.go": "// コメント\npackage handler\n",
        ".manifest.json": "{}",
        "_runs/r1.jsonl": '{"status": "done"}\n{"status": "failed_validation"}\n',
        "_runs/r1.log": "log\n",
    }
    for rel, body in files.items():
        (out_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (out_dir / rel).write_text(body, encoding="utf-8")
    return admin


def test_lists_outputs_without_logs_or_internal_files(outputs):
    items = outputs.get("/api/outputs").json()
    assert {(i["path"], i["kind"]) for i in items} == {
        ("user_api/pkg/handler/詳細設計書_user.md", "doc"),
        ("_commented/user_api/pkg/handler/user.go", "comment"),
    }
    assert outputs.get("/api/runs").json()[0]["counts"] == {"done": 1, "failed_validation": 1}


def test_output_file_is_served_as_plain_text(outputs):
    res = outputs.get("/api/outputs/file", params={"path": "user_api/pkg/handler/詳細設計書_user.md"})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert "<script>" in res.text
    download = outputs.get("/api/outputs/file", params={"path": "user_api/pkg/handler/詳細設計書_user.md", "download": True})
    assert "attachment" in download.headers["content-disposition"]


@pytest.mark.parametrize("path", ["../repos/user_api/pkg/handler/user.go", "_runs/r1.log", ".manifest.json", ""])
def test_output_file_outside_outputs_is_forbidden(outputs, path):
    assert outputs.get("/api/outputs/file", params={"path": path}).status_code == 403


def test_invalid_run_id_is_rejected(admin):
    assert admin.get("/api/runs/..%2Fsecret/log").status_code in (400, 404)
    assert admin.get("/api/runs/nope/log").status_code == 404


# --- 設定 ---------------------------------------------------------------------------------


def test_parse_browse_roots(tmp_path):
    assert parse_browse_roots(f"{tmp_path}, {tmp_path}") == (tmp_path.resolve(),)
    with pytest.raises(ValueError, match="絶対パス"):
        parse_browse_roots("relative/path")
    with pytest.raises(ValueError, match="ありません"):
        parse_browse_roots(str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="1つ以上"):
        parse_browse_roots(" , ")


def test_targets_config_path_not_created_yet_shows_example(client, app_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("TARGETS_CONFIG", str(tmp_path / "conf" / "targets.toml"))
    res = client.get("/api/targets")
    assert res.status_code == 200
    assert res.json()["is_example"] is True
    assert res.json()["write_path"] == str(tmp_path / "conf" / "targets.toml")


# --- セッション（同時に動く Claude Code）-------------------------------------------------------

def test_run_passes_session_count_to_runner(admin, jobs):
    res = start_run(admin, concurrency=10)
    assert res.status_code == 202
    assert res.json()["concurrency"] == 10
    wait_finished(jobs)
    log = admin.get(f"/api/runs/{res.json()['run_id']}/log").json()["text"]
    assert "--concurrency 10" in log


def test_run_without_session_count_uses_runner_default(admin, jobs):
    res = start_run(admin)
    wait_finished(jobs)
    assert res.json()["concurrency"] is None
    assert "--concurrency" not in admin.get(f"/api/runs/{res.json()['run_id']}/log").json()["text"]


# --- 複数人での利用 -------------------------------------------------------------------------

def browser(app, repo: Path | None = None, out: Path | None = None) -> TestClient:
    """別のブラウザ（Cookie が別）として、ログインしてフォルダを選ぶ。"""
    client = TestClient(app)
    assert login(client).status_code == 200
    if repo and out:
        assert set_workspace(client, repo, out).status_code == 200
    return client


def test_folders_are_remembered_per_browser(make_app, repo_dir, root):
    alice = browser(make_app, repo_dir, root / "out_a")
    # まだ選んでいないブラウザには、最後に選ばれたフォルダが初期値として出る
    bob = browser(make_app)
    assert bob.get("/api/config").json()["workspace"]["out"] == str((root / "out_a").resolve())

    assert set_workspace(bob, repo_dir, root / "out_b").status_code == 200
    assert bob.get("/api/config").json()["workspace"]["out"] == str((root / "out_b").resolve())
    assert alice.get("/api/config").json()["workspace"]["out"] == str((root / "out_a").resolve())


def test_people_can_run_on_different_outputs_at_the_same_time(make_app, jobs, repo_dir, root, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_SLEEP", "30")
    alice = browser(make_app, repo_dir, root / "out_a")
    bob = browser(make_app, repo_dir, root / "out_b")

    a = start_run(alice)
    b = start_run(bob)
    assert (a.status_code, b.status_code) == (202, 202)
    assert len(jobs.running_jobs()) == 2

    # それぞれの画面には自分の出力先の実行が出て、他の人の実行は一覧で分かる
    status = alice.get("/api/status").json()
    assert status["job"]["run_id"] == a.json()["run_id"]
    assert [o["run_id"] for o in status["others"]] == [b.json()["run_id"]]
    # 実行中の他の人の出力先のログも読める
    assert alice.get(f"/api/runs/{b.json()['run_id']}/log").status_code == 200

    # 同じ出力先では同時に実行できない
    carol = browser(make_app, repo_dir, root / "out_a")
    res = start_run(carol)
    assert res.status_code == 409
    assert "この出力先では実行中" in res.json()["detail"]

    # 停止は自分の実行だけに効く
    assert bob.post("/api/runs/stop", headers=CSRF).json()["run_id"] == b.json()["run_id"]
    jobs.get(b.json()["run_id"]).proc.wait(timeout=20)
    assert jobs.get(a.json()["run_id"]).running


def test_stop_by_run_id(make_app, jobs, repo_dir, root, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_SLEEP", "30")
    alice = browser(make_app, repo_dir, root / "out_a")
    run_id = start_run(alice).json()["run_id"]
    other = browser(make_app, repo_dir, root / "out_b")

    res = other.post("/api/runs/stop", json={"run_id": run_id}, headers=CSRF)
    assert res.status_code == 200
    assert res.json()["stop_requested"] is True
    assert other.post("/api/runs/stop", json={"run_id": "web-unknown"}, headers=CSRF).status_code == 409


def test_shared_settings_are_locked_while_anyone_runs(make_app, repo_dir, root, monkeypatch):
    # スキル・対象の設定は全員で共有するため、誰かが実行中なら変えられない。フォルダは自分の出力先が空いていれば変えられる
    monkeypatch.setenv("FAKE_RUNNER_SLEEP", "30")
    alice = browser(make_app, repo_dir, root / "out_a")
    start_run(alice)
    bob = browser(make_app, repo_dir, root / "out_b")
    assert bob.put("/api/targets", json={"config": {}}, headers=CSRF).status_code == 409
    assert set_workspace(bob, repo_dir, root / "out_c").status_code == 200


def test_number_of_simultaneous_runs_is_limited(make_app, jobs, repo_dir, root, monkeypatch):
    from godoc_agent.web import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "MAX_RUNNING_JOBS", 1)
    monkeypatch.setenv("FAKE_RUNNER_SLEEP", "30")
    assert start_run(browser(make_app, repo_dir, root / "out_a")).status_code == 202
    res = start_run(browser(make_app, repo_dir, root / "out_b"))
    assert res.status_code == 409
    assert "1 件まで" in res.json()["detail"]


async def write_board(out_dir: Path, run_id: str, concurrency: int, busy: list[str]) -> tuple[SessionBoard, AsyncExitStack]:
    """runner の途中の状態を作る（busy のファイルを処理中にする）。枠は返した stack を閉じるまで占有したまま。"""
    board = SessionBoard(sessions_path(out_dir.resolve(), run_id), run_id, concurrency, total=len(busy) + 1, echo=lambda _: None)
    stack = AsyncExitStack()
    for rel_path in busy:
        session = await stack.enter_async_context(board.session("doc", rel_path))
        session.log("生成 1/3 回目を開始", attempt=1)
    return board, stack


async def test_sessions_show_occupied_slots_and_per_session_log_while_running(admin, out_dir):
    _, stack = await write_board(out_dir, "r1", 10, ["a.go", "b.go", "c.go"])
    async with stack:
        with run_lock(out_dir.resolve()):  # 夜間バッチ・コマンドからの実行が動いている
            data = admin.get("/api/runs/r1/sessions").json()
    assert data["running"] is True and data["interrupted"] is False
    assert data["concurrency"] == 10
    assert data["active"] == 3
    busy = [s for s in data["sessions"] if s["busy"]]
    assert [s["rel_path"] for s in busy] == ["a.go", "b.go", "c.go"]
    assert busy[0]["attempt"] == 1
    assert busy[0]["elapsed_s"] is not None
    assert [e["text"] for e in busy[0]["events"]] == ["開始 doc a.go", "生成 1/3 回目を開始"]


async def test_sessions_of_killed_runner_are_reported_as_interrupted(admin, out_dir):
    _, stack = await write_board(out_dir, "r1", 2, ["a.go"])  # 終了を書き出さずに止まった
    async with stack:
        data = admin.get("/api/runs/r1/sessions").json()
    assert data["running"] is False
    assert data["interrupted"] is True
    assert all(s["elapsed_s"] is None for s in data["sessions"])


async def test_sessions_of_finished_run(admin, out_dir):
    board, stack = await write_board(out_dir, "r1", 2, ["a.go"])
    await stack.aclose()
    board.close()
    data = admin.get("/api/runs/r1/sessions").json()
    assert data["running"] is False and data["interrupted"] is False
    assert data["active"] == 0 and data["finished"] == 1


def test_sessions_missing_invalid_or_broken(admin, out_dir):
    assert admin.get("/api/runs/nope/sessions").status_code == 404
    assert admin.get("/api/runs/..%2Fsecret/sessions").status_code in (400, 404)
    (out_dir / "_runs").mkdir(parents=True, exist_ok=True)
    (out_dir / "_runs" / "r1.sessions.json").write_text("{broken", encoding="utf-8")
    assert admin.get("/api/runs/r1/sessions").status_code == 500
