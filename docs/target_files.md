# 処理対象の言語・ファイルの変更手順

詳細設計書の作成やコメント追記の対象（言語・フォルダ・ファイル・除外）を変えるための手順をまとめる。
処理全体の流れは [workflow.md](workflow.md) を参照。

## 1. 仕組み

対象は設定ファイル（TOML 形式）で決まる。コードの修正は不要。

| 使われる設定ファイル（上から優先） | 用途 |
|---|---|
| `.env` の `TARGETS_CONFIG` に書いたパス | 設定ファイルを別の場所に置く場合 |
| `targets.toml`（このフォルダ直下。コミットしない） | 通常はこれを編集する |
| [targets.example.toml](../targets.example.toml)（コミット済み） | 上の2つがない場合。Go の `<xx>_api/pkg/handler` 配下だけを対象にする |

実行すると、最初に `対象の設定=<使われたファイル> 言語=go,...` と表示される。

設計書スキルは、どの言語でも共通の `skills/design-doc-batch/SKILL.md` を使う（「5. 設計書スキル」を参照）。
言語ごとに切り替わるのは次の2つだけ。

- プロンプトに書く言語名（「次のTypeScriptファイルの詳細設計書を作成してください」など）
- 公開識別子の抽出方法（設計書にすべて書かれているかを検証する対象）

| 言語（`name`） | 公開識別子とみなすもの | 解析処理 |
|---|---|---|
| `go` | 大文字で始まる関数・メソッド・型 | [go_source.py](../godoc_agent/go_source.py) |
| `csharp` | `public` の付いた型・メソッド・コンストラクター・プロパティ・フィールド | [csharp_source.py](../godoc_agent/csharp_source.py) |
| `typescript` | `export` した関数・クラス・インターフェース・型・enum・変数、`export { a as b }` の名前 | [typescript_source.py](../godoc_agent/typescript_source.py) |

どの言語の解析も正規表現による簡易版で、複数行にまたがる宣言などは取りこぼすことがある（各ファイルの先頭の TODO を参照）。

## 2. 設定ファイルの書き方

```toml
# すべての言語に共通の除外（任意）
exclude = ["**/.*/**", "**/vendor/**", "**/testdata/**", "**/node_modules/**"]

[[languages]]
name = "go"                               # 解析に使う言語: go / csharp / typescript
extensions = [".go"]                      # この拡張子のファイルをこの言語として扱う
include = ["**/?*_api/pkg/handler/**"]    # 対象にするパス（どれか1つに一致すれば対象）
exclude = ["**/*_test.go"]                # 除外するパス（任意。どれか1つに一致すれば除外）

[[languages]]
name = "typescript"
enabled = false                           # false にすると、書いたまま無効にできる（省略時は true）
extensions = [".ts", ".tsx"]
include = ["**/src/handlers/**"]
exclude = ["**/*.test.ts", "**/*.d.ts"]
```

| 項目 | 必須 | 内容 |
|---|---|---|
| `exclude`（先頭） | 任意 | すべての言語に共通の除外パターン |
| `[[languages]]` | 必須 | 言語ごとに1つ書く。有効な言語が1つ以上必要 |
| `name` | 必須 | `go`・`csharp`・`typescript` のいずれか。同じ言語は2回書けない |
| `enabled` | 任意 | `true` / `false`。省略時は `true` |
| `extensions` | 必須 | `.` で始まる拡張子。有効な言語どうしで同じ拡張子は書けない（拡張子で言語を決めるため） |
| `include` | 必須 | 対象にするパスのパターン。1つ以上必要 |
| `exclude` | 任意 | その言語だけの除外パターン |

除外は対象より優先される（`include` に一致しても `exclude` に一致すれば対象外）。

### パターンの書き方

パターンは `--repo` からの相対パス（区切りは `/`）全体と照合する。

| 記号 | 意味 | 例 |
|---|---|---|
| `*` | `/` を含まない任意の文字列（0文字以上） | `*_test.go` |
| `?` | `/` 以外の任意の1文字 | `?*_api`（`_api` の前に1文字以上） |
| `[abc]`・`[!abc]` | いずれか1文字・いずれでもない1文字 | `[!.]*.ts` |
| `**/` | 0個以上のフォルダ（先頭や途中に書く） | `**/vendor/**` |
| `/**`（末尾） | そのフォルダの下のすべて（サブフォルダを含む） | `web/src/**` |

- `include` は、`--repo` のフォルダ名を先頭に付けたパスとも照合する。`--repo` に API フォルダ自体（例：`/srv/repos/user_api`）を指定しても、`**/?*_api/pkg/handler/**` で判定できるようにするため
- `exclude` は `--repo` からの相対パスとだけ照合する
- `\`・先頭の `/`・`..` は書けない
- 大文字と小文字は区別する（`Controllers` と `controllers` は別）

## 3. 変更の例

**対象フォルダを変える**（`<xx>_svc/internal/api` 配下にする）

```toml
include = ["**/?*_svc/internal/api/**"]
```

**対象フォルダを複数にする**

```toml
include = ["**/?*_api/pkg/handler/**", "**/?*_api/pkg/usecase/**"]
```

**除外を追加する**（自動生成ファイルを除外する）

```toml
exclude = ["**/*_test.go", "**/*.pb.go", "**/generated/**"]
```

**C# を追加する**（targets.example.toml の C# の例を有効にする）

```toml
[[languages]]
name = "csharp"
extensions = [".cs"]
include = ["**/Controllers/**"]
exclude = ["**/bin/**", "**/obj/**", "**/*Tests.cs", "**/*.Designer.cs", "**/*.g.cs"]
```

複数の言語を有効にすると、1回の実行ですべての言語のファイルを処理する。
出力先は言語に関係なく `--out/<相対パス>.md`（例：`out/Api/Controllers/UserController.cs.md`）。

## 4. 変更の手順

1. **設定ファイルを作る**（初回だけ）
   ```bash
   ssh gb10 "cd /opt/Lang_Chain && cp -n targets.example.toml targets.toml"
   ```
   `targets.toml` はコミットしないので、`git pull` で上書きされない。
2. **`targets.toml` を編集する**
   書き方の誤り（不明な項目名、`name` の綴り間違い、拡張子の重複など）があると、ファイルを探す前に `エラー: 対象の設定ファイル <パス>: ...` を表示して終了コード `2` で止まる。
3. **実際の対象を確認する**
   まず `--limit` で件数を絞って実行し、最初に表示される `対象の設定=...`・`対象=N 件` と `[1/N] done ... <パス>` の行で、想定したファイルが対象になっているか確かめる。
4. **古い出力を片付ける**
   対象が変わっても、以前の対象から外れたファイルの出力は `--out` に残ったまま（自動では削除しない）。不要なら手で削除する。

`targets.toml` を変えるだけなら、テストやコードの修正は不要（テストは `targets.example.toml` を前提にしている）。

### 設定では変えられないもの

- 対応言語の追加（`go`・`csharp`・`typescript` 以外）：`<言語>_source.py` に `extract_facts()` を作り、[analyzers.py](../godoc_agent/analyzers.py) の `ANALYZERS` に1行足す
- コメント以外の差分の検出（コメント追記の検証）：全言語で共通の C 系の字句規則（`//`・`/* */`・`"`・`'`・`` ` ``）を使う。C# の `@"..."` や TypeScript の正規表現リテラルは正しく扱えないことがある（[source_common.py](../godoc_agent/source_common.py) の TODO を参照）

## 5. 設計書スキル

設計書スキルの置き場所は `skills/design-doc-batch/SKILL.md` に固定した（`.env` での指定は不要）。
すべての言語で同じスキルを使うので、スキルには特定の言語に依存しない書き方をしておく。

**以前の設定（`CLAUDE_SKILL_PATH=/opt/Lang_Chain/skills/go-design-doc/SKILL.md` など）から移行する手順**

```bash
# 1. スキルのフォルダ名を design-doc-batch に変える（SKILL.md の name: も design-doc-batch に揃える）
ssh gb10 "cd /opt/Lang_Chain/skills && mv go-design-doc design-doc-batch"
# 2. .env の CLAUDE_SKILL_PATH の行を削除する（残っていると、そちらのパスが使われる）
ssh gb10 "cd /opt/Lang_Chain && sed -i '/^CLAUDE_SKILL_PATH=/d' .env"
# 3. 確認（パスが表示されればよい）
ssh gb10 "ls /opt/Lang_Chain/skills/design-doc-batch/SKILL.md"
```

`CLAUDE_SKILL_PATH` は、スキルを別の場所に置く場合のために残している。

サーバー全体の入れ替え手順（更新されるファイル・確認方法・元に戻す方法）は [migration_multi_language.md](migration_multi_language.md) にまとめている。
