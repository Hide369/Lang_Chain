"""管理者パスワードのハッシュを作り、.env に書く行を表示する。

使い方:
    .venv/bin/python -m godoc_agent.web.password
表示された `WEB_ADMIN_PASSWORD_HASH=...` を .env に追記し、Web 画面を再起動する。
"""

import getpass
import sys

from .auth import hash_password

MIN_LENGTH = 12


def main() -> int:
    password = getpass.getpass("管理者パスワード: ")
    if len(password) < MIN_LENGTH:
        print(f"エラー: パスワードは {MIN_LENGTH} 文字以上にしてください", file=sys.stderr)
        return 2
    if getpass.getpass("もう一度入力: ") != password:
        print("エラー: 2回の入力が一致しません", file=sys.stderr)
        return 2
    print(f"WEB_ADMIN_PASSWORD_HASH={hash_password(password)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
