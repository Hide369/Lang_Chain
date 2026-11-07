from pathlib import Path

import pytest

from godoc_agent import config
from godoc_agent.config import load_settings, targets_config_path

REPO_ROOT = Path(config.__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clear_envs(monkeypatch):
    # 実行環境の .env や環境変数に左右されないようにする
    for name in ("TARGETS_CONFIG", "CLAUDE_SKILL_PATH"):
        monkeypatch.delenv(name, raising=False)


def test_design_skill_path_is_fixed_to_design_doc_batch():
    assert load_settings().claude_skill_path == REPO_ROOT / "skills" / "design-doc-batch" / "SKILL.md"


def test_design_skill_path_can_still_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_SKILL_PATH", str(tmp_path / "SKILL.md"))

    assert load_settings().claude_skill_path == tmp_path / "SKILL.md"


def test_targets_config_prefers_env_then_local_file_then_example(monkeypatch, tmp_path):
    assert targets_config_path(tmp_path) == tmp_path / "targets.example.toml"

    (tmp_path / "targets.toml").write_text("", encoding="utf-8")
    assert targets_config_path(tmp_path) == tmp_path / "targets.toml"

    monkeypatch.setenv("TARGETS_CONFIG", str(tmp_path / "custom.toml"))
    assert targets_config_path(tmp_path) == tmp_path / "custom.toml"


def test_example_targets_config_keeps_previous_go_behavior(monkeypatch):
    monkeypatch.setenv("TARGETS_CONFIG", str(REPO_ROOT / "targets.example.toml"))

    targets = load_settings().targets

    assert [lang.name for lang in targets.languages] == ["go"]  # C#・TypeScript は例として無効で載っている


def test_missing_targets_config_fails_at_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("TARGETS_CONFIG", str(tmp_path / "none.toml"))

    with pytest.raises(FileNotFoundError, match="none.toml"):
        load_settings()
