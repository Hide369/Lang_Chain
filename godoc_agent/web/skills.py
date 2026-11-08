"""画面からのスキル（SKILL.md と参照ファイル一式）の登録。

runner が読む場所にそのまま置くので、Web 画面以外（夜間バッチ・コマンド）からの実行でも同じスキルが使われる。
  設計書     … CLAUDE_SKILL_PATH のフォルダ、未設定なら skills/design-doc-batch/
  コメント追記 … CLAUDE_COMMENT_SKILL_PATH のフォルダ、未設定なら skills/comment-batch/
入れ替えは一時フォルダに書き終えてから差し替え、直前のスキルは <フォルダ名>.prev に1世代だけ残す。
"""

import os
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from ..config import COMMENT_SKILL_NAME, design_skill_path
from ..tasks import COMMENT, DOC

SKILL_FILE = "SKILL.md"
MAX_FILES = 200
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class SkillFile:
    path: str  # スキルのフォルダからの相対パス（/ 区切り）
    content: bytes


def skill_dirs(app_dir: Path) -> dict[str, Path]:
    comment = os.getenv("CLAUDE_COMMENT_SKILL_PATH")
    return {
        DOC: design_skill_path(app_dir).parent,
        COMMENT: Path(comment).parent if comment else app_dir / "skills" / COMMENT_SKILL_NAME,
    }


def _check_rel_path(raw: str) -> str:
    if not raw or "\\" in raw or raw.startswith("/") or "\x00" in raw:
        raise ValueError(f"ファイルのパスが正しくありません: {raw!r}")
    parts = PurePosixPath(raw).parts
    if any(p in ("", ".", "..") for p in parts) or ":" in parts[0]:
        raise ValueError(f"ファイルのパスが正しくありません: {raw!r}")
    return "/".join(parts)


class SkillStore:
    def __init__(self, dirs: dict[str, Path]):
        self.dirs = dirs
        self._lock = threading.Lock()

    def _dir(self, kind: str) -> Path:
        if kind not in self.dirs:
            raise ValueError(f"スキルの種類が正しくありません: {kind!r}")
        return self.dirs[kind]

    def is_registered(self, kind: str) -> bool:
        return (self._dir(kind) / SKILL_FILE).is_file()

    def info(self, kind: str) -> dict:
        root = self._dir(kind)
        skill = root / SKILL_FILE
        files = []
        if skill.is_file():
            for path in (p for p in root.rglob("*") if p.is_file()):
                files.append({"path": path.relative_to(root).as_posix(), "size": path.stat().st_size})
            # SKILL.md を先頭に、残りはパス順（OS によらず同じ順序にする）
            files.sort(key=lambda f: (f["path"] != SKILL_FILE, f["path"]))
        return {
            "kind": kind,
            "dir": str(root),
            "registered": skill.is_file(),
            "updated_at": datetime.fromtimestamp(skill.stat().st_mtime).isoformat(timespec="seconds") if skill.is_file() else None,
            "files": files,
            "has_backup": self._backup(root).is_dir(),
        }

    def read_skill(self, kind: str) -> str:
        path = self._dir(kind) / SKILL_FILE
        if not path.is_file():
            raise FileNotFoundError(kind)
        return path.read_text(encoding="utf-8", errors="replace")

    def install(self, kind: str, files: list[SkillFile]) -> dict:
        root = self._dir(kind)
        checked = self._validate(files)
        with self._lock:
            root.parent.mkdir(parents=True, exist_ok=True)
            staging = root.with_name(f"{root.name}.uploading")
            shutil.rmtree(staging, ignore_errors=True)
            try:
                for rel, content in checked.items():
                    dest = staging / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(content)
                self._swap_in(root, staging)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        return self.info(kind)

    def remove(self, kind: str) -> dict:
        """登録を外す（直前のスキルとして .prev に残す）。設計書スキルは必須なので外せない。"""
        if kind == DOC:
            raise ValueError("設計書スキルは必須のため削除できません（新しいスキルを登録すると入れ替わります）")
        root = self._dir(kind)
        with self._lock:
            if root.is_dir():
                backup = self._backup(root)
                shutil.rmtree(backup, ignore_errors=True)
                root.rename(backup)
        return self.info(kind)

    def restore(self, kind: str) -> dict:
        """直前のスキル（.prev）に戻す。今のスキルは .prev に回る。"""
        root = self._dir(kind)
        with self._lock:
            backup = self._backup(root)
            if not (backup / SKILL_FILE).is_file():
                raise FileNotFoundError("戻せる直前のスキルがありません")
            staging = root.with_name(f"{root.name}.restoring")
            shutil.rmtree(staging, ignore_errors=True)
            backup.rename(staging)
            self._swap_in(root, staging)
        return self.info(kind)

    @staticmethod
    def _backup(root: Path) -> Path:
        return root.with_name(f"{root.name}.prev")

    def _swap_in(self, root: Path, staging: Path) -> None:
        backup = self._backup(root)
        if root.is_dir():
            shutil.rmtree(backup, ignore_errors=True)
            root.rename(backup)
        staging.rename(root)

    @staticmethod
    def _validate(files: list[SkillFile]) -> dict[str, bytes]:
        if not files:
            raise ValueError("ファイルがありません")
        if len(files) > MAX_FILES:
            raise ValueError(f"ファイルが多すぎます（{MAX_FILES} 件まで）")
        checked: dict[str, bytes] = {}
        total = 0
        for f in files:
            rel = _check_rel_path(f.path)
            if rel in checked:
                raise ValueError(f"同じパスのファイルが複数あります: {rel}")
            if len(f.content) > MAX_FILE_BYTES:
                raise ValueError(f"ファイルが大きすぎます（1ファイル {MAX_FILE_BYTES // 1024 // 1024}MB まで）: {rel}")
            total += len(f.content)
            checked[rel] = f.content
        if total > MAX_TOTAL_BYTES:
            raise ValueError(f"合計サイズが大きすぎます（{MAX_TOTAL_BYTES // 1024 // 1024}MB まで）")
        skill = checked.get(SKILL_FILE)
        if skill is None:
            raise ValueError(f"フォルダの直下に {SKILL_FILE} がありません")
        try:
            text = skill.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ValueError(f"{SKILL_FILE} は UTF-8 で保存してください") from e
        if not text.strip():
            raise ValueError(f"{SKILL_FILE} が空です")
        return checked
