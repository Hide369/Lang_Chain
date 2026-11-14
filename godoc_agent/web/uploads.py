"""クライアント PC のフォルダ（ソース一式）をサーバーにアップロードして、対象フォルダとして使う。

runner と Claude Code はサーバーの中でしか動かないため、クライアント PC のファイルはいったんサーバーに置く。
  <state>/uploads/<利用者ID>/<フォルダ名>/src … アップロードしたソース（対象フォルダ）
  <state>/uploads/<利用者ID>/<フォルダ名>/out … 出力先
同じフォルダ名でアップロードし直すと src だけを入れ替え、out は残す（変更のないファイルは差分スキップで再利用される）。

大きなフォルダでもリクエスト1回の大きさとメモリを抑えられるよう、ブラウザは数MBずつに分けて送る。
  begin（受付）→ add_files（複数回）→ complete（src と入れ替え）
受け付け中のファイルは同じフォルダの .staging-<ID> に書き、complete までは対象フォルダに混ざらない。
"""

import re
import secrets
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .skills import SkillFile, _check_rel_path
from .workspace import CLIENT_ID_RE

# 1回のアップロードの上限（サーバーのディスクと、1回の実行で扱える量の目安）
MAX_UPLOAD_FILES = 20_000
MAX_UPLOAD_FILE_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_TOTAL_BYTES = 500 * 1024 * 1024
SRC_DIR = "src"
OUT_DIR = "out"
_STAGING_PREFIX = ".staging-"
# フォルダ名（ブラウザで選んだフォルダの名前）。パスの区切りや先頭のドットは使わせない
_NAME_RE = re.compile(r"^[^\\/:*?\"<>|\x00-\x1f.][^\\/:*?\"<>|\x00-\x1f]{0,99}$")


class UploadNotFound(LookupError):
    """受け付けていない（他のブラウザの、または完了・中止済みの）アップロード。"""


@dataclass
class _Pending:
    client_id: str
    name: str
    staging: Path
    files: int = 0
    total_bytes: int = 0


def validate_upload_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name) or name.endswith((" ", ".")):
        raise ValueError(f"フォルダ名が正しくありません（記号 \\ / : * ? \" < > | と先頭の . は使えません）: {name!r}")
    return name


class UploadStore:
    def __init__(self, state_dir: Path):
        self.root = (state_dir / "uploads").resolve()
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.Lock()

    def project_dir(self, client_id: str, name: str) -> Path:
        if not CLIENT_ID_RE.match(client_id):
            raise ValueError(f"利用者IDが正しくありません: {client_id!r}")
        return self.root / client_id / validate_upload_name(name)

    def begin(self, client_id: str, name: str) -> str:
        project = self.project_dir(client_id, name)
        upload_id = secrets.token_urlsafe(12)
        with self._lock:
            self._remove_abandoned_staging(client_id)
            staging = project / f"{_STAGING_PREFIX}{upload_id}"
            staging.mkdir(parents=True)
            self._pending[upload_id] = _Pending(client_id, project.name, staging)
        return upload_id

    def add_files(self, client_id: str, upload_id: str, files: list[SkillFile]) -> dict:
        with self._lock:
            pending = self._get(client_id, upload_id)
            if pending.files + len(files) > MAX_UPLOAD_FILES:
                raise ValueError(f"ファイル数が上限（{MAX_UPLOAD_FILES} 個）を超えます")
            size = sum(len(f.content) for f in files)
            if pending.total_bytes + size > MAX_UPLOAD_TOTAL_BYTES:
                raise ValueError(f"合計サイズが上限（{MAX_UPLOAD_TOTAL_BYTES // 1024 // 1024}MB）を超えます")
            for f in files:
                if len(f.content) > MAX_UPLOAD_FILE_BYTES:
                    raise ValueError(f"{f.path} が1ファイルの上限（{MAX_UPLOAD_FILE_BYTES // 1024 // 1024}MB）を超えています")
                path = pending.staging / _check_rel_path(f.path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f.content)
            pending.files += len(files)
            pending.total_bytes += size
            return {"files": pending.files, "bytes": pending.total_bytes}

    def complete(self, client_id: str, upload_id: str, is_busy: Callable[[Path], bool]) -> tuple[Path, Path]:
        """受け付けたファイルを対象フォルダ（src）にする。出力先で実行中なら入れ替えない。"""
        with self._lock:
            pending = self._get(client_id, upload_id)
            if pending.files == 0:
                raise ValueError("ファイルが1つもアップロードされていません")
            project = pending.staging.parent
            src, out = project / SRC_DIR, project / OUT_DIR
            if is_busy(out):
                raise RuntimeError("このフォルダの実行中は、アップロードし直せません。終了または停止してから行ってください")
            old = project / f"{_STAGING_PREFIX}old-{upload_id}"
            if src.exists():
                src.replace(old)
            pending.staging.replace(src)
            out.mkdir(exist_ok=True)
            del self._pending[upload_id]
        shutil.rmtree(old, ignore_errors=True)
        return src, out

    def abort(self, client_id: str, upload_id: str) -> None:
        with self._lock:
            pending = self._get(client_id, upload_id)
            del self._pending[upload_id]
        shutil.rmtree(pending.staging, ignore_errors=True)

    def _get(self, client_id: str, upload_id: str) -> _Pending:
        pending = self._pending.get(upload_id)
        if pending is None or pending.client_id != client_id:
            raise UploadNotFound(upload_id)
        return pending

    def _remove_abandoned_staging(self, client_id: str) -> None:
        # ブラウザを閉じた・サーバーを再起動したなどで完了しなかったアップロードの残りを消す（同じブラウザの分だけ）
        active = {p.staging for p in self._pending.values()}
        client_dir = self.root / client_id
        if not client_dir.is_dir():
            return
        for staging in client_dir.glob(f"*/{_STAGING_PREFIX}*"):
            if staging not in active:
                shutil.rmtree(staging, ignore_errors=True)
