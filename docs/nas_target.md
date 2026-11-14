# 対象フォルダをネットワーク上の NAS にする手順

設計書・コメント追記の対象（`--repo`）を、NAS 上のフォルダにする手順。プログラムの変更は不要で、サーバー（GB10）側の設定だけで行う。

```
NAS（nas01:/volume1/repos） ──LAN──> GB10 の /mnt/nas/repos にマウント
                                        └─ runner・Claude Code はここを普通のフォルダとして読む
出力先は GB10 のローカルディスク（/opt/Batch_Exec/out）のまま
```

- runner と Claude Code はサーバー上で動き、ソースを**サーバーのフォルダパス**として読む。`\\nas01\repos` や `smb://...` を直接指定することはできず、クライアント PC で割り当てたネットワークドライブ（`Z:` など）もサーバーからは見えない。そのため、NAS を GB10 にマウントしてから、そのマウント先を対象フォルダに選ぶ
- ソースは社内 LAN を通るだけで、外部には出ない

以下の値は例。実際の環境に合わせて読み替える。

| 例 | 意味 |
|---|---|
| `nas01`（または IP アドレス） | NAS のホスト名 |
| `/volume1/repos`（NFS）・`repos`（SMB） | NAS の共有フォルダ |
| `/mnt/nas/repos` | GB10 側のマウント先 |
| `<実行ユーザー>` | Web 画面（`godoc-web.service`）の `User=` に書いたユーザー |

コマンドは、特に断りがなければ GB10 の端末で実行する（SSH は使えないため、GB10 に直接ログインした端末を使う）。`sudo` が必要な手順がある。

## 0. 方式を決める

| 方式 | 向いている場合 | 必要なパッケージ |
|---|---|---|
| **NFS** | NAS が Linux 系（Synology・QNAP など）で、NFS を有効にできる | `nfs-common` |
| **SMB（CIFS）** | NAS が Windows の共有フォルダ、または NFS を使えない | `cifs-utils` |

どちらでもよいなら、ファイルの所有者を合わせやすい（[6. git pull を使う場合](#6-実行前の-git-pull-を使う場合)を参照）**SMB** を勧める。

**出力先は NAS にしない**（GB10 のローカルディスクのままにする）。理由は次のとおり。

- 二重実行を防ぐロック（`<出力先>/.runner.lock`）は、NFS・SMB では効かないことがある。効かないと、夜間の実行と Web 画面からの実行が同じ出力先に同時に書き込むおそれがある
- 差分スキップ用の manifest は1件ごとに書き換えるため、ネットワーク越しだと遅くなる

生成物を NAS にも置きたい場合は、出力先からコピーする（[8. 生成物を NAS にも置きたい場合](#8-生成物を-nas-にも置きたい場合)）。

## 1. NAS 側の準備

NAS の管理画面で次を行う（操作は NAS の機種によって異なる）。

- **NFS の場合**：共有フォルダの NFS 権限に、GB10 の IP アドレスを追加する。git pull を使わないなら「読み取り専用」でよい
- **SMB の場合**：共有フォルダを読めるアカウントを用意する。git pull を使わないなら読み取り専用のアカウントでよい

GB10 から NAS に届くか確かめる。

```bash
ping -c 3 nas01
showmount -e nas01                 # NFS の場合：共有フォルダの一覧に /volume1/repos が出ればよい（nfs-common の導入後に使える）
```

## 2. GB10 にパッケージを入れ、マウント先を作る

```bash
sudo apt update
sudo apt install -y nfs-common     # NFS の場合
sudo apt install -y cifs-utils     # SMB の場合

sudo mkdir -p /mnt/nas/repos
# マウントしていないときは誰も読めないようにする（下の「NAS が外れたとき」の対策）
sudo chmod 000 /mnt/nas/repos
```

`chmod 000` は、NAS が外れていてマウント先が**空のフォルダに見える**ときの対策。権限がないとエラーで止まるので、「対象 0 件」のまま正常終了したように見えることを防げる。マウント中は NAS 側の権限が使われるため、この設定は影響しない。

## 3. 手動でマウントして確かめる

まず一時的にマウントして、読めることを確かめる。

**NFS の場合**

```bash
sudo mount -t nfs -o ro nas01:/volume1/repos /mnt/nas/repos
```

**SMB の場合**：パスワードをコマンドや fstab に直接書かないよう、認証情報ファイルを作る。

```bash
sudo tee /etc/nas-cred >/dev/null <<'EOF'
username=<NASのアカウント>
password=<NASのパスワード>
EOF
sudo chmod 600 /etc/nas-cred      # root だけが読める

sudo mount -t cifs //nas01/repos /mnt/nas/repos \
  -o ro,credentials=/etc/nas-cred,uid=$(id -u <実行ユーザー>),gid=$(id -g <実行ユーザー>),vers=3.0
```

**確認**（共通）：

```bash
mountpoint /mnt/nas/repos                          # "is a mountpoint" と出ればよい
sudo -u <実行ユーザー> ls /mnt/nas/repos           # 実行ユーザーで中身が見えればよい
sudo -u <実行ユーザー> find /mnt/nas/repos -name '*.go' | head   # 対象のソースが見えるか
```

確かめたら、いったん外す。

```bash
sudo umount /mnt/nas/repos
```

## 4. 起動時に自動でマウントする（/etc/fstab）

`/etc/fstab` に1行追記する。git pull を使う場合は `ro` を `rw` にする。

```bash
sudo cp /etc/fstab /etc/fstab.bak    # 戻せるように控えを取る

# NFS の場合
echo 'nas01:/volume1/repos  /mnt/nas/repos  nfs  ro,_netdev,x-systemd.automount,x-systemd.mount-timeout=30  0  0' | sudo tee -a /etc/fstab

# SMB の場合（<UID>・<GID> は `id -u <実行ユーザー>`・`id -g <実行ユーザー>` の数字）
echo '//nas01/repos  /mnt/nas/repos  cifs  ro,_netdev,credentials=/etc/nas-cred,uid=<UID>,gid=<GID>,vers=3.0,x-systemd.automount,x-systemd.mount-timeout=30  0  0' | sudo tee -a /etc/fstab
```

| オプション | 理由 |
|---|---|
| `_netdev` | ネットワークの準備ができてからマウントする |
| `x-systemd.automount` | 最初にアクセスしたときにマウントする。起動時に NAS が止まっていてもサーバーの起動が止まらず、後から NAS が戻れば自動でつながる |
| `x-systemd.mount-timeout=30` | NAS が応答しないとき、30 秒でエラーにする（いつまでも待たない） |

反映して確かめる。

```bash
sudo systemctl daemon-reload
sudo mount -a                                   # 書き間違いがあればここでエラーになる
ls /mnt/nas/repos >/dev/null && mountpoint /mnt/nas/repos   # アクセスするとマウントされ、"is a mountpoint" と出る
```

`mount -a` でエラーが出たら、追記した行を直す（または `sudo cp /etc/fstab.bak /etc/fstab` で戻す）。fstab が壊れたまま再起動すると、サーバーが起動しなくなることがある。

## 5. サービスがマウントを待つようにする

Web 画面（と、使っている場合は夜間タイマー）が、NAS のマウントを前提に起動するよう設定する。

```bash
sudo systemctl edit godoc-web.service
```

開いたエディタに次を書いて保存する。

```ini
[Unit]
RequiresMountsFor=/mnt/nas/repos
```

夜間タイマー（`godoc-nightly.service`）を使っている場合は、同じ設定を入れる。あわせて、`.env` の対象（`GODOC_REPO=/mnt/nas/repos/myapp`）を NAS 上のパスにする（設定は `.env` に一本化しているため、ユニットには `Environment=` を書かない）。

```bash
sudo systemctl edit godoc-nightly.service
```

```ini
[Unit]
RequiresMountsFor=/mnt/nas/repos

```

反映する。

```bash
sudo systemctl daemon-reload
sudo systemctl restart godoc-web.service
```

## 6. 実行前の git pull を使う場合

使わない場合（NAS 側のソースを別の手段で更新している場合）は、この手順を飛ばす。画面の「実行前に対象フォルダを git pull する」はオフにしておく。

git pull は NAS 上のリポジトリに書き込むため、次が必要になる。

1. 4. の fstab で `ro` ではなく `rw` にしてマウントする。NAS 側の権限も書き込み可にする
2. git は、リポジトリの所有者が実行ユーザーと違うと `detected dubious ownership` で止まる
   - **SMB**：fstab の `uid=`・`gid=` で実行ユーザーを指定していれば、所有者が合うため設定は不要
   - **NFS**：NAS 側のファイルの所有者 UID が、GB10 の実行ユーザーの UID と違う場合は、そのリポジトリを信頼するよう登録する

     ```bash
     sudo -u <実行ユーザー> git config --global --add safe.directory /mnt/nas/repos/myapp
     ```
3. GitLab の認証は、ローカルのリポジトリと同じ設定で使える（[migration_web_ui.md](pr23-25/migration_web_ui.md) の「4. 実行前の git pull を使う場合：GitLab の認証」）

**確認**：

```bash
sudo -u <実行ユーザー> git -C /mnt/nas/repos/myapp pull --ff-only   # "Already up to date." などが出ればよい
```

## 7. Web 画面で対象フォルダに選ぶ

**7-1. 選べる範囲にマウント先を足す**

`/opt/Batch_Exec/.env` の `WEB_BROWSE_ROOTS` に、マウント先を追加する。出力先の起点（ローカル）は残しておく。

```
WEB_BROWSE_ROOTS=/mnt/nas/repos,/opt/Batch_Exec/out
```

```bash
sudo systemctl restart godoc-web.service
```

**7-2. 画面で選ぶ**

1. ブラウザで Web 画面を開き、管理者でログインする
2. 「実行」タブの「フォルダ」で、**対象フォルダ**の「選択…」から `/mnt/nas/repos/<リポジトリ>` を選ぶ
3. **出力先**は、ローカルの `/opt/Batch_Exec/out`（またはその下）のままにする

**7-3. 少ない件数で試す**

1. 「対象の設定」タブの「対象ファイルを確認」で、NAS 上のファイルが一覧に出ることを確かめる
2. 「実行」タブで「件数の上限」を `3` にして実行する
3. ログに `対象=3 件` と出て、セッション欄で処理が進み、生成物が出力先にできればよい

## 8. 生成物を NAS にも置きたい場合

出力先はローカルのまま、実行が終わったあとに NAS へコピーする。たとえば、毎日の定期実行の後（例：朝 6 時）にコピーするなら、実行ユーザーの crontab に追加する（NAS 側に書き込み用の共有・マウントが別途必要）。

```bash
sudo -u <実行ユーザー> crontab -e
# 毎朝 6:00 に、生成物（ログ・管理用ファイルを除く）を NAS へコピーする
0 6 * * * rsync -a --exclude '_runs/' --exclude '.*' /opt/Batch_Exec/out/ /mnt/nas/docs/
```

## 9. 運用上の注意

| 項目 | 内容 |
|---|---|
| NAS が止まっているとき | 実行は「フォルダを開く権限がありません」や、マウントの待ち時間切れのエラーで失敗する（`chmod 000` と `x-systemd.automount` の効果）。NAS が戻れば、次の実行から続きが処理される |
| 速度 | 対象ファイルの探索と、Claude Code の Read・Grep・Glob がネットワーク越しになる。通常は LLM の処理時間の方がずっと長いため影響は小さい。遅い場合は、NAS のソースを GB10 のローカルに `rsync` で定期的にコピーし、そのコピーを対象にする |
| 同時セッション数を増やすとき | 10 並列では NAS への読み込みが重なる。遅くなったら同時セッション数を下げて、経過時間を比べる（[performance_troubleshooting.md](performance_troubleshooting.md)） |
| 機密性 | NFS の通信は暗号化されない。必要なら SMB3 の暗号化（fstab のオプションに `seal` を追加）を使う |

## 10. うまくいかないとき

| 症状 | 原因と対処 |
|---|---|
| フォルダ選択ダイアログに `/mnt/nas/repos` が出ない | `WEB_BROWSE_ROOTS` に追加していない、または再起動していない（7-1） |
| 「フォルダを開く権限がありません」 | マウントされていない（`mountpoint /mnt/nas/repos` で確かめる）。マウントされているなら、NAS 側で実行ユーザーに読み取り権限がない（SMB は fstab の `uid=`・`gid=` を確認） |
| `mount error(13): Permission denied`（SMB） | `/etc/nas-cred` のアカウント・パスワードが違う。またはドメインが必要（`domain=<ドメイン>` を追記） |
| `mount.nfs: access denied by server` | NAS の NFS 権限に GB10 の IP アドレスが入っていない（1.） |
| `mount error(95)`（SMB） | SMB のバージョンが合わない。`vers=3.0` を `vers=2.1` などに変える |
| 実行が「対象 0 件」で終わる | マウント先が空に見えている（`chmod 000` をしていない状態で、NAS が外れている）。または対象の設定（include）が NAS 上のフォルダ構成に合っていない（「対象ファイルを確認」で確かめる） |
| git pull で `detected dubious ownership` | 6. の `safe.directory` を設定する（SMB なら `uid=`・`gid=` を確認） |
| git pull で `Read-only file system` | `ro` でマウントしている。git pull を使うなら `rw` にする（6.） |

## 11. 元に戻す

```bash
# 1. Web 画面で、対象フォルダをローカルのフォルダに選び直す
# 2. サービスの追加設定を消す（godoc-nightly.service も同様。GODOC_REPO は元に戻す）
sudo systemctl revert godoc-web.service
# 3. マウントを外し、fstab を元に戻す
sudo umount /mnt/nas/repos
sudo cp /etc/fstab.bak /etc/fstab
sudo systemctl daemon-reload
# 4. .env の WEB_BROWSE_ROOTS から /mnt/nas/repos を消して再起動
sudo systemctl restart godoc-web.service
```

`systemctl revert` は `systemctl edit` で加えた設定をすべて消す。ほかにも追加設定がある場合は、`systemctl edit` で該当の行だけを消す。
