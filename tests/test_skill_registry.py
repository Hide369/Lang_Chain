from pathlib import Path

import pytest

from godoc_agent.skill_registry import (
    COMMENT_ONLY,
    DOCUMENT,
    REWRITE,
    find_skill,
    list_skills,
    read_registry,
    registry_path,
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
