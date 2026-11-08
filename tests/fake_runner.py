"""Web 画面のテスト用に runner の代わりに起動するスクリプト。

受け取った引数を表示し、FAKE_RUNNER_SLEEP 秒待って FAKE_RUNNER_EXIT で終了する。
"""

import os
import sys
import time

print("args:", " ".join(sys.argv[1:]), flush=True)
print("処理中: 日本語の出力", flush=True)
time.sleep(float(os.getenv("FAKE_RUNNER_SLEEP", "0")))
print("done", flush=True)
sys.exit(int(os.getenv("FAKE_RUNNER_EXIT", "0")))
