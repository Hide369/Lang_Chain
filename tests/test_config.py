from pathlib import Path

import pytest

from godoc_agent import config
from godoc_agent.config import DEFAULT_CONCURRENCY, MAX_CONCURRENCY, comment_skill_path, load_settings, targets_config_path

REPO_ROOT = Path(config.__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clear_envs(monkeypatch):
    # 実行環境の .env や環境変数に左右されないようにする
    for name in ("TARGETS_CONFIG", "CLAUDE_SKILL_PATH", "LLM_BASE_URL", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def test_design_skill_path_is_fixed_to_design_doc_batch():
    assert load_settings().claude_skill_path == REPO_ROOT / "skills" / "design-doc-batch" / "SKILL.md"


def test_design_skill_path_can_still_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_SKILL_PATH", str(tmp_path / "SKILL.md"))

    assert load_settings().claude_skill_path == tmp_path / "SKILL.md"


def test_llm_base_url_defaults_to_vllm_on_ollama_port():
    # Ollama から vLLM に移行したサーバーでは、vLLM を Ollama と同じ 11434 番ポートで待ち受けている
    assert load_settings().llm_base_url == "http://127.0.0.1:11434"


def test_llm_base_url_is_read_only_from_llm_base_url(monkeypatch):
    # 設定は .env の LLM_BASE_URL に一本化する（Ollama 時代の OLLAMA_BASE_URL は読まない）
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9999")
    assert load_settings().llm_base_url == "http://127.0.0.1:11434"

    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:8000")
    assert load_settings().llm_base_url == "http://127.0.0.1:8000"


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


def test_comment_skill_is_used_when_placed_in_default_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_COMMENT_SKILL_PATH", raising=False)
    assert comment_skill_path(tmp_path) is None

    default = tmp_path / "skills" / "comment-batch" / "SKILL.md"
    default.parent.mkdir(parents=True)
    default.write_text("# skill\n", encoding="utf-8")
    assert comment_skill_path(tmp_path) == default

    # .env での指定があれば、そちらを優先する
    monkeypatch.setenv("CLAUDE_COMMENT_SKILL_PATH", str(tmp_path / "other" / "SKILL.md"))
    assert comment_skill_path(tmp_path) == tmp_path / "other" / "SKILL.md"


def test_session_count_defaults_to_server_wide_limit_of_ten():
    assert MAX_CONCURRENCY == 10
    assert DEFAULT_CONCURRENCY == MAX_CONCURRENCY


def test_obsolete_llm_concurrency_in_env_is_ignored(monkeypatch):
    # LLM_CONCURRENCY は廃止した。古い .env に残っていても起動を妨げない
    monkeypatch.setenv("LLM_CONCURRENCY", "abc")
    assert not hasattr(load_settings(), "concurrency")


def test_model_and_unlimited_turns_and_timeout_defaults(monkeypatch):
    for name in ("LLM_MODEL", "CLAUDE_MAX_TURNS", "CLAUDE_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert settings.model == "claude-opus-4-6"
    # 事実上の無制限
    assert settings.claude_max_turns == 1_000_000
    assert settings.claude_timeout_s == 1_000_000
