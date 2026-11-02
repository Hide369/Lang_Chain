"""テスト用の claude コマンドの代役。

受け取った引数・作業ディレクトリ・環境変数を FAKE_CLAUDE_DIR/calls.jsonl に記録し、
FAKE_CLAUDE_DIR/responses.json に用意した応答を呼ばれた順に返す。
"""

import json
import os
import sys
import time
from pathlib import Path

state_dir = Path(os.environ["FAKE_CLAUDE_DIR"])
calls_path = state_dir / "calls.jsonl"
record = {
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "base_url": os.environ.get("ANTHROPIC_BASE_URL"),
    "haiku_model": os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL"),
    "nonessential_traffic": os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"),
}
with calls_path.open("a", encoding="utf-8") as f:
    f.write(json.dumps(record, ensure_ascii=False) + "\n")

call_index = len(calls_path.read_text(encoding="utf-8").splitlines()) - 1
response = json.loads((state_dir / "responses.json").read_text(encoding="utf-8"))[call_index]

time.sleep(response.get("sleep", 0))
sys.stderr.write(response.get("stderr", ""))
# 標準出力はASCIIのみにして、Windowsのコンソール文字コードに左右されないようにする
sys.stdout.write(response["stdout"] if "stdout" in response else json.dumps(response["json"]))
sys.exit(response.get("exit", 0))
