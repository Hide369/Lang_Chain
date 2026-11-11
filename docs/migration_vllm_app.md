# vLLM 対応版の設置手順（サーバー）

推論エンジンを Ollama から vLLM に替えたサーバー（GB10）に、アプリの vLLM 対応版を設置する手順をまとめる。
コマンドはすべて GB10 の端末で実行する（SSH は使えないため、GB10 に直接ログインした端末を使う）。systemd のユニットを入れ直すため sudo が要る。

vLLM 自体の導入・起動は [vllm_migration.md](vllm_migration.md) の手順1〜4で済ませておく。この手順書はその後、アプリ（Web 画面・夜間バッチ）を vLLM に向ける部分だけを扱う。

## 0. この版で変わること

| 変更 | 以前 | この版 |
|---|---|---|
| 推論サーバーの接続先 | `.env` の `OLLAMA_BASE_URL`（既定 `http://127.0.0.1:11434`） | `.env` の `LLM_BASE_URL`（既定 `http://127.0.0.1:8000`）。なければ `OLLAMA_BASE_URL` を使う |
| モデル名 | `ollama list` のタグ名（`qwen3-coder-next`） | vLLM の `--served-model-name` の別名（`claude-sonnet-4-5`） |
| 実行前の事前チェック | Ollama 固有の API（`/api/show`）。vLLM では必ず失敗した | OpenAI 互換の `/v1/models`。vLLM・Ollama の両方で動く |
| systemd のユニット | `Wants=`・`After=ollama.service` を持っていた | 推論サーバーに依存しない（vLLM の `Conflicts=ollama.service` と衝突しない） |
| `systemctl reload godoc-web` | 非対応（`Job type reload is not applicable` で失敗） | `.env` を読み直す。実行中のバッチは止めず、次の実行から反映する |
| 依存パッケージ | `ollama` を使っていた | 使わない（`requirements.txt` から削除） |

`deploy/nightly.sh`・runner のコマンドライン・Web 画面の操作は変わらない。

## 1. 事前確認

### 1-1. vLLM が別名で応答すること

```bash
# 提供中のモデル名（claude-sonnet-4-5 が出ること）
curl -s http://127.0.0.1:8000/v1/models

# Claude Code が使う Anthropic 互換API（/v1/messages）が応答すること
curl -s http://127.0.0.1:8000/v1/messages \
  -H "Content-Type: application/json" -H "x-api-key: local" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-sonnet-4-5","messages":[{"role":"user","content":"こんにちは"}],"max_tokens":64}'
```

| 結果 | 対応 |
|---|---|
| どちらも JSON が返る | 次へ進む |
| `Connection refused` | vLLM が止まっている。[vllm_migration.md](vllm_migration.md) の手順3で起動する |
| `/v1/models` に別の名前しか出ない | vLLM を `--served-model-name claude-sonnet-4-5` で起動し直す（または後の `.env` の `LLM_MODEL` を出ている名前に合わせる） |
| `/v1/messages` だけ `404` | vLLM が古く Anthropic 互換API がない。新しいタグのコンテナで起動し直す |

### 1-2. 実行中のバッチがないこと

設置の途中で Web 画面を再起動すると、実行中のバッチも止まる。止まっていることを確認する。

```bash
systemctl is-active godoc-nightly.service; pgrep -af godoc_agent.runner
```

`inactive` と表示され、`pgrep` が何も表示しなければよい。動いている場合は終わるのを待つ（止めても、再実行すれば完了済みを飛ばして残りを処理する）。

### 1-3. 戻すための記録を取る

```bash
mkdir -p ~/godoc-backup
cp /opt/Lang_Chain/.env ~/godoc-backup/env.before-vllm-app
sudo cp /etc/systemd/system/godoc-web.service ~/godoc-backup/
sudo cp /etc/systemd/system/godoc-nightly.service ~/godoc-backup/ 2>/dev/null   # 夜間タイマーを設置していない場合は無視してよい
git -C /opt/Lang_Chain rev-parse HEAD > ~/godoc-backup/commit.txt
```

## 2. プログラムを更新する（`git pull`）

```bash
cd /opt/Lang_Chain && git status --short   # ` M` の行があれば、先に git stash などで退避する
cd /opt/Lang_Chain && git pull
```

`requirements.txt` からは `ollama` を削除しただけなので、`pip install` は不要。
使わなくなったパッケージを消したい場合だけ、次を実行する（残っていても動作に影響はない）。

```bash
/opt/Lang_Chain/.venv/bin/pip uninstall -y ollama
```

テストを実行して、更新が正しく入ったことを確認する（vLLM・Claude Code は使わない）。

```bash
cd /opt/Lang_Chain && .venv/bin/python -m pytest -q   # 最後に「N passed」と出ればよい
```

## 3. `.env` を vLLM 向けに変える

```bash
nano /opt/Lang_Chain/.env
```

次の4つを、vLLM の起動オプションと同じ値にする。`OLLAMA_BASE_URL` の行は消す（残しても `LLM_BASE_URL` が優先されるが、紛らわしいため）。

```bash
LLM_BASE_URL=http://127.0.0.1:8000   # vLLM の --port
LLM_MODEL=claude-sonnet-4-5          # vLLM の --served-model-name
LLM_NUM_CTX=262144                   # vLLM の --max-model-len
LLM_CONCURRENCY=2                    # vLLM の --max-num-seqs
```

```bash
# 確認（4行が上の値で出て、OLLAMA_BASE_URL が出なければよい）
grep -E '^(LLM_BASE_URL|LLM_MODEL|LLM_NUM_CTX|LLM_CONCURRENCY|OLLAMA_BASE_URL)=' /opt/Lang_Chain/.env
```

## 4. systemd のユニットを入れ直す

設置済みのユニット（`/etc/systemd/system/` の下）は `git pull` では変わらない。次の手順で入れ直す。

### 4-1. Web 画面（godoc-web.service）

`User=` は設置済みのものを引き継ぐ。

```bash
WEB_USER=$(systemctl show -p User --value godoc-web.service); echo "$WEB_USER"   # 実行ユーザーが表示されること
sudo cp /opt/Lang_Chain/deploy/systemd/godoc-web.service /etc/systemd/system/
sudo sed -i "s/REPLACE_WITH_USER/$WEB_USER/" /etc/systemd/system/godoc-web.service
```

`echo` で何も表示されなかった場合は、`$WEB_USER` の代わりに実行ユーザー名を直接書く。
`WorkingDirectory=` などを設置時に変えていた場合は、`diff ~/godoc-backup/godoc-web.service /etc/systemd/system/godoc-web.service` で見比べて直す。

### 4-2. 夜間バッチ（godoc-nightly.service。設置している場合だけ）

`GODOC_REPO` などを設置時に書き換えているため、ファイルは入れ替えず、`ollama.service` への依存だけを消す。

```bash
sudo sed -i -E '/^(Wants|After)=/ s/ ?ollama\.service//' /etc/systemd/system/godoc-nightly.service
grep -E '^(Wants|After)=' /etc/systemd/system/godoc-nightly.service   # ollama.service が出なければよい
```

### 4-3. 反映する

```bash
sudo systemctl daemon-reload
sudo systemctl restart godoc-web   # コードを更新したので、今回は reload ではなく restart
```

## 5. 動作確認

```bash
systemctl status godoc-web --no-pager                    # active (running) ならよい
systemctl show godoc-web -p Wants -p After                # ollama.service が出ないこと
systemctl show godoc-web -p ExecReload                    # kill -HUP が出ること
sudo systemctl reload godoc-web && journalctl -u godoc-web -n 5 --no-pager   # 「.env を読み直しました」が出ること
```

最後に、Web 画面（`http://<GB10>:4000`）から **1ファイルだけ** 実行し、成功することを確かめる（実行タブで件数の上限を 1 にする）。

- [ ] `systemctl status godoc-web` が `active (running)`
- [ ] `systemctl reload godoc-web` がエラーなく終わり、journal に「.env を読み直しました」が出る
- [ ] Web 画面からの1ファイルの実行が `done` になる

実行がすぐ終わった場合は、ログ（実行タブ、または `<出力先>/_runs/<run_id>.log`）のエラーで切り分ける。

| エラー | 原因 | 対処 |
|---|---|---|
| `推論サーバーに接続できません` | vLLM が止まっている、`LLM_BASE_URL` が違う | 1-1 の `curl` で確認し、`.env` を直して `sudo systemctl reload godoc-web` |
| `モデル 'xxx' が推論サーバーにありません（提供中: ...）` | `LLM_MODEL` と `--served-model-name` が違う | 「提供中」の名前に `LLM_MODEL` を揃えて `sudo systemctl reload godoc-web` |
| `推論サーバーがエラーを返しました ... (HTTP 401)` | vLLM を `--api-key` 付きで起動している | `--api-key` を外して vLLM を起動し直す（このアプリは API キーを送らない） |
| `.env を読み直しましたが、設定に誤りがあります` が journal に出る | `.env` の値の書き間違い | メッセージに出る項目を直して、もう一度 `reload` |

夜間タイマーを止めていた場合は、確認が済んでから再開する。

```bash
sudo systemctl start godoc-nightly.timer
```

## 6. 以後の運用

| 変えたもの | 反映のしかた |
|---|---|
| `.env` の `LLM_*`・`CLAUDE_*`・`DOC_*` など | `sudo systemctl reload godoc-web`（実行中のバッチは止まらず、次の実行から反映） |
| `.env` の `WEB_*`、または `.env` から行を消したとき | `sudo systemctl restart godoc-web`（reload は値の上書きだけで、消した行は残るため） |
| `git pull` でプログラムを更新したとき | `sudo systemctl restart godoc-web` |

夜間タイマー（godoc-nightly.service）は実行のたびに `.env` を読むので、どの場合も操作は不要。

## 7. 元に戻す

### 7-1. アプリはこの版のまま、Ollama に戻す

この版は Ollama でも動く。[vllm_migration.md の「Ollama に戻す手順」](vllm_migration.md#ollama-に戻す手順いつでも実行可)を行う。
その中で控えておいた `.env`（`OLLAMA_BASE_URL`・`LLM_MODEL=qwen3-coder-next`）を戻し、`sudo systemctl restart godoc-web` する（`LLM_BASE_URL` の行が消えるので reload ではなく restart）。

### 7-2. アプリを以前の版に戻す

```bash
cd /opt/Lang_Chain && git checkout "$(cat ~/godoc-backup/commit.txt)"
.venv/bin/pip install -r requirements.txt                    # ollama パッケージを入れ直す
cp ~/godoc-backup/env.before-vllm-app /opt/Lang_Chain/.env
sudo cp ~/godoc-backup/godoc-web.service /etc/systemd/system/
sudo cp ~/godoc-backup/godoc-nightly.service /etc/systemd/system/ 2>/dev/null
sudo systemctl daemon-reload && sudo systemctl restart godoc-web
```

以前の版は Ollama でしか動かないため、あわせて「Ollama に戻す手順」も行う。最新の版に戻るときは `git checkout main && git pull` から、この手順書を最初からやり直す。
