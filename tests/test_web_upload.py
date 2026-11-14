"""クライアント PC からのアップロードと、生成物の ZIP ダウンロードのテスト。フィクスチャは test_web.py のものを使う。"""

import base64
import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from godoc_agent.runlock import run_lock
from godoc_agent.web import uploads as uploads_module
from test_web import (  # noqa: F401  フィクスチャを読み込む
    CSRF, admin, app_dir, client, jobs, login, make_app, make_client, out_dir, password_hash, repo_dir, root,
    set_workspace, skills, start_run, wait_finished,
)
from test_web import browser


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def upload(client: TestClient, name: str, files: dict[str, bytes], *, batch: int = 2) -> dict:
    """ブラウザと同じく、受付 → 数件ずつ送る → 完了 の順に送る。"""
    res = client.post("/api/uploads", json={"name": name}, headers=CSRF)
    assert res.status_code == 200, res.text
    upload_id = res.json()["upload_id"]
    items = [{"path": p, "content_b64": b64(c)} for p, c in files.items()]
    for i in range(0, len(items), batch):
        res = client.post(f"/api/uploads/{upload_id}/files", json={"files": items[i:i + batch]}, headers=CSRF)
        assert res.status_code == 200, res.text
    return client.post(f"/api/uploads/{upload_id}/complete", headers=CSRF)


SOURCES = {
    "pkg/handler/user.go": b"package handler\n",
    "pkg/handler/health.go": b"package handler\n",
    "README.md": "日本語".encode("utf-8"),
}


def test_uploaded_folder_becomes_target_and_can_be_run(make_app, jobs):
    alice = browser(make_app)
    res = upload(alice, "my_app", SOURCES)
    assert res.status_code == 200
    repo, out = Path(res.json()["repo"]), Path(res.json()["out"])
    assert (repo.name, out.name, repo.parent.name) == ("src", "out", "my_app")
    assert (repo / "pkg/handler/user.go").read_bytes() == b"package handler\n"
    assert (repo / "README.md").read_text(encoding="utf-8") == "日本語"
    assert alice.get("/api/config").json()["workspace"] == {"repo": str(repo), "out": str(out)}

    run = start_run(alice)
    assert run.status_code == 202
    wait_finished(jobs)
    log = alice.get(f"/api/runs/{run.json()['run_id']}/log").json()["text"]
    assert f"--repo {repo}" in log and f"--out {out}" in log


def test_reupload_replaces_sources_but_keeps_outputs(make_app):
    alice = browser(make_app)
    first = upload(alice, "my_app", SOURCES).json()
    (Path(first["out"]) / "詳細設計書_user.md").write_text("# 設計書", encoding="utf-8")

    second = upload(alice, "my_app", {"pkg/handler/user.go": b"package handler // v2\n"}).json()
    assert second == first
    repo = Path(second["repo"])
    assert (repo / "pkg/handler/user.go").read_bytes() == b"package handler // v2\n"
    assert not (repo / "pkg/handler/health.go").exists()  # 前回のファイルは残らない
    assert (Path(second["out"]) / "詳細設計書_user.md").is_file()  # 生成物は残す（差分スキップに使う）
    # 途中のファイルは片付いている
    assert sorted(p.name for p in repo.parent.iterdir()) == ["out", "src"]


def test_uploads_are_separated_per_browser_and_not_shared_as_default(make_app):
    alice = browser(make_app)
    bob = browser(make_app)
    a = upload(alice, "my_app", SOURCES).json()
    b = upload(bob, "my_app", SOURCES).json()
    assert a["repo"] != b["repo"]
    # アップロードしたフォルダは、まだ選んでいない他のブラウザの初期値にしない
    assert browser(make_app).get("/api/config").json()["workspace"]["repo"] is None
    # 他のブラウザのアップロードには送れない
    upload_id = alice.post("/api/uploads", json={"name": "x"}, headers=CSRF).json()["upload_id"]
    res = bob.post(f"/api/uploads/{upload_id}/files", json={"files": [{"path": "a.go", "content_b64": b64(b"x")}]}, headers=CSRF)
    assert res.status_code == 404


@pytest.mark.parametrize("path", ["../evil.go", "/abs.go", "a/../../evil.go", "C:/evil.go", "a\\b.go"])
def test_paths_outside_upload_are_rejected(make_app, path):
    alice = browser(make_app)
    upload_id = alice.post("/api/uploads", json={"name": "my_app"}, headers=CSRF).json()["upload_id"]
    res = alice.post(f"/api/uploads/{upload_id}/files", json={"files": [{"path": path, "content_b64": b64(b"x")}]}, headers=CSRF)
    assert res.status_code == 400


@pytest.mark.parametrize("name", ["..", ".hidden", "a/b", "a\\b", "", "x" * 101])
def test_invalid_folder_names_are_rejected(make_app, name):
    res = browser(make_app).post("/api/uploads", json={"name": name}, headers=CSRF)
    assert res.status_code in (400, 422)


def test_upload_limits(make_app, monkeypatch):
    monkeypatch.setattr(uploads_module, "MAX_UPLOAD_FILE_BYTES", 10)
    monkeypatch.setattr(uploads_module, "MAX_UPLOAD_TOTAL_BYTES", 15)
    monkeypatch.setattr(uploads_module, "MAX_UPLOAD_FILES", 3)
    alice = browser(make_app)
    upload_id = alice.post("/api/uploads", json={"name": "my_app"}, headers=CSRF).json()["upload_id"]
    url = f"/api/uploads/{upload_id}/files"

    res = alice.post(url, json={"files": [{"path": "big.go", "content_b64": b64(b"x" * 11)}]}, headers=CSRF)
    assert res.status_code == 400 and "1ファイルの上限" in res.json()["detail"]
    assert alice.post(url, json={"files": [{"path": "a.go", "content_b64": b64(b"x" * 10)}]}, headers=CSRF).status_code == 200
    res = alice.post(url, json={"files": [{"path": "b.go", "content_b64": b64(b"x" * 6)}]}, headers=CSRF)
    assert res.status_code == 400 and "合計サイズ" in res.json()["detail"]
    files = [{"path": f"{i}.go", "content_b64": b64(b"x")} for i in range(3)]
    res = alice.post(url, json={"files": files}, headers=CSRF)
    assert res.status_code == 400 and "ファイル数" in res.json()["detail"]


def test_empty_upload_cannot_complete_and_abort_cleans_up(make_app):
    alice = browser(make_app)
    upload_id = alice.post("/api/uploads", json={"name": "my_app"}, headers=CSRF).json()["upload_id"]
    assert alice.post(f"/api/uploads/{upload_id}/complete", headers=CSRF).status_code == 400
    assert alice.delete(f"/api/uploads/{upload_id}", headers=CSRF).status_code == 200
    assert alice.post(f"/api/uploads/{upload_id}/complete", headers=CSRF).status_code == 404


def test_reupload_is_refused_while_its_output_is_running(make_app, jobs, monkeypatch):
    monkeypatch.setenv("FAKE_RUNNER_SLEEP", "30")
    alice = browser(make_app)
    out = Path(upload(alice, "my_app", SOURCES).json()["out"])
    assert start_run(alice).status_code == 202
    res = upload(alice, "my_app", SOURCES)
    assert res.status_code == 409
    # 夜間バッチ・コマンドが同じ出力先で動いているときも同じ
    jobs.stop(jobs.job_for(out).run_id)
    jobs.job_for(out).proc.wait(timeout=20)
    with run_lock(out):
        assert upload(alice, "my_app", SOURCES).status_code == 409


def test_upload_requires_admin(client):
    assert client.post("/api/uploads", json={"name": "my_app"}, headers=CSRF).status_code == 401


def test_outputs_can_be_downloaded_as_zip(admin, out_dir):
    out_dir.mkdir(exist_ok=True)
    (out_dir / "pkg").mkdir()
    (out_dir / "pkg" / "詳細設計書_user.md").write_text("# 設計書", encoding="utf-8")
    (out_dir / "_commented").mkdir()
    (out_dir / "_commented" / "user.go").write_text("// c", encoding="utf-8")
    (out_dir / "_runs").mkdir()
    (out_dir / "_runs" / "r1.log").write_text("log", encoding="utf-8")
    (out_dir / ".manifest.json").write_text("{}", encoding="utf-8")

    res = admin.get("/api/outputs/archive")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    assert "attachment" in res.headers["content-disposition"]
    names = sorted(zipfile.ZipFile(io.BytesIO(res.content)).namelist())
    # ログ・管理用のファイルは含めない
    assert names == ["_commented/user.go", "pkg/詳細設計書_user.md"]


def test_zip_download_without_outputs(admin, out_dir):
    out_dir.mkdir(exist_ok=True)
    assert admin.get("/api/outputs/archive").status_code == 404
