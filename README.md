# Lang_Chain

Goソースファイルから詳細設計書(Markdown)を一括生成するLangGraphワークフロー（PoC）。
GB10サーバー上で動かし、クライアントのClaude CodeからSSH経由で起動する。

設計書は、サーバー上の **Claude Code（`claude -p`）がエージェントとして**、既存の **SKILL.md をそのまま使って**書く。
モデルは Ollama の Anthropic 互換API経由で qwen3-coder-next を使うため、ソースコードはサーバーの外に出ない。
LangGraph は、その外側で並列制御・差分スキップ・再開・検証・計測を受け持つ。

同じ仕組みで、**ソースファイルにコメントを追記するスキル**も一括で実行できる（`CLAUDE_COMMENT_SKILL_PATH` を設定した場合。[コメント追記](#コメント追記)を参照）。

runner は常駐サービスではなく、起動すると対象ファイルを処理して終了する**バッチ**である（常駐するのは Ollama だけ）。
必要なときに起動するか、[定期実行（systemd タイマー）](#定期実行systemd-タイマー) で毎晩自動実行する。

## 設計方針（方式A：1ファイル = 1セッション）

- ソースファイル1本×タスク（設計書 `doc` / コメント追記 `comment`）ごとに独立した `thread_id` でグラフを実行する。前のファイルのコンテキストは一切持ち込まない
- `thread_id = <run_id>:<タスク>:<パスのハッシュ>`。同じ `--run-id` で再実行すると、失敗・中断したものだけがチェックポイントから再開される
- ソースのハッシュをタスクごとに記録し（設計書は `out/.manifest.json`、コメント追記は `out/_commented/.manifest.json`）、変更のないファイルは次回以降スキップする
- 設計書の構成・書き方は SKILL.md に任せる。検証するのは、本文が空でないことと、公開識別子（静的解析で抽出）がすべて記載されていることだけ
- 処理対象の言語・フォルダ・ファイル・除外は設定ファイル `targets.toml`（なければ [targets.example.toml](targets.example.toml)）で決める。対応言語は Go・C#・TypeScript（[docs/target_files.md](docs/target_files.md)）
- 設計書スキルはすべての言語で共通の `skills/design-doc-batch/SKILL.md` を使う。言語ごとに変わるのはプロンプト中の言語名と、公開識別子の抽出方法だけ
- 既定（targets.example.toml）の対象は **`<xx>_api/pkg/handler/` 配下（サブディレクトリを含む）のGoファイルだけ**（`xx` は任意）
  - `--repo` には、複数の `xx_api` を含む親ディレクトリと、`xx_api` 自体のどちらを指定してもよい（複数の API を扱うなら親ディレクトリを推奨。「3. 設計書を取得する」の注意を参照）
  - 例：対象 `user_api/pkg/handler/user.go`、`user_api/pkg/handler/admin/role.go` ／ 対象外 `user_api/pkg/model/user.go`、`user_api/internal/pkg/handler/x.go`
- 上記の配下でも、次のファイルは対象外
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
| [godoc_agent/doc_graph.py](godoc_agent/doc_graph.py) | 1ファイル×1タスク分のグラフ（生成・検証・再試行） |
| [godoc_agent/tasks.py](godoc_agent/tasks.py) | タスク（設計書・コメント追記）ごとのプロンプト・検証・出力先の定義 |
| [godoc_agent/runner.py](godoc_agent/runner.py) | バッチ実行、並列数制御、差分スキップ、再開、計測ログ |
| [godoc_agent/claude_code.py](godoc_agent/claude_code.py) | `claude -p` の起動、Ollama への接続設定、セッションの継続（`--resume`） |
| [godoc_agent/prompts.py](godoc_agent/prompts.py) | SKILL.md の読み込みと、エージェントへの指示の組み立て |
| [godoc_agent/targets.py](godoc_agent/targets.py) | 対象の設定ファイル（targets.toml）の読み込みと、対象ファイルの列挙 |
| [godoc_agent/analyzers.py](godoc_agent/analyzers.py) | 言語名から解析処理を引く（[go_source.py](godoc_agent/go_source.py)・[csharp_source.py](godoc_agent/csharp_source.py)・[typescript_source.py](godoc_agent/typescript_source.py)：関数・型・公開識別子などの事実抽出。いずれも正規表現の簡易版） |
| [godoc_agent/source_common.py](godoc_agent/source_common.py) | 言語に共通する字句の分割と、コメント以外の差分検出 |
| [godoc_agent/preflight.py](godoc_agent/preflight.py) | 開始前の Ollama 接続・モデル存在チェック |

起動から終了までの処理の流れ（再開判定・検証・保存・終了コードなど）は [docs/workflow.md](docs/workflow.md) にまとめている。
処理対象の言語・フォルダ・ファイル・除外を変える手順は [docs/target_files.md](docs/target_files.md) にまとめている。

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
cp .env.example .env  # LLM_MODEL などを編集（LLM_NUM_CTX は 1. の OLLAMA_CONTEXT_LENGTH と同じ値にする）

# 4. 既存のスキルをサーバーに置く（クライアントから実行。参照ファイルがあればフォルダごと）
#    設計書スキルは skills/design-doc-batch/ に置く（フォルダ名を design-doc-batch にする。.env での指定は不要）
scp -r ~/.claude/skills/design-doc-batch gb10:/opt/Lang_Chain/skills/
scp -r ~/.claude/skills/<コメント追記スキル名> gb10:/opt/Lang_Chain/skills/   # コメント追記も行う場合
#    コメント追記スキルは、置いた SKILL.md のパスを .env に書く
#      CLAUDE_COMMENT_SKILL_PATH=/opt/Lang_Chain/skills/<コメント追記スキル名>/SKILL.md

# 5. テスト（Ollama・Claude Code 不要）。最後に「N passed」とだけ表示されれば成功
cd /opt/Lang_Chain && .venv/bin/python -m pytest
#    クライアントから実行する場合: ssh gb10 "cd /opt/Lang_Chain && .venv/bin/python -m pytest"
```

テストは `/opt/Lang_Chain`（`pytest.ini` のある場所）で、仮想環境の Python から実行する。上の形なら、ターミナルを開き直して `activate` していなくても動く。

Ollama の接続先（`ANTHROPIC_BASE_URL` など）は runner が `claude` の起動時に設定するため、シェルに設定する必要はない。
テレメトリなどモデル以外への通信も `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` で止めている。

### SKILL.md の扱い

- SKILL.md の全文をシステムプロンプトに入れる。Claude Code のスキル検出には頼らないため、スキルの置き場所は問わない。設計書スキルの既定は `skills/design-doc-batch/SKILL.md`（別の場所に置く場合は `CLAUDE_SKILL_PATH` で指定）
- SKILL.md と同じフォルダ（`references/` など）は `--add-dir` で読めるようにしている
- 次のルールをスキルより優先させている：ファイルの作成・編集やコマンド実行をしない（保存は runner が行う）、設計書の本文だけを返す、質問しない
- 対象ファイルのパスは runner がプロンプトで渡す（スキル内の `$ARGUMENTS` などのプレースホルダーは置換されない）
- 今日の日付も runner が渡す（エージェントはコマンドを実行できず、日付を調べられないため）。実行開始時に一度だけ取得するので、日付をまたいでも同じ実行の設計書は同じ日付になる
- スキルの履歴（バージョン管理）は、前回の設計書を引き継いで更新させる
  - `--out` に前回の設計書があれば、その全文をプロンプトに添えて「前回までの履歴を残し、今回の改訂を1件追加する」よう指示する。なければ初版として作らせる
  - 版の番号の付け方・履歴の書式はスキルに従う。履歴が正しく引き継がれたかはプログラムでは検証しない（書式がスキル次第のため）
  - 前回の設計書もプロンプトに入るため、`too_large` の判定はソースと前回の設計書の合計文字数で行う
  - `--out` の設計書を消すと履歴も失われ、次回は初版から作り直しになる。ソースが変わっていなくても `--force` で再生成すると版が1つ上がる

### コメント追記

`.env` に `CLAUDE_COMMENT_SKILL_PATH`（コメント追記スキルの SKILL.md）を設定すると、設計書の生成と同じ実行の中で、対象の各ファイルにコメントを追記する。
スキルの置き方・扱い（全文をシステムプロンプトに入れる、日付を渡す、ファイル編集をさせない等）は設計書のスキルと同じ。

#### 有効にする手順（設計書の生成がすでに動いているサーバーに追加する場合）

すべてクライアントのターミナルから実行する（sudo は不要）。`<コメント追記スキル名>` はクライアントの `~/.claude/skills/` 配下のフォルダ名、`/srv/repos/myapp` は実際の `--repo` のパスに置き換える。

**手順1. サーバーのプログラムを最新にする**

```bash
ssh gb10 "cd /opt/Lang_Chain && git pull && .venv/bin/pip install -r requirements.txt"
```

**手順2. スキルをサーバーに送る**（参照ファイルも含めてフォルダごと。設計書スキルと同じ `skills/` 配下に置く）

```bash
scp -r ~/.claude/skills/<コメント追記スキル名> gb10:/opt/Lang_Chain/skills/
ssh gb10 "ls /opt/Lang_Chain/skills/<コメント追記スキル名>/SKILL.md"   # パスが表示されればよい
```

**手順3. `.env` にスキルのパスを1行追記する**

既存の `.env` に追記する。`.env.example` をコピーし直すと、設定済みの値（`CLAUDE_BIN` など）が消えるので上書きしないこと。

```bash
ssh gb10 "cd /opt/Lang_Chain && grep -q '^CLAUDE_COMMENT_SKILL_PATH=' .env || echo 'CLAUDE_COMMENT_SKILL_PATH=/opt/Lang_Chain/skills/<コメント追記スキル名>/SKILL.md' >> .env"
ssh gb10 "grep '^CLAUDE_COMMENT_SKILL_PATH=' /opt/Lang_Chain/.env"   # 追記した行が表示されればよい
```

**手順4. 3ファイルだけで試す**（コメント追記だけを実行する）

```bash
ssh gb10 "cd /opt/Lang_Chain && .venv/bin/python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --tasks comment --limit 3 --run-id trial-comment"
```

- 最初に `run_id=trial-comment タスク=comment 対象=3 件` と表示され、1ファイル終わるごとに `[1/3] done ... comment user_api/pkg/handler/xxx.go` のような行が出る
- 1ファイル数分〜十数分かかる。Claude Code から依頼する場合は Bash ツールの時間上限（10分）で打ち切られるため、[2. SSHが切れても止まらないように起動する](#2-sshが切れても止まらないように起動する)の tmux の形で起動する
- 途中で止まったら、同じコマンドをもう一度実行すれば続きから再開する

**手順5. 結果を確認する**

```bash
# コメント追記済みのファイルの一覧
ssh gb10 "cd /opt/Lang_Chain/out/_commented && find . -name '*.go'"
# 元のファイルとの差分（追記されたコメントだけが表示される）
ssh gb10 "diff -ru /srv/repos/myapp/user_api/pkg/handler /opt/Lang_Chain/out/_commented/user_api/pkg/handler"
```

手順4の表示で `done` 以外になったファイルは、次のように対処する（詳細は `out/_runs/trial-comment.jsonl` の `issues` / `error` 欄）。

| 表示 | 意味 | 対処 |
|---|---|---|
| `too_large` | ファイルが大きく、全文を1回の応答で返せない | `.env` の `LLM_NUM_PREDICT` を上げる（例：`16384`）。上限の目安は `LLM_NUM_PREDICT × 1.5` 文字 |
| `failed_validation` | 再生成しても、コメント以外のコードを変えてしまった | 同じコマンドで再実行する（失敗したファイルだけが処理される）。繰り返す場合はスキルにコードを変更しないよう明記する |
| `error` | Claude Code の異常終了など（例：`error_max_turns`） | `error` 欄を確認する。ターン数不足なら `.env` の `CLAUDE_MAX_TURNS` を上げる |

**手順6. 完了**

以降は、手動実行・夜間の定期実行のどちらでも、設計書と一緒にコメント追記が行われる（`--tasks` の指定は不要）。
試した3ファイルは記録済みのため、次回の実行ではソースが変わらない限りスキップされる。
コメント追記を止めるには、`.env` の `CLAUDE_COMMENT_SKILL_PATH` の行を削除する。

#### 動作の詳細

- 対象ファイルは設計書と同じ（`<xx>_api/pkg/handler/` 配下、テストコード除く）
- **Goリポジトリは書き換えない**。コメントを追記したファイルは `--out/_commented/<--repo からの相対パス>` に出力する（例：`out/_commented/user_api/pkg/handler/user.go`）。取り込むかどうかは差分を見て人が判断する（手順5）
- エージェントにはファイルの全文を返させ、**コメントと空白以外のコードが1文字でも変わっていたら不合格**として、差分の行番号を添えて再生成させる（`DOC_MAX_ATTEMPTS` 回まで）。文字列リテラルの中身・改行による文の区切りも比較する
- コメントの書き方・量はスキルに任せ、プログラムでは検証しない
- 出力がファイル全文になるため、1回の応答の出力上限（`LLM_NUM_PREDICT`）に収まらないファイルは事前に `too_large` とする（上限 = `LLM_NUM_PREDICT × 1.5` 文字。既定 8192 なら約 12,000 文字）。大きなファイルを扱うには `LLM_NUM_PREDICT` を上げる
- 前回の出力は引き継がない。Goリポジトリのソースが変わったファイルは、その時点のソースから追記し直す（取り込んだ後はソースが変わるため、新たに不足している箇所だけ追記される想定）

## 使い方

```bash
# PoC: まず5本だけ
python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --limit 5

# 全件（夜間バッチ）
python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --run-id nightly-20261102

# 途中で止まった・失敗があった場合は同じ run-id で再実行
python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --run-id nightly-20261102

# タスクを選んで実行（省略時は doc と、CLAUDE_COMMENT_SKILL_PATH があれば comment も）
python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --tasks comment
```

- `--limit` はファイル×タスクの件数で数える（両方のタスクを実行する場合、`--limit 5` は約2.5ファイル分）
- 並列数（`LLM_CONCURRENCY`）はタスクをまたいだ合計。両方のタスクを実行すると Claude Code の実行回数はおよそ2倍になる

終了コード: `0` 全件成功 / `1` 一部失敗（詳細は計測ログ） / `2` 設定・接続エラーで開始できず

## クライアント(Claude Code)からの運用

処理はすべてサーバー上で完結する。クライアントの役割は「Goソースの更新」「起動」「成果物の取得」だけ。

```
クライアント                         サーバー(GB10)
  Goソース ──(git push / rsync)──>  /srv/repos/myapp      ← --repo で読む
  ssh で起動 ───────────────────>  runner（tmux / nohup で切り離して実行）── Claude Code ── Ollama
  設計書 <──(scp / git pull)──────  /opt/Lang_Chain/out   ← --out に書く
```

### 作業の分担：初回だけの準備と、その後の運用

**SSHで接続できれば、すべての作業をクライアントから行える**（サーバーの前で作業する必要はない）。
ただし、Pythonファイルを置くだけでは動かない。最初に1回だけ、次の準備を SSH 経由で行う。

| 初回だけの準備（詳細は「サーバー(GB10)へのセットアップ」） | sudo |
|---|---|
| Ollama の設定（コンテキスト長など）とモデルの確認 | 要 |
| Claude Code のインストール | 不要 |
| このリポジトリの clone、venv 作成、`pip install` | `/opt` 配下なら要 |
| `.env` の作成（`CLAUDE_BIN` など） | 不要 |
| SKILL.md（設計書・コメント追記）をクライアントから `scp` で配置 | 不要 |
| Goリポジトリの clone と、パスワードなしで `git pull` できる設定（SSH鍵など） | 不要 |
| GB10 のスリープ無効化（「クライアントPCを落としてよいか」参照） | 要 |
| 定期実行タイマーの設置（使う場合。「定期実行（systemd タイマー）」参照） | 要 |

準備が済んだ後の運用は、すべてクライアントからのコマンドで完結する。

| 作業 | クライアントからの操作 |
|---|---|
| 設計書の生成 | 手動なら `ssh gb10 "tmux new -d ..."`（「2. SSHが切れても止まらないように起動する」）。定期実行なら毎晩自動 |
| 進捗・結果の確認 | `ssh gb10 "tail -n 20 /opt/Lang_Chain/out/_runs/<run-id>.log"` / `ssh gb10 systemctl status godoc-nightly.service` |
| 設計書の取得 | `scp -r gb10:/opt/Lang_Chain/out ./design-docs` |
| SKILL.md の更新 | `scp` で上書きする（次の実行から反映される） |
| このリポジトリの更新 | `ssh gb10 "cd /opt/Lang_Chain && git pull && .venv/bin/pip install -r requirements.txt"` |

注意:

- **sudo が必要な作業は、クライアントの Claude Code には任せられない**。Claude Code のコマンド実行は非対話のため、パスワード入力で失敗する。sudo が必要な作業は、自分のターミナルで `ssh -t gb10 "sudo ..."` のように実行する。それ以外の作業は Claude Code に依頼してよい
- 停電などでサーバーが停止した後の電源投入は、物理的な操作（またはリモート電源管理）が必要。起動すれば Ollama は自動で立ち上がり、定期実行も逃した分を起動後に実行する

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

設計書はサーバー上の `--out` 配下に、ソースファイルと同じフォルダ構成で `<--repo からの相対パス>.go.md` として作られる。
`--out ./out` のような相対パスは実行時のカレントディレクトリが基準になるため、このREADMEの手順（`/opt/Lang_Chain` で実行）では `/opt/Lang_Chain/out/` になる。

```
--repo /srv/repos/myapp  --out ./out （/opt/Lang_Chain で実行）の場合

/srv/repos/myapp/user_api/pkg/handler/user.go    → /opt/Lang_Chain/out/user_api/pkg/handler/user.go.md
/srv/repos/myapp/order_api/pkg/handler/order.go  → /opt/Lang_Chain/out/order_api/pkg/handler/order.go.md
/opt/Lang_Chain/out/.manifest.json                 … 生成済みファイルの記録（差分スキップ用）
/opt/Lang_Chain/out/_runs/<run-id>.jsonl           … 計測ログ
/opt/Lang_Chain/out/_commented/user_api/pkg/handler/user.go … コメント追記済みのファイル（コメント追記を有効にした場合）
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

## 定期実行（systemd タイマー）

毎晩決まった時刻に、Goリポジトリを `git pull` してから設計書を生成する。変更のないファイルはスキップされるため、毎日実行しても処理されるのは変更されたファイルだけ。

| ファイル | 役割 |
|---|---|
| [deploy/nightly.sh](deploy/nightly.sh) | `git pull --ff-only` → runner 実行。`git pull` に失敗したら生成しない（古いソースから作らないため） |
| [deploy/systemd/godoc-nightly.service](deploy/systemd/godoc-nightly.service) | 上のスクリプトを1回実行する（実行ユーザー・Goリポジトリのパスをここで指定） |
| [deploy/systemd/godoc-nightly.timer](deploy/systemd/godoc-nightly.timer) | 毎日 2:00 に service を起動する |

### 設置

前提：README「サーバー(GB10)へのセットアップ」が済み、手動実行で設計書が生成できていること。

```bash
cd /opt/Lang_Chain

# 1. .env の CLAUDE_BIN を claude のフルパスにする（systemd の PATH には ~/.local/bin が入らない）
which claude          # 例: /home/<ユーザー>/.local/bin/claude → CLAUDE_BIN=/home/<ユーザー>/.local/bin/claude

# 2. 実行ユーザーで、Goリポジトリの git pull がパスワード入力なしでできることを確認する（SSH鍵・デプロイキーなど）
git -C /srv/repos/myapp pull --ff-only

# 3. 手動でスクリプトを試す
GODOC_REPO=/srv/repos/myapp ./deploy/nightly.sh

# 4. unit ファイルを設置する（実行ユーザーを自分に置き換え）
sed "s/REPLACE_WITH_USER/$USER/" deploy/systemd/godoc-nightly.service | sudo tee /etc/systemd/system/godoc-nightly.service
sudo cp deploy/systemd/godoc-nightly.timer /etc/systemd/system/
sudo systemctl edit --full godoc-nightly.service   # GODOC_REPO（・GODOC_OUT）を実際のパスに直す

# 5. タイマーを有効化
sudo systemctl daemon-reload
sudo systemctl enable --now godoc-nightly.timer
systemctl list-timers godoc-nightly.timer          # 次回の実行予定を確認
```

### 運用

```bash
sudo systemctl start godoc-nightly.service          # 今すぐ1回実行する（終わるまで戻らない。--no-block で即座に戻る）
systemctl status godoc-nightly.service              # 直近の結果（一部失敗・git pull失敗は failed と表示される）
journalctl -u godoc-nightly.service -n 50           # 実行ログ
tail -n 20 /opt/Lang_Chain/out/_runs/nightly-$(date +%Y%m%d).log   # 同じ内容のファイルログ
sudo systemctl disable --now godoc-nightly.timer    # 定期実行を止める
```

- 実行時刻を変えるには `sudo systemctl edit --full godoc-nightly.timer` で `OnCalendar=` を直す（例：`*-*-* 23:30:00`）
- `--run-id` は `nightly-<日付>`。同じ日に `systemctl start` で再実行すると、中断・失敗したファイルの続きから再開する
- 実行時刻にサーバーが停止していた場合は、起動後に実行される（`Persistent=true`）
- 前回の実行が終わっていなければ、次の起動は行われない（systemd が二重起動を防ぐ）。12時間を超えると強制終了する（`TimeoutStartSec=12h`）
- 夜間実行中に、同じ `--out` へ手動実行を重ねないこと（差分スキップの記録が混ざる）
- 設計書の取得方法は、手動実行と同じ（「3. 設計書を取得する」）

## 計測（PoCの判断材料）

`out/_runs/<run_id>.jsonl` に1ファイル1行で記録され、終了時にサマリーが表示される。

- `task`：`doc`（設計書）/ `comment`（コメント追記）
- `duration_s`：1ファイルの処理時間（並列待ちを除く）
- `attempts`：生成回数（検証NGで再生成した場合に増える）
- `llm_calls[].num_turns` / `output_tokens` / `elapsed_s`：Claude Code 1回の実行ごとのターン数・出力トークン数・所要時間
- `llm_calls[].input_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens`：Claude Code 1回の実行ごとの入力トークン数（`usage` の値そのまま）。返らない項目は `null`
- `llm_calls[].total_input_tokens`：上の3つのうち値のあるものの合計（プロンプトキャッシュ分は `input_tokens` に含まれないため）。すべて `null` なら `null`
- `warnings`：合否に影響しない注意。`total_input_tokens` が `LLM_NUM_CTX × LLM_CTX_WARN_RATIO`（既定 0.9）を超えた実行があると残る。`done` でも残り、進行表示にも `[警告: ...]` として出る
- `status`：`done` / `failed_validation` / `too_large` / `error`（`error` の詳細は `error` 欄。例：`error_max_turns`）

サマリーには、入力トークン数（1回あたり）の最大値・平均値と、警告が出た件数も表示される（値が取れた実行だけで集計し、1回も取れなければ表示しない）。

入力トークン数の警告は、Ollama が `OLLAMA_CONTEXT_LENGTH` を超えた分を黙って切り捨てる問題を後から見つけるためのもの。`LLM_NUM_CTX` は Ollama の `OLLAMA_CONTEXT_LENGTH` と**同じ値にする**（ずれると判定もずれる）。

確認事項（PoCで確認する）：

- Ollama の Anthropic 互換APIが `usage.input_tokens` などを実際に返すか（返さなければ `null` になり、警告も出ない）
- Claude Code の `usage` が「最後の1ターンの値」か「その実行の全ターンの合計」か。合計の場合は実際のコンテキストより大きく出るため、`num_turns` と合わせて見る

## 既知の制約（技術的負債）

- **事実抽出は正規表現による簡易版**。複雑な宣言を取りこぼす可能性がある。本番では `go/ast` を使う補助ツールに置き換える
- **入力の切り捨てを検出できない**。Claude Code はコンテキストの大きさを知らないまま会話を伸ばし、Ollama は `OLLAMA_CONTEXT_LENGTH` を超えた分を黙って切り捨てる。大きすぎるファイルは「1トークン≒3文字」の概算で事前に `too_large` とし、ターン数は `CLAUDE_MAX_TURNS` で抑えている。実行後は入力トークン数が上限に近ければ `warnings` に残すが、切り捨てそのものを検出するわけではなく、`usage` が返らなければ警告も出ない
- 安定性は、ローカルモデルが Claude Code のツールを正しく使えるかに左右される。同じ操作の繰り返しや途中終了は `error`（`error_max_turns` など）として記録される
- 設計書の構成（見出し）は検証しない。SKILL.md どおりの構成になっているかは、PoCで出力を確認する
- コメント追記の検証は、Pythonで書いた簡易な字句解析でコメントと空白を除いて比較している。コードの同一性は保証するが、出力が `gofmt` 済みかどうか・コンパイルできるかは確認しない（取り込む前に `gofmt -l` / `go build` で確認する）
- この版から `thread_id` にタスク名が入ったため、更新前の版で中断した実行は同じ `--run-id` でも再開されず、最初から処理される（差分スキップの記録 `.manifest.json` はそのまま使える）
- 履歴の引き継ぎ（前回までの行が残っているか、版が上がっているか）は検証しない。ローカルモデルが履歴を書き換えたり落としたりしないかは、PoCで2回目以降の出力を確認する
- パッケージ単位の集約、人による承認（interrupt）は未実装（次のフェーズ）
- LangSmithのトレースはスキル・プロンプト・生成された設計書を外部に送信するため、既定では無効
