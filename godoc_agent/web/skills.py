"""画面からのスキル（SKILL.md と参照ファイル一式）の登録。

runner が読む場所にそのまま置くので、Web 画面以外（夜間バッチ・コマンド）からの実行でも同じスキルが使われる。
  設計書（doc）       … CLAUDE_SKILL_PATH のフォルダ、未設定なら skills/design-doc-batch/
  コメント追記（comment） … CLAUDE_COMMENT_SKILL_PATH のフォルダ、未設定なら skills/comment-batch/
  追加のスキル          … skills/<スキル名>/。名前・出力の種類・表示名は skills/registry.json に記録する
入れ替えは一時フォルダに書き終えてから差し替え、直前のスキルは <フォルダ名>.prev に1世代だけ残す。
"""

import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from ..skill_registry import (
    COMMENT,
    DOC,
    OUTPUT_TYPE_LABELS,
    OUTPUT_TYPES,
    SkillDef,
    custom_skill,
    find_skill,
    list_skills,
    read_labels,
    read_registry,
    validate_label,
    validate_name,
    write_registry,
)

SKILL_FILE = "SKILL.md"
MAX_FILES = 200
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class SkillFile:
    path: str  # スキルのフォルダからの相対パス（/ 区切り）
    content: bytes


def _check_rel_path(raw: str) -> str:
    if not raw or "\\" in raw or raw.startswith("/") or "\x00" in raw:
        raise ValueError(f"ファイルのパスが正しくありません: {raw!r}")
    parts = PurePosixPath(raw).parts
    if any(p in ("", ".", "..") for p in parts) or ":" in parts[0]:
        raise ValueError(f"ファイルのパスが正しくありません: {raw!r}")
    return "/".join(parts)


class SkillStore:
    def __init__(self, app_dir: Path):
        self.app_dir = app_dir
        self._lock = threading.Lock()

    # --- 参照 ---------------------------------------------------------------------------

    def all(self) -> list[SkillDef]:
        return list_skills(self.app_dir)

    def get(self, name: str) -> SkillDef:
        return find_skill(self.app_dir, name)

    def registered(self) -> list[SkillDef]:
        """SKILL.md が置かれていて、実行に使えるスキル。"""
        return [s for s in self.all() if s.registered]

    def is_registered(self, name: str) -> bool:
        return any(s.name == name for s in self.registered())

    def info(self, skill: SkillDef) -> dict:
        root = skill.skill_dir
        files = []
        if skill.registered:
            for path in (p for p in root.rglob("*") if p.is_file()):
                files.append({"path": path.relative_to(root).as_posix(), "size": path.stat().st_size})
            # SKILL.md を先頭に、残りはパス順（OS によらず同じ順序にする）
            files.sort(key=lambda f: (f["path"] != SKILL_FILE, f["path"]))
        return {
            "name": skill.name,
            "label": skill.label,
            "display_name": skill.display_name,
            "type": skill.output_type,
            "type_label": OUTPUT_TYPE_LABELS[skill.output_type],
            "builtin": skill.builtin,
            "out_subdir": skill.out_subdir,
            "dir": str(root),
            "registered": skill.registered,
            "updated_at": (
                datetime.fromtimestamp(skill.skill_path.stat().st_mtime).isoformat(timespec="seconds")
                if skill.registered else None
            ),
            "files": files,
            "has_backup": self._backup(root).is_dir(),
        }

    def read_skill(self, name: str) -> str:
        path = self.get(name).skill_path
        if not path.is_file():
            raise FileNotFoundError(name)
        return path.read_text(encoding="utf-8", errors="replace")

    # --- 変更 ---------------------------------------------------------------------------

    def install(
        self, name: str, files: list[SkillFile], output_type: str | None = None, label: str | None = None,
    ) -> dict:
        """スキルを登録する（同じ名前があれば入れ替える）。追加のスキルは出力の種類と表示名も記録する。

        label を省略（None）すると、入れ替えでは前の表示名のまま。空文字なら表示名を消す。
        """
        checked = self._validate(files)
        with self._lock:
            entries = read_registry(self.app_dir)
            labels = read_labels(self.app_dir)
            if name in (DOC, COMMENT):
                skill = self.get(name)
                if output_type not in (None, skill.output_type):
                    raise ValueError(f"{skill.label}スキルの出力の種類は変えられません")
                if label:
                    raise ValueError(f"{skill.label}スキルの表示名は変えられません")
            else:
                validate_name(name)
                output_type = output_type or entries.get(name)
                if output_type not in OUTPUT_TYPES:
                    raise ValueError("出力の種類（文書・コメントだけ変更・コードも変更）を選んでください")
                if label is not None:
                    labels[name] = validate_label(label)
                skill = custom_skill(self.app_dir, name, output_type, labels.get(name, ""))
            self._write_files(skill.skill_dir, checked)
            if not skill.builtin and (entries.get(name) != output_type or label is not None):
                write_registry(self.app_dir, {**entries, name: output_type}, labels)
        return self.info(self.get(name))

    def set_label(self, name: str, label: str) -> dict:
        """追加のスキルの表示名だけを変える（SKILL.md はそのまま）。空文字なら表示名を消す。"""
        with self._lock:
            skill = self.get(name)
            if skill.builtin:
                raise ValueError(f"{skill.label}スキルの表示名は変えられません")
            entries = read_registry(self.app_dir)
            labels = read_labels(self.app_dir)
            labels[name] = validate_label(label)
            write_registry(self.app_dir, entries, labels)
        return self.info(self.get(name))

    def remove(self, name: str) -> None:
        """登録を外す（フォルダは .prev に移す）。設計書スキルは必須なので外せない。"""
        if name == DOC:
            raise ValueError("設計書スキルは必須のため削除できません（新しいスキルを登録すると入れ替わります）")
        with self._lock:
            skill = self.get(name)
            root = skill.skill_dir
            if root.is_dir():
                backup = self._backup(root)
                shutil.rmtree(backup, ignore_errors=True)
                root.rename(backup)
            if not skill.builtin:
                entries = read_registry(self.app_dir)
                entries.pop(name, None)
                write_registry(self.app_dir, entries, read_labels(self.app_dir))

    def restore(self, name: str) -> dict:
        """直前のスキル（.prev）に戻す。今のスキルは .prev に回る。"""
        with self._lock:
            root = self.get(name).skill_dir
            backup = self._backup(root)
            if not (backup / SKILL_FILE).is_file():
                raise FileNotFoundError("戻せる直前のスキルがありません")
            staging = root.with_name(f"{root.name}.restoring")
            shutil.rmtree(staging, ignore_errors=True)
            backup.rename(staging)
            self._swap_in(root, staging)
        return self.info(self.get(name))

    # --- 内部 ---------------------------------------------------------------------------

    @staticmethod
    def _backup(root: Path) -> Path:
        return root.with_name(f"{root.name}.prev")

    def _write_files(self, root: Path, checked: dict[str, bytes]) -> None:
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
