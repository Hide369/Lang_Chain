# Lang_Chain

Goソースファイルから詳細設計書(Markdown)を一括生成するLangGraphワークフロー（PoC）。
GB10サーバー上のOllama（qwen3-coder-next）で動かし、クライアントのClaude CodeからSSH経由で起動する。

生成エンジンは `--engine` で切り替えられる。並列制御・差分スキップ・再開・検証・計測ログは共通。

| エンジン | 生成方法 | 設計書の書き方の指示 |
|---|---|---|
| `ollama`（既定） | LangChain から Ollama を1回呼び出す。対象ファイルのソースをプロンプトに含める | [prompts/design_doc_template.md](prompts/design_doc_template.md) |
| `claude-code` | サーバー上の Claude Code（`claude -p`）がエージェントとして動き、対象ファイルや関連ファイルを自分で読む。モデルは同じ Ollama | 既存の **SKILL.md をそのまま使う** |

## 設計方針（方式A：1ファイル = 1セッション）

- Goファイル1本ごとに独立した `thread_id` でグラフを実行する。前のファイルのコンテキストは一切持ち込まない
- `thread_id = <run_id>:<パスのハッシュ>`。同じ `--run-id` で再実行すると、失敗・中断したファイルだけがチェックポイントから再開される
- ソースのハッシュを `out/.manifest.json` に記録し、変更のないファイルは次回以降スキップする
- 設計書の対象は **`<xx>_api/pkg/handler/` 配下（サブディレクトリを含む）のGoファイルだけ**（`xx` は任意）
  - `--repo` には、複数の `xx_api` を含む親ディレクトリと、`xx_api` 自体のどちらを指定してもよい
  - 例：対象 `user_api/pkg/handler/user.go`、`user_api/pkg/handler/admin/role.go` ／ 対象外 `user_api/pkg/model/user.go`、`user_api/internal/pkg/handler/x.go`
- 上記の配下でも、次のファイルは対象外（常に除外され、オプションで含めることはできない）
  - テストコード（ファイル名が `_test.go` で終わるもの）
  - `vendor/`、`testdata/`、`node_modules/`、`.` で始まるディレクトリ配下のファイル

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
| [godoc_agent/claude_code.py](godoc_agent/claude_code.py) | `--engine claude-code` 用。`claude -p` を起動し、結果を `ollama` エンジンと同じ形で返す |
| [prompts/design_doc_template.md](prompts/design_doc_template.md) | 設計書テンプレート。`ollama` ではプロンプト兼検証基準、`claude-code` では検証基準（`## ` 見出し）としてだけ使う |

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

## クライアント(Claude Code)からの運用

処理はすべてサーバー上で完結する。クライアントの役割は「Goソースの更新」「起動」「成果物の取得」だけ。

```
クライアント                         サーバー(GB10)
  Goソース ──(git push / rsync)──>  /srv/repos/myapp      ← --repo で読む
  ssh で起動 ───────────────────>  runner（tmux / nohup で常駐）── Ollama
  設計書 <──(scp / git pull)──────  /opt/Lang_Chain/out   ← --out に書く
```

### 1. Goリポジトリをサーバーに置く

`--repo` はサーバー上のパスとして直接読み込まれる。クライアントからソースを送る機能はない。

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

   `run_id=... 対象=N 件` の後に `[1/N] done ...` のような進捗行が出ていればよい。すぐに `エラー:` が出ている場合は、Ollamaへの接続や設定を見直す

クライアントPCを落としても、サーバー側の要因では処理が止まる。

| 要因 | 対策 |
|---|---|
| GB10 のスリープ・自動サスペンド（Ubuntuデスクトップ版では既定で有効な場合がある） | `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target` で無効にする |
| サーバーの再起動、Ollama の停止 | 同じ `--run-id` で再実行すれば続きから再開する |

PCを再起動した後は、ログの末尾にサマリー（`対象: ... 件 / 経過時間: ...`）が出ていれば完了している。
失敗した件があった場合や途中で止まっていた場合は、同じ `--run-id` で再実行すると残りだけが処理される。完了したら次の手順で設計書を取得する。

### 3. 設計書を取得する

設計書はサーバー上の `--out` 配下に、Goファイルと同じフォルダ構成で `<相対パス>.go.md` として作られる
（同じ場所に `.manifest.json` と `_runs/*.jsonl` も置かれる）。

```powershell
# scp でクライアントへコピー
scp -r gb10:/opt/Lang_Chain/out ./design-docs
```

または `--out /srv/repos/myapp/docs/design` のようにGoリポジトリ内へ出力し、サーバーでコミット・pushすれば、クライアントは `git pull` で受け取れる。

## テンプレートの作り方（Claude Code の SKILL.md からの移行）

> `--engine claude-code` では SKILL.md をそのまま使うため、この書き換えは不要（テンプレートには必須の `## ` 見出しだけを書けばよい）。以下は `ollama` エンジン向け。

[prompts/design_doc_template.md](prompts/design_doc_template.md) は2つの用途で使われるため、SKILL.md をそのまま貼り替えてはいけない。

1. **LLMへのプロンプト**：ファイル全体（HTMLコメントを含む）がそのままLLMに渡される
2. **検証の基準**：HTMLコメント外の `## ` 見出しがすべて「必須セクション」となり、生成結果に**完全一致**で含まれないと再生成される

移行時のルール:

- `## ` は「設計書に必ず出力する固定の見出し」だけに使う。`## 手順` `## ルール` のような説明用の見出しを残すと、検証が通らず `failed_validation` になる
- 記述ルールや記入例は `<!-- -->` の中に書く（LLMには届くが、必須セクションの判定からは外れる）
- 関数ごとなど繰り返す見出しは `### {{関数名}}` のように `###` 以下にする
- YAML frontmatter（`name:` / `description:`）は削除する
- Read / Write / Glob などツール操作の手順は削除する（サーバー側のLLMはツールを使えない。ソースはプロンプトに含まれ、保存は runner が行う）
- 別ファイル（references/ など）を参照している場合は、その内容をこのファイルに直接書き込む
- テンプレートは毎回プロンプトに入るため、長すぎると `context_overflow` が増える。記入例は短いものを1つ残す程度がよい

## Claude Code エンジン（`--engine claude-code`）

既存の SKILL.md をそのまま使い、サーバー上の Claude Code にエージェントとして設計書を書かせる。
モデルは Ollama の Anthropic 互換API経由で qwen3-coder-next を使うため、ソースコードはサーバーの外に出ない。

```
runner（並列制御・差分スキップ・再開・計測）
  └─ ファイルごとに: claude -p "<対象ファイル>の詳細設計書を作成"
        ├─ システムプロンプト: SKILL.md の全文 + バッチ用ルール
        ├─ ツール: Read / Glob / Grep のみ（作業ディレクトリ = --repo）
        └─ モデル: Ollama(qwen3-coder-next)
  └─ 検証（テンプレートの ## 見出し・公開識別子）→ NG なら同じセッションを --resume して指摘だけ送る
```

### セットアップ（GB10）

```bash
# 1. Claude Code をインストール
curl -fsSL https://claude.ai/install.sh | bash
claude --version

# 2. 既存のスキルをサーバーに置く（クライアントからコピー。参照ファイルがあればフォルダごと）
scp -r ~/.claude/skills/<スキル名> gb10:/opt/Lang_Chain/skills/

# 3. .env に追記
#   CLAUDE_SKILL_PATH=/opt/Lang_Chain/skills/<スキル名>/SKILL.md
```

Ollama の接続先（`ANTHROPIC_BASE_URL` など）は runner が `claude` の起動時に設定するため、シェルに設定する必要はない。
テレメトリなどモデル以外への通信も `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` で止めている。

### SKILL.md の扱い

- SKILL.md の全文をシステムプロンプトに入れる。Claude Code のスキル検出には頼らないため、スキルのインストール場所は問わない
- SKILL.md と同じフォルダ（`references/` など）は `--add-dir` で読めるようにしている
- 次のルールをスキルより優先させている：ファイルの作成・編集やコマンド実行をしない（保存は runner が行う）、設計書の本文だけを返す、質問しない
- 検証の基準（必須の見出し）は [prompts/design_doc_template.md](prompts/design_doc_template.md) の `## ` 見出しから取る。**SKILL.md の出力形式と同じ見出しをテンプレートに書いておく**こと（本文の書き方はテンプレートに書かなくてよい）

### 今の方式との比較（PoC）

同じファイルで比較するため、**`--out` と `--run-id` はエンジンごとに分ける**（差分スキップやチェックポイントが混ざらないように）。

```bash
python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out-ollama --run-id cmp-ollama --limit 5
python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out-cc     --run-id cmp-cc     --limit 5 --engine claude-code
```

`out-*/_runs/*.jsonl` の `status`・`duration_s`・`llm_calls[].num_turns` と、設計書の中身を見比べて判断する。

## 計測（PoCの判断材料）

`out/_runs/<run_id>.jsonl` に1ファイル1行で記録され、終了時にサマリーが表示される。

- `duration_s`：1ファイルの処理時間（並列待ちを除く）
- `llm_calls[].tokens_per_s` / `prompt_tokens` / `done_reason`：Ollamaの性能と切り捨ての兆候（`ollama` エンジンのみ）
- `llm_calls[].num_turns`：エージェントのターン数（`claude-code` エンジンのみ）
- `status`：`done` / `failed_validation` / `too_large` / `context_overflow` / `error`

## 既知の制約（技術的負債）

- **事実抽出は正規表現による簡易版**。複雑な宣言を取りこぼす可能性がある。本番では `go/ast` を使う補助ツールに置き換える
- 文字数による上限チェックは「1トークン≒3文字」の概算。実際の切り捨ては `prompt_tokens` から `context_overflow` として検出する
- パッケージ単位の集約、人による承認（interrupt）は未実装（次のフェーズ）
- **`claude-code` エンジンでは入力の切り捨てを検出できない**。Claude Code はコンテキストの大きさを知らないまま会話を伸ばし、Ollama は `OLLAMA_CONTEXT_LENGTH` を超えた分を黙って切り捨てる。ターン数を `CLAUDE_MAX_TURNS` で抑え、結果の質は比較で確認する
- `claude-code` エンジンの安定性は、ローカルモデルがツールを正しく使えるかに左右される。同じ操作の繰り返しや途中終了は `error`（`error_max_turns` など）として記録される
- LangSmithのトレースはソースコードを外部に送信するため、既定では無効
