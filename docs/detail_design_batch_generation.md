# Goファイルの詳細設計書 一括生成手順（Claude Code ヘッドレスモード × GB10 / Ollama / Qwen3-Coder-Next）

## 1. 目的

リポジトリ内のGoファイル1つにつき1回、Claude Code を**新しいセッション**で起動します。
スキルを使って、詳細設計書を順番に自動生成します。
推論はすべて社内の GB10 サーバー（Ollama ＋ Qwen3-Coder-Next）で行い、ソースコードを外部に送信しません。

- 1ファイル＝1セッションなので、前の処理のコンテキストが次に混ざらない
- 途中で止まっても、再実行すれば未作成分だけ続きから処理する
- `*_test.go` と `vendor/` 配下は対象外

## 2. 構成

```
[クライアントPC]                              [GB10 サーバー]
 VS Code 統合ターミナル                        Ollama (:11434)
   └ gen_design.sh                              └ qwen3-coder-next
       └ claude -p ... ──(Anthropic互換API)──▶
          ※ファイル単位で毎回新しいセッション
```

Claude Code は Anthropic Messages API で通信します。Ollama（v0.14.0 以降）はこの API と互換のエンドポイントを持つため、接続先を GB10 の Ollama に向けるだけで動きます。

> LiteLLM Proxy を経由する構成にした場合は、`ANTHROPIC_BASE_URL` を Proxy の URL に、モデル名を Proxy 側の名前に読み替えてください（docs/proxy を参照）。

## 3. 前提条件

| 項目 | 内容 |
|---|---|
| Ollama | GB10 上で v0.14.0 以降が稼働していること（`ollama -v`） |
| モデル | `qwen3-coder-next` が取得済みであること（`ollama list` に表示されるタグ名を使う） |
| Claude Code CLI | クライアントで `claude` コマンドが実行できること（`claude --version`） |
| 詳細設計書スキル | `.claude/skills/<スキル名>/SKILL.md`（プロジェクト）または `~/.claude/skills/<スキル名>/SKILL.md` に配置済み |
| 実行場所 | リポジトリのルート（VS Code の統合ターミナル） |
| シェル | Git Bash（推奨）。PowerShell 版は参考 |

> VS Code 拡張のチャット画面からは連続実行できません。統合ターミナルから CLI（`claude -p`）を使います。

## 4. 全体の流れ

1. GB10 の Ollama を設定する（初回のみ）
2. クライアントから接続を確認する
3. 1ファイルで動作確認する
4. 対象ファイル一覧を確認する
5. 一括実行スクリプトを実行する
6. 結果を確認し、失敗分を再実行する

## 5. 手順

### 5.1 GB10 の Ollama を設定する（初回のみ）

Ollama の既定のコンテキスト長は短く（4096 トークン程度）、Claude Code のシステムプロンプトとツール定義だけで溢れます。**コンテキスト長の拡張は必須**です。

GB10 で次を実行します。

```bash
sudo systemctl edit ollama
```

開いたファイルに次を追記します。

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_CONTEXT_LENGTH=65536"
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_NUM_PARALLEL=1"
```

| 変数 | 値 | 理由 |
|---|---|---|
| `OLLAMA_HOST` | `0.0.0.0:11434` | クライアントPCから接続できるようにする |
| `OLLAMA_CONTEXT_LENGTH` | `65536` | Claude Code に必要なコンテキスト長を確保する |
| `OLLAMA_KEEP_ALIVE` | `-1` | セッションごとにモデルが再ロードされるのを防ぐ（連続実行の高速化） |
| `OLLAMA_NUM_PARALLEL` | `1` | 1件ずつ順番に処理するため、メモリを1リクエストに集中させる |

反映と確認：

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
ollama run qwen3-coder-next "hello" > /dev/null
ollama ps     # CONTEXT 列が 65536 になっていることを確認
```

> GB10 はメモリに余裕があるため、大きいファイルを扱う場合は `131072` まで上げても構いません。メモリ不足で起動しない場合は値を下げます。

### 5.2 クライアントから接続を確認する

`gb10` の部分は、GB10 のホスト名または IP アドレスに置き換えます。

```bash
curl http://gb10:11434/api/tags
```

`qwen3-coder-next` を含む JSON が返れば接続できています。

続いて、Claude Code の接続先を GB10 にして応答を確認します。

```bash
export ANTHROPIC_BASE_URL="http://gb10:11434"
export ANTHROPIC_AUTH_TOKEN="ollama"
export ANTHROPIC_API_KEY=""
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

claude -p "こんにちは。1行で返答してください。" --model qwen3-coder-next
```

| 変数 | 説明 |
|---|---|
| `ANTHROPIC_BASE_URL` | 接続先を GB10 の Ollama にする |
| `ANTHROPIC_AUTH_TOKEN` | 値は何でもよい（Ollama は無視する）が、設定は必要 |
| `ANTHROPIC_API_KEY` | 空にして Anthropic のキーが使われないようにする |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | テレメトリなどの外部通信を止める |

> VS Code 拡張ですでにローカルLLMへ接続している場合、`~/.claude/settings.json` の `env` に同じ設定が入っていることがあります。その設定は CLI にも効きます。スクリプトでは確実を期して明示的に設定します。

### 5.3 1ファイルで動作確認する

```bash
claude -p "/go-detail-design ./internal/foo/bar.go
出力先は docs/design/test.md です。必ず Write ツールでファイルに保存してください。日本語で書いてください。" \
  --model qwen3-coder-next \
  --allowedTools "Read,Grep,Glob,Write,Skill" \
  --permission-mode acceptEdits \
  --max-turns 30 \
  --strict-mcp-config
```

確認すること：

- 指定した出力先にファイルが作成されている（ローカルモデルは、本文を画面に出すだけでファイルを書かずに終わることがあります）
- スキルで定めた構成・項目がそろっている
- 処理時間（一括実行時の目安になります）

うまくいかない場合は「8. トラブルシューティング」を参照してください。

### 5.4 対象ファイル一覧を確認する

```bash
find . -name '*.go' ! -name '*_test.go' ! -path './vendor/*' | sort
```

| 条件 | 意味 |
|---|---|
| `-name '*.go'` | Goファイルを対象にする |
| `! -name '*_test.go'` | テストファイルを除く |
| `! -path './vendor/*'` | vendor 配下を除く |

除外を追加する例：

- モック：`! -name 'mock_*.go'`
- protobuf 生成コード：`! -name '*.pb.go'`

特定のパッケージだけを対象にする場合は、`find ./internal/order ...` のように起点を変えます。

### 5.5 一括実行スクリプトを実行する

#### Git Bash 版（推奨）

`scripts/gen_design.sh` として保存します。

```bash
#!/usr/bin/env bash
# gen_design.sh : Goファイルごとに新しいセッションで詳細設計書を生成する（GB10 / Ollama）
set -u

# ===== 接続設定 =====
GB10_URL="http://gb10:11434"      # ← GB10 のホスト名/IP
MODEL="qwen3-coder-next"          # ← ollama list のタグ名

export ANTHROPIC_BASE_URL="$GB10_URL"
export ANTHROPIC_AUTH_TOKEN="ollama"
export ANTHROPIC_API_KEY=""
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
# 内部で使われる補助モデルも同じローカルモデルに固定する
export ANTHROPIC_DEFAULT_OPUS_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$MODEL"

# ===== 生成設定 =====
SKILL="go-detail-design"          # ← 実際のスキル名
OUT_DIR="docs/design"             # ← 出力先
MAX_LINES=1500                    # これを超えるファイルは別扱い（コンテキスト超過対策）
TIMEOUT="30m"                     # 1ファイルあたりの上限時間
MAX_TURNS=30                      # 1ファイルあたりの最大ターン数（無限ループ対策）

mkdir -p logs "$OUT_DIR"

# ===== 事前チェック =====
if ! curl -sf "$GB10_URL/api/tags" > /dev/null; then
  echo "GB10 の Ollama に接続できません: $GB10_URL"
  exit 1
fi

# ===== 本処理 =====
find . -name '*.go' ! -name '*_test.go' ! -path './vendor/*' | sort |
while read -r f; do
  name=$(echo "${f#./}" | sed 's#/#_#g; s#\.go$##')
  out="$OUT_DIR/${name}.md"

  # 作成済みはスキップ（途中で止まっても再実行で続きから）
  [ -s "$out" ] && { echo "skip: $f"; continue; }

  # 大きすぎるファイルは別リストへ
  lines=$(wc -l < "$f")
  if [ "$lines" -gt "$MAX_LINES" ]; then
    echo "$f (${lines}行)" >> logs/too_large.txt
    echo "skip(大): $f (${lines}行)"
    continue
  fi

  echo "=== $f (${lines}行) ==="
  start=$(date +%s)

  timeout "$TIMEOUT" claude -p "/$SKILL $f
出力先は $out です。必ず Write ツールでファイルに保存してください。
本文を画面に出力するだけで終わらないでください。日本語で書いてください。" \
    --model "$MODEL" \
    --allowedTools "Read,Grep,Glob,Write,Skill" \
    --permission-mode acceptEdits \
    --max-turns "$MAX_TURNS" \
    --strict-mcp-config \
    < /dev/null > "logs/${name}.log" 2>&1
  rc=$?

  elapsed=$(( $(date +%s) - start ))

  # 終了コードではなく「設計書ができたか」で成否を判定する
  if [ -s "$out" ]; then
    echo "  OK (${elapsed}秒)"
  else
    echo "$f (rc=$rc, ${elapsed}秒)" >> logs/failed.txt
    echo "  NG (rc=$rc) → logs/${name}.log を確認"
  fi
done
```

実行：

```bash
bash scripts/gen_design.sh
```

> **注意：`< /dev/null` は削除しないこと。**
> これがないと `claude` が `while read` の入力を読み込んでしまい、1ファイル目で処理が終わります。

#### PowerShell 版（参考）

Windows の Claude Code は内部で Git Bash を使うため、通常は Git Bash 版を推奨します。PowerShell で実行する場合は、PowerShell 7（`pwsh`）を使ってください。この版には時間制限（timeout）がありません。

`scripts/gen_design.ps1` として保存します。

```powershell
# gen_design.ps1 : Goファイルごとに新しいセッションで詳細設計書を生成する（GB10 / Ollama）
$Gb10Url  = "http://gb10:11434"     # ← GB10 のホスト名/IP
$Model    = "qwen3-coder-next"      # ← ollama list のタグ名
$Skill    = "go-detail-design"      # ← 実際のスキル名
$OutDir   = "docs/design"
$MaxLines = 1500
$MaxTurns = 30

$env:ANTHROPIC_BASE_URL   = $Gb10Url
$env:ANTHROPIC_AUTH_TOKEN = "ollama"
Remove-Item Env:ANTHROPIC_API_KEY -ErrorAction SilentlyContinue
$env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1"
$env:ANTHROPIC_DEFAULT_OPUS_MODEL   = $Model
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = $Model
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL  = $Model

New-Item -ItemType Directory -Force -Path logs, $OutDir | Out-Null

try { Invoke-RestMethod "$Gb10Url/api/tags" | Out-Null }
catch { Write-Host "GB10 の Ollama に接続できません: $Gb10Url"; exit 1 }

$root = (Get-Location).Path
Get-ChildItem -Recurse -Filter *.go -File |
  Where-Object { $_.Name -notlike '*_test.go' -and $_.FullName -notmatch '[\\/]vendor[\\/]' } |
  Sort-Object FullName |
  ForEach-Object {
    $rel  = $_.FullName.Substring($root.Length + 1) -replace '\\', '/'
    $name = ($rel -replace '\.go$', '') -replace '/', '_'
    $out  = "$OutDir/$name.md"

    if ((Test-Path $out) -and (Get-Item $out).Length -gt 0) { Write-Host "skip: $rel"; return }

    $lines = (Get-Content $_.FullName).Count
    if ($lines -gt $MaxLines) {
      Add-Content logs/too_large.txt "$rel ($lines行)"
      Write-Host "skip(大): $rel ($lines行)"; return
    }

    Write-Host "=== $rel ($lines行) ==="
    $prompt = "/$Skill $rel`n出力先は $out です。必ず Write ツールでファイルに保存してください。`n本文を画面に出力するだけで終わらないでください。日本語で書いてください。"
    claude -p $prompt --model $Model --allowedTools "Read,Grep,Glob,Write,Skill" `
      --permission-mode acceptEdits --max-turns $MaxTurns --strict-mcp-config *> "logs/$name.log"

    if ((Test-Path $out) -and (Get-Item $out).Length -gt 0) { Write-Host "  OK" }
    else { Add-Content logs/failed.txt $rel; Write-Host "  NG → logs/$name.log を確認" }
  }
```

実行：

```powershell
pwsh -File scripts/gen_design.ps1
```

### 5.6 結果を確認する

| 確認先 | 内容 |
|---|---|
| `docs/design/` | 生成された詳細設計書 |
| `logs/<ファイル名>.log` | 各ファイルの実行ログ（モデルの応答） |
| `logs/failed.txt` | 設計書が作成されなかったファイル |
| `logs/too_large.txt` | 行数が多く、対象外にしたファイル |

- **失敗分のやり直し：** スクリプトをもう一度実行します。作成済みのファイルは自動でスキップされます。
- **作り直し：** 対象の設計書を削除してから再実行します。
- **大きいファイル：** `too_large.txt` に載ったファイルは、後述の「7. ローカルモデル利用時の注意」を参照して個別に対応します。

## 6. オプションの説明

| オプション | 説明 |
|---|---|
| `-p "<プロンプト>"` | ヘッドレスモードで実行する。1回の呼び出しが1セッションになる |
| `--model qwen3-coder-next` | 使用するモデル。`ollama list` のタグ名と一致させる |
| `--allowedTools "Read,Grep,Glob,Write,Skill"` | 確認なしで使えるツール。読み取り・設計書の書き込み・スキル呼び出しに限定する |
| `--permission-mode acceptEdits` | ファイルの作成・編集を自動承認する |
| `--max-turns 30` | 1セッションのやり取りの上限。ローカルモデルが同じ操作を繰り返して止まらなくなるのを防ぐ |
| `--strict-mcp-config` | 設定ファイルの MCP サーバー（Context7 など）を読み込まない。外部送信の防止と、コンテキストの節約になる |
| `timeout 30m` | 1ファイルの処理時間の上限（Git Bash 版のみ） |

> Bash コマンドの実行は許可していません。スキルの中でコマンド実行が必要な場合は、`--allowedTools` に `"Bash(go doc:*)"` のように**必要なコマンドだけ**を追加します。

## 7. ローカルモデル利用時の注意

### 7.1 コンテキスト長とファイルサイズ

Claude Code はシステムプロンプトとツール定義だけで多くのトークンを使います。そこにスキルの指示、Goファイル本体、生成する設計書が加わります。

- `OLLAMA_CONTEXT_LENGTH=65536` で、目安としておおむね 1,000〜1,500 行程度のファイルまで扱えます
- それを超えるファイルは `too_large.txt` に記録されます。次のどちらかで対応します
  - GB10 のコンテキスト長を `131072` に上げて、`MAX_LINES` を引き上げて再実行する
  - 「型定義」「主要な関数」など範囲を区切った指示で、個別に生成する

### 7.2 処理時間

- 1ファイルあたりの処理時間は、5.3 で計測した値を目安にします
- `OLLAMA_KEEP_ALIVE=-1` にしておかないと、セッションごとにモデルのロードが入り大幅に遅くなります
- 大量に処理する場合は、パッケージ単位に分けて夜間に流すと効率的です

### 7.3 出力品質

- ローカルモデルは、スキルの指示から外れたり、ファイルに保存せずに終わったりすることがあります。スクリプトは**設計書ファイルの有無で成否を判定**しています
- 最初の数ファイルは必ず中身を確認し、必要に応じてスキルの指示（見出し構成・記述ルール）をより具体的にします
- テンプレート（見出しだけの雛形）をスキルに含めると、構成のぶれが減ります

### 7.4 外部送信の防止

次の設定により、ソースコードや設計書の内容は GB10 以外に送信されません。

- `ANTHROPIC_BASE_URL` を GB10 に向ける
- `ANTHROPIC_API_KEY` を空にする
- `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` で付随通信を止める
- `--strict-mcp-config` でクラウド版 MCP（Context7 など）を読み込まない

## 8. 運用上の注意

- **事前にコミットしておく：** 実行前に作業ツリーをきれいにしておくと、`git status` / `git diff` で生成物だけを確認できます。
- **ログの管理：** `logs/` は `.gitignore` に追加しておきます。
- **段階的に広げる：** まず `find` の起点を1パッケージに絞って流し、品質と処理時間を確認してから全体に広げます。
- **ファイル間の関連：** 各セッションは他ファイルの設計書の内容を覚えていません。パッケージ全体の概要や用語集が必要な場合は、スキル側で既存の設計書や用語集を読むよう指示します。

## 9. トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| 「Ollama に接続できません」 | GB10 の `OLLAMA_HOST` が `0.0.0.0` になっているか、ファイアウォールで 11434 番ポートが開いているかを確認する |
| モデルが見つからないエラー | `--model` の値が `ollama list` のタグ名と一致しているか確認する（`:latest` などの付け方も含む） |
| 応答がすぐ途切れる・指示を無視する | コンテキスト長が足りていない。GB10 で `ollama ps` を実行し、CONTEXT 列を確認する |
| 1ファイル目だけ処理して終わる | Bash 版で `< /dev/null` が抜けている |
| ログに本文はあるが設計書ファイルがない | モデルが Write ツールを使わずに終わっている。プロンプトの「必ず Write ツールで保存」の指示を残したまま再実行する。改善しない場合は下記の「スキル埋め込み方式」を使う |
| スキルが使われない | スキルの配置場所と名前を確認する。改善しない場合は下記の「スキル埋め込み方式」を使う |
| 同じ操作を繰り返して終わらない | `--max-turns` と `timeout` で打ち切られる。ログを見て、スキルの指示を具体的にする |
| 1件ごとの開始が遅い | モデルが毎回ロードされている。`OLLAMA_KEEP_ALIVE=-1` を確認する |
| 日本語が文字化けする | PowerShell 5.1 を使っている。Git Bash か PowerShell 7 で実行する |

### スキル埋め込み方式（スキルが安定して使われない場合）

ローカルモデルがスキルをうまく呼び出せない場合は、SKILL.md の内容をプロンプトに直接埋め込みます。モデルがスキルを呼び出す必要がなくなるため、最も確実です。

Git Bash 版の `claude -p "/$SKILL $f ...` の部分を、次のように置き換えます。

```bash
SKILL_FILE=".claude/skills/$SKILL/SKILL.md"

  timeout "$TIMEOUT" claude -p "以下の手順に従って、$f の詳細設計書を作成してください。

----- 手順ここから -----
$(cat "$SKILL_FILE")
----- 手順ここまで -----

出力先は $out です。必ず Write ツールでファイルに保存してください。
本文を画面に出力するだけで終わらないでください。日本語で書いてください。" \
    --model "$MODEL" \
    --allowedTools "Read,Grep,Glob,Write" \
    ...
```

`SKILL_FILE` の定義は、ループの外（生成設定の部分）に置きます。
