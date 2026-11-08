"""登録済みのスキル（タスク）の一覧。runner と Web 画面で共通に使う。

組み込みのスキルは2つで、置き場所と出力先はこれまでどおり。
  doc     … 詳細設計書（文書）。CLAUDE_SKILL_PATH、未設定なら skills/design-doc-batch/SKILL.md。出力は --out 直下
  comment … コメント追記。CLAUDE_COMMENT_SKILL_PATH、未設定なら skills/comment-batch/SKILL.md。出力は --out/_commented
それ以外のスキルは skills/registry.json に名前と出力の種類を登録し、skills/<名前>/SKILL.md に置く。
出力は --out/_<名前>/ の下（文書は <相対パス>.md、ソースは <相対パス>）。
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import COMMENT_SKILL_NAME, DESIGN_SKILL_NAME, comment_skill_path, design_skill_path

DOC = "doc"
COMMENT = "comment"
BUILTIN_NAMES = (DOC, COMMENT)

# 出力の種類
DOCUMENT = "document"  # ファイルごとの文書（Markdown）。前回の出力を引き継いで改訂させる
COMMENT_ONLY = "comment"  # コメントだけを変えたソース全文。コメント以外の変更は不合格
REWRITE = "rewrite"  # コードも変えたソース全文。本文が空でないことだけを確かめ、取り込みは人が判断する
OUTPUT_TYPES = (DOCUMENT, COMMENT_ONLY, REWRITE)
OUTPUT_TYPE_LABELS = {DOCUMENT: "文書（Markdown）", COMMENT_ONLY: "コメントだけ変更したソース", REWRITE: "コードも変更したソース"}

REGISTRY_FILE = "registry.json"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# 組み込みのスキル名・フォルダ名・出力先の予約名とは重ならないようにする
_RESERVED = {DOC, COMMENT, DESIGN_SKILL_NAME, COMMENT_SKILL_NAME, "runs", "commented"}


@dataclass(frozen=True)
class SkillDef:
    name: str  # タスク名（--tasks に書く名前）
    output_type: str
    skill_path: Path  # SKILL.md
    out_subdir: str  # --out からの相対フォルダ（"" は --out 直下）
    builtin: bool

    @property
    def skill_dir(self) -> Path:
        return self.skill_path.parent

    @property
    def label(self) -> str:
        return {DOC: "設計書", COMMENT: "コメント追記"}.get(self.name, self.name)

    @property
    def registered(self) -> bool:
        return self.skill_path.is_file()


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"スキル名は英小文字・数字・-・_ の40文字以内で、英小文字か数字で始めてください: {name!r}")
    if name in _RESERVED:
        raise ValueError(f"スキル名 {name!r} は予約されているため使えません")
    return name


def skills_root(app_dir: Path) -> Path:
    return app_dir / "skills"


def registry_path(app_dir: Path) -> Path:
    return skills_root(app_dir) / REGISTRY_FILE


def read_registry(app_dir: Path) -> dict[str, str]:
    """追加のスキル名 → 出力の種類。ファイルがなければ空。"""
    path = registry_path(app_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        skills = data["skills"]
        result = {}
        for name, entry in skills.items():
            validate_name(name)
            if entry.get("type") not in OUTPUT_TYPES:
                raise ValueError(f"{name} の type が正しくありません: {entry.get('type')!r}")
            result[name] = entry["type"]
        return result
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError, ValueError) as e:
        raise ValueError(f"スキルの登録ファイルが壊れています: {path}: {e}") from e


def write_registry(app_dir: Path, entries: dict[str, str]) -> None:
    path = registry_path(app_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"skills": {name: {"type": entries[name]} for name in sorted(entries)}}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def custom_skill(app_dir: Path, name: str, output_type: str) -> SkillDef:
    return SkillDef(
        name=name, output_type=output_type, skill_path=skills_root(app_dir) / name / "SKILL.md",
        out_subdir=f"_{name}", builtin=False,
    )


def list_skills(app_dir: Path) -> list[SkillDef]:
    """組み込み（SKILL.md がまだなくても含める）と、登録ファイルにある追加のスキル。"""
    comment = comment_skill_path(app_dir) or skills_root(app_dir) / COMMENT_SKILL_NAME / "SKILL.md"
    skills = [
        SkillDef(DOC, DOCUMENT, design_skill_path(app_dir), "", builtin=True),
        SkillDef(COMMENT, COMMENT_ONLY, comment, "_commented", builtin=True),
    ]
    skills += [custom_skill(app_dir, name, t) for name, t in sorted(read_registry(app_dir).items())]
    return skills


def find_skill(app_dir: Path, name: str) -> SkillDef:
    for skill in list_skills(app_dir):
        if skill.name == name:
            return skill
    raise ValueError(f"スキル {name!r} は登録されていません")
