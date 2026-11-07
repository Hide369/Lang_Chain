# 処理対象ファイルの変更手順

詳細設計書の作成やコメント追記の対象になる Go ファイルを変えるための手順をまとめる。
処理全体の流れは [workflow.md](workflow.md) を参照。

## 1. 対象ファイルが決まる場所

対象ファイルと除外ファイルの判定は、[go_source.py](../godoc_agent/go_source.py) の `discover_go_files()` だけで行っている。
呼び出し元は [runner.py](../godoc_agent/runner.py) の1か所だけで、ほかの場所で対象を追加したり除外したりはしていない。

`discover_go_files()` は `--repo` 配下を次の順に判定する。

| 順番 | 判定 | 設定している場所 |
|---|---|---|
| 1 | 拡張子が `.go` のファイルを集める（サブディレクトリも含む） | `repo.rglob("*.go")` に直接書かれている |
| 2 | 途中のフォルダに `vendor`・`testdata`・`.git`・`node_modules`、または `.` で始まる名前があれば除外 | 定数 `_EXCLUDED_DIRS` |
| 3 | `<xx>_api/pkg/handler` より下にないファイルは除外 | 定数 `_API_DIR_SUFFIX`・`_HANDLER_SUBPATH`（判定は `_is_handler_dir()`） |
| 4 | ファイル名が `_test.go` で終わるものは除外 | `path.name.endswith("_test.go")` に直接書かれている |

2・4 は 3 より優先される。そのため、対象フォルダの中にあっても `vendor/` の下のファイルや `_test.go` は対象外になる。

### 実行時の絞り込み（設定ではない）

`discover_go_files()` で見つかったファイルは、runner でさらに次のものが除かれる。
これは実行するたびに変わる条件で、「どのファイルが対象か」の設定ではない。

- 前回から内容が変わっておらず、出力もすでにあるファイル（`--force` を付けると再処理する）
- `--limit` で指定した件数を超えた分

## 2. 変更したい内容ごとの修正箇所

| 変えたいこと | 修正箇所 |
|---|---|
| 対象フォルダの名前（`_api`・`pkg/handler`） | `_API_DIR_SUFFIX`・`_HANDLER_SUBPATH` |
| 除外するフォルダ | `_EXCLUDED_DIRS` |
| ファイル名による除外（`_test.go` 以外も除外したい等） | `discover_go_files()` の `_test.go` の判定 |
| 対象フォルダの「形」（完全一致・接頭辞での一致など） | `_is_handler_dir()` |

### 対象フォルダの名前を変える

```python
# 変更前: <xx>_api/pkg/handler 配下が対象
_API_DIR_SUFFIX = "_api"
_HANDLER_SUBPATH = ("pkg", "handler")

# 例: <xx>_svc/internal/api 配下を対象にする
_API_DIR_SUFFIX = "_svc"
_HANDLER_SUBPATH = ("internal", "api")

# 例: 階層が1つだけなら末尾のカンマを付ける
_HANDLER_SUBPATH = ("handler",)   # ("handler") と書くとタプルではなく文字列になり、正しく判定されない
```

定数で変えられるのは**名前だけ**で、次の「形」は `_is_handler_dir()` で決まっている。これらを変えたい場合は関数自体を書き換える。

| 固定されている部分 | 内容 |
|---|---|
| 接尾辞での一致 | `endswith` で判定するため「`_api` で終わる」という意味になる。完全一致（例：`api` だけ）や接頭辞での一致（例：`api_` で始まる）にはならない |
| 接尾辞だけの名前は対象外 | `_api` という名前のフォルダは対象にならない（`xx` 部分が1文字以上必要） |
| 直下の並び | `_HANDLER_SUBPATH` は API フォルダの**すぐ下**に続いている必要がある。例：`user_api/internal/pkg/handler` は対象外 |
| サブディレクトリ | 指定パスより下の階層はすべて対象になる |
| `--repo` の指定 | `--repo` に API フォルダ自体（例：`/srv/repos/user_api`）を指定しても判定できるよう、`--repo` のフォルダ名も含めて調べる |

出力パス（`out/<相対パス>.md`・`out/_commented/<相対パス>`）は `--repo` からの相対パスで決まるため、修正は不要。

### 除外するフォルダを変える

```python
# 例: generated フォルダも除外する
_EXCLUDED_DIRS = {"vendor", "testdata", ".git", "node_modules", "generated"}
```

フォルダ名の完全一致で判定する。`.` で始まるフォルダは `_EXCLUDED_DIRS` に関係なく常に除外される。

## 3. 変更後にやること

1. **テストを更新する**
   次のテストは `user_api/pkg/handler/...` や除外条件を前提にしているため、変更に合わせて期待値やパスを直す。
   - [tests/test_go_source.py](../tests/test_go_source.py)：`test_discover_*`（対象・除外の判定そのもの）
   - [tests/test_runner.py](../tests/test_runner.py)：テスト用のファイルパス
   - [tests/test_claude_code.py](../tests/test_claude_code.py)：プロンプトに含まれるパスの例
2. **テストを実行する**
   ```bash
   cd /opt/Lang_Chain && .venv/bin/python -m pytest
   ```
3. **説明を更新する**
   - [go_source.py](../godoc_agent/go_source.py) の `_API_DIR_SUFFIX` の上にあるコメント
   - [README.md](../README.md) の対象範囲の説明と例
   - [workflow.md](workflow.md) の「2. 対象ファイルの列挙」の表
   - この手順書の例
4. **実際の対象を確認する**
   まず `--limit` で件数を絞って実行し、最初に表示される `対象=N 件` と `[1/N] done ... <パス>` の行で、想定したファイルが対象になっているか確かめる。
   対象が変わると、以前の対象から外れたファイルの出力は `--out` に残ったままになるので、不要なら手で削除する。
