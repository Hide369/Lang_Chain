"""出力先ごとの排他ロック。同じ --out に対して runner が同時に2つ動かないようにする。

Web 画面・コマンド・夜間タイマーのどこから起動しても、同じ出力先の manifest と設計書を
同時に書き換えると、差分スキップの記録が食い違ったり、同じファイルを二重に生成したりする。
ロックはプロセスが終了すれば（強制終了でも）OS が解放するため、ロックファイルが残っても問題ない。
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

LOCK_FILE_NAME = ".runner.lock"


class RunLockBusy(RuntimeError):
    """同じ出力先で、別の runner が実行中。"""


def lock_path(out_dir: Path) -> Path:
    return out_dir / LOCK_FILE_NAME


def try_lock_file(f: IO[str]) -> bool:
    """開いたファイルの排他ロックを待たずに取る。取れたら True。プロセスが終了すれば OS が解放する。"""
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def unlock_file(f: IO[str]) -> None:
    if os.name == "nt":
        import msvcrt

        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def run_lock(out_dir: Path) -> Iterator[None]:
    """出力先のロックを取る。取れなければ待たずに RunLockBusy を送出する。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    f = lock_path(out_dir).open("a+", encoding="utf-8")
    try:
        if not try_lock_file(f):
            raise RunLockBusy(f"別の実行が進行中です（出力先: {out_dir}）。終了してから再実行してください")
        try:
            yield
        finally:
            unlock_file(f)
    finally:
        f.close()


def is_locked(out_dir: Path) -> bool:
    """いま別の runner が実行中か。画面表示・起動前の確認用（確認と起動の間に状態が変わることはある）。"""
    if not lock_path(out_dir).is_file():
        return False
    try:
        with run_lock(out_dir):
            return False
    except RunLockBusy:
        return True
