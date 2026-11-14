# GB10 で Ollama から vLLM に切り替える手順（メモリ枯渇対策つき）

GB10 上で vLLM を起動するときは、確保メモリ量を明示的に絞り、競合プロセスを止めてから起動する。これでメモリ枯渇（OOM・OS のフリーズ）を防ぐ。

- 対象環境: GB10（CPU と GPU が 128GB のユニファイドメモリを共有）、vLLM 0.15.0 以上
- モデル: Qwen3-Coder-Next の FP8 版（`Qwen/Qwen3-Coder-Next-FP8`）。Ollama で使っている `qwen3-coder-next` と同じモデルで、コンテキスト長も同じ 256K（262144）にする。BF16 版は重みだけで約160GBあり GB10 に載らないため、FP8 版を使う
- モデル名: `--served-model-name claude-opus-4-6` で別名を付けて提供する。アプリの `.env` の `LLM_MODEL` もこの別名にする（Claude Code が `--model` と補助モデルにこの名前を使う）
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
6. 手順5: アプリ（Claude Code ヘッドレス）の接続先とモデル名を vLLM に合わせ、アプリを vLLM 対応版に入れ替える（`.env`・systemd のユニット）
7. Qwen3-Coder-Next が動かなければ「[gpt-oss-120b に切り替える手順](#gpt-oss-120b-に切り替える手順qwen3-coder-next-が失敗したとき)」で vLLM のままモデルを替える
8. それでも失敗したら「[Ollama に戻す手順](#ollama-に戻す手順いつでも実行可)」で元に戻す（どの段階からでも実行できる）
9. vLLM をもう使わないなら「[vLLM を削除する手順](#vllm-を削除する手順やめる場合)」で片付ける

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

### モデルを先にダウンロードする（進捗を見るため）

モデル（約85GB）は `vllm serve` の初回起動時にも自動でダウンロードされるが、`-d` で起動するとダウンロードの進捗が見えず、止まったのか進んでいるのか区別できない。先にダウンロードだけを単独で実行しておくと、進捗バーが表示される。

```bash
# 同じイメージを使うので、ホスト側への追加インストールは不要
# -it を付けると、ファイルごとの進捗バーが表示される。途中で止まっても再実行すれば続きから再開する
docker run --rm -it \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  nvcr.io/nvidia/vllm:<タグ> \
  hf download Qwen/Qwen3-Coder-Next-FP8
# hf コマンドがないと出た場合は huggingface-cli download に置き換える
```

- `You are sending unauthenticated requests to the HF Hub. Please set a HF_TOKEN...` という警告が出ても、ダウンロードは続く。公開モデルなのでトークンは必須ではない（速度制限が厳しくなるだけ）。警告を消したい場合は、Hugging Face で Read 権限のトークンを作り、`-e HF_TOKEN=<トークン>` を付ける
- ダウンロード済みのモデルは `~/.cache/huggingface` に残り、次の `vllm serve` はそれを使う

すでに `vllm serve` のコンテナでダウンロードが始まっている場合は、別の端末で容量の増え方を見る。約85GBに近づけば終わりかけている。

```bash
watch -n 10 'du -sh ~/.cache/huggingface/hub/models--Qwen--Qwen3-Coder-Next-FP8'
```

### コンテナで vLLM を起動する

手順3の `vllm serve` は、次のようにコンテナ内で実行する。`-d` を付けているので、コマンドはコンテナIDを1行表示してすぐ終わる。以降の処理はバックグラウンドで進むため、止まったわけではない。状況は `docker logs -f vllm` で見る。

```bash
docker run -d --name vllm --gpus all --network host --ipc=host \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  nvcr.io/nvidia/vllm:<タグ> \
  vllm serve Qwen/Qwen3-Coder-Next-FP8 \
    --served-model-name claude-opus-4-6 \
    --host 0.0.0.0 --port 11434 \
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
| `--max-num-seqs` | 256前後 | 2 | 同時処理数。指定しなくてもよい（アプリ側でサーバー全体の同時セッションを 10 までに抑える） |
| `--kv-cache-dtype` | auto | fp8 | KV キャッシュをほぼ半減。256K を載せるために使う |
| `--served-model-name` | モデルのリポジトリ名 | claude-opus-4-6 | API で指定するモデル名（別名）。`.env` の `LLM_MODEL` と揃える |
| `--enable-auto-tool-choice` `--tool-call-parser` | 無効 | 有効・`qwen3_coder` | Claude Code などのツール呼び出しを解釈する |
| `--enforce-eager` | 無効 | 必要時のみ | CUDA グラフ分のメモリを節約（速度は低下） |

```bash
vllm serve Qwen/Qwen3-Coder-Next-FP8 \
  --served-model-name claude-opus-4-6 \
  --host 0.0.0.0 --port 11434 \
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
3. `--max-num-seqs` を 1 に下げる（Web 画面の同時セッション数・`--concurrency` も 1 にする）
4. `--max-model-len` を 65536 に下げる

## 手順4: 起動確認と動作テスト

vLLM は、モデルの読み込みとウォームアップがすべて終わってから API を受け付ける。それまでは `curl` が `Connection refused` になるが、これは正常な挙動。`docker logs -f vllm` で、次の順に進むのを待つ。

1. `Loading safetensors checkpoint shards: xx%`（モデルの読み込み）
2. `torch.compile` / `Capturing CUDA graphs`（ウォームアップ。数分止まって見えることがある）
3. `Application startup complete` / `Uvicorn running on http://0.0.0.0:11434`（起動完了。ここから `curl` が通る）

途中で止まって見えるときは、次で進んでいるかを確かめる。

```bash
docker ps -a        # STATUS が Up なら動作中、Exited なら落ちている（docker logs の末尾にエラーが出る）
free -h             # used が増えていれば読み込み中
```

ログが `Using FlashInfer attention backend` で止まって見える場合は、まだ重みの読み込み前の段階にいる。vLLM はこの後に重みのダウンロードと読み込みに入るが、`-d` で起動していると、その間ログには何も出ない。次の2つで、どちらの処理中かを切り分ける。

```bash
# 1. ダウンロード中か（数分おきに実行して増えているか）
du -sh ~/.cache/huggingface/hub/models--Qwen--Qwen3-Coder-Next-FP8

# 2. FlashInfer のカーネルをコンパイル中か（nvcc や ninja が出ていればコンパイル中）
docker top vllm | grep -E "nvcc|ninja|cicc"
```

- 容量が増えている → ダウンロード中。約85GBになるまで待つ
- nvcc などが動いている → GB10 向けに初回だけカーネルをコンパイルしている。数十分かかることがあるが、2回目以降は速い
- どちらも変化がない → 止まっている。`docker rm -f vllm` で止め、手順1.5の「モデルを先にダウンロードする」で先にダウンロードする

ダウンロード中でも、容量の増え方が極端に遅い場合がある（例: 1時間半で11GB。このままでは85GBまで数時間以上かかる）。数分おいて `du -sh` を再実行し、増えていれば「遅いだけ」、同じ値のままなら「止まっている」。どちらの場合も、次のように進捗が見える形に切り替える。ダウンロード済みの分はそのまま使われ、続きから再開する。

```bash
# 1. 今のコンテナを止める
docker rm -f vllm

# 2. 進捗バー付きでダウンロード（トークンを付けると速度制限がゆるくなる）
docker run --rm -it \
  -e HF_TOKEN=hf_xxxxxxxx \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  nvcr.io/nvidia/vllm:<タグ> \
  hf download Qwen/Qwen3-Coder-Next-FP8
```

- 起動時の `Please set a HF_TOKEN` の警告は、未認証だと速度が制限されるという意味。ダウンロードが遅い主な原因になりうる
- トークンは https://huggingface.co/settings/tokens で、Read 権限のものを無料で作れる
- 進捗バーに出る速度（MB/s）が、トークンを付けても遅い場合は、回線やプロキシ側の問題を疑う
- ダウンロードが終わったら、手順1.5の `docker run -d ...` で vLLM を起動し直す

ログに起動完了が出たら、API を叩いて応答とメモリ残量を確認する。

```bash
# モデル一覧（claude-opus-4-6 が出ること）
curl http://localhost:11434/v1/models

# 推論テスト
curl http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-4-6","messages":[{"role":"user","content":"こんにちは"}],"max_tokens":64}'

# Claude Code が使う Anthropic 互換API（/v1/messages）の応答テスト
curl http://localhost:11434/v1/messages   -H "Content-Type: application/json" -H "x-api-key: local" -H "anthropic-version: 2023-06-01"   -d '{"model":"claude-opus-4-6","messages":[{"role":"user","content":"こんにちは"}],"max_tokens":64}'

# 推論中にもう一つの端末でメモリを監視
watch -n 2 free -h
```

- [ ] `/v1/chat/completions` と `/v1/messages` の両方で応答が返る（`/v1/messages` が 404 なら vLLM が古い。新しいタグを使う）
- [ ] 推論中も `available` が 10GB 以上残っている（OS の余裕）
- [ ] ログに `KV cache` の確保量と最大同時処理数が出ている（`grep -i "kv cache" ~/vllm.log`）。262144 トークンが収まっていること

## 手順5: アプリ（Claude Code ヘッドレス）から使う

アプリはバッチ処理で `claude -p --model <LLM_MODEL>` を起動する。ヘッドレスモードの Claude Code は Claude のモデル名しか受け付けないため、Ollama では Modelfile で既存モデルを Claude 名で包んでいた。vLLM には Modelfile がないので、代わりに `--served-model-name` で Claude 名の別名を付ける。

`--served-model-name` には名前を複数書ける。どの名前で呼んでも同じモデルが応答する。重みは1つだけ読み込まれるので、メモリは増えない。

### 案1: `--served-model-name` に Claude 名の別名を足す（推奨）

Modelfile の直接の代わりになる。手順1.5・3 の起動コマンドの `--served-model-name` を次のようにする。`claude-opus-4-6` の部分は、Ollama の Modelfile で付けていた名前と同じにする。

```bash
docker run -d --name vllm --gpus all --network host --ipc=host \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  nvcr.io/nvidia/vllm:<タグ> \
  vllm serve Qwen/Qwen3-Coder-Next-FP8 \
    --served-model-name claude-opus-4-6 qwen3-coder-next \
    --host 0.0.0.0 --port 11434 \
    --gpu-memory-utilization 0.8 \
    --max-model-len 262144 \
    --max-num-seqs 2 \
    --kv-cache-dtype fp8 \
    --enable-auto-tool-choice --tool-call-parser qwen3_coder
```

アプリの `.env` を vLLM に向ける。接続先は `LLM_BASE_URL` に書く（`claude` 起動時の `ANTHROPIC_BASE_URL` と、実行前の事前チェックに使われる）。末尾に `/v1` は付けない（Claude Code が `/v1/messages` を付ける）。`LLM_MODEL` は `--served-model-name` の先頭の名前にする。

```bash
# /opt/Lang_Chain/.env
LLM_BASE_URL=http://127.0.0.1:11434
LLM_MODEL=claude-opus-4-6
```

- 以前の版のアプリは `OLLAMA_BASE_URL` しか読まない。この版でも `LLM_BASE_URL` がなければ `OLLAMA_BASE_URL` を使うが、vLLM では `LLM_BASE_URL` を書き、`OLLAMA_BASE_URL` の行は消す
- 以前の版のアプリは、実行前の事前チェックで Ollama 固有の API を呼ぶため、vLLM では必ず失敗する。下の「アプリを vLLM 対応版に入れ替える」を行う

- アプリは `ANTHROPIC_DEFAULT_OPUS_MODEL`・`SONNET`・`HAIKU` をすべて `LLM_MODEL` にするので、別名は1つあれば足りる。補助処理（haiku 系）も同じ名前で vLLM に届く
- アプリは `ANTHROPIC_AUTH_TOKEN=local` を渡す（以前の版は `ollama`）。vLLM を `--api-key` なしで起動していれば値は見られない
- `/v1/models` の一覧と応答の `model` 欄には先頭の名前（`claude-opus-4-6`）が出る
- gpt-oss-120b で動かす場合も同じ。`--served-model-name claude-opus-4-6 gpt-oss-120b` とし、`.env` の `LLM_NUM_CTX=131072` は「gpt-oss-120b に切り替える手順」のとおりにする
- 運用メモの systemd 化をする場合は、`ExecStart` の `--served-model-name` も同じにする

起動後、Claude Code が使う Anthropic 互換の `/v1/messages` に Claude 名で応答が返るかを確かめる。

```bash
# 1. 別名が一覧に出るか
curl http://localhost:11434/v1/models

# 2. Anthropic 互換エンドポイントに Claude 名で届くか
curl http://localhost:11434/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-4-6","max_tokens":64,"messages":[{"role":"user","content":"こんにちは"}]}'

# 3. Claude Code のヘッドレスモードで届くか
ANTHROPIC_BASE_URL=http://127.0.0.1:11434 ANTHROPIC_AUTH_TOKEN=local ANTHROPIC_API_KEY= \
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
  claude -p "こんにちは。1行で返答してください。" --model claude-opus-4-6
```

- [ ] `/v1/models` に `claude-opus-4-6` が出る
- [ ] `/v1/messages` が応答する（`404` なら vLLM が古く Anthropic 互換エンドポイントがない。新しいタグのコンテナにする）
- [ ] `claude -p` が応答する

### Claude Code のシンキングを止める（必須）

Qwen3-Coder-Next はシンキングしないモデルなので、Claude Code 側でシンキングを止めておく。止めないと、次のどちらかになる。

- **`--reasoning-parser` なしで起動した場合**：Claude Code はシンキングを使う指定を付けてリクエストを送る。vLLM 側にそれを処理する reasoning parser がないため、サーバー側で reasoning parser のエラーが出る
- **`--reasoning-parser qwen3` を付けて起動した場合**：vLLM は応答の中からシンキングの区切り（`</think>`）を探すが、Qwen3-Coder-Next はこの区切りを出さない。そのため応答全体がシンキング扱いになり、本文が空になる。VS Code のチャット欄では、シンキングだけ表示されてレスポンスが表示されない

vLLM の起動コマンドには `--reasoning-parser` を**付けない**。そのうえで、Claude Code の設定（`~/.claude/settings.json`）でシンキングを止める。

```json
{
  "alwaysThinkingEnabled": false,
  "env": {
    "MAX_THINKING_TOKENS": "0"
  }
}
```

- VS Code の拡張機能で使う場合は、設定後に VS Code を再読み込みする。チャット欄にシンキングの切り替えがあれば、そちらもオフにする
- アプリのヘッドレスバッチ（`claude -p`）は、アプリが起動時に `MAX_THINKING_TOKENS=0` を渡すので、この設定は要らない（実行ユーザーの設定に別の値があっても上書きする）。上の設定が要るのは、VS Code や端末から対話で使う場合
- Claude Code のバージョンによっては、この設定だけではシンキングが止まらないという報告がある。その場合は、サーバー側のエラー全文を確認し、案2の LiteLLM Proxy でシンキング指定を取り除く方法を検討する

### 案2: LiteLLM Proxy で名前を付け替える

vLLM は元の名前のまま起動し、前に置いた LiteLLM Proxy で Claude 名を vLLM のモデルに対応させる。docs/proxy の構成に組み込める。モデルや GB10 を複数台にしたとき、バッチ用と対話用で使うモデルを分けたいときに向く。

```yaml
# LiteLLM の config.yaml（抜粋）
model_list:
  - model_name: claude-opus-4-6
    litellm_params:
      model: hosted_vllm/qwen3-coder-next
      api_base: http://gb10:11434/v1
```

- `.env` の `LLM_BASE_URL` は Proxy の URL に、`LLM_MODEL` は `model_name` の値にする（事前チェックは Proxy の `/v1/models` に `model_name` が出ることを確かめる）
- 経由する段が1つ増えるので、まず案1で動くことを確かめてから移すとよい

### 案3: Claude Code のモデル別名だけで済ませる（要検証）

`--model sonnet` と別名で指定し、`ANTHROPIC_DEFAULT_SONNET_MODEL` などに vLLM 側の名前（`qwen3-coder-next`）を入れる方法。vLLM 側は変えずに済む。ただし、アプリは `--model` と `ANTHROPIC_DEFAULT_*_MODEL` に同じ `LLM_MODEL` を渡すため、アプリの修正が要る。ヘッドレスモードのモデル名の確認をこれで通れるかも、使う Claude Code のバージョンで試してから採用する。

### アプリを vLLM 対応版に入れ替える（Web 画面・夜間バッチ）

上の確認が済んだら、アプリ自体を vLLM 対応版に入れ替える。事前確認・戻し方を含む詳しい設置手順は [migration_vllm_app.md](migration_vllm_app.md) を参照（ここはその要約）。アプリは推論サーバーを OpenAI 互換の `/v1/models` で事前チェックし、Claude Code から Anthropic 互換の `/v1/messages` を呼ぶ（どちらも vLLM・Ollama の両方にある）。

```bash
cd /opt/Lang_Chain

# 1. アプリを vLLM 対応版に更新（Ollama 専用の事前チェックをやめた版。ollama パッケージは不要になった）
git pull
.venv/bin/pip install -r requirements.txt

# 2. .env を vLLM 向けに変える（OLLAMA_BASE_URL の行は消す。残っていても LLM_BASE_URL が優先される）
#    LLM_BASE_URL=http://127.0.0.1:11434
#    LLM_MODEL=claude-opus-4-6          # --served-model-name と同じ
#    LLM_NUM_CTX=262144                   # --max-model-len と同じ
nano .env

# 3. systemd のユニットを入れ直す（ollama.service への依存を外し、reload に対応した版）
#    設置済みのユニットは古いままなので、User= を合わせてコピーし直す
WEB_USER=$(systemctl show -p User --value godoc-web.service)
sudo cp deploy/systemd/godoc-web.service /etc/systemd/system/
sudo sed -i "s/REPLACE_WITH_USER/$WEB_USER/" /etc/systemd/system/godoc-web.service
#    夜間タイマーを使っている場合だけ（GODOC_REPO などを残すため、ollama.service への依存だけを消す）
sudo sed -i -E '/^(Wants|After)=/ s/ ?ollama\.service//' /etc/systemd/system/godoc-nightly.service
sudo systemctl daemon-reload

# 4. Web 画面を起動し直す（コードを更新したので、今回は reload ではなく restart）
sudo systemctl restart godoc-web
systemctl status godoc-web            # active (running) ならよい

# 5. 夜間バッチを再開（止めていた場合）
sudo systemctl start godoc-nightly.timer
```

- [ ] `systemctl show godoc-web -p Wants -p After` に `ollama.service` が出ない
- [ ] `systemctl show godoc-web -p ExecReload` に `kill -HUP` が出る
- [ ] Web 画面から1ファイルだけ設計書を生成し、成功する（`モデル 'claude-opus-4-6' が推論サーバーにありません` と出たら `LLM_MODEL` と `--served-model-name` を揃える）

以後、`.env` の `LLM_*`・`CLAUDE_*` などだけを変えたときは `sudo systemctl reload godoc-web` で読み直せる（実行中のバッチは止めず、次の実行から反映）。`WEB_*` を変えたとき・コードを更新したときは `restart` する。

#### `systemctl reload godoc-web` が失敗していた理由

- 旧版のユニットには `ExecReload=` がなく、`Job type reload is not applicable for unit godoc-web.service` で失敗していた（`.env` を読み直す仕組み自体がなかった）
- 旧版のユニットは `Wants=`・`After=ollama.service` を持っていた。「運用メモ」の `vllm.service`（`Conflicts=ollama.service`）と同時に使うと、godoc-web の起動・再起動で ollama.service の起動が要求され、vLLM と衝突して失敗する（または vLLM が止まる）
- アプリの事前チェックが Ollama 固有の API（`/api/show`）を呼んでいたため、vLLM では実行のたびに `モデル ... が見つかりません` で失敗していた

## トラブルシューティング

| 症状 | 考えられる原因 | 対処 |
| --- | --- | --- |
| ログが `Using FlashInfer attention backend` から進まない | 重みのダウンロード中か、FlashInfer のカーネルを初回コンパイル中 | 手順4の切り分け（`du -sh` と `docker top vllm`）で確認する |
| `docker run -d` の後、何も表示されず止まったように見える | `-d` でバックグラウンド起動しているため。初回はモデル（約85GB）のダウンロード中で、進捗も出ない | `docker logs -f vllm` で確認する。進捗を見たい場合は、手順1.5の「モデルを先にダウンロードする」で先にダウンロードする |
| `curl http://localhost:11434/v1/models` が `Connection refused` | まだ読み込み・ウォームアップ中か、コンテナが落ちている | `docker ps -a` で Up か確認。Up なら `Application startup complete` が出るまで待つ。Exited なら `docker logs vllm` の末尾のエラーを見る |
| `/v1/models` が `401 Unauthorized` | 起動時に `--api-key` を付けている | `-H "Authorization: Bearer <キー>"` を付けて呼ぶ |
| `Please set a HF_TOKEN` の警告が出る | Hugging Face に未認証でアクセスしている | 警告だけで処理は続く。消したい場合は `-e HF_TOKEN=<トークン>` を付ける |
| モデルのダウンロードが極端に遅い・途中で止まる | 未認証のため速度が制限されている、または回線の問題 | `docker rm -f vllm` で止め、`-e HF_TOKEN=<トークン>` を付けて `hf download` で進捗を見ながらダウンロードし直す（手順4参照）。続きから再開する |
| 読み込みが進まない・途中で落ちる | Ollama がモデルを保持したままで、メモリが足りない | 手順1で Ollama を止め、`free -h` で `available` を確認してから `docker rm -f vllm` で作り直す |
| 起動時に `CUDA out of memory` | 予約割合が空きより大きい | 手順1・2を実施する。それでも出る場合は `--gpu-memory-utilization` を 0.75 まで下げ、`--max-model-len` も下げる |
| `No available memory for the cache blocks` | 重みを載せた後に KV キャッシュの余地がない | `--max-model-len` を下げる、`fp8` KV キャッシュを使う。`--gpu-memory-utilization` は 0.85 までにとどめる |
| `max_model_len` が KV キャッシュに収まらないと出る | コンテキスト長が大きすぎる | `--max-model-len` をエラーが示す値以下にし、`.env` の `LLM_NUM_CTX` も揃える |
| CUDA グラフ取得中に落ちる | グラフ用メモリ不足 | `--enforce-eager` を追加 |
| 端末が固まる・OS が応答しない | OS の分まで使い切った | 再起動後、`--gpu-memory-utilization 0.75`・`--max-model-len 131072` で起動し直す |
| 推論中に落ちる | 長い入力や同時リクエストで KV 不足 | `--max-num-seqs` と `--max-model-len` を下げる |
| VS Code（Claude Code）から使うと、サーバー側で reasoning parser のエラーが出る | Claude Code がシンキングを指定してリクエストしている | 手順5の「Claude Code のシンキングを止める」で、Claude Code 側のシンキングをオフにする |
| `--reasoning-parser` を付けると、シンキングは表示されるがレスポンスが表示されない | Qwen3-Coder-Next はシンキングの区切りを出さないため、応答全体がシンキング扱いになる | `--reasoning-parser` を外して起動し直し、Claude Code 側のシンキングをオフにする |
| ツール呼び出しを含む長い入力で `!!!!` が延々と出る | ツール呼び出しの解釈の不具合（報告例あり） | `--tool-call-parser qwen3_xml` に変えて起動し直す |
| 実行がすぐ `推論サーバーに接続できません` で終わる | vLLM が止まっている、または `LLM_BASE_URL` が違う | `curl http://127.0.0.1:11434/v1/models` で確認し、`.env` の `LLM_BASE_URL` を直して `sudo systemctl reload godoc-web` |
| 実行がすぐ `モデル 'xxx' が推論サーバーにありません` で終わる | `LLM_MODEL` と `--served-model-name` が違う | エラーに出る「提供中」の名前に `LLM_MODEL` を揃えて `sudo systemctl reload godoc-web` |
| `.env` を直したのに古い設定で動く | Web 画面が起動時の `.env` のまま | `sudo systemctl reload godoc-web`（行を消した場合は `restart`。reload は値の上書きだけで、消した行は残る） |

上の対処をすべて試しても Qwen3-Coder-Next が安定しない場合は、次の「gpt-oss-120b に切り替える手順」に進む。

## gpt-oss-120b に切り替える手順（Qwen3-Coder-Next が失敗したとき）

手順3の調整を試しても Qwen3-Coder-Next が起動しない、またはツール呼び出しが安定しない場合は、vLLM のまま gpt-oss-120b（`openai/gpt-oss-120b`）に切り替える。重みは約65GBと小さく、メモリに余裕がある。アカウント登録は要らない（Apache-2.0、利用申請のないモデル）。

gpt-oss-120b のコンテキスト長は最大 131072（128K）で、256K にはできない。アプリの `.env` を次のように変える（変更前の `.env` は「準備」で `~/ollama-backup/env.bak` に控えてある）。`LLM_NUM_CTX` を下げると、大きなファイルは事前チェックで `too_large` として対象外になる。

```bash
# /opt/Lang_Chain/.env（変えたら sudo systemctl reload godoc-web）
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
    --host 0.0.0.0 --port 11434 \
    --gpu-memory-utilization 0.7 \
    --max-model-len 131072 \
    --max-num-seqs 2 \
    --enable-auto-tool-choice --tool-call-parser openai

docker logs -f vllm

# 5. 動作テスト
curl http://localhost:11434/v1/models
curl http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-oss-120b","messages":[{"role":"user","content":"こんにちは"}],"max_tokens":64}'
```

- [ ] `/v1/models` に `gpt-oss-120b` が出る
- [ ] 応答が返る
- [ ] 推論中も `available` が 10GB 以上残っている

- メモリが足りない場合は、手順3と同じ順（`--enforce-eager` を追加 → `--max-model-len` を下げる）で調整する。`--gpu-memory-utilization` は 0.6 まで下げられる
- gpt-oss-120b でのツール呼び出しは `tool_choice="auto"` だけに対応している
- Qwen3-Coder-Next に戻すときは、`docker rm -f vllm` の後に手順1.5の `docker run` で起動し直し、`.env` の `LLM_MODEL`（`claude-opus-4-6`）・`LLM_NUM_CTX` を戻して `sudo systemctl reload godoc-web` する
- gpt-oss-120b でも動かない場合は、次の「Ollama に戻す手順」で元に戻す
- 参照: [vLLM の gpt-oss-120b レシピ](https://recipes.vllm.ai/openai/gpt-oss-120b)

## Ollama に戻す手順（いつでも実行可）

vLLM の導入や起動に失敗したとき、または試用をやめるときは、次の手順で元に戻す。Ollama 本体・モデル・設定は残しているので、vLLM を止めて Ollama を起動するだけで戻る。OS が応答しない場合は、再起動してから実行する。

```bash
# 1. vLLM を止める（起動方法に合う行だけでよい。該当しない行のエラーは無視できる）
docker update --restart no vllm      # 再起動ポリシーを付けていた場合（運用メモ参照）
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

# 5. アプリの .env を戻して、Web画面を再起動（LLM_BASE_URL の行が消えるので reload ではなく restart。
#    控えた .env の OLLAMA_BASE_URL・LLM_MODEL（Modelfile で付けた Claude 名）がそのまま使われる）
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

### 再起動後も自動で起動する（コンテナで導入した場合）

コンテナで導入した場合は、Docker の再起動ポリシーを付けるのが簡単。動作中のコンテナにそのまま適用できるので、作り直しやモデルのダウンロードのやり直しは要らない。

```bash
# 1. 自動で再起動するように設定（動作中のコンテナにそのまま適用される）
docker update --restart unless-stopped vllm

# 2. Docker 自体が OS 起動時に立ち上がるか確認（enabled ならよい）
systemctl is-enabled docker

# 3. Ollama が起動時に立ち上がってメモリを取らないようにする
sudo systemctl disable --now ollama
```

- 再起動後は、モデルの読み込みとウォームアップに数分かかる。`Application startup complete` が出るまでは `Connection refused` になる
- 手動で止めるときは `docker stop vllm`。`unless-stopped` なので、手動で止めた場合は次の再起動では立ち上がらない
- コンテナを作り直すときは、`docker run` に `--restart unless-stopped` を付けておけば同じ設定になる
- Ollama に戻すときは、「Ollama に戻す手順」の前に `docker update --restart no vllm` を実行する。実行しないと、再起動のたびに vLLM が立ち上がり Ollama とメモリを取り合う

### systemd で常駐化する

安定した設定が決まったら、systemd で常駐化して毎回同じ値で起動する方法もある。Docker の再起動ポリシーを使う場合は不要。

```ini
# /etc/systemd/system/vllm.service
[Unit]
Description=vLLM Qwen3-Coder-Next (served as claude-opus-4-6)
After=network.target
Conflicts=ollama.service

[Service]
User=<ユーザー名>
ExecStartPre=+/bin/sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'
ExecStart=/path/to/venv/bin/vllm serve Qwen/Qwen3-Coder-Next-FP8 --served-model-name claude-opus-4-6 --host 0.0.0.0 --port 11434 --gpu-memory-utilization 0.8 --max-model-len 262144 --max-num-seqs 2 --kv-cache-dtype fp8 --enable-auto-tool-choice --tool-call-parser qwen3_coder
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

- `ExecStartPre` のキャッシュ解放は root 権限が必要なため、先頭に `+` を付けて root で実行している
- `Conflicts=ollama.service` で、vLLM 起動時に Ollama が自動停止する
- GB10 1台に載るのは Qwen3-Coder-Next 1モデルだけ。ほかのモデルを同時に動かす余裕はない
- gpt-oss-120b に切り替えて運用する場合は、`Description` と `ExecStart` の `vllm serve` 以降を「gpt-oss-120b に切り替える手順」の内容にする
- コンテナで導入した場合は、`ExecStart` を手順1.5の `docker run`（`-d` を外し `--rm` を付ける）に、`ExecStop` を `docker stop vllm` にする
