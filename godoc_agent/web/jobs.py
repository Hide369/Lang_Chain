"""Web 画面からの runner の実行を管理する。

複数人がそれぞれ選んだ出力先で、runner を同時に動かせる（同じ出力先では同時に1つだけ）。
各 runner の中では Claude Code のセッションを並列に動かし、その合計はサーバー全体で最大10に抑える（slots.py）。

runner は別プロセスで起動する（コマンドで実行したときと同じ動きにし、Web サーバーが落ちても
バッチの途中経過は manifest に残るようにするため）。出力は計測ログと同じ `<out>/_runs/` にログとして残す。
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..config import MAX_CONCURRENCY
from ..runlock import LOCK_FILE_NAME, is_locked
from ..sessions import sessions_path
from ..slots import GlobalSlots
from .settings import APP_DIR

# 停止を指示してから、応じないプロセスを強制終了するまでの秒数
STOP_GRACE_S = 30
# 1回に返すログの最大バイト数
LOG_CHUNK_BYTES = 64 * 1024
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# 実行のきっかけ（run_id の先頭にも使う）
MANUAL = "manual"
SCHEDULED = "scheduled"
_RUN_ID_PREFIX = {MANUAL: "web", SCHEDULED: "scheduled"}
# Web 画面から同時に動かせる runner の数。runner は最低1セッション使うため、全体のセッション数の上限より多くしても意味がない
MAX_RUNNING_JOBS = MAX_CONCURRENCY
# 終了した実行を、状態の表示用に覚えておく数（出力先ごとの直近の1件は数に関係なく残す）
_KEEP_FINISHED_JOBS = 50


def _output_kind(rel: Path) -> str:
    """生成物を出力したスキル名。設計書は --out 直下、それ以外は _<スキル名>（コメント追記は _commented）の下。"""
    top = rel.parts[0]
    if len(rel.parts) > 1 and top.startswith("_"):
        return "comment" if top == "_commented" else top[1:]
    return "doc"


class JobBusy(RuntimeError):
    """すでに実行中のため、新しい実行を受け付けない。"""


@dataclass
class Job:
    run_id: str
    repo: Path
    out_dir: Path
    tasks: list[str]
    limit: int
    force: bool
    started_at: str
    log_path: Path
    proc: subprocess.Popen
    git_pull: bool = False
    trigger: str = MANUAL
    concurrency: int | None = None  # None は runner の既定値（config.DEFAULT_CONCURRENCY）
    stop_requested: bool = False
    finished_at: str | None = None

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def to_dict(self) -> dict:
        exit_code = self.proc.poll()
        if exit_code is not None and self.finished_at is None:
            self.finished_at = datetime.now().isoformat(timespec="seconds")
        return {
            "run_id": self.run_id,
            "repo": str(self.repo),
            "out": str(self.out_dir),
            "tasks": self.tasks,
            "limit": self.limit,
            "force": self.force,
            "git_pull": self.git_pull,
            "trigger": self.trigger,
            "concurrency": self.concurrency,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "running": exit_code is None,
            "exit_code": exit_code,
            "stop_requested": self.stop_requested,
        }


class JobManager:
    def __init__(self, runner_cmd: list[str] | None = None, cwd: Path = APP_DIR, global_slots: GlobalSlots | None = None):
        self._runner_cmd = runner_cmd or [sys.executable, "-m", "godoc_agent.runner"]
        self._cwd = cwd
        self._global_slots = global_slots or GlobalSlots()
        # 起動した順。終了したものも、状態の表示のためにしばらく残す
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    @property
    def current(self) -> Job | None:
        """最後に起動した実行。"""
        return next(reversed(self._jobs.values()), None)

    @property
    def running(self) -> bool:
        """Web から起動した実行が1つでも動いているか。"""
        return any(job.running for job in list(self._jobs.values()))

    def running_jobs(self) -> list[Job]:
        return [job for job in list(self._jobs.values()) if job.running]

    def get(self, run_id: str) -> Job | None:
        return self._jobs.get(run_id)

    def job_for(self, out_dir: Path | None) -> Job | None:
        """その出力先で最後に起動した実行（画面は、自分が選んでいる出力先の実行を表示する）。"""
        if out_dir is None:
            return None
        return next((job for job in reversed(list(self._jobs.values())) if job.out_dir == out_dir), None)

    def status(self, out_dir: Path | None) -> dict:
        job = self.job_for(out_dir)
        running = self.running_jobs()
        return {
            "job": job.to_dict() if job else None,
            # Web 以外（夜間バッチ・コマンド）から、選んでいる出力先で実行が動いているか
            "busy_elsewhere": not (job and job.running) and out_dir is not None and is_locked(out_dir),
            # 他の人が別の出力先で動かしている実行（Claude Code のセッションはサーバー全体で取り合う）
            "others": [
                {k: v for k, v in other.to_dict().items() if k in ("run_id", "out", "trigger", "started_at", "concurrency")}
                for other in running if other is not job
            ],
            "server": {"active_sessions": self._global_slots.busy_count(), "max_sessions": self._global_slots.limit},
        }

    def start(
        self, repo: Path, out_dir: Path, tasks: list[str], limit: int, force: bool,
        *, git_pull: bool = False, trigger: str = MANUAL, concurrency: int | None = None,
    ) -> Job:
        with self._lock:
            same_out = self.job_for(out_dir)
            if same_out is not None and same_out.running:
                raise JobBusy(
                    f"この出力先では実行中です（run_id={same_out.run_id}）。終了または停止してから実行するか、別の出力先を選んでください"
                )
            if len(self.running_jobs()) >= MAX_RUNNING_JOBS:
                raise JobBusy(f"同時に実行できるのは {MAX_RUNNING_JOBS} 件までです。どれかが終了してから実行してください")
            if is_locked(out_dir):
                raise JobBusy("別の実行（夜間バッチ・コマンドからの実行）が進行中です。終了してから実行してください")

            runs_dir = out_dir / "_runs"
            runs_dir.mkdir(parents=True, exist_ok=True)
            run_id = self._new_run_id(runs_dir, _RUN_ID_PREFIX[trigger])
            log_path = runs_dir / f"{run_id}.log"
            cmd = [*self._runner_cmd, "--repo", str(repo), "--out", str(out_dir), "--run-id", run_id]
            if tasks:
                cmd += ["--tasks", ",".join(tasks)]
            if limit:
                cmd += ["--limit", str(limit)]
            if force:
                cmd.append("--force")
            if concurrency:
                cmd += ["--concurrency", str(concurrency)]
            if git_pull:
                # 古いソースから作らないよう、git pull に失敗したら runner を起動しない
                cmd = [sys.executable, "-m", "godoc_agent.web.pull_and_run", str(repo), "--", *cmd]

            with log_path.open("ab") as log:
                header = f"=== {datetime.now():%Y-%m-%d %H:%M:%S} start ({trigger}) repo={repo} out={out_dir}\n"
                log.write(header.encode("utf-8"))
                log.flush()
                proc = subprocess.Popen(
                    cmd,
                    cwd=self._cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                    # 停止のとき、runner が起動した claude もまとめて止められるようにプロセスグループを分ける
                    start_new_session=os.name != "nt",
                )
            job = Job(
                run_id=run_id, repo=repo, out_dir=out_dir, tasks=tasks, limit=limit, force=force,
                started_at=datetime.now().isoformat(timespec="seconds"), log_path=log_path, proc=proc,
                git_pull=git_pull, trigger=trigger, concurrency=concurrency,
            )
            self._jobs[run_id] = job
            self._forget_old_jobs()
            return job

    def _forget_old_jobs(self) -> None:
        finished = [job for job in self._jobs.values() if not job.running]
        latest_per_out = {job.out_dir: job for job in finished}  # 後に起動したものが残る
        for job in finished[: max(0, len(finished) - _KEEP_FINISHED_JOBS)]:
            if latest_per_out.get(job.out_dir) is not job:
                del self._jobs[job.run_id]

    def stop(self, run_id: str) -> Job | None:
        """実行中の runner を止める。完了済みのファイルは manifest に残り、再実行で続きから処理される。"""
        with self._lock:
            job = self._jobs.get(run_id)
            if job is None or not job.running:
                return None
            job.stop_requested = True
            self._signal(job.proc, signal.SIGTERM)
            threading.Thread(target=self._kill_after_grace, args=(job.proc,), daemon=True).start()
            return job

    def _kill_after_grace(self, proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            self._signal(proc, signal.SIGKILL if os.name != "nt" else signal.SIGTERM)

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass  # すでに終了している

    def _new_run_id(self, runs_dir: Path, prefix: str) -> str:
        base = datetime.now().strftime(f"{prefix}-%Y%m%d-%H%M%S")
        run_id, n = base, 1
        # 別の出力先で同じ秒に起動した実行とも重ならないようにする（run_id で実行を見分けるため）
        while (
            run_id in self._jobs
            or (runs_dir / f"{run_id}.log").exists()
            or (runs_dir / f"{run_id}.jsonl").exists()
        ):
            n += 1
            run_id = f"{base}-{n}"
        return run_id

    # --- 過去の実行・ログ ---------------------------------------------------------------

    def read_log(self, out_dir: Path, run_id: str, offset: int) -> dict:
        """ログを offset バイト目から返す。書き込み途中の行は次回に回す。"""
        if not _RUN_ID_RE.match(run_id):
            raise ValueError(f"run_id が正しくありません: {run_id!r}")
        path = out_dir / "_runs" / f"{run_id}.log"
        if not path.is_file():
            raise FileNotFoundError(run_id)
        size = path.stat().st_size
        offset = min(max(offset, 0), size)
        with path.open("rb") as f:
            f.seek(offset)
            chunk = f.read(LOG_CHUNK_BYTES)
        job = self._jobs.get(run_id)
        still_writing = job is not None and job.log_path == path and job.running
        if still_writing or len(chunk) == LOG_CHUNK_BYTES:
            # 行の途中（UTF-8 の文字の途中を含む）で切らないよう、最後の改行までにする
            cut = chunk.rfind(b"\n")
            if cut >= 0:
                chunk = chunk[: cut + 1]
            elif still_writing:
                chunk = b""
        next_offset = offset + len(chunk)
        return {"text": chunk.decode("utf-8", errors="replace"), "offset": next_offset, "eof": next_offset >= size}

    def read_sessions(self, out_dir: Path, run_id: str) -> dict:
        """runner が書き出したセッション枠の状況。実行中なら各セッションの経過秒数を付ける。"""
        if not _RUN_ID_RE.match(run_id):
            raise ValueError(f"run_id が正しくありません: {run_id!r}")
        path = sessions_path(out_dir, run_id)
        if not path.is_file():
            raise FileNotFoundError(run_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        # 強制終了された runner は終了を書き出せないため、プロセスの状態で確かめる
        job = self._jobs.get(run_id)
        if job is not None and job.out_dir == out_dir:
            alive = job.running
        else:  # 夜間バッチ・コマンドからの実行。出力先のロックで判断する
            alive = is_locked(out_dir)
        data["interrupted"] = bool(data.get("running")) and not alive
        data["running"] = bool(data.get("running")) and alive
        now = time.time()
        for session in data.get("sessions", []):
            started = session.get("started_ts")
            session["elapsed_s"] = round(now - started) if data["running"] and session.get("busy") and started else None
        return data

    @staticmethod
    def history(out_dir: Path, limit: int = 30) -> list[dict]:
        """過去の実行（Web・夜間バッチ・コマンド）を新しい順に返す。件数は計測ログから数える。"""
        runs_dir = out_dir / "_runs"
        if not runs_dir.is_dir():
            return []
        files: dict[str, list[Path]] = {}
        for path in runs_dir.iterdir():
            if path.suffix in (".log", ".jsonl") and _RUN_ID_RE.match(path.stem):
                files.setdefault(path.stem, []).append(path)
        runs = []
        for run_id, paths in files.items():
            counts: Counter[str] = Counter()
            jsonl = runs_dir / f"{run_id}.jsonl"
            if jsonl.is_file():
                for line in jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        counts[json.loads(line).get("status", "unknown")] += 1
                    except (json.JSONDecodeError, AttributeError):
                        counts["unknown"] += 1
            runs.append({
                "run_id": run_id,
                "updated_at": datetime.fromtimestamp(max(p.stat().st_mtime for p in paths)).isoformat(timespec="seconds"),
                "has_log": (runs_dir / f"{run_id}.log").is_file(),
                "counts": dict(counts),
            })
        runs.sort(key=lambda r: r["updated_at"], reverse=True)
        return runs[:limit]

    # --- 生成物 -------------------------------------------------------------------------

    @staticmethod
    def list_outputs(out_dir: Path) -> list[dict]:
        """出力先の生成物（設計書・コメント追記済みファイル）。ログや管理用のファイルは除く。"""
        if not out_dir.is_dir():
            return []
        items = []
        for path in sorted(out_dir.rglob("*")):
            rel = path.relative_to(out_dir)
            if not path.is_file() or rel.parts[0] == "_runs" or any(p.startswith(".") for p in rel.parts):
                continue
            if path.name == LOCK_FILE_NAME:
                continue
            stat = path.stat()
            items.append({
                "path": rel.as_posix(),
                "kind": _output_kind(rel),
                "size": stat.st_size,
                "updated_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            })
        return items

    @staticmethod
    def output_file(out_dir: Path, rel_path: str) -> Path:
        """画面から指定された生成物のパス。出力先の外・ログ・管理用ファイルは返さない。"""
        root = out_dir.resolve()
        path = (root / rel_path).resolve()
        if not path.is_relative_to(root) or path == root:
            raise PermissionError(rel_path)
        rel = path.relative_to(root)
        if rel.parts[0] == "_runs" or any(p.startswith(".") for p in rel.parts):
            raise PermissionError(rel_path)
        if not path.is_file():
            raise FileNotFoundError(rel_path)
        return path
