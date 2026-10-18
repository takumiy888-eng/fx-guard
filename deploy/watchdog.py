"""Botの応答が止まっていたら再起動して通知（systemd timer から5分ごとに実行）"""
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import requests

HOME = Path(os.environ.get("FXGUARD_HOME", "/opt/fxguard"))


def main():
    db = HOME / "data" / "fxguard.db"
    if not db.exists():
        return
    con = sqlite3.connect(db, timeout=10)
    row = con.execute("SELECT value FROM kv WHERE key='status'").fetchone()
    if not row:
        return
    st = json.loads(row[0])
    state = st.get("state") or ""
    # 過去データ取得・学習中は計算に時間がかかるので長めに待つ
    limit = 2700 if any(k in state for k in ("学習", "準備", "取得")) else 300
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(st["updated"])).total_seconds()
    if age < limit:
        return
    subprocess.run(["systemctl", "restart", "fxguard-bot"], check=False)
    cfg = json.loads((HOME / "config.json").read_text()) if (HOME / "config.json").exists() else {}
    url = cfg.get("discord_webhook")
    if url:
        try:
            requests.post(url, json={"content": f"⚠ Botの応答が{int(age // 60)}分止まっていたため自動で再起動しました"}, timeout=8)
        except requests.RequestException:
            pass


if __name__ == "__main__":
    main()
