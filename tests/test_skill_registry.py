from pathlib import Path

import pytest

from godoc_agent.skill_registry import (
    COMMENT_ONLY,
    DOCUMENT,
    REWRITE,
    find_skill,
    list_skills,
    read_labels,
    read_registry,
    registry_path,
    validate_label,
    write_registry,
)


@pytest.fixture(autouse=True)
def clear_envs(monkeypatch):
    monkeypatch.delenv("CLAUDE_SKILL_PATH", raising=False)
    monkeypatch.delenv("CLAUDE_COMMENT_SKILL_PATH", raising=False)


def test_builtin_skills_keep_their_locations_and_outputs(tmp_path: Path):
    doc, comment = list_skills(tmp_path)
    assert (doc.name, doc.output_type, doc.out_subdir) == ("doc", DOCUMENT, "")
    assert doc.skill_path == tmp_path / "skills" / "design-doc-batch" / "SKILL.md"
    assert (comment.name, comment.output_type, comment.out_subdir) == ("comment", COMMENT_ONLY, "_commented")
    assert comment.skill_path == tmp_path / "skills" / "comment-batch" / "SKILL.md"
    assert not doc.registered


def test_registered_skills_are_listed_with_their_output_folder(tmp_path: Path):
    write_registry(tmp_path, {"test-spec": DOCUMENT, "fmt": REWRITE})
    assert read_registry(tmp_path) == {"test-spec": DOCUMENT, "fmt": REWRITE}
    fmt = find_skill(tmp_path, "fmt")
    assert fmt.skill_path == tmp_path / "skills" / "fmt" / "SKILL.md"
    assert fmt.out_subdir == "_fmt"
    assert [s.name for s in list_skills(tmp_path)] == ["doc", "comment", "fmt", "test-spec"]


def test_unknown_skill_is_reported(tmp_path: Path):
    with pytest.raises(ValueError, match="lint.*登録されていません"):
        find_skill(tmp_path, "lint")


@pytest.mark.parametrize(
    "content",
    ["not json", '{"skills": {"fmt": {"type": "unknown"}}}', '{"skills": {"Bad Name": {"type": "document"}}}', "{}"],
)
def test_broken_registry_is_reported(tmp_path: Path, content: str):
    path = registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="登録ファイルが壊れています"):
        read_registry(tmp_path)


def test_display_name_is_used_as_label_and_kept_in_registry(tmp_path: Path):
    write_registry(tmp_path, {"test-spec": DOCUMENT, "fmt": REWRITE}, {"test-spec": "テスト仕様書"})
    assert read_labels(tmp_path) == {"test-spec": "テスト仕様書"}
    # 種類の読み出しは表示名があっても変わらない
    assert read_registry(tmp_path) == {"test-spec": DOCUMENT, "fmt": REWRITE}
    spec, fmt = find_skill(tmp_path, "test-spec"), find_skill(tmp_path, "fmt")
    assert (spec.label, spec.display_name, spec.skill_dir.name) == ("テスト仕様書", "テスト仕様書", "test-spec")
    # 表示名がなければスキル名を表示する
    assert fmt.label == "fmt"


def test_registry_without_labels_still_reads(tmp_path: Path):
    path = registry_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"skills": {"fmt": {"type": "rewrite"}}}', encoding="utf-8")
    assert read_labels(tmp_path) == {}
    assert find_skill(tmp_path, "fmt").label == "fmt"


@pytest.mark.parametrize("label, error", [("あ" * 41, "40 文字"), ("改\n行", "制御文字")])
def test_invalid_display_name_is_rejected(label: str, error: str):
    with pytest.raises(ValueError, match=error):
        validate_label(label)


def test_display_name_is_trimmed():
    assert validate_label("  テスト仕様書 ") == "テスト仕様書"
