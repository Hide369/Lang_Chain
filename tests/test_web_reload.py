import logging
import os

from godoc_agent.web.__main__ import reload_env


def test_reload_overrides_values_loaded_at_startup(monkeypatch, tmp_path, caplog):
    # 起動時に読んだ古い値（Ollama 時代の設定）が残っていても、.env の新しい値で上書きする
    monkeypatch.setenv("LLM_MODEL", "qwen3-coder-next")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:11434")
    env = tmp_path / ".env"
    env.write_text("LLM_MODEL=claude-sonnet-4-5\nLLM_BASE_URL=http://127.0.0.1:8000\n", encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="godoc_agent.web"):
        reload_env(env)

    assert os.environ["LLM_MODEL"] == "claude-sonnet-4-5"
    assert os.environ["LLM_BASE_URL"] == "http://127.0.0.1:8000"
    assert "読み直しました" in caplog.text


def test_reload_reports_invalid_settings_without_raising(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("LLM_NUM_CTX", "262144")
    env = tmp_path / ".env"
    env.write_text("LLM_NUM_CTX=abc\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="godoc_agent.web"):
        reload_env(env)  # Web 画面は止めない

    assert "LLM_NUM_CTX" in caplog.text
