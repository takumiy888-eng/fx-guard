"""SQLite: 取引履歴・影のトレード・思考ログ・資産推移・経済指標・画面からの指示"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS trades (
  id TEXT PRIMARY KEY, pair TEXT, side INTEGER, units INTEGER, entry REAL, sl REAL, tp1 REAL, tp2 REAL,
  sl_dist REAL, risk_pct REAL, risk_jpy REAL, open_time TEXT, close_time TEXT, exit_avg REAL,
  pnl_jpy REAL, r REAL, status TEXT, tp1_done INTEGER DEFAULT 0, reason TEXT, features TEXT,
  params_version INTEGER);
CREATE TABLE IF NOT EXISTS shadows (
  id INTEGER PRIMARY KEY AUTOINCREMENT, pair TEXT, side INTEGER, signal_time TEXT, entry_ref REAL,
  sl_dist REAL, reason TEXT, features TEXT, evaluated INTEGER DEFAULT 0, r REAL, outcome TEXT);
CREATE TABLE IF NOT EXISTS log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, msg TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS equity (ts TEXT PRIMARY KEY, equity REAL, peak REAL);
CREATE TABLE IF NOT EXISTS events (ts TEXT, ccy TEXT, title TEXT, impact TEXT, PRIMARY KEY (ts, ccy, title));
CREATE TABLE IF NOT EXISTS commands (
  id INTEGER PRIMARY KEY AUTOINCREMENT, cmd TEXT, arg TEXT, created TEXT, done INTEGER DEFAULT 0, result TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def q(self, sql: str, args=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def x(self, sql: str, args=()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid

    # ---- kv
    def kv_get(self, key: str, default=None):
        r = self.q("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def kv_set(self, key: str, value) -> None:
        self.x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value, ensure_ascii=False, default=str)))

    # ---- log（思考ログ）
    def log(self, kind: str, msg: str, data=None) -> None:
        self.x("INSERT INTO log(ts,kind,msg,data) VALUES(?,?,?,?)",
               (now_iso(), kind, msg, json.dumps(data, ensure_ascii=False, default=str) if data else None))

    def logs(self, limit=200, kind=None) -> list[dict]:
        if kind == "main":  # 見送り以外
            return self.q("SELECT * FROM log WHERE kind!='skip' ORDER BY id DESC LIMIT ?", (limit,))
        if kind:
            return self.q("SELECT * FROM log WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit))
        return self.q("SELECT * FROM log ORDER BY id DESC LIMIT ?", (limit,))

    # ---- trades
    def trade_insert(self, t: dict) -> None:
        cols = list(t)
        vals = [json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v for v in t.values()]
        self.x(f"INSERT OR REPLACE INTO trades({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)

    def trade_update(self, tid: str, **kw) -> None:
        sets = ",".join(f"{k}=?" for k in kw)
        self.x(f"UPDATE trades SET {sets} WHERE id=?", (*kw.values(), tid))

    def trades(self, status=None, limit=1000) -> list[dict]:
        if status:
            rows = self.q("SELECT * FROM trades WHERE status=? ORDER BY open_time DESC LIMIT ?", (status, limit))
        else:
            rows = self.q("SELECT * FROM trades ORDER BY open_time DESC LIMIT ?", (limit,))
        for r in rows:
            r["features"] = json.loads(r["features"]) if r.get("features") else {}
        return rows

    # ---- shadows
    def shadow_insert(self, pair, side, signal_time, entry_ref, sl_dist, reason, feats) -> None:
        self.x("INSERT INTO shadows(pair,side,signal_time,entry_ref,sl_dist,reason,features) VALUES(?,?,?,?,?,?,?)",
               (pair, side, signal_time, entry_ref, sl_dist, reason, json.dumps(feats, ensure_ascii=False)))

    def shadows(self, evaluated=None) -> list[dict]:
        if evaluated is None:
            rows = self.q("SELECT * FROM shadows ORDER BY id")
        else:
            rows = self.q("SELECT * FROM shadows WHERE evaluated=? ORDER BY id", (int(evaluated),))
        for r in rows:
            r["features"] = json.loads(r["features"]) if r.get("features") else {}
        return rows

    # ---- equity
    def equity_snap(self, ts: str, equity: float, peak: float) -> None:
        self.x("INSERT OR REPLACE INTO equity(ts,equity,peak) VALUES(?,?,?)", (ts, equity, peak))

    def equity_series(self, limit=5000) -> list[dict]:
        return self.q("SELECT * FROM (SELECT * FROM equity ORDER BY ts DESC LIMIT ?) ORDER BY ts", (limit,))

    # ---- events
    def events_upsert(self, rows) -> None:
        with self.lock:
            self.conn.executemany("INSERT OR IGNORE INTO events(ts,ccy,title,impact) VALUES(?,?,?,?)", rows)
            self.conn.commit()

    def events_between(self, t0: datetime, t1: datetime) -> list[dict]:
        return self.q("SELECT * FROM events WHERE ts>=? AND ts<=? ORDER BY ts",
                      (t0.astimezone(timezone.utc).isoformat(), t1.astimezone(timezone.utc).isoformat()))

    # ---- commands（画面→Bot）
    def command_add(self, cmd: str, arg: str = "") -> int:
        return self.x("INSERT INTO commands(cmd,arg,created) VALUES(?,?,?)", (cmd, arg, now_iso()))

    def commands_pending(self) -> list[dict]:
        return self.q("SELECT * FROM commands WHERE done=0 ORDER BY id")

    def command_done(self, cid: int, result: str = "") -> None:
        self.x("UPDATE commands SET done=1, result=? WHERE id=?", (result, cid))
