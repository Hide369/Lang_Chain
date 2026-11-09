# 処理が遅いときの切り分け

バッチ（Web 画面・夜間バッチ・コマンドからの実行）が全体的に遅いときに、原因を絞り込む手順をまとめる。
まず「[1. まず確認すること](#1-まず確認すること)」で当たりを付け、「[2. 考えられる原因と対処](#2-考えられる原因と対処)」で該当する項目を見る。

> Web 画面自体の操作が重い場合は、別の問題として扱う。画面は2秒ごとに実行状態とログの続きを取りに行くだけで、
> 履歴・生成物の一覧はジョブが変わったときにしか読み直さないため、バッチの速さにはほぼ影響しない。

## 1. まず確認すること

### 1-1. Ollama の状態（GB10 で実行）

```bash
ollama ps
journalctl -u ollama --since "1 hour ago" | grep -i "loading model"
```

| 確認する点 | 正常 | 異常なら疑う原因 |
|---|---|---|
| `ollama ps` の `PROCESSOR` | `100% GPU` | `CPU` が混ざっている → [2-1](#2-1-コンテキスト長が大きすぎるメモリに収まっていない) |
| `ollama ps` の `CONTEXT` | `.env` の `LLM_NUM_CTX` と同じ値 | 値が違う → [2-1](#2-1-コンテキスト長が大きすぎるメモリに収まっていない)・[2-7](#2-7-モデルの再読み込みが起きている) |
| `loading model` の出現回数 | 起動時の1回だけ | 何度も出ている → [2-7](#2-7-モデルの再読み込みが起きている) |

### 1-2. 計測ログの集計

実行ごとの計測ログ `<out>/_runs/<run_id>.jsonl` には、1ファイル×1タスクごとに所要時間・試行回数・Claude Code の実行の記録（`llm_calls`）が残る。
次のコマンドで、タスクごとの傾向を集計できる（`<run_id>` は Web 画面の「実行履歴」や、ログの `run_id=...` で確認する）。

```bash
cd /opt/Lang_Chain
.venv/bin/python - out/_runs/<run_id>.jsonl <<'EOF'
import json, sys
from collections import defaultdict

rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8") if line.strip()]
by_task = defaultdict(list)
for r in rows:
    by_task[r["task"]].append(r)

def avg(xs):
    xs = [x for x in xs if x is not None]
    return f"{sum(xs) / len(xs):.0f}" if xs else "-"

for task, rs in by_task.items():
    calls = [c for r in rs for c in (r.get("llm_calls") or [])]
    print(f"[{task}] {len(rs)} 件")
    print(f"  1件あたりの所要時間(秒): 平均 {avg([r['duration_s'] for r in rs])} / 最大 {max(r['duration_s'] for r in rs):.0f}")
    print(f"  試行回数(attempts): 平均 {avg([r.get('attempts') for r in rs])}（2回以上: {sum(1 for r in rs if (r.get('attempts') or 0) >= 2)} 件）")
    print(f"  Claude Code 1回あたり: 所要 {avg([c.get('elapsed_s') for c in calls])} 秒 / "
          f"ターン {avg([c.get('num_turns') for c in calls])} / "
          f"入力 {avg([c.get('total_input_tokens') for c in calls])} トークン / "
          f"出力 {avg([c.get('output_tokens') for c in calls])} トークン")

print("\n時間のかかった上位5件:")
for r in sorted(rows, key=lambda r: r["duration_s"], reverse=True)[:5]:
    print(f"  {r['duration_s']:6.0f}s  {r['task']:<8} {r['status']:<17} attempts={r.get('attempts')}  {r['rel_path']}")
EOF
```

集計結果は次のように読む。

| 項目 | 多い・大きい場合に疑う原因 |
|---|---|
| ターン（`num_turns`） | [2-3](#2-3-エージェントのターン数が多い) |
| 試行回数（`attempts`） | [2-4](#2-4-検証ngによる作り直しが多い) |
| 入力トークン（`total_input_tokens`） | [2-2](#2-2-claude-code-が毎回送る入力が大きい)・[2-5](#2-5-処理の件数入力出力の量そのものが多い) |
| Claude Code 1回あたりの所要時間（`elapsed_s`） | ターン数が少ないのに長ければ [2-1](#2-1-コンテキスト長が大きすぎるメモリに収まっていない)・[2-6](#2-6-並列数の設定がかみ合っていない) |
| 1件の所要時間が `CLAUDE_TIMEOUT_S`（既定 1800 秒）付近 | [2-8](#2-8-応答しない実行がタイムアウトまで枠をふさいでいる) |

## 2. 考えられる原因と対処

可能性の高い順に並べている。

### 2-1. コンテキスト長が大きすぎる（メモリに収まっていない）

- Ollama は、コンテキスト長（`OLLAMA_CONTEXT_LENGTH`）に比例する大きさの作業用メモリ（KV キャッシュ）を確保する。256K（`262144`）は 64K の4倍で、`OLLAMA_NUM_PARALLEL=2` ならさらに2倍になる
- GPU のメモリに収まらないと一部が CPU で処理され、大幅に遅くなる
- 特に、コンテキスト長を上げた後から遅くなった場合は最も疑わしい

**確認**：`ollama ps` の `PROCESSOR` が `100% GPU` でない。

**対処**：
- `OLLAMA_NUM_PARALLEL` を `1` に下げる（`.env` の `LLM_CONCURRENCY` も `1` にする）
- それでも収まらなければ、コンテキスト長を `131072` などに下げる。`OLLAMA_CONTEXT_LENGTH` と `.env` の `LLM_NUM_CTX` は**必ず同じ値**にする（大きすぎるファイルを事前に弾く基準がずれるため）

### 2-2. Claude Code が毎回送る入力が大きい

- 1回の実行ごとに新しいセッションで Claude Code を起動する（[claude_code.py](../godoc_agent/claude_code.py)）。そのため、Claude Code のシステムプロンプト・ツール定義・スキル（SKILL.md）が毎回すべて送られる
- ファイルごとに内容が違ううえ並列で動かしているので、Ollama が前回の読み込み結果を使い回せることは少ない

**確認**：入力トークンが、対象ファイルの大きさの割に大きい。

**対処**：
- SKILL.md を簡潔にする（同じ内容の繰り返し・長い例を減らす）。スキルが参照するファイルも、必要なものだけにする
- 毎回の新しいセッションは、前のファイルの内容が混ざるのとコンテキスト超過の繰り返しを防ぐための設計なので、変えない

### 2-3. エージェントのターン数が多い

- 1回の実行で最大 `CLAUDE_MAX_TURNS`（既定 30）回モデルを呼ぶ。ターンのたびにそれまでの会話全体を読み直させるため、ターン数が多いほど時間は比例以上に増える
- ローカルモデルが同じファイルを何度も読む、目的のファイルを探して検索を繰り返す、といった動きで増えやすい

**確認**：ターン（`num_turns`）が多い。

**対処**：
- SKILL.md に「読むべきファイル」「読まなくてよいファイル」を具体的に書き、探索を減らす
- 上限に達すると `error_max_turns` で失敗するため、`CLAUDE_MAX_TURNS` を下げるのは、失敗が増えてもよいか見極めてからにする

### 2-4. 検証NGによる作り直しが多い

- 検証に通らない（公開識別子の書き漏れ、コメント追記でコードを変えてしまった等）と、最大 `DOC_MAX_ATTEMPTS`（既定 3）回まで最初から生成し直す
- 作り直しの依頼には直前の出力も入るため、入力はさらに大きくなる

**確認**：試行回数（`attempts`）が 2 以上の件が多い。計測ログの `issues` に指摘の内容が残る。

**対処**：
- `issues` に多い指摘を SKILL.md に明記する（例：「公開されている関数・型はすべて記載する」「コードは1文字も変えない」）
- `DOC_MAX_ATTEMPTS` を下げると時間は減るが、`failed_validation` が増える

### 2-5. 処理の件数・入力出力の量そのものが多い

- 処理件数は「対象ファイル数 × タスク数（設計書・コメント追記・追加スキル）」になる
- コメント追記・コード変更ありのスキルはファイル全文を出力するため、出力に時間がかかる
- 設計書は前回の版を添えて改訂させるため、版を重ねるほど入力が増える
- 「変更のないファイルも作り直す」（`--force`）をオンにすると、変更のないファイルも全件が対象になる

**確認**：ログ先頭の `対象=N 件（変更なしでスキップ=M 件）`、および出力トークンの大きさ。

**対処**：
- 必要なタスクだけを選んで実行する（Web 画面のスキル選択、コマンドなら `--tasks`）
- 対象外にしてよいフォルダ・ファイル（生成コード・テスト・vendor など）を `targets.toml` で除外する（[target_files.md](target_files.md)）
- `--force` は必要なときだけ使う

### 2-6. 並列数の設定がかみ合っていない

- 同時に動かす Claude Code の数は `.env` の `LLM_CONCURRENCY`（既定 2。タスクをまたいだ合計）、Ollama が同時に処理する数は `OLLAMA_NUM_PARALLEL` で決まる
- `LLM_CONCURRENCY` の方が大きいと、Ollama 側で待ち行列ができ、Claude Code の実行時間が延びる（タイムアウトも起きやすくなる）
- 同じ値でも、Ollama が同時に2件を処理すると1件ごとの速度が落ち、合計の速度はあまり伸びないことがある

**確認**：2つの値が同じか。`LLM_CONCURRENCY=1` と `2` で、同じ件数の実行の経過時間（ログ末尾の `経過時間`）を比べる。

**対処**：2つの値を揃えたうえで、計測して速い方を採る。メモリが厳しい場合は [2-1](#2-1-コンテキスト長が大きすぎるメモリに収まっていない) のとおり `1` にする。

### 2-7. モデルの再読み込みが起きている

- `OLLAMA_KEEP_ALIVE` が効いていない、または別の設定（コンテキスト長など）の要求が混ざると、Ollama はモデルを読み込み直す。1回に数十秒〜数分かかる

**確認**：`journalctl -u ollama` に `loading model` が何度も出ている。1件目や、時間が空いた後の1件だけが極端に遅い。

**対処**：
- Ollama の設定（`sudo systemctl edit ollama`）に `OLLAMA_KEEP_ALIVE=-1` と `OLLAMA_MAX_LOADED_MODELS=1` があるか確認する
- GB10 で他の用途に別のモデルを動かしていないか確認する

### 2-8. 応答しない実行がタイムアウトまで枠をふさいでいる

- Claude Code が応答しなくなった実行は、`CLAUDE_TIMEOUT_S`（既定 1800 秒 = 30 分）が過ぎるまで失敗にならず、その間は並列の枠を1つふさぐ

**確認**：計測ログに所要時間が 1800 秒前後の `error`（`Claude Code が 1800 秒以内に終了しませんでした`）がある。

**対処**：
- 正常な実行の所要時間（集計の最大値）に余裕を持たせた値まで `CLAUDE_TIMEOUT_S` を下げる
- 繰り返しタイムアウトするファイルは、大きすぎる・構造が複雑などの原因がないか個別に確認する

## 3. 設定値の一覧

遅さに関わる設定をまとめる。`.env` の値を変えたら、Web 画面（`sudo systemctl restart godoc-web`）を再起動する。Ollama の設定を変えたら `sudo systemctl daemon-reload && sudo systemctl restart ollama` を実行する。

| 設定 | 場所 | 既定値 | 影響 |
|---|---|---|---|
| `OLLAMA_CONTEXT_LENGTH` | Ollama（systemd） | — | 大きいほどメモリを使い、収まらないと遅くなる |
| `LLM_NUM_CTX` | `.env` | `262144` | `OLLAMA_CONTEXT_LENGTH` と同じ値にする |
| `OLLAMA_NUM_PARALLEL` | Ollama（systemd） | — | Ollama が同時に処理する数 |
| `LLM_CONCURRENCY` | `.env` | `2` | 同時に動かす Claude Code の数。`OLLAMA_NUM_PARALLEL` と揃える |
| `OLLAMA_KEEP_ALIVE` | Ollama（systemd） | — | `-1` にしてモデルの再読み込みを防ぐ |
| `CLAUDE_MAX_TURNS` | `.env` | `30` | 1回の実行のターン数の上限 |
| `DOC_MAX_ATTEMPTS` | `.env` | `3` | 検証NGのときに作り直す回数の上限 |
| `CLAUDE_TIMEOUT_S` | `.env` | `1800` | 1回の実行のタイムアウト（秒） |
| `LLM_NUM_PREDICT` | `.env` | `8192` | 1回の応答の最大出力トークン数 |
