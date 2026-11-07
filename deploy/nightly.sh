#!/usr/bin/env bash
# 夜間バッチ: Goリポジトリを最新化してから詳細設計書を生成する。
# systemd タイマー(godoc-nightly.timer)から起動される。手動で試すときは
#   GODOC_REPO=/srv/repos/myapp ./deploy/nightly.sh
set -euo pipefail

: "${GODOC_REPO:?環境変数 GODOC_REPO にGoリポジトリのパスを指定してください}"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
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
