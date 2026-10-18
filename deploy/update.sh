#!/usr/bin/env bash
# 新しいバージョンに更新（設定・取引履歴・学習結果はそのまま）
set -euo pipefail
APP=/opt/fxguard
git config --global --add safe.directory "$APP" 2>/dev/null || true
git -C "$APP" pull --ff-only
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
chown -R fxguard:fxguard "$APP"
systemctl restart fxguard-bot fxguard-web
echo "✅ 更新しました"
