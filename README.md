# Batch_Exec

Goソースファイルから詳細設計書(Markdown)を一括生成するLangGraphワークフロー（PoC）。
GB10サーバー上で動かし、クライアント PC のブラウザ（Web 画面）か、GB10 の端末から起動する（GB10 へは SSH で接続できない前提）。

設計書は、サーバー上の **Claude Code（`claude -p`）がエージェントとして**、既存の **SKILL.md をそのまま使って**書く。
モデルは同じサーバー内の vLLM の Anthropic 互換API（`/v1/messages`）経由で Qwen3-Coder-Next（別名 `claude-opus-4-6` で提供）を使うため、ソースコードはサーバーの外に出ない（Ollama に戻すこともできる）。
LangGraph は、その外側で並列制御・差分スキップ・検証・計測を受け持つ。

同じ仕組みで、**ソースファイルにコメントを追記するスキル**も一括で実行できる（`CLAUDE_COMMENT_SKILL_PATH` を設定した場合。[コメント追記](#コメント追記)を参照）。

runner は常駐サービスではなく、起動すると対象ファイルを処理して終了する**バッチ**である（常駐するのは推論サーバー（vLLM）と Web 画面だけ）。
必要なときに起動するか、[定期実行（systemd タイマー）](#定期実行systemd-タイマー) で毎晩自動実行する。

## 設計方針（方式A：1ファイル = 1セッション）

- ソースファイル1本×タスク（設計書 `doc` / コメント追記 `comment`）ごとに、毎回まっさらな状態でグラフを実行する。前のファイルや前回の実行のコンテキストは一切持ち込まない
- チェックポイントは使わない。中断・失敗しても再実行すれば、完了済みのファイルは差分スキップで飛ばされ、残りが最初から処理される
- ソースのハッシュをタスクごとに記録し（設計書は `out/.manifest.json`、コメント追記は `out/_commented/.manifest.json`）、変更のないファイルは次回以降スキップする
- 設計書の構成・書き方は SKILL.md に任せる。検証するのは、本文が空でないことと、公開識別子（静的解析で抽出）がすべて記載されていることだけ
- 処理対象の言語・フォルダ・ファイル・除外は設定ファイル `targets.toml`（なければ [targets.example.toml](targets.example.toml)）で決める。対応言語は Go・C#・TypeScript（[docs/other/target_files.md](docs/other/target_files.md)）
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
   │            └── retry ───┤（本文が空・公開識別子の記載漏れ → 新しいセッションに元の依頼＋直前の出力＋指摘を送る）
   └─> too_large             └─> failed_validation

generate の中身（ファイルごと）:
  claude -p "<対象ファイル>の詳細設計書を作成"
    ├─ システムプロンプト: SKILL.md の全文 + バッチ用ルール
    ├─ ツール: Read / Glob / Grep のみ（作業ディレクトリ = --repo）
    └─ モデル: vLLM(Qwen3-Coder-Next、別名 claude-opus-4-6)
```

| ファイル | 役割 |
|---|---|
| [godoc_agent/doc_graph.py](godoc_agent/doc_graph.py) | 1ファイル×1タスク分のグラフ（生成・検証・再試行） |
| [godoc_agent/tasks.py](godoc_agent/tasks.py) | タスク（設計書・コメント追記・追加のスキル）ごとのプロンプト・検証・出力先の定義 |
| [godoc_agent/skill_registry.py](godoc_agent/skill_registry.py) | 登録済みのスキル（組み込みの設計書・コメント追記と、skills/registry.json に登録した追加のスキル）の一覧 |
| [godoc_agent/runner.py](godoc_agent/runner.py) | バッチ実行、並列数制御、差分スキップ、計測ログ |
| [godoc_agent/sessions.py](godoc_agent/sessions.py) | 同時に動くセッション（最大10）の枠の割り当てと、枠ごとの状況の記録（Web 画面のセッション欄が表示する） |
| [godoc_agent/claude_code.py](godoc_agent/claude_code.py) | `claude -p` の起動（呼び出しごとに新しいセッション）、推論サーバー（vLLM）への接続設定 |
| [godoc_agent/prompts.py](godoc_agent/prompts.py) | SKILL.md の読み込みと、エージェントへの指示の組み立て |
| [godoc_agent/targets.py](godoc_agent/targets.py) | 対象の設定ファイル（targets.toml）の読み込みと、対象ファイルの列挙 |
| [godoc_agent/analyzers.py](godoc_agent/analyzers.py) | 言語名から解析処理を引く（[go_source.py](godoc_agent/go_source.py)・[csharp_source.py](godoc_agent/csharp_source.py)・[typescript_source.py](godoc_agent/typescript_source.py)：関数・型・公開識別子などの事実抽出。いずれも正規表現の簡易版） |
| [godoc_agent/source_common.py](godoc_agent/source_common.py) | 言語に共通する字句の分割と、コメント以外の差分検出 |
| [godoc_agent/preflight.py](godoc_agent/preflight.py) | 開始前の推論サーバー接続・モデル存在チェック（OpenAI 互換の `/v1/models`。vLLM・Ollama の両方で動く） |

起動から終了までの処理の流れ（検証・保存・終了コードなど）は [docs/other/workflow.md](docs/other/workflow.md) にまとめている。
処理対象の言語・フォルダ・ファイル・除外を変える手順は [docs/other/target_files.md](docs/other/target_files.md) にまとめている。
処理が遅いときの切り分け（原因の候補・計測ログの集計方法・関係する設定）は [docs/other/performance_troubleshooting.md](docs/other/performance_troubleshooting.md) にまとめている。
対象フォルダをネットワーク上の NAS にする手順（マウント・サービスの設定・git pull・元に戻す方法）は [docs/other/nas_target.md](docs/other/nas_target.md) にまとめている。
サーバーの更新手順書は、作成日（PR のマージ日）ごとに `docs/pr<最初のPR番号>-<最後のPR番号>/` にまとめている（その日のPRが1件なら `docs/pr<番号>/`）。それ以外の仕様・使い方の文書は `docs/other/` に置く。
以前の版（Go 専用）からサーバーを入れ替える手順は [docs/pr18-22/migration_multi_language.md](docs/pr18-22/migration_multi_language.md) にまとめている。
チェックポイント廃止・再生成の見直し版（PR #21）への入れ替え手順は [docs/pr18-22/migration_drop_checkpointer.md](docs/pr18-22/migration_drop_checkpointer.md) にまとめている。
Web 画面版（PR #23・#24）への入れ替え手順（夜間タイマーからの切り替え・GitLab の認証を含む）は [docs/pr23-25/migration_web_ui.md](docs/pr23-25/migration_web_ui.md) にまとめている。
Ollama を止めて vLLM に切り替える手順（アカウント登録の要否・メモリ枯渇対策・Ollama に戻す手順・vLLM を削除する手順を含む）は [docs/pr33-38/vllm_migration.md](docs/pr33-38/vllm_migration.md) にまとめている。
vLLM に切り替えた後、アプリ（Web 画面・夜間バッチ）を vLLM 対応版に入れ替える設置手順（`.env`・systemd のユニットの入れ直し・元に戻す方法）は [docs/pr33-38/migration_vllm_app.md](docs/pr33-38/migration_vllm_app.md) にまとめている。
vLLM のモデルを Qwen3-Coder-Next から Gemma 4 31B に切り替える手順（事前ダウンロード・Hugging Face にログインできない場合の入手先・元に戻す方法）は [docs/pr39/vllm_gemma4.md](docs/pr39/vllm_gemma4.md) にまとめている。
既存サーバーの設置フォルダを `/opt/Lang_Chain` から `/opt/Batch_Exec` に移す手順（仮想環境の作り直し・`.env`・systemd の入れ直し・元に戻す方法）は [docs/pr40-42/migration_rename_batch_exec.md](docs/pr40-42/migration_rename_batch_exec.md) にまとめている。

## サーバー(GB10)へのセットアップ

コマンドは GB10 の端末で実行する（SSH は使えないため、GB10 に直接ログインした端末を使う）。

```bash
# 1. 推論サーバー（vLLM）を起動する（手順は docs/pr33-38/vllm_migration.md）
#    --served-model-name claude-opus-4-6 --max-model-len 262144 --port 11434 で起動しておく
#    （Ollama から移行したサーバーでは、Ollama と同じ 11434 番ポートで vLLM を待ち受ける）
curl http://127.0.0.1:11434/v1/models   # claude-opus-4-6 が出ること

# 2. Claude Code をインストール
curl -fsSL https://claude.ai/install.sh | bash
which claude         # tmux / nohup から起動する場合は、このフルパスを CLAUDE_BIN に指定する

# 3. このフォルダを配置して依存関係をインストール
cd /opt/Batch_Exec
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # LLM_MODEL などを編集（LLM_MODEL は --served-model-name、LLM_NUM_CTX は --max-model-len と同じ値にする）

# 4. 既存のスキルをサーバーに置く（参照ファイルがあればフォルダごと）
#    Web 画面を使う場合は、ここでは置かずに、画面の「スキル」タブからクライアント PC のフォルダを選んで登録する（docs/other/web_ui.md）
#    Web 画面を使わない場合は、スキルのフォルダを USB メモリや NAS などで GB10 に持ってきてからコピーする
#    設計書スキルは skills/design-doc-batch/ に置く（フォルダ名を design-doc-batch にする。.env での指定は不要）
cp -r <持ってきたフォルダ>/design-doc-batch /opt/Batch_Exec/skills/
cp -r <持ってきたフォルダ>/<コメント追記スキル名> /opt/Batch_Exec/skills/   # コメント追記も行う場合
#    コメント追記スキルは、置いた SKILL.md のパスを .env に書く
#      CLAUDE_COMMENT_SKILL_PATH=/opt/Batch_Exec/skills/<コメント追記スキル名>/SKILL.md

# 5. テスト（推論サーバー・Claude Code 不要）。最後に「N passed」とだけ表示されれば成功
cd /opt/Batch_Exec && .venv/bin/python -m pytest
```

テストは `/opt/Batch_Exec`（`pytest.ini` のある場所）で、仮想環境の Python から実行する。上の形なら、ターミナルを開き直して `activate` していなくても動く。

推論サーバーの接続先（`ANTHROPIC_BASE_URL` など。`.env` の `LLM_BASE_URL` から作る）は runner が `claude` の起動時に設定するため、シェルに設定する必要はない。
テレメトリなどモデル以外への通信も `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` で止めている。
シンキングも `MAX_THINKING_TOKENS=0` で止めている（ローカルモデルはシンキングしないため。指定したままだと vLLM でエラーになるか本文が空になる）。

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
`skills/comment-batch/SKILL.md` に置いた場合（Web 画面から登録した場合を含む）は、`.env` に書かなくても有効になる。
スキルの置き方・扱い（全文をシステムプロンプトに入れる、日付を渡す、ファイル編集をさせない等）は設計書のスキルと同じ。

#### 有効にする手順（設計書の生成がすでに動いているサーバーに追加する場合）

すべて GB10 の端末で実行する（sudo は不要）。`<コメント追記スキル名>` はスキルのフォルダ名、`/srv/repos/myapp` は実際の `--repo` のパスに置き換える。
Web 画面を使っている場合は、手順2・3の代わりに画面の「スキル」タブでコメント追記スキルを登録すればよい（`skills/comment-batch/` に置かれ、`.env` の追記は不要）。

**手順1. サーバーのプログラムを最新にする**

```bash
cd /opt/Batch_Exec && git pull && .venv/bin/pip install -r requirements.txt
```

**手順2. スキルをサーバーに置く**（参照ファイルも含めてフォルダごと。設計書スキルと同じ `skills/` 配下に置く）

スキルのフォルダを USB メモリや NAS などで GB10 に持ってきてからコピーする。

```bash
cp -r <持ってきたフォルダ>/<コメント追記スキル名> /opt/Batch_Exec/skills/
ls /opt/Batch_Exec/skills/<コメント追記スキル名>/SKILL.md   # パスが表示されればよい
```

**手順3. `.env` にスキルのパスを1行追記する**

既存の `.env` に追記する。`.env.example` をコピーし直すと、設定済みの値（`CLAUDE_BIN` など）が消えるので上書きしないこと。

```bash
cd /opt/Batch_Exec && grep -q '^CLAUDE_COMMENT_SKILL_PATH=' .env || echo 'CLAUDE_COMMENT_SKILL_PATH=/opt/Batch_Exec/skills/<コメント追記スキル名>/SKILL.md' >> .env
grep '^CLAUDE_COMMENT_SKILL_PATH=' /opt/Batch_Exec/.env   # 追記した行が表示されればよい
```

**手順4. 3ファイルだけで試す**（コメント追記だけを実行する）

```bash
cd /opt/Batch_Exec && .venv/bin/python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --tasks comment --limit 3 --run-id trial-comment
```

- 最初に `run_id=trial-comment タスク=comment 対象=3 件` と表示され、1ファイル終わるごとに `[1/3] done ... comment user_api/pkg/handler/xxx.go` のような行が出る
- 1ファイル数分〜十数分かかる。途中で端末を閉じる可能性があれば、[2. 端末を閉じても止まらないように起動する](#2-端末を閉じても止まらないように起動する)の tmux の形で起動する
- 途中で止まったら、同じコマンドをもう一度実行すれば、完了済みのファイルを飛ばして残りを最初から処理する

**手順5. 結果を確認する**

```bash
# コメント追記済みのファイルの一覧
cd /opt/Batch_Exec/out/_commented && find . -name '*.go'
# 元のファイルとの差分（追記されたコメントだけが表示される）
diff -ru /srv/repos/myapp/user_api/pkg/handler /opt/Batch_Exec/out/_commented/user_api/pkg/handler
```

手順4の表示で `done` 以外になったファイルは、次のように対処する（詳細は `out/_runs/trial-comment.jsonl` の `issues` / `error` 欄）。

| 表示 | 意味 | 対処 |
|---|---|---|
| `too_large` | ファイルが大きく、全文を1回の応答で返せない | `.env` の `LLM_NUM_PREDICT` を上げる（例：`16384`）。上限の目安は `LLM_NUM_PREDICT × 1.5` 文字 |
| `failed_validation` | 再生成しても、コメント以外のコードを変えてしまった | 同じコマンドで再実行する（失敗したファイルだけが処理される）。繰り返す場合はスキルにコードを変更しないよう明記する |
| `error` | Claude Code の異常終了、UTF-8 で読めないソース、出力ファイルの保存失敗など（例：`error_max_turns`） | `error` 欄を確認する。ターン数不足なら `.env` の `CLAUDE_MAX_TURNS` を上げる |

**手順6. 完了**

以降は、手動実行・夜間の定期実行のどちらでも、設計書と一緒にコメント追記が行われる（`--tasks` の指定は不要）。
試した3ファイルは記録済みのため、次回の実行ではソースが変わらない限りスキップされる。
コメント追記を止めるには、`.env` の `CLAUDE_COMMENT_SKILL_PATH` の行を削除する（`skills/comment-batch/` に置いている場合は、そのフォルダも移すか、Web 画面の「登録を外す」を使う）。

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

# 途中で止まった・失敗があった場合は同じコマンドで再実行（完了済みは飛ばし、残りを最初から処理する）
python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --run-id nightly-20261102

# タスクを選んで実行（省略時は doc と、コメント追記スキル（CLAUDE_COMMENT_SKILL_PATH または skills/comment-batch/SKILL.md）があれば comment も）
python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --tasks comment

# 追加で登録したスキル（skills/registry.json。Web 画面の「スキル」タブから登録できる）も名前で選べる
python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --tasks doc,test-spec
```

- `--limit` はファイル×タスクの件数で数える（両方のタスクを実行する場合、`--limit 5` は約2.5ファイル分）
- 並列数（`--concurrency`。1〜10、既定 10）はタスクをまたいだ合計。両方のタスクを実行すると Claude Code の実行回数はおよそ2倍になる
- さらに、サーバー全体（Web 画面の複数人の実行・夜間バッチ・コマンドの合計）でも同時に 10 セッションまでに抑える。空きがなければ空くまで待つ
- 入力がコンテキスト（`LLM_NUM_CTX` = vLLM の `--max-model-len`、256K）を超えるファイルは、失敗にせず**警告付きでスキップ**して次のファイルへ進む（計測ログの `status` が `too_large` / `context_overflow`、`warnings` に理由）

終了コード: `0` 全件成功（警告付きのスキップを含む） / `1` 一部失敗（詳細は計測ログ） / `2` 設定・接続エラー、または同じ出力先で別の実行が進行中のため開始できず

## ブラウザ（Web 画面）からの運用

GB10 上で Web 画面を常駐させると、クライアント PC のブラウザ（例：`http://192.168.31.26:4000`）から次のことができる。

- 実行・停止と、進捗のログ・実行履歴・生成物（設計書・コメント追記済みファイル）の確認
- スキル（SKILL.md と参照ファイル）の登録。設計書・コメント追記以外のスキルも、名前と出力の種類（文書・コメントだけ変更・コードも変更）を決めて追加できる
- 対象フォルダ（`--repo`）と出力先フォルダ（`--out`）のダイアログでの選択。クライアント PC のフォルダをアップロードして対象にし、生成物を ZIP でまとめて持ち帰ることもできる
- 対象の設定（targets.toml）の編集と、対象になるファイルのプレビュー
- 毎日の定期実行（オン/オフ・時刻・タスク・実行前の git pull）の設定。systemd の夜間タイマーの代わりに使える

閲覧は誰でもでき、実行と設定の変更は管理者パスワードでログインした人だけができる。
複数人が同時に使える。対象・出力先フォルダはブラウザごとに選び、出力先が違えば同時に実行できる（Claude Code のセッションはサーバー全体で合計10まで）。
同じ出力先では、Web 画面・コマンド・夜間タイマーの間でも同時に1つしか動かない（runner が出力先ごとにロックを取る）。
この版への更新手順は [docs/pr40-42/migration_multi_user.md](docs/pr40-42/migration_multi_user.md)。
設置・使い方・セキュリティ上の注意は [docs/other/web_ui.md](docs/other/web_ui.md) にまとめている。

## 運用（GB10 の端末と Web 画面）

処理はすべてサーバー上で完結する。**GB10 へは SSH で接続できない**ため、操作は次の2つで行う。

- **Web 画面**（クライアント PC のブラウザ）：実行・停止・進捗の確認・生成物のダウンロード・スキルの登録・定期実行の設定。日常の運用はこちらで足りる
- **GB10 の端末**（GB10 に直接ログインする）：初回の準備、sudo が必要な作業、このリポジトリの更新、コマンドからの実行

```
クライアント PC                                       サーバー(GB10)
  Goソース ──(git push)──> GitLab ──(git pull / HTTPS)──>  /srv/repos/myapp      ← --repo で読む
  ブラウザ ──(Web 画面 http://<GB10>:4000)────────────>  runner ── Claude Code ── vLLM
                                       GB10 の端末からコマンドでも起動できる（tmux / nohup で切り離す）
  設計書 <──(Web 画面のダウンロード / NAS / git)───────  /opt/Batch_Exec/out   ← --out に書く
```

### 作業の分担：初回だけの準備と、その後の運用

Pythonファイルを置くだけでは動かない。最初に1回だけ、次の準備を GB10 の端末で行う。

| 初回だけの準備（詳細は「サーバー(GB10)へのセットアップ」） | sudo |
|---|---|
| vLLM の起動（コンテキスト長・別名など）とモデルの確認 | 要 |
| Claude Code のインストール | 不要 |
| このリポジトリの clone、venv 作成、`pip install` | `/opt` 配下なら要 |
| `.env` の作成（`CLAUDE_BIN` など） | 不要 |
| Web 画面の設置（[docs/other/web_ui.md](docs/other/web_ui.md)） | 要 |
| SKILL.md（設計書・コメント追記）の配置（Web 画面の「スキル」タブから登録できる） | 不要 |
| Goリポジトリの clone（HTTPS）と、パスワードなしで `git pull` できる設定（アクセストークン。[docs/other/web_ui.md](docs/other/web_ui.md#gitlab-の認証https-とアクセストークン)） | 不要 |
| GB10 のスリープ無効化（「端末を閉じてよいか」参照） | 要 |
| 定期実行の設定（使う場合。Web 画面の「定期実行」か「定期実行（systemd タイマー）」） | タイマーなら要 |

準備が済んだ後の運用は、次のとおり。

| 作業 | Web 画面 | GB10 の端末 |
|---|---|---|
| 設計書の生成 | 「実行」タブで実行。定期実行なら毎日自動 | `tmux new -d ...`（「2. 端末を閉じても止まらないように起動する」） |
| 進捗・結果の確認 | ログ・実行履歴・セッション欄 | `tail -n 20 /opt/Batch_Exec/out/_runs/<run-id>.log` |
| 設計書の取得 | 生成物の一覧から「ダウンロード」 | NAS へのコピー・git（「3. 設計書を取得する」） |
| SKILL.md の更新 | 「スキル」タブで登録し直す（次の実行から反映される） | `skills/` 配下を上書きする |
| このリポジトリの更新 | — | `cd /opt/Batch_Exec && git pull && .venv/bin/pip install -r requirements.txt` の後、`sudo systemctl restart godoc-web`（`.env` だけを変えたときは `sudo systemctl reload godoc-web`） |

注意:

- **sudo が必要な作業は GB10 の端末で行う**。Web 画面からはできない
- 停電などでサーバーが停止した後の電源投入は、物理的な操作（またはリモート電源管理）が必要。起動すれば vLLM（systemd 化した場合）と Web 画面は自動で立ち上がり、定期実行も逃した分を起動後に実行する

### 1. Goリポジトリをサーバーに置く

`--repo` はサーバー上のパスとして直接読み込まれる。クライアントからソースを送る機能はない（Web 画面もサーバー上のフォルダを選ぶだけ）。

置き場所は任意で、このREADMEの `/srv/repos/myapp` は例にすぎない。置いたパスを `--repo` に指定すればよい。

- `/srv` 配下は管理者権限が必要なため、先に実行ユーザーが書き込めるようにする（例：`sudo mkdir -p /srv/repos && sudo chown $USER /srv/repos`）。こだわりがなければ `~/repos/myapp` などホームディレクトリ配下が手軽
- runner はGoソースを読むだけで、リポジトリの中身は変更しない
- `xx_api` が複数ある場合は、**それらの親ディレクトリを `--repo` に指定する**（理由は「3. 設計書を取得する」の注意を参照）

置き方:

- **git（推奨）**：サーバーで `git clone`（HTTPS）し、実行前に `git pull` する。どのコミットから生成したかが明確になる
- **NAS**：NAS 上のフォルダを GB10 にマウントして対象にする（[docs/other/nas_target.md](docs/other/nas_target.md)）
- **持ち込み**：未コミットの変更も対象にしたい場合は、USB メモリなどで GB10 に持ってきてコピーする（SSH を使う `rsync` / `scp` は使えない）

### 2. 端末を閉じても止まらないように起動する

Web 画面から実行する場合は、この節は不要（runner は Web 画面のサービスの下で動き、ブラウザを閉じても止まらない）。

GB10 の端末で `python -m godoc_agent.runner ...` のように直接起動すると、プロセスがその端末にぶら下がる。
端末を閉じたり GB10 からログアウトしたりすると処理も止まるため、**全件実行では必ず tmux か nohup で切り離す**。

```bash
# A. tmux（後から接続して進捗を見られる。推奨）
tmux new -d -s godoc 'cd /opt/Batch_Exec && mkdir -p out/_runs && .venv/bin/python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --run-id nightly-20261102 2>&1 | tee out/_runs/nightly-20261102.log'

# B. nohup（tmuxがない場合）
cd /opt/Batch_Exec && mkdir -p out/_runs && nohup .venv/bin/python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --run-id nightly-20261102 > out/_runs/nightly-20261102.log 2>&1 < /dev/null &
```

- `--run-id` は計測ログ・標準出力ログのファイル名になる。同じ値で再実行すると同じログに追記されるので、明示しておくと追いやすい
- 万一止まっても、同じコマンドで再実行すれば完了済みのファイルを飛ばし、残りを最初から処理する
- `--limit 5` 程度のPoC実行で、終わるまで端末を開いておけるなら直接起動でもよい（この場合は端末を閉じると止まる）

進捗の確認:

```bash
tail -n 20 /opt/Batch_Exec/out/_runs/nightly-20261102.log   # ログの末尾
tmux attach -t godoc                                        # tmuxの場合（Ctrl-b d で離脱）
```

### 端末を閉じてよいか

tmux / nohup で起動していれば、**起動後は端末を閉じて GB10 からログアウトしてよい**。Web 画面から実行した場合は、ブラウザを閉じてもクライアント PC を落としてもよい。
端末を閉じる前に次の2点を確認する。

1. 起動コマンドが戻ってきている（tmux / nohup はバックグラウンドに切り離して即座に戻る）
2. サーバーで処理が始まっている

   ```bash
   tail -n 5 /opt/Batch_Exec/out/_runs/nightly-20261102.log
   ```

   `run_id=... 対象=N 件` の後に `[1/N] done ...` のような進捗行が出ていればよい。すぐに `エラー:` が出ている場合は、vLLM・Claude Code・`.env` の設定を見直す

端末を閉じても、サーバー側の要因では処理が止まる。

| 要因 | 対策 |
|---|---|
| GB10 のスリープ・自動サスペンド（Ubuntuデスクトップ版では既定で有効な場合がある） | `sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target` で無効にする |
| サーバーの再起動、vLLM の停止 | 同じコマンドで再実行すれば、完了済みを飛ばして残りを処理する |

後で確認するときは、ログの末尾にサマリー（`対象: ... 件 / 経過時間: ...`）が出ていれば完了している。
失敗した件があった場合や途中で止まっていた場合は、同じコマンドで再実行すると残りだけが（最初から）処理される。完了したら次の手順で設計書を取得する。

### 3. 設計書を取得する

設計書はサーバー上の `--out` 配下に、ソースファイルと同じフォルダ構成で `詳細設計書_<拡張子を除いたファイル名>.md` として作られる（例：`user.go` → `詳細設計書_user.md`）。
以前の名前（`user.go.md`）の設計書が残っていれば、次の実行時に新しい名前へ移し、履歴（版）を引き継ぐ。
拡張子だけが違うソース（`user.go` と `user.ts` など）が同じフォルダにあると出力先が重なるため、2つ目以降は `error` として記録し生成しない。
`--out ./out` のような相対パスは実行時のカレントディレクトリが基準になるため、このREADMEの手順（`/opt/Batch_Exec` で実行）では `/opt/Batch_Exec/out/` になる。

```
--repo /srv/repos/myapp  --out ./out （/opt/Batch_Exec で実行）の場合

/srv/repos/myapp/user_api/pkg/handler/user.go    → /opt/Batch_Exec/out/user_api/pkg/handler/詳細設計書_user.md
/srv/repos/myapp/order_api/pkg/handler/order.go  → /opt/Batch_Exec/out/order_api/pkg/handler/詳細設計書_order.md
/opt/Batch_Exec/out/.manifest.json                 … 生成済みファイルの記録（差分スキップ用）
/opt/Batch_Exec/out/_runs/<run-id>.jsonl           … 計測ログ
/opt/Batch_Exec/out/_commented/user_api/pkg/handler/user.go … コメント追記済みのファイル（コメント追記を有効にした場合）
```

> **注意：`--repo` に `xx_api` 自体を指定する場合**
> 出力パスに API 名が含まれなくなるため、複数の API を同じ `--out` に出力すると、同名ファイルが上書きされ `.manifest.json` も混ざる。
>
> ```
> --repo /srv/repos/user_api  → out/pkg/handler/詳細設計書_user.md
> --repo /srv/repos/order_api → out/pkg/handler/詳細設計書_order.md   ← 同じ out に混在する
> ```
>
> `xx_api` の親ディレクトリを `--repo` に指定する（推奨）か、API ごとに `--out ./out/user_api` のように出力先を分けること。

クライアントへの取得方法（SSH を使う `scp` は使えない）:

- **Web 画面**：生成物の一覧からファイルを選び、「ダウンロード」で保存する（1ファイルずつ）
- **NAS**：GB10 に NAS をマウントしていれば、出力先を NAS へコピーし、クライアント PC からは NAS の共有フォルダで受け取る（[docs/other/nas_target.md の「生成物を NAS にも置きたい場合」](docs/other/nas_target.md#8-生成物を-nas-にも置きたい場合)）
- **git**：`--out /srv/repos/myapp/docs/design` のようにGoリポジトリ内へ出力し、サーバーでコミット・push すれば、クライアントは `git pull` で受け取れる（push には書き込み権限のあるトークンが必要。読み取り専用のトークンとは分ける）

## 定期実行（systemd タイマー）

> Web 画面を使う場合は、画面の「定期実行」で同じことができる（時刻などを画面から変えられる）。その場合はこのタイマーは設置しない（設置済みなら `sudo systemctl disable --now godoc-nightly.timer` で止める）。詳しくは [docs/other/web_ui.md](docs/other/web_ui.md) を参照。

毎晩決まった時刻に、Goリポジトリを `git pull` してから設計書を生成する。変更のないファイルはスキップされるため、毎日実行しても処理されるのは変更されたファイルだけ。

| ファイル | 役割 |
|---|---|
| [deploy/nightly.sh](deploy/nightly.sh) | `git pull --ff-only` → runner 実行。`git pull` に失敗したら生成しない（古いソースから作らないため） |
| [deploy/systemd/godoc-nightly.service](deploy/systemd/godoc-nightly.service) | 上のスクリプトを1回実行する（実行ユーザー・Goリポジトリのパスをここで指定） |
| [deploy/systemd/godoc-nightly.timer](deploy/systemd/godoc-nightly.timer) | 毎日 2:00 に service を起動する |

### 設置

前提：README「サーバー(GB10)へのセットアップ」が済み、手動実行で設計書が生成できていること。

```bash
cd /opt/Batch_Exec

# 1. .env の CLAUDE_BIN を claude のフルパスにする（systemd の PATH には ~/.local/bin が入らない）
which claude          # 例: /home/<ユーザー>/.local/bin/claude → CLAUDE_BIN=/home/<ユーザー>/.local/bin/claude

# 2. 実行ユーザーで、Goリポジトリの git pull がパスワード入力なしでできることを確認する
#    （HTTPS とアクセストークン。設定方法は docs/other/web_ui.md の「GitLab の認証（HTTPS とアクセストークン）」）
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
tail -n 20 /opt/Batch_Exec/out/_runs/nightly-$(date +%Y%m%d).log   # 同じ内容のファイルログ
sudo systemctl disable --now godoc-nightly.timer    # 定期実行を止める
```

- 実行時刻を変えるには `sudo systemctl edit --full godoc-nightly.timer` で `OnCalendar=` を直す（例：`*-*-* 23:30:00`）
- `--run-id` は `nightly-<日付>`。同じ日に `systemctl start` で再実行すると、中断・失敗したファイルだけを最初から処理し、同じ日のログに追記する
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

入力トークン数の警告は、コンテキスト上限に近い実行を後から見つけるためのもの（Ollama は `OLLAMA_CONTEXT_LENGTH` を超えた分を黙って切り捨て、vLLM は `--max-model-len` を超えるとリクエストごと失敗する）。`LLM_NUM_CTX` は vLLM の `--max-model-len`（Ollama なら `OLLAMA_CONTEXT_LENGTH`）と**同じ値にする**（ずれると判定もずれる）。

確認事項（PoCで確認する）：

- 推論サーバー（vLLM）の Anthropic 互換APIが `usage.input_tokens` などを実際に返すか（返さなければ `null` になり、警告も出ない）
- Claude Code の `usage` が「最後の1ターンの値」か「その実行の全ターンの合計」か。合計の場合は実際のコンテキストより大きく出るため、`num_turns` と合わせて見る

## 既知の制約（技術的負債）

- **事実抽出は正規表現による簡易版**。複雑な宣言を取りこぼす可能性がある。本番では `go/ast` を使う補助ツールに置き換える
- **入力の切り捨てを検出できない**。Claude Code はコンテキストの大きさを知らないまま会話を伸ばす（Ollama は超えた分を黙って切り捨て、vLLM はその実行が `error` になる）。大きすぎるファイルは「1トークン≒3文字」の概算で事前に `too_large` とし、ターン数は `CLAUDE_MAX_TURNS` で抑えている。実行後は入力トークン数が上限に近ければ `warnings` に残すが、切り捨てそのものを検出するわけではなく、`usage` が返らなければ警告も出ない
- **再生成は新しいセッションで行う**。以前は同じセッションを `--resume` して指摘だけを送っていたが、試行のたびにツールで読んだ内容が積み上がりコンテキストを超えたため、毎回「最初の依頼＋直前の出力＋指摘」を送り直す形にした。代わりに、再生成では関連ファイルを読み直す分の時間がかかり、入力は直前の出力1つ分だけ初回より大きくなる
- 安定性は、ローカルモデルが Claude Code のツールを正しく使えるかに左右される。同じ操作の繰り返しや途中終了は `error`（`error_max_turns` など）として記録される
- 設計書の構成（見出し）は検証しない。SKILL.md どおりの構成になっているかは、PoCで出力を確認する
- コメント追記の検証は、Pythonで書いた簡易な字句解析でコメントと空白を除いて比較している。コードの同一性は保証するが、出力が `gofmt` 済みかどうか・コンパイルできるかは確認しない（取り込む前に `gofmt -l` / `go build` で確認する）
- チェックポイント（`state/checkpoints.sqlite`）は使わなくなった。更新前の版で作られたファイルは不要なので削除してよい。`.env` の `CHECKPOINT_DB` も読まれない（差分スキップの記録 `.manifest.json` はそのまま使える）
- 履歴の引き継ぎ（前回までの行が残っているか、版が上がっているか）は検証しない。ローカルモデルが履歴を書き換えたり落としたりしないかは、PoCで2回目以降の出力を確認する
- パッケージ単位の集約、人による承認（interrupt）は未実装（次のフェーズ）
- LangSmithのトレースはスキル・プロンプト・生成された設計書を外部に送信するため、既定では無効
