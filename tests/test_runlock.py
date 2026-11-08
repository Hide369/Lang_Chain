from pathlib import Path

import pytest

from godoc_agent.runlock import RunLockBusy, is_locked, run_lock


def test_second_lock_on_same_out_dir_is_rejected(tmp_path: Path):
    out = tmp_path / "out"
    assert not is_locked(out)
    with run_lock(out):
        assert is_locked(out)
        with pytest.raises(RunLockBusy, match="別の実行が進行中"):
            with run_lock(out):
                pass
    # 抜けたら解放され、次の実行が取れる
    assert not is_locked(out)
    with run_lock(out):
        pass


def test_different_out_dirs_do_not_block_each_other(tmp_path: Path):
    with run_lock(tmp_path / "a"):
        with run_lock(tmp_path / "b"):
            assert is_locked(tmp_path / "a")
