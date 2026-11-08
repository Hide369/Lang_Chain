"""対象リポジトリを `git pull --ff-only` で最新にしてから runner を起動する。

Web 画面の「実行前に git pull」と定期実行で使う（deploy/nightly.sh と同じ流れ）。
- pull の前に、対象フォルダがクローンしたリポジトリの最上位フォルダであることを確かめ、pull 先をログに出す
  （上位の別のリポジトリを pull してしまうのを防ぐため。git_repo.py を参照）
- 古いソースから設計書を作らないよう、確認や git pull に失敗したら runner は起動しない

使い方:
    python -m godoc_agent.web.pull_and_run <リポジトリ> -- <runner のコマンド...>
"""

import os
import subprocess
import sys
from pathlib import Path

from .git_repo import GitRepoError, inspect_repo

# 設定・接続エラーと同じ終了コード（runner の終了コード 2 と揃える）
EXIT_PULL_FAILED = 2


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--":
        print("使い方: python -m godoc_agent.web.pull_and_run <リポジトリ> -- <コマンド...>", file=sys.stderr)
        return EXIT_PULL_FAILED
    repo, cmd = Path(argv[0]), argv[2:]

    try:
        info = inspect_repo(repo)
    except GitRepoError as e:
        print(f"エラー: {e}。生成せずに終了します", flush=True)
        return EXIT_PULL_FAILED
    print(f"git pull --ff-only  {info.describe()}", flush=True)

    # 認証の入力待ちで止まらないよう、標準入力は渡さず、git にも尋ねさせない
    pull = subprocess.run(
        ["git", "-C", str(repo), "pull", "--ff-only"],
        stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if pull.returncode != 0:
        print(
            f"エラー: git pull に失敗したため、生成せずに終了します（終了コード {pull.returncode}。"
            "古いソースから設計書を作らないため）",
            flush=True,
        )
        return EXIT_PULL_FAILED
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    print(f"commit: {head.stdout.strip() or '不明'}", flush=True)
    return subprocess.call(cmd, stdin=subprocess.DEVNULL)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
