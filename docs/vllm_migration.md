# GB10 で Ollama から vLLM に切り替える手順（メモリ枯渇対策つき）

GB10 上で vLLM を起動するときは、確保メモリ量を明示的に絞り、競合プロセスを止めてから起動する。これでメモリ枯渇（OOM・OS のフリーズ）を防ぐ。

- 対象環境: GB10（CPU と GPU が 128GB のユニファイドメモリを共有）、vLLM 0.15.0 以上
- モデル: Qwen3-Coder-Next の FP8 版（`Qwen/Qwen3-Coder-Next-FP8`）。Ollama で使っている `qwen3-coder-next` と同じモデルで、コンテキスト長も同じ 256K（262144）にする。BF16 版は重みだけで約160GBあり GB10 に載らないため、FP8 版を使う
- 枯渇の主な原因: vLLM の既定 `--gpu-memory-utilization 0.9` がメモリの約9割を起動時に予約するため、OS や他プロセス（Ollama など）の分が残らない
- もう一つの原因: 長い `max-model-len` と多い同時処理数が、KV キャッシュ用に大量のメモリを予約する

コマンドはすべて、GB10 に直接ログインした端末で実行する（SSH は使えない前提）。

## 導入の流れ

現在動いている Ollama を止めて vLLM に切り替える。Ollama 本体・モデル・設定は消さないので、失敗してもいつでも戻せる。

1. 事前確認: 空きメモリと常駐プロセスを見る
2. 準備: Ollama の設定と `.env` を控え、夜間バッチを止める
3. 手順1: Ollama を止める（アンインストールはしない）
4. 手順1.5: vLLM を導入する（初回のみ）
5. 手順2〜4: キャッシュを解放し、vLLM を起動して動作を確認する
6. Qwen3-Coder-Next が動かなければ「[gpt-oss-120b に切り替える手順](#gpt-oss-120b-に切り替える手順qwen3-coder-next-が失敗したとき)」で vLLM のままモデルを替える
7. それでも失敗したら「[Ollama に戻す手順](#ollama-に戻す手順いつでも実行可)」で元に戻す（どの段階からでも実行できる）
8. vLLM をもう使わないなら「[vLLM を削除する手順](#vllm-を削除する手順やめる場合)」で片付ける

## アカウント登録の要否

アカウント登録なしで導入できる。vLLM 本体・モデルとも公開されており、ログインやトークンは要らない（インターネット接続は必要）。

| 必要なもの | 入手先 | アカウント |
| --- | --- | --- |
| vLLM 本体（コンテナ） | NVIDIA NGC `nvcr.io/nvidia/vllm` | 不要（公開イメージ。`docker login` なしで pull できる） |
| モデル Qwen3-Coder-Next（FP8） | Hugging Face `Qwen/Qwen3-Coder-Next-FP8` | 不要（Apache-2.0、利用申請のないモデル） |
| 予備のモデル gpt-oss-120b | Hugging Face `openai/gpt-oss-120b` | 不要（Apache-2.0、利用申請のないモデル） |
| Docker・NVIDIA Container Toolkit | DGX OS に同梱 | 不要 |

- `docker pull` で `unauthorized` と出た場合だけ、NGC の無料アカウントで API キーを作り `docker login nvcr.io` する
- モデルのダウンロードが `401` や `gated` で失敗した場合だけ、Hugging Face のアカウントでトークンを作り、`docker run` に `-e HF_TOKEN=<トークン>` を付ける
- 参照: [NVIDIA DGX Spark vLLM プレイブック](https://build.nvidia.com/spark/vllm)、[Qwen/Qwen3-Coder-Next-FP8](https://huggingface.co/Qwen/Qwen3-Coder-Next-FP8)、[DGX Spark で Qwen3-Coder-Next を動かした報告（NVIDIA フォーラム）](https://forums.developer.nvidia.com/t/how-to-run-qwen3-coder-next-on-spark/359571)

## 事前確認

起動前に、空きメモリと常駐プロセスを確認する。GB10 では `nvidia-smi` のメモリ欄が実態を示さないことがあるため、`free -h` を主に見る。

```bash
# 空きメモリ（available 列を見る）
free -h

# GPUを使っているプロセス
nvidia-smi

# メモリ使用量の多い順にプロセスを表示
ps aux --sort=-%mem | head -n 15

# Ollamaがモデルを保持していないか
ollama ps
```

- [ ] Ollama を止めた後に、`available` が 105GB 以上あること（Qwen3-Coder-Next FP8 の重み約85GB ＋ KV キャッシュ分）
- [ ] Ollama・別の vLLM・SGLang など、モデルを保持するプロセスが動いていないこと
- [ ] ディスクの空きが 100GB 以上あること（`df -h ~`。モデル約85GB ＋ コンテナイメージ）

## 準備: 戻すための記録を取る

vLLM に触る前に、今の Ollama の設定とアプリの `.env` を控える。「Ollama に戻す手順」でこの記録と照らし合わせる。

```bash
mkdir -p ~/ollama-backup

# Ollama の systemd 設定（OLLAMA_CONTEXT_LENGTH など）と自動起動の有無
systemctl cat ollama > ~/ollama-backup/ollama-unit.txt
systemctl show ollama -p Environment > ~/ollama-backup/ollama-env.txt
systemctl is-enabled ollama > ~/ollama-backup/ollama-enabled.txt

# モデル一覧
ollama list > ~/ollama-backup/ollama-list.txt

# アプリの設定（パスは自分の環境に合わせる）
cp /opt/Lang_Chain/.env ~/ollama-backup/env.bak

# Web画面・夜間バッチの状態
systemctl is-active godoc-web godoc-nightly.timer > ~/ollama-backup/godoc-state.txt
```

vLLM を試している間は Ollama が止まるため、設計書の生成は失敗する。夜間バッチを使っている場合は止めておく。

```bash
sudo systemctl stop godoc-nightly.timer
```

## 手順1: 競合プロセスを止める

Ollama はモデルをメモリに保持したままになるため、vLLM と同じ GB10 で同時に動かさない。

```bash
# 読み込み中のモデルをアンロード
ollama stop <モデル名>

# Ollamaサービス自体を停止（自動起動も止める場合は disable）
sudo systemctl stop ollama
sudo systemctl disable ollama   # 任意

# 前回のvLLMが残っていれば停止
pkill -f "vllm serve"
```

停止後に `free -h` を再実行し、`available` が増えたことを確認する。

Ollama はアンインストールしない。モデル（`/usr/share/ollama/.ollama/models`）と `sudo systemctl edit ollama` で入れた設定も残す。残しておけば、戻すときに再ダウンロードも再設定も要らない。

## 手順1.5: vLLM を導入する（初回のみ）

GB10（Blackwell、ARM64）では、NVIDIA が公開している vLLM コンテナを使う。pip で入れる通常版は GB10 の GPU（sm_121）に未対応のことがあるため使わない。アカウント登録は要らない。

```bash
# Docker（DGX OS に同梱）を確認。permission denied なら sudo を付けるか docker グループに入る
docker --version

# タグは NGC カタログ（https://catalog.ngc.nvidia.com/orgs/nvidia/containers/vllm）の最新を使う
docker pull nvcr.io/nvidia/vllm:<タグ>

# Qwen3-Coder-Next には vLLM 0.15.0 以上が必要。古ければ新しいタグを選び直す
docker run --rm nvcr.io/nvidia/vllm:<タグ> vllm --version

# コンテナから GB10 の GPU が見えるか
docker run --rm --gpus all nvcr.io/nvidia/vllm:<タグ> nvidia-smi

# モデルの保存先（コンテナを消しても残る）
mkdir -p ~/.cache/huggingface
```

手順3の `vllm serve` は、次のようにコンテナ内で実行する。モデル（約85GB）は初回起動時に自動でダウンロードされる。

```bash
docker run -d --name vllm --gpus all --network host --ipc=host \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  nvcr.io/nvidia/vllm:<タグ> \
  vllm serve Qwen/Qwen3-Coder-Next-FP8 \
    --served-model-name qwen3-coder-next \
    --host 0.0.0.0 --port 8000 \
    --gpu-memory-utilization 0.8 \
    --max-model-len 262144 \
    --max-num-seqs 2 \
    --kv-cache-dtype fp8 \
    --enable-auto-tool-choice --tool-call-parser qwen3_coder

# ログ（手順3・4の ~/vllm.log の代わり）
docker logs -f vllm

# 設定を変えて起動し直すときは、先に削除する
docker rm -f vllm
```

## 手順2: ページキャッシュを解放する

ユニファイドメモリでは、モデルファイル読み込み時のページキャッシュがメモリを占有し、vLLM の確保が失敗することがある。起動直前にキャッシュを解放する。

```bash
sync
echo 3 | sudo tee /proc/sys/vm/drop_caches
free -h   # buff/cache が減り available が増えたことを確認
```

キャッシュ解放は実行中のプロセスに害はないが、直後のディスク読み込みは一時的に遅くなる。

## 手順3: メモリ制限付きで vLLM を起動する

まず保守的な値で起動し、安定を確認してから少しずつ緩める。

Qwen3-Coder-Next FP8 は重みだけで約85GBあるため、`--gpu-memory-utilization` は 0.75 未満にすると重みが載らない。メモリが足りないときは、まず `--max-model-len` を下げる。

| パラメータ | 既定値 | 推奨初期値 | 効果 |
| --- | --- | --- | --- |
| `--gpu-memory-utilization` | 0.9 | 0.8 | vLLM が予約するメモリの割合（0.8 で約100GB）。残り約25GBが OS の分 |
| `--max-model-len` | 262144 | 262144 | Ollama の `OLLAMA_CONTEXT_LENGTH`・`.env` の `LLM_NUM_CTX` と同じ値にする |
| `--max-num-seqs` | 256前後 | 2 | 同時処理数。`.env` の `LLM_CONCURRENCY` と揃える |
| `--kv-cache-dtype` | auto | fp8 | KV キャッシュをほぼ半減。256K を載せるために使う |
| `--served-model-name` | モデルのリポジトリ名 | qwen3-coder-next | API で指定するモデル名。`.env` の `LLM_MODEL` と揃える |
| `--enable-auto-tool-choice` `--tool-call-parser` | 無効 | 有効・`qwen3_coder` | Claude Code などのツール呼び出しを解釈する |
| `--enforce-eager` | 無効 | 必要時のみ | CUDA グラフ分のメモリを節約（速度は低下） |

```bash
vllm serve Qwen/Qwen3-Coder-Next-FP8 \
  --served-model-name qwen3-coder-next \
  --host 0.0.0.0 --port 8000 \
  --gpu-memory-utilization 0.8 \
  --max-model-len 262144 \
  --max-num-seqs 2 \
  --kv-cache-dtype fp8 \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder \
  2>&1 | tee ~/vllm.log
```

まだ枯渇する場合は、次の順で一つずつ追加・変更する。`--max-model-len` を変えたら、`.env` の `LLM_NUM_CTX` も同じ値にする。

1. `--enforce-eager` を追加
2. `--max-model-len` を 131072 に下げる
3. `--max-num-seqs` を 1 に下げる（`.env` の `LLM_CONCURRENCY` も 1 にする）
4. `--max-model-len` を 65536 に下げる

## 手順4: 起動確認と動作テスト

ログに起動完了が出たら、API を叩いて応答とメモリ残量を確認する。

```bash
# モデル一覧（qwen3-coder-next が出ること）
curl http://localhost:8000/v1/models

# 推論テスト
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-coder-next","messages":[{"role":"user","content":"こんにちは"}],"max_tokens":64}'

# 推論中にもう一つの端末でメモリを監視
watch -n 2 free -h
```

- [ ] 応答が返る
- [ ] 推論中も `available` が 10GB 以上残っている（OS の余裕）
- [ ] ログに `KV cache` の確保量と最大同時処理数が出ている（`grep -i "kv cache" ~/vllm.log`）。262144 トークンが収まっていること

## トラブルシューティング

| 症状 | 考えられる原因 | 対処 |
| --- | --- | --- |
| 起動時に `CUDA out of memory` | 予約割合が空きより大きい | 手順1・2を実施する。それでも出る場合は `--gpu-memory-utilization` を 0.75 まで下げ、`--max-model-len` も下げる |
| `No available memory for the cache blocks` | 重みを載せた後に KV キャッシュの余地がない | `--max-model-len` を下げる、`fp8` KV キャッシュを使う。`--gpu-memory-utilization` は 0.85 までにとどめる |
| `max_model_len` が KV キャッシュに収まらないと出る | コンテキスト長が大きすぎる | `--max-model-len` をエラーが示す値以下にし、`.env` の `LLM_NUM_CTX` も揃える |
| CUDA グラフ取得中に落ちる | グラフ用メモリ不足 | `--enforce-eager` を追加 |
| 端末が固まる・OS が応答しない | OS の分まで使い切った | 再起動後、`--gpu-memory-utilization 0.75`・`--max-model-len 131072` で起動し直す |
| 推論中に落ちる | 長い入力や同時リクエストで KV 不足 | `--max-num-seqs` と `--max-model-len` を下げる |
| ツール呼び出しを含む長い入力で `!!!!` が延々と出る | ツール呼び出しの解釈の不具合（報告例あり） | `--tool-call-parser qwen3_xml` に変えて起動し直す |

上の対処をすべて試しても Qwen3-Coder-Next が安定しない場合は、次の「gpt-oss-120b に切り替える手順」に進む。

## gpt-oss-120b に切り替える手順（Qwen3-Coder-Next が失敗したとき）

手順3の調整を試しても Qwen3-Coder-Next が起動しない、またはツール呼び出しが安定しない場合は、vLLM のまま gpt-oss-120b（`openai/gpt-oss-120b`）に切り替える。重みは約65GBと小さく、メモリに余裕がある。アカウント登録は要らない（Apache-2.0、利用申請のないモデル）。

gpt-oss-120b のコンテキスト長は最大 131072（128K）で、256K にはできない。アプリの `.env` を次のように変える（変更前の `.env` は「準備」で `~/ollama-backup/env.bak` に控えてある）。`LLM_NUM_CTX` を下げると、大きなファイルは事前チェックで `too_large` として対象外になる。

```bash
# /opt/Lang_Chain/.env
LLM_MODEL=gpt-oss-120b
LLM_NUM_CTX=131072
```

| 項目 | Qwen3-Coder-Next | gpt-oss-120b |
| --- | --- | --- |
| モデル | `Qwen/Qwen3-Coder-Next-FP8` | `openai/gpt-oss-120b` |
| 重みの大きさ | 約85GB | 約65GB |
| `--gpu-memory-utilization` | 0.8 | 0.7 |
| `--max-model-len`（= `LLM_NUM_CTX`） | 262144 | 131072（上限） |
| `--kv-cache-dtype` | fp8 | 指定しない（KV キャッシュが小さく不要） |
| `--tool-call-parser` | `qwen3_coder` | `openai` |
| `--served-model-name`（= `LLM_MODEL`） | qwen3-coder-next | gpt-oss-120b |
| 必要な vLLM | 0.15.0 以上 | 0.10.1 以上 |

```bash
# 1. Qwen3-Coder-Next の vLLM を止める
docker rm -f vllm

# 2. ディスクの空きを確認（gpt-oss-120b は約65GB）。足りない場合だけ Qwen3-Coder-Next のモデルを消す
df -h ~
sudo rm -rf ~/.cache/huggingface/hub/models--Qwen--Qwen3-Coder-Next-FP8   # 足りない場合だけ

# 3. ページキャッシュを解放
sync; echo 3 | sudo tee /proc/sys/vm/drop_caches
free -h

# 4. gpt-oss-120b で起動（モデルは初回起動時に自動でダウンロードされる）
docker run -d --name vllm --gpus all --network host --ipc=host \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  nvcr.io/nvidia/vllm:<タグ> \
  vllm serve openai/gpt-oss-120b \
    --served-model-name gpt-oss-120b \
    --host 0.0.0.0 --port 8000 \
    --gpu-memory-utilization 0.7 \
    --max-model-len 131072 \
    --max-num-seqs 2 \
    --enable-auto-tool-choice --tool-call-parser openai

docker logs -f vllm

# 5. 動作テスト
curl http://localhost:8000/v1/models
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-oss-120b","messages":[{"role":"user","content":"こんにちは"}],"max_tokens":64}'
```

- [ ] `/v1/models` に `gpt-oss-120b` が出る
- [ ] 応答が返る
- [ ] 推論中も `available` が 10GB 以上残っている

- メモリが足りない場合は、手順3と同じ順（`--enforce-eager` を追加 → `--max-model-len` を下げる）で調整する。`--gpu-memory-utilization` は 0.6 まで下げられる
- gpt-oss-120b でのツール呼び出しは `tool_choice="auto"` だけに対応している
- Qwen3-Coder-Next に戻すときは、`docker rm -f vllm` の後に手順1.5の `docker run` で起動し直し、`.env` の `LLM_MODEL`・`LLM_NUM_CTX` を戻す
- gpt-oss-120b でも動かない場合は、次の「Ollama に戻す手順」で元に戻す
- 参照: [vLLM の gpt-oss-120b レシピ](https://recipes.vllm.ai/openai/gpt-oss-120b)

## Ollama に戻す手順（いつでも実行可）

vLLM の導入や起動に失敗したとき、または試用をやめるときは、次の手順で元に戻す。Ollama 本体・モデル・設定は残しているので、vLLM を止めて Ollama を起動するだけで戻る。OS が応答しない場合は、再起動してから実行する。

```bash
# 1. vLLM を止める（起動方法に合う行だけでよい。該当しない行のエラーは無視できる）
docker rm -f vllm                    # コンテナで起動した場合
sudo systemctl disable --now vllm    # 運用メモの systemd 化をした場合
pkill -f "vllm serve"                # 直接起動した場合

# 2. メモリが解放されたか（available が大きく戻っていればよい）
sync; echo 3 | sudo tee /proc/sys/vm/drop_caches
free -h

# 3. Ollama を自動起動ありで起動
sudo systemctl enable --now ollama

# 4. 設定が控えたものと同じか（何も表示されなければ同じ）
systemctl show ollama -p Environment | diff - ~/ollama-backup/ollama-env.txt
ollama list | diff - ~/ollama-backup/ollama-list.txt
curl http://127.0.0.1:11434/api/tags

# 5. アプリの .env を変えていた場合だけ戻して、Web画面を再起動
cp ~/ollama-backup/env.bak /opt/Lang_Chain/.env
sudo systemctl restart godoc-web

# 6. 夜間バッチを止めていた場合は再開
sudo systemctl start godoc-nightly.timer
```

- [ ] `systemctl is-active ollama` が `active`
- [ ] `systemctl is-enabled ollama` が `~/ollama-backup/ollama-enabled.txt` と同じ
- [ ] Web 画面から1ファイルだけ設計書を生成し、成功する

- vLLM の systemd サービスを作っていた場合は必ず `disable` する。有効のままだと、再起動時に `Conflicts=ollama.service` により Ollama が止まることがある
- vLLM を完全にやめる場合は、続けて「[vLLM を削除する手順](#vllm-を削除する手順やめる場合)」を行う

## vLLM を削除する手順（やめる場合）

「Ollama に戻す手順」を終えてから行う。vLLM をもう使わない場合だけ実行する。再挑戦するなら、イメージとモデルを残しておけば再ダウンロード（合計で約100GB）が要らない。

```bash
# 1. vLLM のプロセス・コンテナが残っていないか
pgrep -af "vllm serve"                  # 何も表示されなければよい
docker ps -a --filter name=vllm         # 見出し行だけならよい

# 2. コンテナが残っていれば削除
docker rm -f vllm

# 3. コンテナイメージを削除（タグは一覧で確認する）
docker images nvcr.io/nvidia/vllm
docker rmi nvcr.io/nvidia/vllm:<タグ>

# 4. モデル（約85GB）を削除。コンテナが root で書き込んだファイルなので sudo が要る
sudo rm -rf ~/.cache/huggingface/hub/models--Qwen--Qwen3-Coder-Next-FP8
sudo rm -rf ~/.cache/huggingface/hub/models--openai--gpt-oss-120b   # gpt-oss-120b に切り替えた場合（約65GB）

# 5. 運用メモの systemd 化をしていた場合だけ、サービスを削除
sudo systemctl disable --now vllm
sudo rm /etc/systemd/system/vllm.service
sudo systemctl daemon-reload
sudo systemctl reset-failed

# 6. コンテナを使わず pip（venv）で入れていた場合だけ、その venv を削除
rm -rf /path/to/venv

# 7. ログを削除
rm -f ~/vllm.log
```

- [ ] `docker images nvcr.io/nvidia/vllm` に何も表示されない
- [ ] `systemctl status vllm` が `Unit vllm.service could not be found.` になる
- [ ] `df -h ~` で空き容量が増えている
- [ ] `systemctl is-active ollama` が `active` のまま

次のものは削除しない。

- Ollama のモデル（`/usr/share/ollama/.ollama/models`）と設定（`sudo systemctl edit ollama` で入れたもの）
- Docker と NVIDIA Container Toolkit（DGX OS に同梱で、ほかの用途でも使う）
- `~/.cache/huggingface` フォルダ自体（ほかのモデルが入っている場合がある。消すのは Qwen3-Coder-Next-FP8・gpt-oss-120b のフォルダだけ）
- `~/ollama-backup`（Ollama が元どおり動くことを数日確認してから消してよい）

## 運用メモ

安定した設定が決まったら、systemd で常駐化して毎回同じ値で起動する。

```ini
# /etc/systemd/system/vllm.service
[Unit]
Description=vLLM Qwen3-Coder-Next
After=network.target
Conflicts=ollama.service

[Service]
User=<ユーザー名>
ExecStartPre=+/bin/sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'
ExecStart=/path/to/venv/bin/vllm serve Qwen/Qwen3-Coder-Next-FP8 --served-model-name qwen3-coder-next --host 0.0.0.0 --port 8000 --gpu-memory-utilization 0.8 --max-model-len 262144 --max-num-seqs 2 --kv-cache-dtype fp8 --enable-auto-tool-choice --tool-call-parser qwen3_coder
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

- `ExecStartPre` のキャッシュ解放は root 権限が必要なため、先頭に `+` を付けて root で実行している
- `Conflicts=ollama.service` で、vLLM 起動時に Ollama が自動停止する
- GB10 1台に載るのは Qwen3-Coder-Next 1モデルだけ。ほかのモデルを同時に動かす余裕はない
- gpt-oss-120b に切り替えて運用する場合は、`Description` と `ExecStart` の `vllm serve` 以降を「gpt-oss-120b に切り替える手順」の内容にする
- コンテナで導入した場合は、`ExecStart` を手順1.5の `docker run`（`-d` を外し `--rm` を付ける）に、`ExecStop` を `docker stop vllm` にする
