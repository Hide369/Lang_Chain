# Web 画面版への移行手順（サーバー）

Web 画面を追加した版（PR #23・#24）に、サーバー（GB10）を入れ替える手順をまとめる。
入れ替え前の版は PR #22（コミット `072ecbc`）を想定している。

- コマンドはすべて GB10 の端末で実行する（SSH は使えないため、GB10 に直接ログインした端末を使う）。sudo が必要な手順はパスワードを入力する
- 例のパスは `/opt/Batch_Exec`（このリポジトリ）、`/srv/repos/myapp`（対象リポジトリ）、`/opt/Batch_Exec/out`（出力先）。実際のパスに置き換える
- 画面の使い方・設定項目の詳細は [web_ui.md](../other/web_ui.md) を参照

## 0. この版で変わること

| 変更 | 以前 | この版 |
|---|---|---|
| 実行のしかた | GB10 の端末でコマンドを実行する / systemd の夜間タイマー | ブラウザ（`http://192.168.31.26:4000`）から実行・停止・進捗確認ができる（コマンドもこれまでどおり使える） |
| 権限 | なし（GB10 にログインできる人が実行できる） | 閲覧は誰でも、実行・設定変更は管理者パスワードでログインした人だけ |
| 同時実行 | 防ぐ仕組みなし | 同じ出力先では1つしか動かない（Web 画面・コマンド・夜間タイマーのどこから起動しても）。後から起動した方は `エラー: 別の実行が進行中です`（終了コード 2） |
| 定期実行 | systemd の夜間タイマー（時刻の変更に sudo が必要） | 画面で設定する（オン/オフ・時刻・スキル・実行前の git pull）。**systemd の夜間タイマーは止める** |
| スキル | GB10 の `skills/` に手で置く。設計書とコメント追記の2つだけ | 画面から登録できる。名前と出力の種類（文書・コメントだけ変更・コードも変更）を決めて追加もできる。`--tasks` にスキル名を書ける |
| コメント追記スキル | `.env` の `CLAUDE_COMMENT_SKILL_PATH` が必要 | `skills/comment-batch/SKILL.md` に置けば `.env` は不要（これまでの `CLAUDE_COMMENT_SKILL_PATH` もそのまま使える） |
| 対象の設定（targets.toml） | サーバーで直接編集する | 画面でも編集でき、保存前に対象ファイルを一覧で確かめられる |
| 実行前のソース更新 | 夜間タイマー（nightly.sh）だけが `git pull` | 画面の実行・定期実行で選べる。pull の前に、対象フォルダがクローンの最上位フォルダであること・追跡ブランチがあることを確かめ、pull 先をログに出す |

設計書・コメント追記の出力先、差分スキップの記録（manifest）、計測ログの形式は変わらない。生成済みの設計書はそのまま次回の改訂に使われる。

## 1. 事前確認

### 1-1. 実行中のバッチがないこと

```bash
systemctl is-active godoc-nightly.service; tmux ls 2>/dev/null; pgrep -af godoc_agent.runner
```

`inactive` と表示され、`tmux ls` と `pgrep` が何も表示しなければよい。動いている場合は終わるのを待つ。

### 1-2. サーバーで直接書き換えたファイルがないこと

```bash
cd /opt/Batch_Exec && git status --short
```

` M`（変更）が表示されたら、`git diff` で内容を確かめてから `git stash` で退避する（[migration_drop_checkpointer.md](../pr18-22/migration_drop_checkpointer.md) の 1-2 と同じ）。
`??`（`out/`・`state/`・`.env`・`skills/`・`targets.toml` など Git の管理外のファイル）はそのままでよい。

### 1-3. 夜間タイマーの設定を控える

画面の定期実行に同じ対象フォルダ・出力先を設定するため、いまの値を控えておく（タイマーを設置していなければ不要）。

```bash
systemctl cat godoc-nightly.service | grep -E 'GODOC_|^User='; systemctl list-timers godoc-nightly.timer
```

| 控える値 | 使う場所 |
|---|---|
| `GODOC_REPO`（対象リポジトリ） | 画面の「対象フォルダ」（手順 7） |
| `GODOC_OUT`（なければ `/opt/Batch_Exec/out`） | 画面の「出力先」（手順 7） |
| `User=` | Web 画面の実行ユーザー（手順 6。同じユーザーにする） |
| タイマーの時刻（`list-timers` の NEXT） | 画面の定期実行の時刻（手順 8） |

## 2. プログラムを更新する

```bash
cd /opt/Batch_Exec && git pull && .venv/bin/pip install -r requirements.txt
```

`requirements.txt` に `fastapi`・`uvicorn` が追加されたため、`pip install` が必要。

```bash
# テスト（Ollama・Claude Code 不要）。最後に「207 passed」と表示されればよい
cd /opt/Batch_Exec && .venv/bin/python -m pytest
```

テストは本物の `git` コマンドを使うものがある（git pull の確認のテスト）。`git` が入っていれば追加の準備は不要。

## 3. `.env` を設定する

### 3-1. `CLAUDE_BIN` をフルパスにする

Web 画面は systemd のサービスとして動き、そこから runner と claude を起動する。systemd の PATH には `~/.local/bin` が入らないため、フルパスで書く。
夜間タイマーを設置済みなら、すでにフルパスになっているはず。

```bash
which claude; grep '^CLAUDE_BIN=' /opt/Batch_Exec/.env
# 2行目が CLAUDE_BIN=claude のままなら、1行目のパスに直す（例）
cd /opt/Batch_Exec && sed -i 's|^CLAUDE_BIN=.*|CLAUDE_BIN=/home/<ユーザー>/.local/bin/claude|' .env
```

### 3-2. 管理者パスワード

パスワードを決め（12文字以上）、ハッシュを作る。パスワード自体はどこにも保存されない。

```bash
# パスワードを2回入力する。表示された WEB_ADMIN_PASSWORD_HASH=... の行をコピーする
cd /opt/Batch_Exec && .venv/bin/python -m godoc_agent.web.password
# コピーした行を .env に追記する（シングルクォートで囲む）
echo 'WEB_ADMIN_PASSWORD_HASH=scrypt:32768:8:1:...' >> /opt/Batch_Exec/.env
```

### 3-3. 待ち受けとフォルダ選択の範囲

`WEB_BROWSE_ROOTS` は、画面のフォルダ選択ダイアログで辿れる起点。対象フォルダ・出力先はこの下からしか選べない。
**書いたフォルダが存在しないと Web 画面は起動しない**ので、先に作っておく。

```bash
mkdir -p /opt/Batch_Exec/out && ls -d /srv/repos /opt/Batch_Exec/out
cd /opt/Batch_Exec && printf 'WEB_HOST=0.0.0.0\nWEB_PORT=4000\nWEB_BROWSE_ROOTS=/srv/repos,/opt/Batch_Exec/out\n' >> .env
# 確認（WEB_ で始まる4行が表示されればよい。ハッシュは同じ行が2回入っていないこと）
grep '^WEB_' /opt/Batch_Exec/.env | cut -c1-60
```

設計書の内容を LAN の誰にも見せたくない場合は `WEB_VIEW_REQUIRES_LOGIN=true` も追記する（既定は閲覧はログイン不要）。

## 4. 実行前の git pull を使う場合：GitLab の認証

画面の「実行前に git pull」・定期実行の git pull を使う場合だけ必要。使わない場合は手順 5 へ。

git pull は Web 画面の実行ユーザー（手順 6 の `User=`）で、パスワード入力なしで動く必要がある（入力待ちで止まらないよう、git に尋ねさせない設定で実行する）。
SSH は使えないため、**HTTPS とプロジェクトアクセストークン**で認証する。手順は [web_ui.md の「GitLab の認証（HTTPS とアクセストークン）」](../other/web_ui.md#gitlab-の認証https-とアクセストークン) を参照（実行ユーザーで行う）。

**確認**（対象リポジトリごとに行う）

```bash
# pull 先の URL（GitLab）と追跡ブランチ（例: origin/main）が表示されればよい
git -C /srv/repos/myapp remote -v; git -C /srv/repos/myapp rev-parse --abbrev-ref @{u}
# 対象フォルダが clone の最上位フォルダであること（2行が同じパスならよい）
git -C /srv/repos/myapp rev-parse --show-toplevel; echo /srv/repos/myapp
# パスワードを聞かれずに成功すればよい
GIT_TERMINAL_PROMPT=0 git -C /srv/repos/myapp pull --ff-only
```

`--show-toplevel` が別のパスを表示する場合（git 管理されていない親フォルダに複数の `xx_api` をクローンしている場合など）、その対象フォルダでは「実行前に git pull」は使えない（画面でエラーになる）。git pull をオフにして、別の方法で各リポジトリを更新する。

## 5. スキル・対象の設定・出力先（引き継ぎ）

ファイルの移動は不要。これまでの置き場所のまま画面に表示され、画面から入れ替えた場合も同じ場所に保存される。

| 対象 | この版での扱い |
|---|---|
| `skills/design-doc-batch/`（または `CLAUDE_SKILL_PATH`） | そのまま設計書スキルとして使われる |
| コメント追記スキル（`CLAUDE_COMMENT_SKILL_PATH`） | そのまま使われる。画面から登録し直すと、そのフォルダが入れ替わる |
| `targets.toml` | そのまま使われる。画面から保存すると、ファイル内のコメントは消える（値は残る） |
| `out/`（設計書・manifest・計測ログ） | そのまま使われる。画面の出力先に同じフォルダを選べば、実行履歴・生成物も画面に表示される |

`.gitignore` に `skills/` を追加したが、もともと `skills/` はコミットしていないので影響はない。

## 6. Web 画面を常駐させる

```bash
# 1. まず手動で起動して確かめる（Ctrl+C で止める）。「Uvicorn running on http://0.0.0.0:4000」と表示されればよい
cd /opt/Batch_Exec && .venv/bin/python -m godoc_agent.web
#    → クライアント PC のブラウザで http://192.168.31.26:4000 を開き、画面が出ることを確かめてから Ctrl+C

# 2. サービスとして登録する（sudo。実行ユーザーは夜間タイマーと同じユーザーにする）
cd /opt/Batch_Exec && sudo cp deploy/systemd/godoc-web.service /etc/systemd/system/ \
  && sudo sed -i "s/REPLACE_WITH_USER/$USER/" /etc/systemd/system/godoc-web.service \
  && sudo systemctl daemon-reload && sudo systemctl enable --now godoc-web.service
# 3. 確認（active と表示されればよい）
systemctl is-active godoc-web.service; journalctl -u godoc-web.service -n 5 --no-pager
```

`エラー: ...` で起動しない場合は、`journalctl` の表示に従って `.env` を直し、`sudo systemctl restart godoc-web.service` で再起動する。
ファイアウォール（ufw）が有効なら、LAN からだけ 4000 番を開ける。

```bash
sudo ufw status | head -1 && sudo ufw allow from 192.168.31.0/24 to any port 4000 proto tcp
```

## 7. 画面での初期設定と試し実行

ブラウザで `http://192.168.31.26:4000` を開き、右上から管理者パスワードでログインする。

1. **スキル**タブ：設計書スキル（とコメント追記スキル）が「登録済み」と表示されることを確かめる
2. **対象の設定**タブ：内容がいまの `targets.toml` と同じことを確かめる。「対象ファイルを確認」は、手順 3 のフォルダ選択の後で使える
3. **実行**タブの「フォルダ」：対象フォルダ・出力先を、手順 1-3 で控えた `GODOC_REPO`・`GODOC_OUT` と同じにする
4. **実行**タブ：スキルを選び、件数の上限を `2` 程度にして「実行」。git pull を使う場合は「実行前に対象フォルダを git pull する」も選ぶ
5. ログに次が表示され、状態が「完了」になればよい
   - git pull を選んだ場合：`pull 先: <GitLab の URL>（origin/main）` と `commit: ...`
   - `[1/N] done ...` の行
6. 生成物の一覧にファイルが出て、開くと本文が表示されることを確かめる

## 8. 定期実行を画面に切り替える

**二重に実行しないよう、先に systemd の夜間タイマーを止める**（設置していなければ不要）。

```bash
sudo systemctl disable --now godoc-nightly.timer
# 確認（何も表示されないか、0 timers と表示されればよい）
systemctl list-timers godoc-nightly.timer --no-pager
```

画面の**実行**タブの「定期実行」で、「毎日実行する」・時刻（手順 1-3 で控えた時刻）・スキル・「実行前に対象フォルダを git pull する」を選んで「保存」。
「次回 YYYY-MM-DD HH:MM」と表示されればよい。翌日、実行履歴に `scheduled-<日時>` が増えていることを確かめる。

## 9. 変更しないもの

| 対象 | 理由 |
|---|---|
| `deploy/nightly.sh`・`godoc-nightly.service` | 使わなくなるが、残しておいてよい（元に戻すときに使う） |
| `.env` のほかの設定 | 追加した `WEB_*`・`CLAUDE_BIN` 以外は変わらない |
| 対象リポジトリ | runner も git pull も書き換えない（pull で最新にするだけ） |

## 10. 元に戻す

```bash
# 1. Web 画面を止める
sudo systemctl disable --now godoc-web.service
# 2. 夜間タイマーを戻す
sudo systemctl enable --now godoc-nightly.timer
# 3. プログラムを前の版に戻す（必要な場合だけ。PR #22 の時点）
cd /opt/Batch_Exec && git checkout 072ecbc
```

`.env` に追加した `WEB_*` の行は、前の版では読まれないので残っていてよい。
画面から登録したコメント追記スキルを `skills/comment-batch/` に置いた場合、前の版では `.env` に `CLAUDE_COMMENT_SKILL_PATH` を書かないと使われない。
最新版に戻すときは `git checkout main && git pull` を実行する。
