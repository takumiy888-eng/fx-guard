"""設定（config.json）と秘密情報（secrets.json：画面パスワード）"""
from __future__ import annotations

import copy
import json
import os
import secrets
import threading
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

DEFAULT_CONFIG = {
    "data_source": "GMOコイン 外国為替FX 公開API（登録不要）",
    "pairs": ["USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY", "EUR_USD", "GBP_USD"],
    "virtual_capital": 10000,        # 仮想資金（円）
    "monthly_target_pct": 5.0,       # 画面に表示する月間目標
    "discord_webhook": "",
    "poll_seconds": 15,
    "max_spread_pips": 2.0,
    "max_leverage": 10,
    "learn_weekday": 5,              # 土曜
    "learn_hour_jst": 10,            # 10時に週次学習
    "history_years": 2.5,            # 学習に使う過去データ
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class Store:
    def __init__(self, base: Path):
        self.base = Path(base)
        self.cfg_path = self.base / "config.json"
        self.sec_path = self.base / "secrets.json"
        self.lock = threading.Lock()

    def load(self) -> dict:
        data = json.loads(self.cfg_path.read_text(encoding="utf-8")) if self.cfg_path.exists() else {}
        return deep_merge(DEFAULT_CONFIG, data)

    def save(self, cfg: dict) -> dict:
        merged = deep_merge(DEFAULT_CONFIG, cfg)
        with self.lock:
            tmp = self.cfg_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.cfg_path)
        return merged

    # ---- secrets
    def _sec(self) -> dict:
        return json.loads(self.sec_path.read_text(encoding="utf-8")) if self.sec_path.exists() else {}

    def _sec_save(self, d: dict) -> None:
        with self.lock:
            self.sec_path.write_text(json.dumps(d), encoding="utf-8")
            try:
                os.chmod(self.sec_path, 0o600)
            except OSError:
                pass

    def flask_secret(self) -> str:
        d = self._sec()
        if not d.get("flask_secret"):
            d["flask_secret"] = secrets.token_hex(32)
            self._sec_save(d)
        return d["flask_secret"]

    def set_password(self, pw: str) -> None:
        d = self._sec()
        d["password_hash"] = generate_password_hash(pw)
        self._sec_save(d)

    def check_password(self, pw: str) -> bool:
        h = self._sec().get("password_hash")
        return bool(h) and check_password_hash(h, pw)

    def has_password(self) -> bool:
        return bool(self._sec().get("password_hash"))

    def configured(self) -> bool:
        """登録不要なので常に運用可能"""
        return True


if __name__ == "__main__":  # 導入スクリプトから: python -m core.config set-password <pw>
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "set-password":
        Store(Path(__file__).resolve().parents[1]).set_password(sys.argv[2])
        print("password set")
