#!/usr/bin/env bash
# 夜間バッチ: Goリポジトリを最新化してから詳細設計書を生成する。
# systemd タイマー(godoc-nightly.timer)から起動される。設定（GODOC_REPO・GODOC_OUT を含む）は
# すべて <このフォルダ>/.env に書く。手動で試すときは、環境変数で一時的に上書きもできる
#   GODOC_REPO=/srv/repos/myapp ./deploy/nightly.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# .env の GODOC_REPO・GODOC_OUT を読む（runner と同じく python-dotenv で解釈し、すでにある環境変数を優先する）。
# `source .env` にしないのは、.env の書き方（引用符・# のコメント）を runner と同じ解釈にそろえるため
if [[ -f "$APP_DIR/.env" ]]; then
  # 代入で受けてから eval する（読み込みに失敗したとき set -e で止めるため。eval "$(...)" だと失敗が隠れる）
  env_exports="$("$APP_DIR/.venv/bin/python" - "$APP_DIR/.env" <<'PY'
import os
import shlex
import sys

from dotenv import dotenv_values

for key in ("GODOC_REPO", "GODOC_OUT"):
    value = dotenv_values(sys.argv[1]).get(key)
    if value and not os.environ.get(key):
        print(f"export {key}={shlex.quote(value)}")
PY
)"
  eval "$env_exports"
fi

: "${GODOC_REPO:?.env の GODOC_REPO に対象リポジトリのパスを指定してください}"
OUT_DIR="${GODOC_OUT:-$APP_DIR/out}"
# 日付単位のrun_id（ログのファイル名）。同じ日に再実行すると、完了済みを飛ばして中断・失敗したファイルを最初から処理する
RUN_ID="nightly-$(date +%Y%m%d)"

mkdir -p "$OUT_DIR/_runs"
# 出力は journal（systemd）とファイルの両方に残す
exec > >(tee -a "$OUT_DIR/_runs/$RUN_ID.log") 2>&1

echo "=== $(date '+%F %T') start run_id=$RUN_ID repo=$GODOC_REPO out=$OUT_DIR"
# 古いソースから設計書を作らないよう、更新に失敗したら生成せずに終了する
git -C "$GODOC_REPO" pull --ff-only
echo "commit: $(git -C "$GODOC_REPO" rev-parse --short HEAD)"

cd "$APP_DIR"
status=0
.venv/bin/python -m godoc_agent.runner --repo "$GODOC_REPO" --out "$OUT_DIR" --run-id "$RUN_ID" || status=$?
echo "=== $(date '+%F %T') end exit=$status"
exit "$status"
