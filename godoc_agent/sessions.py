"""同時に動く Claude Code のセッション（枠）の割り当てと、その状況の記録。

runner は同時セッション数（LLM_CONCURRENCY・--concurrency）の数だけ「枠」を用意し、
1ファイル×1タスクの処理を空いた枠に割り当てる。どの枠で何を処理しているか・各枠の直近の出来事を
`<out>/_runs/<run_id>.sessions.json` に書き出し、Web 画面はそれを読んで枠の占有状況とセッションごとのログを表示する。
同じ出来事は `[S01] ...` の形で標準出力（Web から起動した場合は実行ログ）にも出す。
"""

import asyncio
import json
import os
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

SESSIONS_SUFFIX = ".sessions.json"
# 1つの枠について残す直近の出来事の数（ファイルが際限なく大きくならないように）
MAX_EVENTS_PER_SESSION = 50
# Windows では読み手がファイルを開いている間 os.replace が失敗することがあるため、少しだけ再試行する
_WRITE_RETRIES = 5
_WRITE_RETRY_WAIT_S = 0.05


def sessions_path(out_dir: Path, run_id: str) -> Path:
    return out_dir / "_runs" / f"{run_id}{SESSIONS_SUFFIX}"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class _Slot:
    slot: int
    task: str | None = None
    rel_path: str | None = None
    started_at: str | None = None
    started_ts: float | None = None
    attempt: int = 0
    handled: int = 0  # この枠で処理し終えた件数
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS_PER_SESSION))

    @property
    def busy(self) -> bool:
        return self.rel_path is not None

    def to_dict(self) -> dict:
        return {
            "slot": self.slot,
            "busy": self.busy,
            "task": self.task,
            "rel_path": self.rel_path,
            "started_at": self.started_at,
            "started_ts": self.started_ts,
            "attempt": self.attempt,
            "handled": self.handled,
            "events": list(self.events),
        }


class Session:
    """1つの枠で処理中の1ファイル×1タスク。doc_graph はこれに出来事を知らせる。"""

    def __init__(self, board: "SessionBoard", slot: _Slot):
        self._board = board
        self._slot = slot

    @property
    def slot(self) -> int:
        return self._slot.slot

    def log(self, text: str, *, attempt: int | None = None) -> None:
        if attempt is not None:
            self._slot.attempt = attempt
        self._board._event(self._slot, text)


class SessionBoard:
    def __init__(
        self, path: Path, run_id: str, concurrency: int, total: int,
        *, echo: Callable[[str], None] | None = None, clock: Callable[[], float] = time.time,
    ):
        if concurrency < 1:
            raise ValueError(f"同時セッション数は1以上で指定してください: {concurrency}")
        self._path = path
        self._run_id = run_id
        self._concurrency = concurrency
        self._total = total
        self._finished = 0
        self._running = True
        self._echo = echo or (lambda line: print(line, flush=True))
        self._clock = clock
        self._slots = [_Slot(i) for i in range(1, concurrency + 1)]
        self._free: asyncio.Queue[int] = asyncio.Queue()
        for slot in self._slots:
            self._free.put_nowait(slot.slot)
        self._write_failed = False
        self._write()

    @asynccontextmanager
    async def session(self, task: str, rel_path: str) -> AsyncIterator[Session]:
        """空いた枠を1つ占有する。空きがなければ空くまで待つ。"""
        number = await self._free.get()
        slot = self._slots[number - 1]
        slot.task, slot.rel_path, slot.attempt = task, rel_path, 0
        slot.started_at, slot.started_ts = _now_iso(), self._clock()
        try:
            self._event(slot, f"開始 {task} {rel_path}")
            yield Session(self, slot)
        finally:
            slot.task = slot.rel_path = slot.started_at = slot.started_ts = None
            slot.attempt = 0
            slot.handled += 1
            self._finished += 1
            self._write()
            self._free.put_nowait(number)

    def __enter__(self) -> "SessionBoard":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        """実行の終了を記録する（Web 画面はこれを見て「実行中」の表示をやめる）。"""
        self._running = False
        self._write()

    def snapshot(self) -> dict:
        return {
            "run_id": self._run_id,
            "running": self._running,
            "concurrency": self._concurrency,
            "active": sum(s.busy for s in self._slots),
            "total": self._total,
            "finished": self._finished,
            "updated_at": _now_iso(),
            "sessions": [s.to_dict() for s in self._slots],
        }

    def _event(self, slot: _Slot, text: str) -> None:
        slot.events.append({"at": _now_iso(), "text": text})
        self._echo(f"[S{slot.slot:02d}] {text}")
        self._write()

    def _write(self) -> None:
        data = json.dumps(self.snapshot(), ensure_ascii=False)
        tmp = self._path.with_suffix(".tmp")
        error: OSError | None = None
        for _ in range(_WRITE_RETRIES):
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(data, encoding="utf-8")
                os.replace(tmp, self._path)
                return
            except OSError as e:
                error = e
                time.sleep(_WRITE_RETRY_WAIT_S)
        # 状況の表示用のファイルなので、書けなくても生成処理は止めない。気づけるよう1回だけ知らせる
        if not self._write_failed:
            self._write_failed = True
            print(f"注意: セッションの状況を書き出せません（{self._path}）: {error}", file=sys.stderr, flush=True)
