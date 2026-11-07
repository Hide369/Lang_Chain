# チェックポイント廃止・再生成の見直し版への移行手順（サーバー）

コンテキスト超過エラーへの対策を入れた版（PR #21）に、サーバー（GB10）を入れ替える手順をまとめる。
コマンドはすべてクライアントのターミナルから実行する。sudo は不要。

## 0. この版で変わること

| 変更 | 以前 | この版 |
|---|---|---|
| チェックポイント | SQLite（`state/checkpoints.sqlite`）に保存し、同じ `--run-id` で再実行すると途中から再開した | 保存しない。毎回まっさらな状態から実行する |
| 中断・失敗後の再実行 | 失敗した時点の会話から再開した（膨らんだ会話を引き継ぎ、同じエラーを繰り返すことがあった） | 完了済みのファイルは差分スキップで飛ばし、残りを最初から処理する |
| 検証NG時の再生成 | 同じセッションを `--resume` し、指摘だけを送った（試行のたびに会話が伸びた） | 新しいセッションで、最初の依頼＋直前の出力＋指摘を送る（入力は「初回＋直前の出力1つ分」で頭打ち） |
| `--run-id` | 再開に使うセッションの識別に使った | 計測ログ・標準出力ログのファイル名を決めるためだけに使う |

`--run-id` の指定や `deploy/nightly.sh`・systemd の使い方は変わらない。

## 1. 事前確認

### 1-1. 実行中のバッチがないこと

入れ替え中にバッチが動いていると、途中で別の版のプログラムに変わってしまう。止まっていることを確認する。

```bash
ssh gb10 "systemctl is-active godoc-nightly.service; tmux ls 2>/dev/null; pgrep -af godoc_agent.runner"
```

`inactive` と表示され、`tmux ls` と `pgrep` が何も表示しなければよい。
動いている場合は、終わるのを待つ（止めた場合も、この版では再実行すれば残りが最初から処理される）。

### 1-2. サーバーで直接書き換えたファイルがないか

暫定対応として、サーバー上の `godoc_agent/runner.py` をインメモリのチェックポイントに書き換えている場合がある。
書き換えたままだと `git pull` が失敗するため、差分を確認する。

```bash
ssh gb10 "cd /opt/Lang_Chain && git status --short"
```

| 表示 | 対応 |
|---|---|
| 何も表示されない | そのまま「2. プログラムを更新する」へ進む |
| ` M godoc_agent/runner.py` | 次の手順で書き換えを退避してから進む（この版に同じ対策が入っているため、書き換えは不要になる） |
| ほかのファイルも ` M` | 何を変えたか `git diff` で確認してから、同じように退避する |
| `??`（`out/`・`state/`・`.env` など Git の管理外のファイル） | そのままでよい |

```bash
# 書き換えた内容を確認する
ssh gb10 "cd /opt/Lang_Chain && git diff godoc_agent/runner.py"
# 退避する（消さずに残すので、あとから git stash show -p で見返せる）
ssh gb10 "cd /opt/Lang_Chain && git stash push -m 'tmp: in-memory checkpointer' godoc_agent/runner.py"
# 確認（runner.py が表示されなければよい）
ssh gb10 "cd /opt/Lang_Chain && git status --short"
```

## 2. プログラムを更新する（`git pull`）

```bash
ssh gb10 "cd /opt/Lang_Chain && git pull"
```

次の16ファイルが自動で入れ替わる。手作業は不要。

| 種類 | ファイル |
|---|---|
| 変更したプログラム | `godoc_agent/claude_code.py`・`config.py`・`doc_graph.py`・`prompts.py`・`runner.py`・`tasks.py` |
| 設定の例・依存 | `.env.example`・`requirements.txt`（`langgraph-checkpoint-sqlite` を削除） |
| 夜間実行 | `deploy/nightly.sh`（コメントのみ） |
| ドキュメント | `README.md`・`docs/workflow.md`・`docs/migration_multi_language.md` |
| テスト | `tests/` 配下の4ファイル |

`requirements.txt` からは削除しただけなので、`pip install` は不要。
使わなくなったパッケージを消したい場合だけ、次を実行する（残っていても動作に影響はない）。

```bash
ssh gb10 "/opt/Lang_Chain/.venv/bin/pip uninstall -y langgraph-checkpoint-sqlite"
```

## 3. サーバー上で片付けるもの（Git の管理外）

| 対象 | 作業 | 必須か |
|---|---|---|
| `state/checkpoints.sqlite` | 削除する（もう読まれない） | 任意（残っていても使われない） |
| `.env` の `CHECKPOINT_DB=...` | 行を削除する（もう読まれない） | 任意（書いていなければ不要） |

```bash
# 1. チェックポイントを削除する（ls で中身を確認してから消す）
ssh gb10 "cd /opt/Lang_Chain && ls -la state/ && rm -f state/checkpoints.sqlite*"
# 2. .env から CHECKPOINT_DB の行を削除する
ssh gb10 "cd /opt/Lang_Chain && sed -i '/^CHECKPOINT_DB=/d' .env"
# 3. 確認（0 と表示されればよい）
ssh gb10 "grep -c '^CHECKPOINT_DB=' /opt/Lang_Chain/.env"
```

`checkpoints.sqlite*` としているのは、SQLite が作る `-wal`・`-shm` ファイルも一緒に消すため。

## 4. 変更しないもの

| 対象 | 理由 |
|---|---|
| `.env` のほかの設定 | 設定項目は `CHECKPOINT_DB` の削除だけ |
| `out/`（設計書・manifest） | 出力先と差分スキップの記録の形式は変わっていない。生成済みの設計書はそのまま次回の改訂に使われる |
| `out/_runs/`（過去の計測ログ） | 形式はほぼ同じ（各行から `thread_id` がなくなっただけ） |
| `skills/`・`targets.toml` | スキルと処理対象の決め方は変わっていない |
| `deploy/systemd/` | 起動のしかたは変わっていない |

## 5. 入れ替え後の確認

```bash
# テスト（Ollama・Claude Code 不要）。「102 passed」と表示されればよい
ssh gb10 "cd /opt/Lang_Chain && .venv/bin/python -m pytest"
```

次に、件数を絞って1回実行する（`/srv/repos/myapp` は実際の `--repo` のパスに置き換える）。
以前コンテキスト超過で失敗したファイルが分かっていれば、それが対象に入るようにすると効果を確かめやすい。

```bash
ssh gb10 "cd /opt/Lang_Chain && .venv/bin/python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --limit 3 --run-id check-pr21"
```

### 5-1. 再生成で入力が伸びていないことを確認する

計測ログ（1ファイル×タスク = 1行）の `llm_calls` に、試行ごとの入力トークン数（`total_input_tokens`）が入っている。
次のコマンドで、ファイルごとに試行ごとの値を表示する。

```bash
ssh gb10 '/opt/Lang_Chain/.venv/bin/python - /opt/Lang_Chain/out/_runs/check-pr21.jsonl' <<'EOF'
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    log = json.loads(line)
    tokens = [c.get("total_input_tokens") for c in log.get("llm_calls") or []]
    print(f"{log['status']:<17} {log['task']:<7} {log['rel_path']}  入力トークン(試行ごと)={tokens}")
EOF
```

| 見え方 | 判断 |
|---|---|
| 試行が1回だけ（`[12345]`） | 再生成が起きていない。判断材料にならないので、ほかのファイルや夜間実行のログでも確認する |
| 2回目以降が1回目とほぼ同じ（`[30000, 33000]`） | 期待どおり。増える分は直前の出力1つ分 |
| 試行のたびに大きく増える（`[30000, 60000, 90000]`） | 古い版のまま動いている。2. の `git pull` ができているか確認する |
| `None` | Ollama が使用量を返していない。この方法では確認できない |

終了時の表示や計測ログの `warnings` に「入力が切り捨てられた可能性があります」が出る場合、1回の試行の中で入力が大きすぎる。
ファイル自体が大きいか、エージェントがツールで読む量が多い。`CLAUDE_MAX_TURNS` を下げるか、そのファイルを対象から外すことを検討する。

## 6. 元に戻す場合

```bash
# PR #21 より前の版に戻す（戻した後は main に戻すまで git pull しない）
ssh gb10 "cd /opt/Lang_Chain && git checkout 1a57d8d"
# 削除していた依存を入れ直す
ssh gb10 "cd /opt/Lang_Chain && .venv/bin/pip install -r requirements.txt"
# 1-2. で退避した暫定対応（インメモリ化）も戻す場合
ssh gb10 "cd /opt/Lang_Chain && git stash list && git stash pop"
```

元に戻した版では、チェックポイント（`state/checkpoints.sqlite`）は初回の実行で新しく作られる。
main に戻すときは、`git stash` で退避した変更を消して（`git stash drop`）から `git checkout main && git pull` する。
