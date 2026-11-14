import asyncio
import json
from pathlib import Path

import pytest

from godoc_agent.sessions import SessionBoard, sessions_path
from godoc_agent.slots import GlobalSlots


def test_slots_are_shared_between_instances_up_to_the_limit(tmp_path: Path):
    # runner は別プロセスで動くため、別々のインスタンス（別々に開いたロックファイル）どうしで枠を取り合う
    first, second = GlobalSlots(tmp_path, limit=2), GlobalSlots(tmp_path, limit=2)

    a = first.try_acquire()
    b = second.try_acquire()
    assert a is not None and b is not None and a.number != b.number
    assert first.try_acquire() is None
    assert second.busy_count() == 2

    a.release()
    assert second.busy_count() == 1
    c = second.try_acquire()
    assert c is not None and c.number == a.number
    b.release()
    c.release()
    assert first.busy_count() == 0


def test_release_is_idempotent(tmp_path: Path):
    slot = GlobalSlots(tmp_path, limit=1).try_acquire()
    slot.release()
    slot.release()
    assert GlobalSlots(tmp_path, limit=1).try_acquire() is not None


def test_limit_must_be_positive(tmp_path: Path):
    with pytest.raises(ValueError):
        GlobalSlots(tmp_path, limit=0)


async def test_session_waits_for_server_wide_slot_held_by_another_run(tmp_path: Path):
    # 他の実行（別の runner）がサーバー全体の枠をすべて使っている間は、自分の枠が空いていても始めない
    other_run = GlobalSlots(tmp_path / "slots", limit=1).try_acquire()
    global_slots = GlobalSlots(tmp_path / "slots", limit=1, poll_interval_s=0.01)
    path = sessions_path(tmp_path / "out", "r1")
    board = SessionBoard(path, "r1", 2, total=1, echo=lambda _: None, global_slots=global_slots)
    started = asyncio.Event()

    async def work():
        async with board.session("doc", "a.go"):
            started.set()

    task = asyncio.create_task(work())
    await asyncio.sleep(0.1)
    assert not started.is_set()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["active"] == 0
    assert any("空きを待ちます" in e["text"] for s in data["sessions"] for e in s["events"])

    other_run.release()
    await asyncio.wait_for(task, timeout=5)
    assert started.is_set()
    # 終わったら全体の枠も返す
    assert global_slots.busy_count() == 0
