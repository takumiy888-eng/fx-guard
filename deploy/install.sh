#!/usr/bin/env bash
# FX Guard を VPS（Ubuntu 22.04 / 24.04）に導入する
#
# VPSのコンソールに1行貼り付けるだけ:
#   curl -fsSL https://raw.githubusercontent.com/<ユーザー名>/<リポジトリ>/main/deploy/install.sh | sudo bash -s -- https://github.com/<ユーザー名>/<リポジトリ>.git
# （コードをVPSに置いてある場合は: sudo bash deploy/install.sh）
#
# やること: Python環境 / 専用ユーザー / 自動起動・自動再起動 / HTTPS（無料証明書）/ ファイアウォール / 見張り番
set -euo pipefail

REPO="${1:-}"
APP=/opt/fxguard
SVC_USER=fxguard
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")/.." 2>/dev/null && pwd || echo /nonexistent)"

if [ "$(id -u)" -ne 0 ]; then echo "sudo を付けて実行してください"; exit 1; fi
say() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }

say "必要なソフトを入れています（数分かかります）"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -q
apt-get install -y -q python3 python3-venv python3-pip git curl ufw gnupg \
  debian-keyring debian-archive-keyring apt-transport-https unattended-upgrades >/dev/null

say "プログラムを配置しています"
if [ -n "$REPO" ]; then
  if [ -d "$APP/.git" ]; then git -C "$APP" pull --ff-only; else git clone --depth 1 "$REPO" "$APP"; fi
elif [ -f "$SRC_DIR/run_bot.py" ]; then
  mkdir -p "$APP"
  if [ "$SRC_DIR" != "$APP" ]; then cp -r "$SRC_DIR"/. "$APP"/; fi
else
  echo "プログラムが見つかりません。GitHubのURLを付けて実行してください"; exit 1
fi

id "$SVC_USER" >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin "$SVC_USER"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
mkdir -p "$APP/data" "$APP/logs"
chown -R "$SVC_USER:$SVC_USER" "$APP"

# メモリが少ないVPS向けにスワップを用意（学習時の一時的なメモリ不足対策）
if [ "$(swapon --show | wc -l)" -eq 0 ] && [ "$(free -m | awk '/Mem:/{print $2}')" -lt 2000 ]; then
  say "スワップ領域（1GB）を作成"
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

PW_FILE="$APP/.initial_password"
if [ ! -f "$APP/secrets.json" ] || ! grep -q password_hash "$APP/secrets.json"; then
  PW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')"
  (cd "$APP" && sudo -u "$SVC_USER" "$APP/.venv/bin/python" -m core.config set-password "$PW" >/dev/null)
  echo "$PW" > "$PW_FILE"; chmod 600 "$PW_FILE"
fi

say "自動起動・自動再起動を設定"
cat > /etc/systemd/system/fxguard-bot.service <<UNIT
[Unit]
Description=FX Guard trading bot
After=network-online.target
Wants=network-online.target

[Service]
User=$SVC_USER
WorkingDirectory=$APP
Environment=FXGUARD_HOME=$APP
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP/.venv/bin/python run_bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/fxguard-web.service <<UNIT
[Unit]
Description=FX Guard dashboard
After=network-online.target

[Service]
User=$SVC_USER
WorkingDirectory=$APP
Environment=FXGUARD_HOME=$APP
Environment=FXGUARD_HTTPS=1
ExecStart=$APP/.venv/bin/python run_web.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# 見張り番: Botの応答が5分以上止まったら再起動してDiscordに通知
cat > /etc/systemd/system/fxguard-watchdog.service <<UNIT
[Unit]
Description=FX Guard watchdog

[Service]
Type=oneshot
Environment=FXGUARD_HOME=$APP
ExecStart=$APP/.venv/bin/python $APP/deploy/watchdog.py
UNIT
cat > /etc/systemd/system/fxguard-watchdog.timer <<UNIT
[Unit]
Description=Run FX Guard watchdog every 5 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now fxguard-bot fxguard-web fxguard-watchdog.timer >/dev/null

say "HTTPS（無料証明書）を設定"
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y -q && apt-get install -y -q caddy >/dev/null
fi
IP="$(curl -fsS -4 https://api.ipify.org || curl -fsS -4 ifconfig.me)"
HOST="${IP//./-}.sslip.io"
cat > /etc/caddy/Caddyfile <<CADDY
$HOST {
    encode gzip
    reverse_proxy 127.0.0.1:8080
}
CADDY
systemctl restart caddy

say "ファイアウォールを設定（SSH・HTTP・HTTPSのみ許可）"
ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

sleep 3
echo
echo "============================================================"
echo "  ✅ FX Guard の導入が完了しました"
echo
echo "  管理画面:   https://$HOST"
if [ -f "$PW_FILE" ]; then
echo "  パスワード: $(cat "$PW_FILE")"
echo "  （ログイン後、設定タブで変更できます。この画面を閉じる前に控えてください）"
fi
echo
echo "  あとは何もしなくて大丈夫です。"
echo "  Botが自動で過去データを取得・学習し（30分ほど）、そのまま運用を始めます"
echo "============================================================"
