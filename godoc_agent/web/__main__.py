"""Web 画面を起動する。

使い方:
    .venv/bin/python -m godoc_agent.web
接続先・ポートは .env の WEB_HOST / WEB_PORT（既定 127.0.0.1:4000）。

`systemctl reload godoc-web`（SIGHUP）で .env を読み直す。実行中のバッチは止めずに、
次に起動するバッチ（LLM_* / CLAUDE_* などの設定）から反映する。WEB_* の変更は restart が必要。
"""

import logging
import signal
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from ..config import load_settings
from .app import create_app
from .settings import APP_DIR, load_web_settings

ENV_PATH = APP_DIR / ".env"
logger = logging.getLogger("godoc_agent.web")


def reload_env(env_path: Path = ENV_PATH) -> None:
    """.env を読み直して環境変数を上書きする。runner は Web 画面の環境変数を引き継いで起動するため、次の実行から反映される。"""
    # 起動時は既存の環境変数を優先して読むため、override しないと .env の変更が反映されない
    load_dotenv(env_path, override=True)
    try:
        # 誤りは実行時ではなく、読み直した時点で知らせる（Web 画面自体は動かし続ける）
        load_settings()
    except (ValueError, OSError) as e:
        logger.error(".env を読み直しましたが、設定に誤りがあります（次の実行は失敗します）: %s", e)
        return
    logger.info(".env を読み直しました（次の実行から反映。WEB_* の変更は restart が必要）")


def _install_reload_handler() -> None:
    if not hasattr(signal, "SIGHUP"):  # Windows には SIGHUP がない
        return
    signal.signal(signal.SIGHUP, lambda signum, frame: reload_env())


def main() -> int:
    load_dotenv(ENV_PATH)
    # 定期実行の開始・失敗を journal（systemd）に残す
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_web_settings()
        app = create_app(settings)
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    print(f"フォルダ選択の起点={', '.join(map(str, settings.browse_roots))} 設定の保存先={settings.state_dir}", flush=True)
    # uvicorn は SIGINT・SIGTERM だけを扱うので、SIGHUP は先に登録しておけば上書きされない
    _install_reload_handler()
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
