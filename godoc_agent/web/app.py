"""Web 画面（FastAPI）。

- 閲覧（状態・ログ・履歴・生成物）は誰でもできる（WEB_VIEW_REQUIRES_LOGIN=true なら管理者だけ）
- 実行・停止と、設定の変更（フォルダ・スキル・対象）は管理者だけ。設定の変更は実行中は受け付けない
"""

import base64
import binascii
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..tasks import COMMENT, DOC
from .auth import LoginThrottle, SessionStore, validate_password_hash, verify_password
from .jobs import JobBusy, JobManager
from .settings import APP_DIR, WebSettings
from .skills import SkillFile, SkillStore, skill_dirs
from .targets_editor import TargetsEditor
from .workspace import WorkspaceStore

COOKIE_NAME = "godoc_session"
# 状態を変える操作に必須のヘッダー。別サイトのフォームからは付けられないため、CSRF 対策になる
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "godoc-web"
STATIC_DIR = Path(__file__).resolve().parent / "static"


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class RunRequest(BaseModel):
    tasks: list[str] = Field(default_factory=list, max_length=2)
    limit: int = Field(default=0, ge=0, le=100_000)
    force: bool = False


class WorkspaceRequest(BaseModel):
    repo: str = Field(min_length=1, max_length=4096)
    out: str = Field(min_length=1, max_length=4096)


class UploadedFile(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    content_b64: str


class SkillUpload(BaseModel):
    files: list[UploadedFile] = Field(min_length=1)


class TargetsRequest(BaseModel):
    config: dict


def create_app(
    settings: WebSettings,
    jobs: JobManager | None = None,
    throttle: LoginThrottle | None = None,
    app_dir: Path = APP_DIR,
    skills: SkillStore | None = None,
) -> FastAPI:
    validate_password_hash(settings.admin_password_hash)
    jobs = jobs or JobManager()
    sessions = SessionStore(settings.session_ttl_s)
    throttle = throttle or LoginThrottle()
    workspace = WorkspaceStore(settings.state_dir, settings.browse_roots)
    skills = skills or SkillStore(skill_dirs(app_dir))
    targets = TargetsEditor(app_dir)

    app = FastAPI(title="設計書生成", docs_url=None, redoc_url=None, openapi_url=None)

    def available_tasks() -> list[str]:
        return [DOC] + ([COMMENT] if skills.is_registered(COMMENT) else [])

    def is_admin(request: Request) -> bool:
        return sessions.is_valid(request.cookies.get(COOKIE_NAME))

    def require_csrf_header(request: Request) -> None:
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise HTTPException(403, "不正なリクエストです")

    def require_admin(request: Request) -> None:
        if request.method not in ("GET", "HEAD"):
            require_csrf_header(request)
        if not is_admin(request):
            raise HTTPException(401, "管理者としてログインしてください")

    def require_viewer(request: Request) -> None:
        if settings.view_requires_login and not is_admin(request):
            raise HTTPException(401, "ログインしてください")

    def require_idle() -> None:
        # 実行中に設定を変えると、どの設定で動いた結果か分からなくなるため
        if jobs.running:
            raise HTTPException(409, "実行中は設定を変更できません。終了または停止してから変更してください")

    def current_out() -> Path | None:
        out = workspace.get().out
        return Path(out) if out else None

    def out_for_view() -> Path:
        out = current_out()
        if out is None:
            raise HTTPException(409, "出力先フォルダが未設定です")
        return out

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'",
        )
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/app.js", include_in_schema=False)
    def script() -> FileResponse:
        return FileResponse(STATIC_DIR / "app.js", media_type="text/javascript")

    # --- ログイン ------------------------------------------------------------------------

    @app.get("/api/me")
    def me(request: Request) -> dict:
        return {"admin": is_admin(request), "view_requires_login": settings.view_requires_login}

    @app.post("/api/login", dependencies=[Depends(require_csrf_header)])
    def login(body: LoginRequest, request: Request, response: Response) -> dict:
        client = request.client.host if request.client else "unknown"
        if (wait := throttle.retry_after(client)) > 0:
            raise HTTPException(429, f"ログインの失敗が続いたため、{int(wait // 60) + 1} 分ほど待ってから再試行してください")
        if not verify_password(body.password, settings.admin_password_hash):
            throttle.record_failure(client)
            raise HTTPException(401, "パスワードが違います")
        throttle.record_success(client)
        sessions.revoke(request.cookies.get(COOKIE_NAME))
        response.set_cookie(
            COOKIE_NAME, sessions.create(), max_age=settings.session_ttl_s,
            httponly=True, secure=settings.cookie_secure, samesite="strict", path="/",
        )
        return {"admin": True}

    @app.post("/api/logout", dependencies=[Depends(require_csrf_header)])
    def logout(request: Request, response: Response) -> dict:
        sessions.revoke(request.cookies.get(COOKIE_NAME))
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"admin": False}

    # --- 閲覧 --------------------------------------------------------------------------

    @app.get("/api/config", dependencies=[Depends(require_viewer)])
    def config() -> dict:
        ws = workspace.get()
        return {"tasks": available_tasks(), "workspace": {"repo": ws.repo, "out": ws.out}}

    @app.get("/api/status", dependencies=[Depends(require_viewer)])
    def status() -> dict:
        return jobs.status(current_out())

    @app.get("/api/runs", dependencies=[Depends(require_viewer)])
    def history() -> list[dict]:
        out = current_out()
        return jobs.history(out) if out else []

    @app.get("/api/runs/{run_id}/log", dependencies=[Depends(require_viewer)])
    def run_log(run_id: str, offset: int = 0) -> dict:
        job = jobs.current
        # 実行後に出力先を変えても、その実行のログは読めるようにする
        out = job.out_dir if job is not None and job.run_id == run_id else out_for_view()
        try:
            return jobs.read_log(out, run_id, offset)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(404, "ログが見つかりません") from e

    @app.get("/api/outputs", dependencies=[Depends(require_viewer)])
    def outputs() -> list[dict]:
        out = current_out()
        return jobs.list_outputs(out) if out else []

    @app.get("/api/outputs/file", dependencies=[Depends(require_viewer)])
    def output_file(path: str, download: bool = False):
        try:
            file = jobs.output_file(out_for_view(), path)
        except PermissionError as e:
            raise HTTPException(403, "このファイルは表示できません") from e
        except FileNotFoundError as e:
            raise HTTPException(404, "ファイルが見つかりません") from e
        if download:
            return FileResponse(file, media_type="application/octet-stream", filename=file.name)
        # ブラウザに HTML として解釈させないよう、常にプレーンテキストで返す
        return PlainTextResponse(file.read_text(encoding="utf-8", errors="replace"))

    # --- 実行（管理者だけ） --------------------------------------------------------------

    @app.post("/api/runs", status_code=202, dependencies=[Depends(require_admin)])
    def start_run(body: RunRequest) -> dict:
        ws = workspace.get()
        if not ws.repo or not ws.out:
            raise HTTPException(400, "対象フォルダと出力先フォルダを設定してください")
        try:
            # 保存後にフォルダが消えた・範囲が変わった場合に備えて、実行のたびに確かめる
            repo, out = workspace.check_repo(ws.repo), workspace.check_out(ws.out)
        except (ValueError, PermissionError) as e:
            raise HTTPException(400, str(e)) from e
        unknown = [t for t in body.tasks if t not in available_tasks()]
        if unknown:
            raise HTTPException(400, f"実行できないタスクです（スキル未登録）: {', '.join(unknown)}")
        if not skills.is_registered(DOC) and (DOC in body.tasks or not body.tasks):
            raise HTTPException(400, "設計書スキルが登録されていません。「スキル」から登録してください")
        try:
            job = jobs.start(repo, out, list(dict.fromkeys(body.tasks)), body.limit, body.force)
        except JobBusy as e:
            raise HTTPException(409, str(e)) from e
        return job.to_dict()

    @app.post("/api/runs/stop", dependencies=[Depends(require_admin)])
    def stop_run() -> dict:
        job = jobs.stop()
        if job is None:
            raise HTTPException(409, "Web から起動した実行中の処理はありません")
        return job.to_dict()

    # --- フォルダ（管理者だけ） ------------------------------------------------------------

    @app.get("/api/fs", dependencies=[Depends(require_admin)])
    def browse(path: str | None = None) -> dict:
        try:
            return workspace.browse(path)
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.put("/api/workspace", dependencies=[Depends(require_admin), Depends(require_idle)])
    def set_workspace(body: WorkspaceRequest) -> dict:
        try:
            ws = workspace.set(body.repo, body.out)
        except (ValueError, PermissionError) as e:
            raise HTTPException(400, str(e)) from e
        return {"repo": ws.repo, "out": ws.out}

    # --- スキル（管理者だけ） --------------------------------------------------------------

    def check_kind(kind: str) -> str:
        if kind not in (DOC, COMMENT):
            raise HTTPException(404, "スキルの種類が正しくありません")
        return kind

    @app.get("/api/skills", dependencies=[Depends(require_admin)])
    def list_skills() -> list[dict]:
        return [skills.info(DOC), skills.info(COMMENT)]

    @app.get("/api/skills/{kind}/content", dependencies=[Depends(require_admin)])
    def skill_content(kind: str) -> PlainTextResponse:
        try:
            return PlainTextResponse(skills.read_skill(check_kind(kind)))
        except FileNotFoundError as e:
            raise HTTPException(404, "スキルが登録されていません") from e

    @app.put("/api/skills/{kind}", dependencies=[Depends(require_admin), Depends(require_idle)])
    def upload_skill(kind: str, body: SkillUpload) -> dict:
        check_kind(kind)
        try:
            files = [SkillFile(f.path, base64.b64decode(f.content_b64, validate=True)) for f in body.files]
        except (binascii.Error, ValueError) as e:
            raise HTTPException(400, "ファイルの内容が正しく送られていません") from e
        try:
            return skills.install(kind, files)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/api/skills/{kind}", dependencies=[Depends(require_admin), Depends(require_idle)])
    def remove_skill(kind: str) -> dict:
        try:
            return skills.remove(check_kind(kind))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/skills/{kind}/restore", dependencies=[Depends(require_admin), Depends(require_idle)])
    def restore_skill(kind: str) -> dict:
        try:
            return skills.restore(check_kind(kind))
        except FileNotFoundError as e:
            raise HTTPException(404, str(e)) from e

    # --- 対象の設定（targets.toml。閲覧は誰でも、変更は管理者だけ） ---------------------------

    @app.get("/api/targets", dependencies=[Depends(require_viewer)])
    def get_targets() -> dict:
        try:
            return targets.get()
        except (OSError, ValueError) as e:
            raise HTTPException(500, f"対象の設定ファイルを読めません: {e}") from e

    @app.put("/api/targets", dependencies=[Depends(require_admin), Depends(require_idle)])
    def save_targets(body: TargetsRequest) -> dict:
        try:
            return targets.save(body.config)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/targets/preview", dependencies=[Depends(require_admin)])
    def preview_targets(body: TargetsRequest) -> dict:
        repo = workspace.get().repo
        if not repo:
            raise HTTPException(400, "対象フォルダを設定してください")
        try:
            return targets.preview(body.config, workspace.check_repo(repo))
        except (ValueError, PermissionError) as e:
            raise HTTPException(400, str(e)) from e

    return app
