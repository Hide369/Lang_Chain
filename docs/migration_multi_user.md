# 更新手順：vLLM を 11434 番で使う・設定を .env に一本化・複数人での同時実行

この版をサーバー(GB10)に反映する手順。コマンドは GB10 の端末で実行する。

## 0. この版で変わること

| 変更 | 以前 | この版 |
|---|---|---|
| 推論サーバーの接続先 | `LLM_BASE_URL` の既定 `http://127.0.0.1:8000`。なければ `OLLAMA_BASE_URL` も読んだ | 既定 `http://127.0.0.1:11434`（Ollama から移行したサーバーで、vLLM を同じ 11434 番で待ち受けているため）。`OLLAMA_BASE_URL` は読まない |
| 設定の置き場所 | 夜間バッチの対象・出力先（`GODOC_REPO`・`GODOC_OUT`）は systemd ユニットの `Environment=` | すべて `.env`。`deploy/nightly.sh` も `.env` を読む |
| 同時セッション数 | `.env` の `LLM_CONCURRENCY`（既定 2。vLLM の `--max-num-seqs` と揃える） | `.env` では設定しない（`LLM_CONCURRENCY` は廃止。残っていても無視される）。1回の実行の既定は 10（画面・`--concurrency` で 1〜10） |
| サーバー全体の上限 | なし（Web 画面の実行は同時に1つだけ） | Web 画面の全員の実行・夜間バッチ・コマンドの合計で、Claude Code のセッションは同時に 10 まで（`state/llm_slots/` のファイルロック）。空きがなければ空くまで待つ |
| 複数人での利用 | 対象・出力先フォルダは全員で共通、実行は同時に1つ | フォルダはブラウザごとに選ぶ。出力先が違えば同時に実行できる（最大10件）。同じ出力先では同時に1つ |
| コンテキスト超過（256K） | 大きすぎるファイル（`too_large`）や vLLM に断られたファイルは失敗（終了コード 1、画面は「失敗」） | 失敗にせず**警告付きでスキップ**して続ける（計測ログの `status` が `too_large` / `context_overflow`、`warnings` に理由）。再生成もしない |
| 定期実行のフォルダ | 画面で選んでいるフォルダ（全員共通） | 定期実行の設定を保存した人が、そのとき選んでいたフォルダ。以前に保存した設定は、最後に選ばれたフォルダで動く（以前と同じ） |

## 1. コードを更新する

```bash
cd /opt/Lang_Chain
git pull
```

## 2. .env を直す

```bash
nano /opt/Lang_Chain/.env
```

- `LLM_BASE_URL=http://127.0.0.1:11434` にする（vLLM の `--port` と同じ）
- `LLM_CONCURRENCY` と `OLLAMA_BASE_URL` の行を消す（どちらももう読まない）
- 夜間タイマー（`godoc-nightly.timer`）を使っている場合は、ユニットに書いていた値を移す

```bash
GODOC_REPO=/srv/repos/myapp
# 出力先を変えていた場合だけ
# GODOC_OUT=/opt/Lang_Chain/out
```

確認する（`LLM_BASE_URL` が 11434 で、`LLM_CONCURRENCY`・`OLLAMA_BASE_URL` が出なければよい）。

```bash
grep -E '^(LLM_BASE_URL|LLM_CONCURRENCY|OLLAMA_BASE_URL|GODOC_REPO|GODOC_OUT)=' /opt/Lang_Chain/.env
curl -s http://127.0.0.1:11434/v1/models   # claude-opus-4-6 が出ること
```

## 3. 夜間タイマーのユニットから Environment= を消す（使っている場合だけ）

設定が2か所にあると、`.env` を直しても反映されないことがあるため、ユニット側の `Environment=GODOC_REPO=...` を消す。

```bash
sudo cp deploy/systemd/godoc-nightly.service /etc/systemd/system/
sudo sed -i "s/REPLACE_WITH_USER/$USER/" /etc/systemd/system/godoc-nightly.service
# systemctl edit で上書きしていた場合は、そちらの Environment=GODOC_REPO=... も消す
sudo systemctl edit godoc-nightly.service
sudo systemctl daemon-reload
systemctl show godoc-nightly.service -p Environment   # GODOC_REPO が出なければよい
```

## 4. Web 画面を再起動する

Web 画面のコード（複数人対応）が変わったため、reload ではなく restart する（実行中のバッチは止まるので、終わってから行う）。

```bash
sudo systemctl restart godoc-web.service
systemctl status godoc-web.service            # active (running) ならよい
```

## 5. 動作確認

1. 2台のブラウザ（または通常ウィンドウとシークレットウィンドウ）で画面を開き、それぞれ管理者でログインする
2. それぞれ別の出力先フォルダを選んで「実行」を押す。両方とも実行中になり、状態欄に「サーバー全体の Claude Code セッション: 使用中 n / 10」と「他の実行」が出ればよい
3. 片方で「停止」を押すと、そのブラウザの実行だけが止まる

## 補足：vLLM 側の設定

- アプリはサーバー全体で同時に 10 セッションまでしか送らないため、vLLM の `--max-num-seqs` は指定しなくてよい
- 10 セッション × 長い入力が KV キャッシュに入りきらない場合、vLLM は一部のリクエストを順番待ち（プリエンプション）にする。遅くはなるが落ちはしない
- 入力が `--max-model-len`（256K）を超えたリクエストは vLLM がエラーで断る（vLLM は落ちない）。アプリはそのファイルを警告付きでスキップして残りを続ける
