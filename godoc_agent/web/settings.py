"""Web 画面の設定。環境変数（.env）から読み込み、誤りは起動時に検出する。

画面から変える設定（対象・出力先フォルダ、スキル、targets.toml）はここではなく、
workspace.py・skills.py・targets_editor.py がファイルに保存する。
"""

import os
from dataclasses import dataclass
from pathlib import Path

from ..config import _int_env

APP_DIR = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class WebSettings:
    admin_password_hash: str
    # フォルダ選択ダイアログで辿れる範囲。対象・出力先フォルダはこの下からしか選べない
    browse_roots: tuple[Path, ...]
    # 画面で選んだフォルダなどを保存する場所
    state_dir: Path
    host: str
    port: int
    session_ttl_s: int
    cookie_secure: bool
    view_requires_login: bool


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"環境変数 {name} は true / false で指定してください: {raw!r}")


def parse_browse_roots(raw: str) -> tuple[Path, ...]:
    """WEB_BROWSE_ROOTS（カンマ区切りの絶対パス）を読む。"""
    roots: list[Path] = []
    for item in (part.strip() for part in raw.split(",")):
        if not item:
            continue
        path = Path(item)
        if not path.is_absolute():
            raise ValueError(f"環境変数 WEB_BROWSE_ROOTS は絶対パスで指定してください: {item!r}")
        if not path.is_dir():
            raise ValueError(f"環境変数 WEB_BROWSE_ROOTS のフォルダがありません: {item!r}")
        roots.append(path.resolve())
    if not roots:
        raise ValueError("環境変数 WEB_BROWSE_ROOTS に、画面から選べるフォルダの起点を1つ以上指定してください（カンマ区切り）")
    return tuple(dict.fromkeys(roots))


def load_web_settings() -> WebSettings:
    password_hash = os.getenv("WEB_ADMIN_PASSWORD_HASH", "").strip()
    if not password_hash:
        raise ValueError(
            "環境変数 WEB_ADMIN_PASSWORD_HASH が未設定です。"
            "`.venv/bin/python -m godoc_agent.web.password` で作成した値を .env に書いてください"
        )
    return WebSettings(
        admin_password_hash=password_hash,
        browse_roots=parse_browse_roots(os.getenv("WEB_BROWSE_ROOTS", "")),
        state_dir=Path(os.getenv("WEB_STATE_DIR") or APP_DIR / "state" / "web").resolve(),
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        port=_int_env("WEB_PORT", 4000),
        session_ttl_s=_int_env("WEB_SESSION_TTL_HOURS", 12) * 3600,
        cookie_secure=_bool_env("WEB_COOKIE_SECURE", False),
        view_requires_login=_bool_env("WEB_VIEW_REQUIRES_LOGIN", False),
    )
