"""Web 画面に内蔵した定期実行（毎日決まった時刻に、設定を保存した人が選んでいたフォルダで runner を実行する）。

systemd の夜間タイマー（deploy/systemd/godoc-nightly.timer）の代わりに使う。両方を有効にしないこと。
- 実行時刻に Web 画面が止まっていた場合は、起動後にその日の分を実行する（systemd の Persistent=true と同じ）
- 実行時刻に別の実行が動いていれば、終わってから実行する（その日のうちは待ち続ける）
- 1日に実行するのは1回だけ。設定を保存した時点で今日の実行時刻を過ぎていれば、今日の分は実行せず翌日からにする
- 対象・出力先フォルダは、設定を保存したときにそのブラウザで選んでいたもの（複数人がそれぞれ別のフォルダを選べるため）。
  以前の版で保存した設定（フォルダの記録がない）は、共有の既定値（state/web/workspace.json）を使う
"""

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from ..runlock import is_locked
from ..skill_registry import COMMENT, DOC
from .jobs import SCHEDULED, Job, JobBusy, JobManager
from .skills import SkillStore
from .workspace import WorkspaceStore

log = logging.getLogger(__name__)

# 実行時刻になったかを確かめる間隔（秒）
CHECK_INTERVAL_S = 30
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class Schedule:
    enabled: bool = False
    time: str = "02:00"  # サーバーの現地時刻（HH:MM）
    tasks: list[str] = field(default_factory=lambda: [DOC, COMMENT])
    git_pull: bool = True
    repo: str | None = None
    out: str | None = None
    # 内部の記録（画面からは変えない）
    last_attempt_date: str | None = None  # この日の分は実行済み（または実行できないと確定した）
    last_run: dict | None = None  # 直近の定期実行（run_id・開始時刻、または実行できなかった理由）

    def due_at(self, day: date) -> datetime:
        hour, minute = map(int, self.time.split(":"))
        return datetime.combine(day, time(hour, minute))

    def next_run_at(self, now: datetime) -> datetime | None:
        if not self.enabled:
            return None
        today = now.date()
        if self.last_attempt_date == today.isoformat():
            return self.due_at(today + timedelta(days=1))
        # 今日の分がまだなら、時刻を過ぎていても（待っている間も）今日の時刻を表示する
        return self.due_at(today)


def validate_time(value: str) -> str:
    if not isinstance(value, str) or not _TIME_RE.match(value):
        raise ValueError(f"実行時刻は HH:MM（00:00〜23:59）で指定してください: {value!r}")
    return value


class ScheduleStore:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "schedule.json"
        self._lock = threading.Lock()

    def get(self) -> Schedule:
        if not self.path.is_file():
            return Schedule()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            known = {k: v for k, v in data.items() if k in Schedule.__dataclass_fields__}
            return Schedule(**known)
        except (json.JSONDecodeError, TypeError, AttributeError):
            log.warning("定期実行の設定を読めないため、既定値（無効）を使います: %s", self.path)
            return Schedule()

    def _write(self, schedule: Schedule) -> Schedule:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(schedule), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        return schedule

    def update(self, fn: Callable[[Schedule], Schedule]) -> Schedule:
        with self._lock:
            return self._write(fn(self.get()))

    def save_settings(
        self, enabled: bool, time_: str, tasks: list[str], git_pull: bool, now: datetime,
        *, repo: str | None = None, out: str | None = None,
    ) -> Schedule:
        validate_time(time_)
        if not tasks:
            raise ValueError("タスクを1つ以上選んでください")

        def apply(current: Schedule) -> Schedule:
            new = replace(
                current, enabled=enabled, time=time_, tasks=list(dict.fromkeys(tasks)), git_pull=git_pull,
                repo=repo or current.repo, out=out or current.out,
            )
            # 保存した時点で今日の時刻を過ぎていれば、今日の分は実行しない（保存した途端に動き出さないように）。
            # まだ来ていなければ、今日すでに実行していても新しい時刻でもう一度実行する
            today = now.date()
            new = replace(new, last_attempt_date=today.isoformat() if now >= new.due_at(today) else None)
            return new

        return self.update(apply)


class Scheduler:
    """実行時刻になったら runner を起動する。tick() を定期的に呼ぶ（テストからは直接呼ぶ）。"""

    def __init__(
        self,
        store: ScheduleStore,
        jobs: JobManager,
        workspace: WorkspaceStore,
        skills: SkillStore,
        clock: Callable[[], datetime] = datetime.now,
    ):
        self.store = store
        self.jobs = jobs
        self.workspace = workspace
        self.skills = skills
        self.clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> Job | None:
        schedule = self.store.get()
        now = self.clock()
        today = now.date().isoformat()
        if not schedule.enabled or schedule.last_attempt_date == today or now < schedule.due_at(now.date()):
            return None

        ws = self.workspace.get()
        repo_raw, out_raw = (schedule.repo, schedule.out) if schedule.repo and schedule.out else (ws.repo, ws.out)
        try:
            if not repo_raw or not out_raw:
                raise ValueError("対象フォルダ・出力先フォルダが未設定です")
            repo, out = self.workspace.check_repo(repo_raw), self.workspace.check_out(out_raw)
            # 登録を外したスキルのタスクは除く（設計書スキルがなければ実行できない）
            tasks = [t for t in schedule.tasks if self.skills.is_registered(t)]
            if not tasks:
                raise ValueError("選んだタスクのスキルが登録されていません")
        except (ValueError, PermissionError) as e:
            # 設定の誤りは、その日のうちに待っても直らないので、今日の分は実行できなかったとして記録する
            self._record(today, {"error": str(e), "at": now.isoformat(timespec="seconds")})
            log.warning("定期実行を開始できませんでした: %s", e)
            return None

        # 同じ出力先で手動の実行・夜間バッチ・コマンドからの実行が動いていれば、終わってから実行する
        # （別の出力先の実行とは同時に動く。セッション数はサーバー全体で抑えられる）
        same_out = self.jobs.job_for(out)
        if (same_out is not None and same_out.running) or is_locked(out):
            return None
        try:
            job = self.jobs.start(repo, out, tasks, 0, False, git_pull=schedule.git_pull, trigger=SCHEDULED)
        except JobBusy:
            return None
        self._record(today, {"run_id": job.run_id, "started_at": job.started_at, "out": str(out)})
        log.info("定期実行を開始しました: run_id=%s", job.run_id)
        return job

    def _record(self, day: str, last_run: dict) -> None:
        self.store.update(lambda s: replace(s, last_attempt_date=day, last_run=last_run))

    # --- バックグラウンドでの実行 ------------------------------------------------------------

    def start(self, interval_s: float = CHECK_INTERVAL_S) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(interval_s,), name="godoc-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self, interval_s: float) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # 1回の失敗で定期実行が止まらないよう、記録して次の確認を続ける
                log.exception("定期実行の確認中にエラーが発生しました")
            self._stop.wait(interval_s)
