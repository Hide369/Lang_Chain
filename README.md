# Lang_Chain

Goソースファイルから詳細設計書(Markdown)を一括生成するLangGraphワークフロー（PoC）。
GB10サーバー上のOllama（qwen3-coder-next）で動かし、クライアントのClaude CodeからSSH経由で起動する。

## 設計方針（方式A：1ファイル = 1セッション）

- Goファイル1本ごとに独立した `thread_id` でグラフを実行する。前のファイルのコンテキストは一切持ち込まない
- `thread_id = <run_id>:<パスのハッシュ>`。同じ `--run-id` で再実行すると、失敗・中断したファイルだけがチェックポイントから再開される
- ソースのハッシュを `out/.manifest.json` に記録し、変更のないファイルは次回以降スキップする

```
prepare ──> generate ──> validate ──> done
   │            ▲            │
   │            └── retry ───┤（必須セクション不足・公開識別子の記載漏れ・出力の途中終了）
   └─> too_large             └─> failed_validation / context_overflow
```

| ファイル | 役割 |
|---|---|
| [godoc_agent/doc_graph.py](godoc_agent/doc_graph.py) | 1ファイル分のグラフ（生成・検証・再試行） |
| [godoc_agent/runner.py](godoc_agent/runner.py) | バッチ実行、並列数制御、差分スキップ、再開、計測ログ |
| [godoc_agent/go_source.py](godoc_agent/go_source.py) | Goファイル列挙、関数・型などの事実抽出（簡易版） |
| [godoc_agent/llm.py](godoc_agent/llm.py) | Ollama接続、リトライ、事前チェック。推論エンジンを変える場合はここだけ差し替える |
| [prompts/design_doc_template.md](prompts/design_doc_template.md) | 設計書テンプレート（**既存スキルの内容に置き換える**） |

## サーバー(GB10)へのセットアップ

```bash
# 1. Ollama の設定（systemd）
sudo systemctl edit ollama
#   [Service]
#   Environment="OLLAMA_CONTEXT_LENGTH=65536"
#   Environment="OLLAMA_KEEP_ALIVE=-1"
#   Environment="OLLAMA_NUM_PARALLEL=2"
#   Environment="OLLAMA_MAX_LOADED_MODELS=1"
#   Environment="OLLAMA_FLASH_ATTENTION=1"
sudo systemctl daemon-reload && sudo systemctl restart ollama
ollama list          # モデルのタグ名を確認

# 2. このフォルダを配置して依存関係をインストール
cd /opt/Lang_Chain
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # LLM_MODEL などを編集

# 3. テスト（Ollama不要）
pytest
```

## 使い方

```bash
# PoC: まず5本だけ
python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --limit 5

# 全件（夜間バッチ）
python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --run-id nightly-20261102

# 途中で止まった・失敗があった場合は同じ run-id で再実行
python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --run-id nightly-20261102
```

終了コード: `0` 全件成功 / `1` 一部失敗（詳細は計測ログ） / `2` 設定・接続エラーで開始できず

### クライアント(Claude Code)からの起動

```powershell
ssh gb10 "cd /opt/Lang_Chain && .venv/bin/python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --limit 5"
```

## 計測（PoCの判断材料）

`out/_runs/<run_id>.jsonl` に1ファイル1行で記録され、終了時にサマリーが表示される。

- `duration_s`：1ファイルの処理時間（並列待ちを除く）
- `llm_calls[].tokens_per_s` / `prompt_tokens` / `done_reason`：Ollamaの性能と切り捨ての兆候
- `status`：`done` / `failed_validation` / `too_large` / `context_overflow` / `error`

## 既知の制約（技術的負債）

- **事実抽出は正規表現による簡易版**。複雑な宣言を取りこぼす可能性がある。本番では `go/ast` を使う補助ツールに置き換える
- 文字数による上限チェックは「1トークン≒3文字」の概算。実際の切り捨ては `prompt_tokens` から `context_overflow` として検出する
- パッケージ単位の集約、人による承認（interrupt）、ツールを使うエージェントは未実装（次のフェーズ）
- LangSmithのトレースはソースコードを外部に送信するため、既定では無効
