# 多言語対応版への移行手順（サーバー）

処理対象を設定ファイル（`targets.toml`）で決め、設計書スキルを全言語で共通の `design-doc-batch` にした版（PR #19）に、サーバー（GB10）を入れ替える手順をまとめる。
コマンドはすべて GB10 の端末で実行する（SSH は使えないため、GB10 に直接ログインした端末を使う）。sudo は不要。

設定ファイルの書き方は [target_files.md](../target_files.md) を参照。

## 1. 事前確認

設定ファイルの読み込みに Python 3.11 以降の標準ライブラリ（`tomllib`）を使う。サーバーの Python のバージョンを確認する。

```bash
/opt/Batch_Exec/.venv/bin/python --version   # 3.11 以上であればよい
```

## 2. プログラムを更新する（`git pull`）

```bash
cd /opt/Batch_Exec && git pull
```

次の24ファイルが自動で入れ替わる。手作業は不要。

| 種類 | ファイル |
|---|---|
| 新規のプログラム | `godoc_agent/targets.py`・`analyzers.py`・`source_common.py`・`csharp_source.py`・`typescript_source.py` |
| 変更したプログラム | `godoc_agent/config.py`・`doc_graph.py`・`go_source.py`・`prompts.py`・`runner.py`・`tasks.py` |
| 設定の例 | `targets.example.toml`（新規）・`.env.example`・`.gitignore` |
| ドキュメント | `README.md`・`docs/target_files.md`・`docs/workflow.md` |
| テスト | `tests/` 配下の9ファイル |

`requirements.txt` は変わっていないため、`pip install` は不要。

## 3. サーバー上で手で直すもの（Git の管理外）

| 対象 | 作業 | 必須か |
|---|---|---|
| `skills/go-design-doc/`（フォルダ） | `skills/design-doc-batch/` に名前を変える | 必須 |
| `.env` | `CLAUDE_SKILL_PATH=...` の行を削除する | 必須（残っていると、以前のスキルが使われ続ける） |
| `targets.toml` | `targets.example.toml` をコピーして作る | 任意（対象を変えるときだけ。なければ例のファイルが使われる） |

```bash
# 1. スキルのフォルダ名を変える（今のフォルダ名が go-design-doc でなければ合わせる）
cd /opt/Batch_Exec/skills && mv go-design-doc design-doc-batch
# 2. .env から CLAUDE_SKILL_PATH の行を削除する
cd /opt/Batch_Exec && sed -i '/^CLAUDE_SKILL_PATH=/d' .env
# 3. 確認
ls /opt/Batch_Exec/skills/design-doc-batch/SKILL.md && grep -c '^CLAUDE_SKILL_PATH=' /opt/Batch_Exec/.env
```

`ls` で SKILL.md のパスが表示され、`grep -c` が `0` を表示すれば完了。
SKILL.md の先頭に `name:` がある場合は、`design-doc-batch` に揃えておく（runner はこの項目を使わないが、名前を揃えておくと分かりやすい）。

対象の言語・フォルダを変える場合だけ、設定ファイルを作って編集する（書き方は [target_files.md](../target_files.md)）。

```bash
cd /opt/Batch_Exec && cp -n targets.example.toml targets.toml
```

## 4. 変更しないもの

| 対象 | 理由 |
|---|---|
| `.env` のほかの設定 | 設定項目は変わっていない |
| `out/`（設計書・manifest） | 出力先と差分スキップの記録の形式は変わっていない |
| `state/`（チェックポイント） | 現在の版では使っていない（削除してよい） |
| `deploy/`（nightly.sh・systemd） | スキルのパスや対象を参照していない |

## 5. 入れ替え後の確認

```bash
# テスト（Ollama・Claude Code 不要）。「102 passed」と表示されればよい
cd /opt/Batch_Exec && .venv/bin/python -m pytest
```

次に、件数を絞って1回実行する（`/srv/repos/myapp` は実際の `--repo` のパスに置き換える）。

```bash
cd /opt/Batch_Exec && .venv/bin/python -m godoc_agent.runner --repo /srv/repos/myapp --out ./out --limit 3
```

最初の行に `対象の設定=/opt/Batch_Exec/targets.example.toml 言語=go` と表示されれば、新しい仕組みで動いている（`targets.toml` を作った場合は、そのパスが表示される）。
`エラー: SKILL.md が見つかりません: ...` と表示された場合は、3. のフォルダ名の変更が正しくできているか確認する。

## 6. 元に戻す場合

```bash
cd /opt/Batch_Exec/skills && mv design-doc-batch go-design-doc
cd /opt/Batch_Exec && echo 'CLAUDE_SKILL_PATH=/opt/Batch_Exec/skills/go-design-doc/SKILL.md' >> .env
cd /opt/Batch_Exec && git checkout dd92d27   # PR #19 より前の版に戻す（戻した後は main に戻すまで git pull しない）
```
