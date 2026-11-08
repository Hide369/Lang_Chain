"""Web 画面（FastAPI）。

- 閲覧（状態・ログ・履歴・生成物）は誰でもできる（WEB_VIEW_REQUIRES_LOGIN=true なら管理者だけ）
- 実行・停止と、設定の変更（フォルダ・スキル・対象）は管理者だけ。設定の変更は実行中は受け付けない
"""

import base64
import binascii
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..skill_registry import OUTPUT_TYPE_LABELS, OUTPUT_TYPES
from .auth import LoginThrottle, SessionStore, validate_password_hash, verify_password
from .git_repo import GitRepoError, inspect_repo
from .jobs import JobBusy, JobManager
from .schedule import Scheduler, ScheduleStore
from .settings import APP_DIR, WebSettings
from .skills import SkillFile, SkillStore
from .targets_editor import TargetsEditor
from .workspace import WorkspaceStore

COOKIE_NAME = "godoc_session"
# 状態を変える操作に必須のヘッダー。別サイトのフォームからは付けられないため、CSRF 対策になる
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "godoc-web"
STATIC_DIR = Path(__file__).resolve().parent / "static"


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


MAX_TASKS = 20


class RunRequest(BaseModel):
    tasks: list[str] = Field(min_length=1, max_length=MAX_TASKS)
    limit: int = Field(default=0, ge=0, le=100_000)
    force: bool = False
    git_pull: bool = False


class ScheduleRequest(BaseModel):
    enabled: bool
    time: str = Field(min_length=5, max_length=5)
    tasks: list[str] = Field(min_length=1, max_length=MAX_TASKS)
    git_pull: bool = True


class WorkspaceRequest(BaseModel):
    repo: str = Field(min_length=1, max_length=4096)
    out: str = Field(min_length=1, max_length=4096)


class UploadedFile(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    content_b64: str


class SkillUpload(BaseModel):
    files: list[UploadedFile] = Field(min_length=1)
    # 追加のスキルの出力の種類（document / comment / rewrite）。新しく登録するときは必須
    type: str | None = None


class TargetsRequest(BaseModel):
    config: dict


def create_app(
    settings: WebSettings,
    jobs: JobManager | None = None,
    throttle: LoginThrottle | None = None,
    app_dir: Path = APP_DIR,
    skills: SkillStore | None = None,
    clock: Callable[[], datetime] = datetime.now,
) -> FastAPI:
    validate_password_hash(settings.admin_password_hash)
    jobs = jobs or JobManager()
    sessions = SessionStore(settings.session_ttl_s)
    throttle = throttle or LoginThrottle()
    workspace = WorkspaceStore(settings.state_dir, settings.browse_roots)
    skills = skills or SkillStore(app_dir)
    targets = TargetsEditor(app_dir)
    schedule_store = ScheduleStore(settings.state_dir)
    scheduler = Scheduler(schedule_store, jobs, workspace, skills, clock)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 定期実行は Web 画面が動いている間だけ動く（起動時に、過ぎていた今日の分があれば実行する）
        scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()

    app = FastAPI(title="設計書生成", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.scheduler = scheduler

    def task_options() -> list[dict]:
        """実行・定期実行で選べるタスク（SKILL.md が置かれている登録済みのスキル）。"""
        return [{"name": s.name, "label": s.label, "type": s.output_type} for s in registered_skills()]

    def registered_skills():
        try:
            return skills.registered()
        except ValueError as e:  # skills/registry.json が壊れている
            raise HTTPException(500, str(e)) from e

    def check_pullable(repo: Path) -> None:
        # 実行時（pull_and_run）にも同じ確認をするが、ボタンを押した時点で分かるようにする
        try:
            inspect_repo(repo)
        except GitRepoError as e:
            raise HTTPException(400, f"実行前の git pull ができません: {e}") from e

    def check_tasks(names: list[str]) -> list[str]:
        available = {s.name for s in registered_skills()}
        unknown = [t for t in names if t not in available]
        if unknown:
            raise HTTPException(400, f"登録されていないスキルです: {', '.join(unknown)}")
        return list(dict.fromkeys(names))

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
        return {"tasks": task_options(), "workspace": {"repo": ws.repo, "out": ws.out}}

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
        tasks = check_tasks(body.tasks)
        if body.git_pull:
            check_pullable(repo)
        try:
            job = jobs.start(repo, out, tasks, body.limit, body.force, git_pull=body.git_pull)
        except JobBusy as e:
            raise HTTPException(409, str(e)) from e
        return job.to_dict()

    @app.post("/api/runs/stop", dependencies=[Depends(require_admin)])
    def stop_run() -> dict:
        job = jobs.stop()
        if job is None:
            raise HTTPException(409, "Web から起動した実行中の処理はありません")
        return job.to_dict()

    # --- 定期実行（閲覧は誰でも、変更は管理者だけ） ------------------------------------------------

    def schedule_view() -> dict:
        schedule = schedule_store.get()
        next_run = schedule.next_run_at(clock())
        return {
            "enabled": schedule.enabled,
            "time": schedule.time,
            "tasks": schedule.tasks,
            "git_pull": schedule.git_pull,
            "next_run_at": next_run.isoformat(timespec="minutes") if next_run else None,
            "last_run": schedule.last_run,
        }

    @app.get("/api/schedule", dependencies=[Depends(require_viewer)])
    def get_schedule() -> dict:
        return schedule_view()

    @app.put("/api/schedule", dependencies=[Depends(require_admin)])
    def save_schedule(body: ScheduleRequest) -> dict:
        tasks = check_tasks(body.tasks)
        repo = workspace.get().repo
        # フォルダが未設定なら、実行時に「開始できなかった」と記録される（ここでは保存を妨げない）
        if body.enabled and body.git_pull and repo:
            check_pullable(Path(repo))
        try:
            schedule_store.save_settings(body.enabled, body.time, tasks, body.git_pull, clock())
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return schedule_view()

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

    def all_skills() -> list[dict]:
        try:
            return [skills.info(s) for s in skills.all()]
        except ValueError as e:  # skills/registry.json が壊れている
            raise HTTPException(500, str(e)) from e

    @app.get("/api/skills", dependencies=[Depends(require_admin)])
    def list_skills() -> dict:
        return {
            "skills": all_skills(),
            "types": [{"type": t, "label": OUTPUT_TYPE_LABELS[t]} for t in OUTPUT_TYPES],
        }

    @app.get("/api/skills/{name}/content", dependencies=[Depends(require_admin)])
    def skill_content(name: str) -> PlainTextResponse:
        try:
            return PlainTextResponse(skills.read_skill(name))
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        except FileNotFoundError as e:
            raise HTTPException(404, "スキルが登録されていません") from e

    @app.put("/api/skills/{name}", dependencies=[Depends(require_admin), Depends(require_idle)])
    def upload_skill(name: str, body: SkillUpload) -> dict:
        try:
            files = [SkillFile(f.path, base64.b64decode(f.content_b64, validate=True)) for f in body.files]
        except (binascii.Error, ValueError) as e:
            raise HTTPException(400, "ファイルの内容が正しく送られていません") from e
        try:
            return skills.install(name, files, body.type)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/api/skills/{name}", dependencies=[Depends(require_admin), Depends(require_idle)])
    def remove_skill(name: str) -> dict:
        try:
            skills.remove(name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"skills": all_skills()}

    @app.post("/api/skills/{name}/restore", dependencies=[Depends(require_admin), Depends(require_idle)])
    def restore_skill(name: str) -> dict:
        try:
            return skills.restore(name)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
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
