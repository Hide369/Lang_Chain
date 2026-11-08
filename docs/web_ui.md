# Web 画面（ブラウザからの実行・設定）

クライアント PC のブラウザから、GB10 上の runner を実行・停止し、進捗のログと生成物を確認できる。
スキルの登録、対象フォルダ・出力先フォルダの選択、対象の設定（targets.toml）の編集も画面から行える。

```
クライアント PC（ブラウザ）            サーバー(GB10)
  http://192.168.31.26:4000  ───>  Web 画面（python -m godoc_agent.web、常駐）
                                      └─ runner を別プロセスで起動 ── Claude Code ── Ollama
```

ソースコードと設計書の生成処理は、これまでどおりサーバーの中だけで行う。ブラウザに送るのは、画面の表示に必要なもの（ログ、生成物の本文、フォルダ名）だけ。

## 1. できること・権限

| 操作 | ログインなし | 管理者（パスワードでログイン） |
|---|---|---|
| 状態・ログ・実行履歴・生成物の閲覧、生成物のダウンロード | ○（`WEB_VIEW_REQUIRES_LOGIN=true` なら ×） | ○ |
| 対象の設定の閲覧 | ○（同上） | ○ |
| 実行・停止 | × | ○ |
| 対象フォルダ・出力先フォルダの選択（フォルダ選択ダイアログ） | × | ○ |
| 対象の設定の変更・対象ファイルのプレビュー | × | ○ |
| スキルの登録・確認・直前に戻す・登録を外す | × | ○（「スキル」タブは管理者にだけ表示） |

管理者は1種類だけで、パスワードを知っている人が管理者になる。ユーザーごとのアカウントはない。

### 実行の競合を防ぐ仕組み

- Web 画面から同時に動かせる runner は1つだけ。実行中は「実行」ボタンが押せず、API に直接送っても `409` で断る
- runner 本体も、出力先フォルダごとにロック（`<出力先>/.runner.lock`）を取る。夜間タイマー・コマンド・Web 画面のどこから起動しても、同じ出力先では同時に1つしか動かない。後から起動した方は `エラー: 別の実行が進行中です` で止まる（終了コード `2`）
  - 夜間タイマーが動いている間は、画面の状態に「他の実行が進行中（夜間バッチ・コマンド）」と表示され、実行ボタンは押せない
  - 出力先が違う実行どうしは同時に動ける（データは壊れないが、Ollama を取り合うので遅くなる）。夜間タイマーと同じ出力先（既定 `/opt/Lang_Chain/out`）を選んでおくと確実に排他できる
- 実行中は設定（フォルダ・スキル・対象の設定）を変更できない。どの設定で動いた結果か分からなくなるのを防ぐため

### 停止

「停止」を押すと、runner と、runner が起動した claude をまとめて止める（30 秒たっても止まらなければ強制終了する）。
完了済みのファイルは manifest に記録されているので、もう一度「実行」を押すと残りのファイルだけが最初から処理される。

## 2. 設置（初回だけ）

```bash
ssh gb10
cd /opt/Lang_Chain
git pull
.venv/bin/pip install -r requirements.txt     # fastapi・uvicorn が追加された

# 1. 管理者パスワードを決め、ハッシュを .env に書く（パスワード自体はどこにも保存されない）
.venv/bin/python -m godoc_agent.web.password  # 12文字以上。表示された WEB_ADMIN_PASSWORD_HASH=... の行を .env に追記
#    （`>> .env` でリダイレクトしてもよい。入力の確認は画面に出る）

# 2. 残りの設定を .env に追記する（値は環境に合わせる）
cat >> .env <<'EOF'
WEB_HOST=0.0.0.0
WEB_PORT=4000
WEB_BROWSE_ROOTS=/srv/repos,/opt/Lang_Chain/out
EOF

# 3. 手動で起動して確かめる（Ctrl+C で止める）
.venv/bin/python -m godoc_agent.web
#    クライアント PC のブラウザで http://192.168.31.26:4000 を開く

# 4. 常駐させる（sudo が必要。自分のターミナルから ssh -t gb10 で実行する）
sudo cp deploy/systemd/godoc-web.service /etc/systemd/system/
sudo sed -i "s/REPLACE_WITH_USER/$USER/" /etc/systemd/system/godoc-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now godoc-web.service
systemctl status godoc-web.service            # active (running) ならよい
```

ファイアウォール（ufw）を使っている場合は、LAN からだけ 4000 番を開ける。

```bash
sudo ufw allow from 192.168.31.0/24 to any port 4000 proto tcp
```

### 設定項目（.env）

| 項目 | 必須 | 内容 |
|---|---|---|
| `WEB_ADMIN_PASSWORD_HASH` | 必須 | 管理者パスワードのハッシュ。`python -m godoc_agent.web.password` で作る。パスワードを変えるときも同じ手順で作り直し、Web 画面を再起動する |
| `WEB_BROWSE_ROOTS` | 必須 | フォルダ選択ダイアログで辿れる起点（カンマ区切りの絶対パス）。対象・出力先はこの下からしか選べない。広くしすぎない（例：`/srv/repos,/opt/Lang_Chain/out`） |
| `WEB_HOST` | 任意 | 待ち受けるアドレス。既定 `127.0.0.1`（GB10 自身からだけ）。LAN から使うなら `0.0.0.0` |
| `WEB_PORT` | 任意 | 既定 `4000` |
| `WEB_SESSION_TTL_HOURS` | 任意 | ログインの有効時間。既定 `12` |
| `WEB_VIEW_REQUIRES_LOGIN` | 任意 | `true` にすると閲覧にもログインが必要。既定 `false` |
| `WEB_COOKIE_SECURE` | 任意 | HTTPS で使う場合は `true`。既定 `false` |
| `WEB_STATE_DIR` | 任意 | 画面で選んだフォルダの保存先。既定 `/opt/Lang_Chain/state/web` |

設定の誤り（ハッシュの形式、`WEB_BROWSE_ROOTS` のフォルダがないなど）があると、`エラー: ...` を表示して起動しない。

## 3. 使い方

1. ブラウザで `http://192.168.31.26:4000` を開き、右上から管理者パスワードでログインする
2. **スキル**タブで設計書スキルを登録する（初回だけ。すでに `skills/design-doc-batch/` に置いてあれば「登録済み」と表示される）
   - 「フォルダを選んで登録」… SKILL.md のあるフォルダを選ぶと、参照ファイル（`references/` など）ごと登録する
   - 「SKILL.md だけ登録」… SKILL.md 1ファイルだけを登録する
   - コメント追記も行う場合は、コメント追記スキルも登録する（登録するとタスクに「コメント追記」が出る）
3. **実行**タブの「フォルダ」で、対象フォルダ（`--repo`）と出力先（`--out`）を「選択…」から選ぶ
   - 出力先は、ダイアログ下の「新しいフォルダ名」に名前を入れると、選んでいるフォルダの下に新しいフォルダを指定できる（実行時に作られる）
   - 出力先を対象フォルダの中にはできない（生成物が次回の対象に混ざるため）
4. **対象の設定**タブで、言語・対象（include）・除外（exclude）を確認する。「対象ファイルを確認」で、保存前の設定でどのファイルが処理されるかを一覧できる。変えたら「保存」
5. **実行**タブでタスク・件数の上限・再生成を選んで「実行」。ログが流れ、終わると状態・実行履歴・生成物が更新される
6. 生成物の一覧からファイルを選ぶと本文が表示される。「ダウンロード」で保存できる

### スキルの保存先

画面から登録したスキルは、runner が読む場所にそのまま置かれる。夜間タイマー・コマンドからの実行でも同じスキルが使われる。

| スキル | 保存先 |
|---|---|
| 設計書 | `CLAUDE_SKILL_PATH` のフォルダ。未設定なら `skills/design-doc-batch/` |
| コメント追記 | `CLAUDE_COMMENT_SKILL_PATH` のフォルダ。未設定なら `skills/comment-batch/` |

- 登録すると、直前のスキルは `<フォルダ名>.prev` に1世代だけ残る。「直前に戻す」で入れ替えられる
- コメント追記スキルの「登録を外す」は、フォルダを `.prev` に移す。コメント追記は実行されなくなる（「直前に戻す」で戻せる）
- `skills/comment-batch/SKILL.md` があれば、`.env` に `CLAUDE_COMMENT_SKILL_PATH` を書かなくてもコメント追記を行う（この版からの変更）
- 上限：1スキルあたり 200 ファイル・合計 10MB・1ファイル 2MB。SKILL.md は UTF-8 で、フォルダの直下に必要

### 対象の設定の保存先

`TARGETS_CONFIG`、未設定なら `/opt/Lang_Chain/targets.toml` に保存する（runner が読む場所と同じ）。
`targets.toml` がない間は例の設定（targets.example.toml）が表示され、保存すると `targets.toml` が作られる。
検証は runner と同じ処理で行うので、書き方の誤りは保存時にエラーとして表示される（保存されない）。
画面から保存すると、ファイル内のコメントは消える。書き方の詳細は [target_files.md](target_files.md) を参照。

## 4. 運用

| 作業 | 方法 |
|---|---|
| 状態の確認 | `systemctl status godoc-web.service` / `journalctl -u godoc-web.service -n 50` |
| 再起動（`.env` を変えたとき、`git pull` で更新したとき） | `sudo systemctl restart godoc-web.service` |
| パスワードの変更 | `python -m godoc_agent.web.password` で作り直して `.env` を書き換え、再起動 |
| 実行のログ | 画面のほか、`<出力先>/_runs/web-<日時>.log` に残る（計測ログは同じ名前の `.jsonl`） |

- Web 画面を再起動すると、ログイン状態は消える（もう一度ログインする）
- 再起動しても、画面で選んだフォルダは `state/web/workspace.json` に残る
- Web 画面を止める（再起動する）と、実行中の runner も止まる（`KillMode=control-group`）。完了済みのファイルは manifest に残るので、再実行で続きから処理される

## 5. セキュリティ上の注意

- **通信は HTTP（暗号化なし）**。同じ LAN 上で通信を盗聴できる人には、パスワード・設計書の本文が見える。信頼できない人がいるネットワークでは次のどちらかにする
  - SSH トンネル：`WEB_HOST=127.0.0.1` のままにし、クライアントで `ssh -N -L 4000:127.0.0.1:4000 gb10` を実行してから `http://localhost:4000` を開く
  - HTTPS のリバースプロキシ（nginx・Caddy など）を前に置き、`WEB_COOKIE_SECURE=true` にする
- 閲覧は既定でログイン不要。設計書の内容を LAN 内の誰にも見せたくない場合は `WEB_VIEW_REQUIRES_LOGIN=true` にする
- ログインに5回続けて失敗した接続元は、15分間ログインできなくなる
- フォルダ選択ダイアログは `WEB_BROWSE_ROOTS` の外を表示しない。生成物の表示は出力先の中のファイルだけで、`_runs/` やドットファイル（`.manifest.json` など）は表示しない
- 状態を変える操作（ログイン・実行・設定変更）には専用のヘッダーを必須にし、Cookie は `SameSite=Strict`・`HttpOnly` にしている（別のサイトから操作を送り込まれるのを防ぐため）

## 6. 既知の制約

- 管理者は1人分（パスワード1つ）。誰が実行したかは記録しない
- ログイン状態はサーバーのメモリに持つため、Web 画面を複数プロセスで動かす構成には対応しない（1プロセスで動かす）
- 出力先が違えば、夜間タイマーと Web 画面からの実行が同時に動きうる（「実行の競合を防ぐ仕組み」を参照）
- 夜間バッチ（deploy/nightly.sh）と違い、Web 画面からの実行は対象リポジトリの `git pull` を行わない。最新のソースで生成したい場合は、先にリポジトリを更新しておく
