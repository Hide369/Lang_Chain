"""環境変数と対象の設定ファイルから設定を読み込む。値の検証はここで行い、起動時に失敗させる。"""

import os
from dataclasses import dataclass
from pathlib import Path

from .targets import TargetConfig, load_targets


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as e:
        raise ValueError(f"環境変数 {name} は整数で指定してください: {raw!r}") from e
    if value < minimum:
        raise ValueError(f"環境変数 {name} は {minimum} 以上で指定してください: {value}")
    return value


def _ratio_env(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as e:
        raise ValueError(f"環境変数 {name} は数値で指定してください: {raw!r}") from e
    if not 0 < value <= 1:  # NaN もここで弾かれる
        raise ValueError(f"環境変数 {name} は 0 より大きく 1 以下で指定してください: {raw!r}")
    return value


# 設計書スキルはすべての言語で共通。置き場所を固定し、.env での指定を不要にする
DESIGN_SKILL_NAME = "design-doc-batch"


def targets_config_path(base_dir: Path) -> Path:
    """対象の設定ファイル。TARGETS_CONFIG > targets.toml > targets.example.toml の順に使う。"""
    if raw := os.getenv("TARGETS_CONFIG"):
        return Path(raw)
    local = base_dir / "targets.toml"
    # 設定をコピーしていない環境でも、例の設定（従来と同じ Go の対象）のまま動くようにする
    return local if local.is_file() else base_dir / "targets.example.toml"


@dataclass(frozen=True)
class Settings:
    ollama_base_url: str
    model: str
    num_ctx: int
    ctx_warn_ratio: float
    num_predict: int
    concurrency: int
    max_attempts: int
    checkpoint_db: Path
    claude_bin: str
    claude_skill_path: Path
    claude_comment_skill_path: Path | None
    claude_max_turns: int
    claude_timeout_s: int
    targets: TargetConfig

    @property
    def max_source_chars(self) -> int:
        # Ollamaはコンテキスト超過を「黙って切り捨てる」ため、大きすぎるファイルは送る前に弾く。
        # 1トークン≒3文字（Goコード+日本語の保守的な見積り）とし、Claude Code の指示文・
        # スキル・関連ファイル・出力の分として半分をエージェントの作業領域に残す。
        return self.num_ctx * 3 // 2

    @property
    def max_comment_source_chars(self) -> int:
        # コメント追記はファイル全文を出力するため、1回の応答の出力上限(LLM_NUM_PREDICT)にも収める。
        # 1トークン≒3文字とし、追記するコメントの分として出力上限の半分を残す
        return min(self.max_source_chars, self.num_predict * 3 // 2)

    @property
    def ctx_warn_tokens(self) -> float:
        # 1回の実行の入力トークン数がこれを超えたら、切り捨ての可能性として警告する
        return self.num_ctx * self.ctx_warn_ratio


def load_settings() -> Settings:
    base_dir = Path(__file__).resolve().parent.parent
    return Settings(
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        model=os.getenv("LLM_MODEL", "qwen3-coder-next"),
        num_ctx=_int_env("LLM_NUM_CTX", 65536, minimum=4096),
        ctx_warn_ratio=_ratio_env("LLM_CTX_WARN_RATIO", 0.9),
        num_predict=_int_env("LLM_NUM_PREDICT", 8192),
        concurrency=_int_env("LLM_CONCURRENCY", 2),
        max_attempts=_int_env("DOC_MAX_ATTEMPTS", 3),
        checkpoint_db=Path(os.getenv("CHECKPOINT_DB", base_dir / "state" / "checkpoints.sqlite")),
        claude_bin=os.getenv("CLAUDE_BIN", "claude"),
        claude_skill_path=Path(
            os.getenv("CLAUDE_SKILL_PATH") or base_dir / "skills" / DESIGN_SKILL_NAME / "SKILL.md"
        ),
        claude_comment_skill_path=Path(skill) if (skill := os.getenv("CLAUDE_COMMENT_SKILL_PATH")) else None,
        claude_max_turns=_int_env("CLAUDE_MAX_TURNS", 30),
        # エージェントはファイルを読みながら複数回LLMを呼ぶため、長めに取る
        claude_timeout_s=_int_env("CLAUDE_TIMEOUT_S", 1800),
        targets=load_targets(targets_config_path(base_dir)),
    )
