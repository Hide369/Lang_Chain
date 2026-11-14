# 設置フォルダの移行手順（/opt/Lang_Chain → /opt/Batch_Exec）

リポジトリ名を `Lang_Chain` から `Batch_Exec` に変更したことに合わせ、サーバー上の設置フォルダも `/opt/Batch_Exec` に移す。
README・docs・systemd のユニットは、すでに `/opt/Batch_Exec` を前提に書き換えてある。

## 0. 事前確認

実行中のバッチがないこと（Web 画面の実行履歴、または `systemctl status godoc-nightly.service`）を確認してから行う。

## 1. サービスを止める

```bash
sudo systemctl stop godoc-web.service godoc-nightly.timer
```

## 2. フォルダを移し、リモートURLを新しい名前にする

```bash
sudo mv /opt/Lang_Chain /opt/Batch_Exec
cd /opt/Batch_Exec && git remote set-url origin https://github.com/Hide369/Batch_Exec.git
git pull
```

GitHub は旧URLから自動で転送するが、転送は将来同名のリポジトリが作られると切れるため、URL は必ず更新する。

## 3. 仮想環境を作り直す

`.venv` の中のスクリプトは作成時の絶対パス（`/opt/Lang_Chain/.venv/...`）を埋め込んでいるため、フォルダを移すと `pip` などが動かなくなる。作り直す。

```bash
cd /opt/Batch_Exec && rm -rf .venv && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## 4. `.env` と `targets.toml` のパスを書き換える

```bash
cd /opt/Batch_Exec && sed -i 's|/opt/Lang_Chain|/opt/Batch_Exec|g' .env
[ -f targets.toml ] && sed -i 's|/opt/Lang_Chain|/opt/Batch_Exec|g' targets.toml
grep -rn 'Lang_Chain' .env targets.toml 2>/dev/null   # 何も表示されなければよい
```

出力先（`out/`）は一緒に移動しているので、生成済みの記録（`.manifest.json`）はそのまま引き継がれる。

## 5. systemd のユニットを入れ直す

ユニットの `REPLACE_WITH_USER` は、今まで使っていた実行ユーザーに置き換える。

```bash
cd /opt/Batch_Exec
sudo cp deploy/systemd/godoc-web.service deploy/systemd/godoc-nightly.service deploy/systemd/godoc-nightly.timer /etc/systemd/system/
sudo sed -i 's|REPLACE_WITH_USER|<実行ユーザー>|' /etc/systemd/system/godoc-web.service /etc/systemd/system/godoc-nightly.service
sudo systemctl daemon-reload
sudo systemctl start godoc-web.service godoc-nightly.timer
```

## 6. 動作確認

```bash
cd /opt/Batch_Exec && .venv/bin/python -m pytest -q          # 最後に「N passed」と出ればよい
systemctl status godoc-web.service --no-pager                # active (running) であればよい
grep -rn 'Lang_Chain' /etc/systemd/system/godoc-*            # 何も表示されなければよい
```

Web 画面を開き、フォルダ選択ダイアログで `/opt/Batch_Exec/out` を選べることを確認する。

## 元に戻す方法

```bash
sudo systemctl stop godoc-web.service godoc-nightly.timer
sudo mv /opt/Batch_Exec /opt/Lang_Chain
cd /opt/Lang_Chain && sed -i 's|/opt/Batch_Exec|/opt/Lang_Chain|g' .env && rm -rf .venv && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

その後、`/etc/systemd/system/godoc-*` の `/opt/Batch_Exec` を `/opt/Lang_Chain` に戻して `sudo systemctl daemon-reload` し、サービスを起動する。
