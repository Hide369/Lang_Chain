# Lang_Chain

Goソースファイルから詳細設計書(Markdown)を一括生成するLangGraphワークフロー（PoC）。
GB10サーバー上で動かし、クライアントのClaude CodeからSSH経由で起動する。

設計書は、サーバー上の **Claude Code（`claude -p`）がエージェントとして**、既存の **SKILL.md をそのまま使って**書く。
モデルは Ollama の Anthropic 互換API経由で qwen3-coder-next を使うため、ソースコードはサーバーの外に出ない。
LangGraph は、その外側で並列制御・差分スキップ・再開・検証・計測を受け持つ。

## 設計方針（方式A：1ファイル = 1セッション）

- Goファイル1本ごとに独立した `thread_id` でグラフを実行する。前のファイルのコンテキストは一切持ち込まない
- `thread_id = <run_id>:<パスのハッシュ>`。同じ `--run-id` で再実行すると、失敗・中断したファイルだけがチェックポイントから再開される
- ソースのハッシュを `out/.manifest.json` に記録し、変更のないファイルは次回以降スキップする
- 設計書の構成・書き方は SKILL.md に任せる。検証するのは、本文が空でないことと、公開識別子（静的解析で抽出）がすべて記載されていることだけ
- 設計書の対象は **`<xx>_api/pkg/handler/` 配下（サブディレクトリを含む）のGoファイルだけ**（`xx` は任意）
  - `--repo` には、複数の `xx_api` を含む親ディレクトリと、`xx_api` 自体のどちらを指定してもよい（複数の API を扱うなら親ディレクトリを推奨。「3. 設計書を取得する」の注意を参照）
  - 例：対象 `user_api/pkg/handler/user.go`、`user_api/pkg/handler/admin/role.go` ／ 対象外 `user_api/pkg/model/user.go`、`user_api/internal/pkg/handler/x.go`
- 上記の配下でも、次のファイルは対象外（常に除外され、オプションで含めることはできない）
  - テストコード（ファイル名が `_test.go` で終わるもの）
  - `vendor/`、`testdata/`、`node_modules/`、`.` で始まるディレクトリ配下のファイル

```
prepare ──> generate ──> validate ──> done
   │            ▲            │
   │            └── retry ───┤（本文が空・公開識別子の記載漏れ → 同じセッションを --resume して指摘だけ送る）
   └─> too_large             └─> failed_validation

generate の中身（ファイルごと）:
  claude -p "<対象ファイル>の詳細設計書を作成"
    ├─ システムプロンプト: SKILL.md の全文 + バッチ用ルール
    ├─ ツール: Read / Glob / Grep のみ（作業ディレクトリ = --repo）
    └─ モデル: Ollama(qwen3-coder-next)
```

| ファイル | 役割 |
|---|---|
| [godoc_agent/doc_graph.py](godoc_agent/doc_graph.py) | 1ファイル分のグラフ（生成・検証・再試行） |
| [godoc_agent/runner.py](godoc_agent/runner.py) | バッチ実行、並列数制御、差分スキップ、再開、計測ログ |
| [godoc_agent/claude_code.py](godoc_agent/claude_code.py) | `claude -p` の起動、Ollama への接続設定、セッションの継続（`--resume`） |
| [godoc_agent/prompts.py](godoc_agent/prompts.py) | SKILL.md の読み込みと、エージェントへの指示の組み立て |
| [godoc_agent/go_source.py](godoc_agent/go_source.py) | 対象Goファイルの列挙、関数・型などの事実抽出（簡易版） |
| [godoc_agent/preflight.py](godoc_agent/preflight.py) | 開始前の Ollama 接続・モデル存在チェック |

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

# 2. Claude Code をインストール
curl -fsSL https://claude.ai/install.sh | bash
which claude         # tmux / nohup から起動する場合は、このフルパスを CLAUDE_BIN に指定する

# 3. このフォルダを配置して依存関係をインストール
cd /opt/Lang_Chain
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # CLAUDE_SKILL_PATH・LLM_MODEL などを編集

# 4. 既存のスキルをサーバーに置く（クライアントから実行。参照ファイルがあればフォルダごと）
scp -r ~/.claude/skills/<スキル名> gb10:/opt/Lang_Chain/skills/

# 5. テスト（Ollama・Claude Code 不要）
pytest
```

Ollama の接続先（`ANTHROPIC_BASE_URL` など）は runner が `claude` の起動時に設定するため、シェルに設定する必要はない。
テレメトリなどモデル以外への通信も `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` で止めている。

### SKILL.md の扱い

- SKILL.md の全文をシステムプロンプトに入れる。Claude Code のスキル検出には頼らないため、スキルの置き場所は問わない（`CLAUDE_SKILL_PATH` で指定）
- SKILL.md と同じフォルダ（`references/` など）は `--add-dir` で読めるようにしている
- 次のルールをスキルより優先させている：ファイルの作成・編集やコマンド実行をしない（保存は runner が行う）、設計書の本文だけを返す、質問しない
- 対象ファイルのパスは runner がプロンプトで渡す（スキル内の `$ARGUMENTS` などのプレースホルダーは置換されない）

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

## クライアント(Claude Code)からの運用

処理はすべてサーバー上で完結する。クライアントの役割は「Goソースの更新」「起動」「成果物の取得」だけ。

```
クライアント                         サーバー(GB10)
  Goソース ──(git push / rsync)──>  /srv/repos/myapp      ← --repo で読む
  ssh で起動 ───────────────────>  runner（tmux / nohup で常駐）── Claude Code ── Ollama
  設計書 <──(scp / git pull)──────  /opt/Lang_Chain/out   ← --out に書く
```

### 1. Goリポジトリをサーバーに置く

`--repo` はサーバー上のパスとして直接読み込まれる。クライアントからソースを送る機能はない。

置き場所は任意で、このREADMEの `/srv/repos/myapp` は例にすぎない。置いたパスを `--repo` に指定すればよい。

- `/srv` 配下は管理者権限が必要なため、先に実行ユーザーが書き込めるようにする（例：`sudo mkdir -p /srv/repos && sudo chown $USER /srv/repos`）。こだわりがなければ `~/repos/myapp` などホームディレクトリ配下が手軽
- runner はGoソースを読むだけで、リポジトリの中身は変更しない
- `xx_api` が複数ある場合は、**それらの親ディレクトリを `--repo` に指定する**（理由は「3. 設計書を取得する」の注意を参照）

置き方:

- **git（推奨）**：サーバーで `git clone` し、実行前に `git pull` する。どのコミットから生成したかが明確になる
- **転送**：未コミットの変更も対象にしたい場合は `rsync` / `scp -r` でサーバーへコピーする

### 2. SSHが切れても止まらないように起動する

`ssh gb10 "python -m godoc_agent.runner ..."` のように直接起動すると、プロセスがSSHセッションにぶら下がる。
次の場合はサーバー側の処理も止まるため、**全件実行では必ず tmux か nohup で切り離す**。

- クライアントPCのシャットダウン・スリープ、ネットワーク切断でSSHが切れる
- Claude Code の Bash ツールの時間上限（最大10分）で打ち切られる

```bash
# A. tmux（後から接続して進捗を見られる。推奨）
ssh gb10 "tmux new -d -s godoc 'cd /opt/Lang_Chain && mkdir -p out/_runs && .venv/bin/python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --run-id nightly-20261102 2>&1 | tee out/_runs/nightly-20261102.log'"

# B. nohup（tmuxがない場合）
ssh gb10 "cd /opt/Lang_Chain && mkdir -p out/_runs && nohup .venv/bin/python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --run-id nightly-20261102 > out/_runs/nightly-20261102.log 2>&1 < /dev/null &"
```

- **`--run-id` は必ず明示する**。省略すると実行のたびに日時から新しい値が作られ、中断した実行を再開できない
- 万一止まっても、同じ `--run-id` で再実行すれば完了済みのファイルを飛ばして続きから再開する
- `--limit 5` 程度のPoC実行なら、10分以内に終わる見込みがあれば直接起動でもよい（この場合はPCを落とすと止まる）

進捗の確認:

```bash
ssh gb10 "tail -n 20 /opt/Lang_Chain/out/_runs/nightly-20261102.log"   # ログの末尾
ssh -t gb10 tmux attach -t godoc                                        # tmuxの場合（Ctrl-b d で離脱）
```

### クライアントPCを落としてよいか

tmux / nohup で起動していれば、**起動後はクライアントPCを落としてよい**。落とす前に次の2点を確認する。

1. 起動コマンドが戻ってきている（tmux / nohup はバックグラウンドに切り離して即座に戻る）
2. サーバーで処理が始まっている

   ```bash
   ssh gb10 "tail -n 5 /opt/Lang_Chain/out/_runs/nightly-20261102.log"
   ```

   `run_id=... 対象=N 件` の後に `[1/N] done ...` のような進捗行が出ていればよい。すぐに `エラー:` が出ている場合は、Ollama・Claude Code・`.env` の設定を見直す

クライアントPCを落としても、サーバー側の要因では処理が止まる。

| 要因 | 対策 |
|---|---|
| GB10 のスリープ・自動サスペンド（Ubuntuデスクトップ版では既定で有効な場合がある） | `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target` で無効にする |
| サーバーの再起動、Ollama の停止 | 同じ `--run-id` で再実行すれば続きから再開する |

PCを再起動した後は、ログの末尾にサマリー（`対象: ... 件 / 経過時間: ...`）が出ていれば完了している。
失敗した件があった場合や途中で止まっていた場合は、同じ `--run-id` で再実行すると残りだけが処理される。完了したら次の手順で設計書を取得する。

### 3. 設計書を取得する

設計書はサーバー上の `--out` 配下に、Goファイルと同じフォルダ構成で `<--repo からの相対パス>.go.md` として作られる。
`--out ./out` のような相対パスは実行時のカレントディレクトリが基準になるため、このREADMEの手順（`/opt/Lang_Chain` で実行）では `/opt/Lang_Chain/out/` になる。

```
--repo /srv/repos/myapp  --out ./out （/opt/Lang_Chain で実行）の場合

/srv/repos/myapp/user_api/pkg/handler/user.go    → /opt/Lang_Chain/out/user_api/pkg/handler/user.go.md
/srv/repos/myapp/order_api/pkg/handler/order.go  → /opt/Lang_Chain/out/order_api/pkg/handler/order.go.md
/opt/Lang_Chain/out/.manifest.json                 … 生成済みファイルの記録（差分スキップ用）
/opt/Lang_Chain/out/_runs/<run-id>.jsonl           … 計測ログ
```

> **注意：`--repo` に `xx_api` 自体を指定する場合**
> 出力パスに API 名が含まれなくなるため、複数の API を同じ `--out` に出力すると、同名ファイルが上書きされ `.manifest.json` も混ざる。
>
> ```
> --repo /srv/repos/user_api  → out/pkg/handler/user.go.md
> --repo /srv/repos/order_api → out/pkg/handler/order.go.md   ← 同じ out に混在する
> ```
>
> `xx_api` の親ディレクトリを `--repo` に指定する（推奨）か、API ごとに `--out ./out/user_api` のように出力先を分けること。

クライアントへの取得方法:

```powershell
# scp でクライアントへコピー
scp -r gb10:/opt/Lang_Chain/out ./design-docs
```

または `--out /srv/repos/myapp/docs/design` のようにGoリポジトリ内へ出力し、サーバーでコミット・pushすれば、クライアントは `git pull` で受け取れる。

## 計測（PoCの判断材料）

`out/_runs/<run_id>.jsonl` に1ファイル1行で記録され、終了時にサマリーが表示される。

- `duration_s`：1ファイルの処理時間（並列待ちを除く）
- `attempts`：生成回数（検証NGで再生成した場合に増える）
- `llm_calls[].num_turns` / `output_tokens` / `elapsed_s`：Claude Code 1回の実行ごとのターン数・出力トークン数・所要時間
- `status`：`done` / `failed_validation` / `too_large` / `error`（`error` の詳細は `error` 欄。例：`error_max_turns`）

## 既知の制約（技術的負債）

- **事実抽出は正規表現による簡易版**。複雑な宣言を取りこぼす可能性がある。本番では `go/ast` を使う補助ツールに置き換える
- **入力の切り捨てを検出できない**。Claude Code はコンテキストの大きさを知らないまま会話を伸ばし、Ollama は `OLLAMA_CONTEXT_LENGTH` を超えた分を黙って切り捨てる。大きすぎるファイルは「1トークン≒3文字」の概算で事前に `too_large` とし、ターン数は `CLAUDE_MAX_TURNS` で抑えている
- 安定性は、ローカルモデルが Claude Code のツールを正しく使えるかに左右される。同じ操作の繰り返しや途中終了は `error`（`error_max_turns` など）として記録される
- 設計書の構成（見出し）は検証しない。SKILL.md どおりの構成になっているかは、PoCで出力を確認する
- パッケージ単位の集約、人による承認（interrupt）は未実装（次のフェーズ）
- LangSmithのトレースはスキル・プロンプト・生成された設計書を外部に送信するため、既定では無効
