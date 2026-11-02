"""環境変数から設定を読み込む。値の検証はここで行い、起動時に失敗させる。"""

import os
from dataclasses import dataclass
from pathlib import Path


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"環境変数 {name} は整数で指定してください: {raw!r}") from e
    if value < minimum:
        raise ValueError(f"環境変数 {name} は {minimum} 以上で指定してください: {value}")
    return value


@dataclass(frozen=True)
class Settings:
    ollama_base_url: str
    model: str
    num_ctx: int
    num_predict: int
    timeout_s: int
    concurrency: int
    max_attempts: int
    template_path: Path
    checkpoint_db: Path
    # --engine claude-code 用
    claude_bin: str
    claude_skill_path: Path | None
    claude_max_turns: int
    claude_timeout_s: int

    @property
    def max_source_chars(self) -> int:
        # Ollamaはコンテキスト超過を「黙って切り捨てる」ため、送る前に弾く。
        # 1トークン≒3文字（Goコード+日本語の保守的な見積り）とし、
        # テンプレート・出力分として半分をLLMの作業領域に残す。
        return self.num_ctx * 3 // 2


def load_settings() -> Settings:
    base_dir = Path(__file__).resolve().parent.parent
    return Settings(
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        model=os.getenv("LLM_MODEL", "qwen3-coder-next"),
        num_ctx=_int_env("LLM_NUM_CTX", 65536, minimum=4096),
        num_predict=_int_env("LLM_NUM_PREDICT", 8192),
        timeout_s=_int_env("LLM_TIMEOUT_S", 600),
        concurrency=_int_env("LLM_CONCURRENCY", 2),
        max_attempts=_int_env("DOC_MAX_ATTEMPTS", 3),
        template_path=Path(os.getenv("DOC_TEMPLATE_PATH", base_dir / "prompts" / "design_doc_template.md")),
        checkpoint_db=Path(os.getenv("CHECKPOINT_DB", base_dir / "state" / "checkpoints.sqlite")),
        claude_bin=os.getenv("CLAUDE_BIN", "claude"),
        claude_skill_path=Path(skill) if (skill := os.getenv("CLAUDE_SKILL_PATH")) else None,
        claude_max_turns=_int_env("CLAUDE_MAX_TURNS", 30),
        # エージェントはファイルを読みながら複数回LLMを呼ぶため、1回の呼び出しより長く取る
        claude_timeout_s=_int_env("CLAUDE_TIMEOUT_S", 1800),
    )
