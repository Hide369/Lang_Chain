"""Web 画面を起動する。

使い方:
    .venv/bin/python -m godoc_agent.web
接続先・ポートは .env の WEB_HOST / WEB_PORT（既定 127.0.0.1:4000）。
"""

import logging
import sys

import uvicorn
from dotenv import load_dotenv

from .app import create_app
from .settings import APP_DIR, load_web_settings


def main() -> int:
    load_dotenv(APP_DIR / ".env")
    # 定期実行の開始・失敗を journal（systemd）に残す
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_web_settings()
        app = create_app(settings)
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    print(f"フォルダ選択の起点={', '.join(map(str, settings.browse_roots))} 設定の保存先={settings.state_dir}", flush=True)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
