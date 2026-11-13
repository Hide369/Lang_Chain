# vLLM のモデルを Qwen3-Coder-Next から Gemma 4 31B に切り替える手順

vLLM（Docker）で動いている Qwen3-Coder-Next を、Gemma 4 の最上位モデル **Gemma 4 31B**（`google/gemma-4-31B-it`）に切り替える。モデルは先にダウンロードだけしておき、切り替えるときは `docker run` の引数を変えて起動し直すだけにする。Qwen3-Coder-Next のモデルは消さないので、いつでも戻せる。

- 前提: [vllm_migration.md](vllm_migration.md) の手順で、vLLM（コンテナ名 `vllm`）が Qwen3-Coder-Next で動いていること
- 対象環境: GB10（128GB ユニファイドメモリ、ARM64、Blackwell sm_121）
- コマンドはすべて、GB10 に直接ログインした端末で実行する

## 導入の流れ

1. 事前確認: ディスクの空きとコンテナイメージの対応状況を見る
2. 手順1: Gemma 4 31B をダウンロードする（Qwen3-Coder-Next は動かしたままでよい）
3. 手順2: Qwen3-Coder-Next の vLLM を止め、Gemma 4 31B で起動し直す
4. 手順3: 動作確認する
5. 手順4: アプリの `.env` を合わせる
6. うまくいかなければ「[Qwen3-Coder-Next に戻す手順](#qwen3-coder-next-に戻す手順いつでも実行可)」で戻す

## モデルの選び方

Gemma 4 は E2B・E4B・26B A4B（MoE）・31B（Dense）の4種類で、31B が最上位（品質が最も高い）。

| 項目 | Qwen3-Coder-Next（今） | Gemma 4 31B（BF16・推奨） | Gemma 4 31B（NVFP4・速度重視） |
| --- | --- | --- | --- |
| モデル | `Qwen/Qwen3-Coder-Next-FP8` | `google/gemma-4-31B-it` | `nvidia/Gemma-4-31B-IT-NVFP4` |
| 重みの大きさ | 約85GB | 約62GB | 約20GB |
| ライセンス | Apache-2.0 | Apache-2.0 | Gemma 4 と同じ（NVIDIA 公式の量子化版） |
| コンテキスト長（`--max-model-len`） | 262144 | 131072（上限） | 131072（上限） |
| `--tool-call-parser` | `qwen3_coder` | `gemma4` | `gemma4` |
| 必要な vLLM | 0.15.0 以上 | 0.19.0 以上（transformers 5 以上を含むもの） | 同左 |

- **速度に注意**：31B は全パラメータを毎回計算する Dense モデルのため、メモリ帯域の狭い GB10 では遅い。単体の GB10 での報告例は BF16 で 1リクエストあたり毎秒約3.5トークン、NVFP4 で約6〜7トークン（Qwen3-Coder-Next よりかなり遅い）。夜間バッチの所要時間が大きく伸びる可能性があるので、まず1ファイルで時間を測る
- 品質を最優先するなら BF16、遅すぎる場合は同じ 31B の NVFP4 版に替える。どちらも以下の手順はモデル名を替えるだけで同じ
- `LLM_NUM_CTX` が 262144 から 131072 に下がるため、大きなファイルは事前チェックで `too_large` として対象外になる（gpt-oss-120b に切り替えたときと同じ）

## アカウント登録の要否

Gemma 4 は Gemma 3 までと違い Apache-2.0 で公開されており、通常は Hugging Face のログインやトークンなしでダウンロードできる。

ダウンロードが `401` や `gated` で失敗した場合（Hugging Face にログインできない場合）は、次の入手先を使う。どれも中身は同じ重みで、手順2の起動コマンドの `--model` に保存先のパスを書けば同じように動く。

| 入手先 | 例 | アカウント |
| --- | --- | --- |
| Hugging Face（公式） | `google/gemma-4-31B-it` | 通常は不要 |
| Hugging Face（再配布版） | `unsloth/Gemma-4-31B-it` など、公式の重みをそのまま再アップロードしたもの | 不要 |
| ModelScope | サイトで `gemma-4-31B-it` を検索し、Google 公式の重みのリポジトリを使う | 不要 |
| Kaggle Models | Google 公式の配布元 | Kaggle（Google アカウント）が必要 |

- 再配布版を使うときは、モデルカードに「公式の重みから変更していない」と書かれているものを選ぶ
- 社内ルールで入手元が決まっている場合は、それに従う

## 事前確認

```bash
# ディスクの空き（Gemma 4 31B BF16 は約62GB。Qwen3-Coder-Next の約85GB は残すので、合わせて置けるか）
df -h ~

# 今の vLLM のイメージのタグとバージョン（0.19.0 以上なら、このイメージのまま Gemma 4 を動かせる見込み）
docker inspect vllm --format '{{.Config.Image}}'
docker run --rm nvcr.io/nvidia/vllm:<タグ> vllm --version
docker run --rm nvcr.io/nvidia/vllm:<タグ> python3 -c "import transformers; print(transformers.__version__)"
```

- [ ] ディスクの空きが 80GB 以上ある
- [ ] vLLM が 0.19.0 以上、transformers が 5 以上（満たさない場合は、NGC の新しいタグにするか、手順2の「別のイメージを使う場合」を使う）

## 手順1: Gemma 4 31B を先にダウンロードする

ダウンロードは GPU を使わないので、**Qwen3-Coder-Next の vLLM を動かしたまま**実行できる。今の vLLM と同じ `~/.cache/huggingface` に保存するので、起動時は再ダウンロードしない。

```bash
# 進捗バー付きでダウンロード（途中で止まっても再実行すれば続きから再開する）
docker run --rm -it \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  nvcr.io/nvidia/vllm:<タグ> \
  hf download google/gemma-4-31B-it
# hf コマンドがないと出た場合は huggingface-cli download に置き換える
# 速度重視の NVFP4 版も試す場合は、続けて hf download nvidia/Gemma-4-31B-IT-NVFP4
```

`401` や `gated` で失敗した場合は、「アカウント登録の要否」の入手先から、フォルダを指定してダウンロードする。

```bash
mkdir -p ~/models

# 例1: Hugging Face の再配布版（ログイン不要）
docker run --rm -it \
  -v ~/models:/models \
  nvcr.io/nvidia/vllm:<タグ> \
  hf download unsloth/Gemma-4-31B-it --local-dir /models/gemma-4-31B-it

# 例2: ModelScope（ログイン不要。<リポジトリ名> はサイトで確認したもの）
pip install modelscope
modelscope download --model <リポジトリ名> --local_dir ~/models/gemma-4-31B-it
```

ダウンロードが終わったか確認する。

```bash
du -sh ~/.cache/huggingface/hub/models--google--gemma-4-31B-it   # Hugging Face から落とした場合（約62GB）
du -sh ~/models/gemma-4-31B-it                                   # フォルダを指定した場合
```

## 手順2: Gemma 4 31B で vLLM を起動し直す

切り替えの時点で、Qwen3-Coder-Next の vLLM を止める。両方を同時に載せるメモリはない。夜間バッチが動いている時間は避ける。

```bash
# 1. 夜間バッチを止める（使っている場合）
sudo systemctl stop godoc-nightly.timer

# 2. Qwen3-Coder-Next の vLLM を止める（モデルは消えない）
docker rm -f vllm

# 3. ページキャッシュを解放
sync; echo 3 | sudo tee /proc/sys/vm/drop_caches
free -h

# 4. Gemma 4 31B で起動（Qwen3-Coder-Next のときと変わるのはモデル・コンテキスト長・パーサだけ）
docker run -d --name vllm --gpus all --network host --ipc=host \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -e HF_HUB_OFFLINE=1 \
  nvcr.io/nvidia/vllm:<タグ> \
  vllm serve google/gemma-4-31B-it \
    --served-model-name claude-sonnet-4-5 gemma-4-31b \
    --host 0.0.0.0 --port 8000 \
    --gpu-memory-utilization 0.75 \
    --max-model-len 131072 \
    --max-num-seqs 2 \
    --kv-cache-dtype fp8 \
    --limit-mm-per-prompt '{"image": 0}' \
    --enable-auto-tool-choice --tool-call-parser gemma4

docker logs -f vllm
```

Qwen3-Coder-Next の起動コマンドとの違いは次のとおり。

| 引数 | Qwen3-Coder-Next | Gemma 4 31B | 理由 |
| --- | --- | --- | --- |
| モデル | `Qwen/Qwen3-Coder-Next-FP8` | `google/gemma-4-31B-it` | — |
| `--served-model-name` | `claude-sonnet-4-5 qwen3-coder-next` | `claude-sonnet-4-5 gemma-4-31b` | 先頭の名前を同じにしておけば、アプリの `LLM_MODEL` は変えなくてよい |
| `--gpu-memory-utilization` | 0.8 | 0.75 | 重みが約62GBと小さい。GB10 で Gemma 4 31B を高い割合で動かすと OS が固まる報告があるため、低めにする |
| `--max-model-len` | 262144 | 131072 | Gemma 4 31B の上限 |
| `--limit-mm-per-prompt` | なし | 画像 0 | Gemma 4 は画像入力つきのモデル。テキストだけ使うので、画像用のメモリを取らない |
| `--tool-call-parser` | `qwen3_coder` | `gemma4` | Gemma 4 はツール呼び出しの書式が違う |
| `-e HF_HUB_OFFLINE=1` | なし | あり | 手順1でダウンロード済みのものだけを使い、起動時にネットへ取りに行かない |

- `--reasoning-parser` は付けない。Claude Code のシンキングは止めてある（アプリは `MAX_THINKING_TOKENS=0` を渡す）ので不要。Gemma 4 のシンキングはリクエストで指定したときだけ有効になる
- 手順1でフォルダを指定してダウンロードした場合は、`-v ~/models:/models` を足し、`vllm serve /models/gemma-4-31B-it` にする（`-e HF_HUB_OFFLINE=1` はあってもなくてもよい）
- NVFP4 版にする場合は、モデルを `nvidia/Gemma-4-31B-IT-NVFP4` に替えるだけ

### 別のイメージを使う場合（今の NGC のイメージが Gemma 4 に未対応のとき）

起動時に `gemma4` のアーキテクチャやパーサが見つからないというエラーが出る場合は、NVIDIA の DGX Spark 向けプレイブックが Gemma 4 用に案内している vLLM の公式イメージを使う。このイメージは最初から `vllm serve` が起動するので、コマンドに `vllm serve` を書かない。

```bash
docker pull vllm/vllm-openai:gemma4-cu130

docker rm -f vllm
docker run -d --name vllm --gpus all --network host --ipc=host \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -e HF_HUB_OFFLINE=1 \
  vllm/vllm-openai:gemma4-cu130 \
    --model google/gemma-4-31B-it \
    --served-model-name claude-sonnet-4-5 gemma-4-31b \
    --host 0.0.0.0 --port 8000 \
    --gpu-memory-utilization 0.75 \
    --max-model-len 131072 \
    --max-num-seqs 2 \
    --kv-cache-dtype fp8 \
    --limit-mm-per-prompt '{"image": 0}' \
    --enable-auto-tool-choice --tool-call-parser gemma4
```

## 手順3: 動作確認

ログに `Application startup complete` が出るまで待つ（見方は [vllm_migration.md の手順4](vllm_migration.md#手順4-起動確認と動作テスト) と同じ）。

```bash
# 1. 別名が一覧に出るか
curl http://localhost:8000/v1/models

# 2. 応答が返るか
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-5","messages":[{"role":"user","content":"こんにちは"}],"max_tokens":64}'

# 3. ツール呼び出しが返るか（tool_calls に get_weather が入ればよい）
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-5","messages":[{"role":"user","content":"東京の天気を調べて"}],
       "tools":[{"type":"function","function":{"name":"get_weather","description":"天気を返す",
       "parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}],
       "max_tokens":256}'

# 4. Claude Code のヘッドレスモードで届くか
ANTHROPIC_BASE_URL=http://127.0.0.1:8000 ANTHROPIC_AUTH_TOKEN=local ANTHROPIC_API_KEY= \
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 MAX_THINKING_TOKENS=0 \
  claude -p "こんにちは。1行で返答してください。" --model claude-sonnet-4-5

# 推論中にもう一つの端末でメモリを監視
watch -n 2 free -h
```

- [ ] `/v1/models` に `claude-sonnet-4-5` と `gemma-4-31b` が出る
- [ ] ツール呼び出しのテストで `tool_calls` が返る（本文に `<|tool_call>` のような文字がそのまま出る場合は、下のトラブルシューティングを見る）
- [ ] `claude -p` が応答する
- [ ] 推論中も `available` が 10GB 以上残っている

## 手順4: アプリの `.env` を合わせる

`--served-model-name` の先頭を `claude-sonnet-4-5` のままにしたので、`LLM_MODEL` は変えなくてよい。コンテキスト長だけ合わせる。

```bash
# /opt/Lang_Chain/.env
LLM_MODEL=claude-sonnet-4-5     # 変えない（--served-model-name の先頭と同じ）
LLM_NUM_CTX=131072              # 262144 から変更（--max-model-len と同じ）
LLM_CONCURRENCY=2               # 変えない（--max-num-seqs と同じ）
```

```bash
sudo systemctl reload godoc-web          # .env を読み直す
sudo systemctl start godoc-nightly.timer # 止めていた場合
```

- [ ] Web 画面から1ファイルだけ設計書を生成し、成功する。かかった時間を Qwen3-Coder-Next のときと比べる（遅すぎる場合は NVFP4 版を検討する）

## トラブルシューティング

| 症状 | 考えられる原因 | 対処 |
| --- | --- | --- |
| 起動時に `gemma4` が見つからない・アーキテクチャ未対応と出る | イメージの vLLM（0.19.0 未満）か transformers（5 未満）が古い | NGC の新しいタグにするか、手順2の「別のイメージを使う場合」を使う |
| 起動時に `LocalEntryNotFoundError` などで落ちる | `HF_HUB_OFFLINE=1` なのに、モデルがキャッシュにない | 手順1のダウンロードが終わっているか、`du -sh` で確認する |
| モデルのダウンロードが `401` や `gated` で失敗する | Hugging Face 側でログインを求められた | 「アカウント登録の要否」の再配布版や ModelScope から入手する |
| 応答の本文に `<\|tool_call>` などがそのまま出て、ツールが実行されない | ツール呼び出しの解釈の不具合、またはチャットテンプレートの違い | 新しいイメージにする。それでも出る場合は、vLLM 同梱の `tool_chat_template_gemma4.jinja` を探して `--chat-template <パス>` を足す（`docker run --rm --entrypoint find <イメージ> / -name tool_chat_template_gemma4.jinja`） |
| 本文が空で、シンキングだけになる | `--reasoning-parser` を付けている、または Claude Code 側のシンキングが有効 | `--reasoning-parser` を外し、Claude Code のシンキングを止める（[vllm_migration.md の手順5](vllm_migration.md#claude-code-のシンキングを止める必須)） |
| 推論中に OS が固まる | メモリの使いすぎ（GB10 で Gemma 4 31B を高い割合で動かすと固まる報告がある） | 再起動後、`--gpu-memory-utilization 0.7`・`--max-model-len 65536` で起動し直す（`LLM_NUM_CTX` も合わせる） |
| `No available memory for the cache blocks` | KV キャッシュの余地がない | `--max-model-len` を下げる |
| 生成が非常に遅い | 31B Dense は GB10 では遅い（モデルの性質） | NVFP4 版（`nvidia/Gemma-4-31B-IT-NVFP4`）に替える |
| 実行がすぐ `モデル 'xxx' が推論サーバーにありません` で終わる | `LLM_MODEL` と `--served-model-name` が違う | `--served-model-name` の先頭を `claude-sonnet-4-5` にする |

## Qwen3-Coder-Next に戻す手順（いつでも実行可）

Qwen3-Coder-Next のモデルは消していないので、起動し直して `.env` を戻すだけで戻る。

```bash
# 1. Gemma 4 の vLLM を止める
docker rm -f vllm

# 2. Qwen3-Coder-Next で起動（vllm_migration.md の手順5・案1と同じコマンド）
docker run -d --name vllm --gpus all --network host --ipc=host \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  nvcr.io/nvidia/vllm:<タグ> \
  vllm serve Qwen/Qwen3-Coder-Next-FP8 \
    --served-model-name claude-sonnet-4-5 qwen3-coder-next \
    --host 0.0.0.0 --port 8000 \
    --gpu-memory-utilization 0.8 \
    --max-model-len 262144 \
    --max-num-seqs 2 \
    --kv-cache-dtype fp8 \
    --enable-auto-tool-choice --tool-call-parser qwen3_coder

# 3. .env の LLM_NUM_CTX を 262144 に戻して読み直す
sudo systemctl reload godoc-web
```

Gemma 4 をもう使わない場合は、モデルを消してディスクを空ける。

```bash
sudo rm -rf ~/.cache/huggingface/hub/models--google--gemma-4-31B-it
sudo rm -rf ~/.cache/huggingface/hub/models--nvidia--Gemma-4-31B-IT-NVFP4   # NVFP4 版を落とした場合
rm -rf ~/models/gemma-4-31B-it                                             # フォルダを指定して落とした場合
docker rmi vllm/vllm-openai:gemma4-cu130                                   # 別のイメージを使った場合
```

## 参照

- [vLLM の Gemma 4 31B レシピ](https://recipes.vllm.ai/Google/gemma-4-31B-it)
- [NVIDIA DGX Spark vLLM プレイブック（Gemma 4 用のイメージの案内）](https://build.nvidia.com/spark/vllm/instructions)
- [DGX Spark での Gemma 4 31B の報告（NVIDIA フォーラム）](https://forums.developer.nvidia.com/t/gemma-4-31b-on-dgx-spark-runtime-fp8-benchmarks-single-dual-node-tp-2/365814)
- [GB10 で Gemma 4 31B を動かすと固まる報告（NVIDIA フォーラム）](https://forums.developer.nvidia.com/t/gemma-4-on-dgx-spark-gb10-system-freeze-at-80-utilization-sm-121-kernel-issues/366060)
- [DGX Spark での Gemma 4 の速度比較](https://blog-vaishak-me.pages.dev/posts/gemma4-dgx-spark)
