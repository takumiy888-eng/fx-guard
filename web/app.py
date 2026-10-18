"""管理画面（スマホ対応・パスワード保護）。Botとは別プロセスで動き、DB経由で状態を読む・指示を出す。"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, render_template, request, session

from core.config import Store
from core.db import DB
from core.gmo import GMOPublic, MarketError
from core.risk import RISK_LEVELS, RULES
from core.strategy import FEATURE_LABELS, PARAM_LABELS

BASE = Path(os.environ.get("FXGUARD_HOME", Path(__file__).resolve().parents[1]))
store = Store(BASE)
db = DB(BASE / "data" / "fxguard.db")

app = Flask(__name__)
app.json.sort_keys = False
app.secret_key = store.flask_secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("FXGUARD_HTTPS") == "1",
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)

_fails: dict[str, list[float]] = {}
LOCAL = os.environ.get("FXGUARD_LOCAL") == "1"   # PC版（run_local.py）


def is_local_request() -> bool:
    """PC版で、そのPC自身のブラウザからのアクセスならパスワード不要"""
    return LOCAL and request.remote_addr in ("127.0.0.1", "::1")


def ok(**kw):
    return jsonify({"ok": True, **kw})


def err(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code


def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not session.get("auth") and not is_local_request():
            return err("ログインが必要です", 401)
        return f(*a, **kw)
    return w


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/login")
def login():
    ip = (request.remote_addr or "?") if LOCAL else \
        request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 600]
    if len(recent) >= 5:
        return err("試行回数が多すぎます。10分後にもう一度お試しください", 429)
    pw = (request.get_json(force=True) or {}).get("password", "")
    if store.check_password(pw):
        session.permanent = True
        session["auth"] = True
        _fails.pop(ip, None)
        return ok()
    recent.append(now)
    _fails[ip] = recent
    return err("パスワードが違います", 401)


@app.post("/api/logout")
def logout():
    session.clear()
    return ok()


@app.get("/api/me")
def me():
    return ok(auth=bool(session.get("auth")) or is_local_request(), local=is_local_request())


@app.get("/api/local_info")
@login_required
def local_info():
    """PC版：同じWi-Fiのスマホから見るためのURLとパスワード（そのPCからのアクセスにだけ返す）"""
    if not is_local_request():
        return ok(available=False)
    return ok(available=True, url=os.environ.get("FXGUARD_LAN_URL", ""),
              password=store._sec().get("phone_password", ""))


@app.get("/api/overview")
@login_required
def overview():
    status = db.kv_get("status") or {}
    alive = False
    if status.get("updated"):
        alive = (datetime.now(timezone.utc) - datetime.fromisoformat(status["updated"])).total_seconds() < 180
    learning = db.kv_get("learning") or {}
    rep = (learning.get("replay") or {})
    return ok(
        status=status, alive=alive, configured=store.configured(), paused=bool(db.kv_get("paused")),
        equity=[[r["ts"], r["equity"]] for r in db.equity_series(3000)],
        logs=db.logs(15, "main"),
        learned_at=learning.get("ran_at"),
        backtest={k: {x: rep[k][x] for x in ("final", "monthly_avg_pct", "max_dd_pct", "months", "months_5pct",
                                              "positive_months", "worst_month")}
                  for k in ("fixed", "ladder", "last12m") if k in rep},
        rules={"levels": RISK_LEVELS, **RULES},
        started_at=db.kv_get("virtual_started_at"),
    )


@app.get("/api/trades")
@login_required
def trades():
    rows = db.trades(limit=500)
    return ok(trades=rows)


@app.get("/api/logs")
@login_required
def logs():
    kind = request.args.get("kind") or None
    return ok(logs=db.logs(int(request.args.get("limit", 300)), kind))


@app.get("/api/learning")
@login_required
def learning():
    return ok(learning=db.kv_get("learning"), history=db.kv_get("params_history", []),
              blocks=db.kv_get("blocks", []), param_labels=PARAM_LABELS, feature_labels=FEATURE_LABELS)


@app.get("/api/settings")
@login_required
def get_settings():
    return ok(config=store.load())


@app.post("/api/settings")
@login_required
def save_settings():
    body = request.get_json(force=True) or {}
    cfg = store.load()
    for k in ("discord_webhook",):
        if k in body:
            cfg[k] = str(body[k]).strip()
    if "pairs" in body:
        pairs = [re.sub(r"^([A-Z]{3})[-/_]?([A-Z]{3})$", r"\1_\2", p.strip().upper())
                 for p in re.split(r"[,\s]+", body["pairs"]) if p.strip()]
        bad = [p for p in pairs if not re.fullmatch(r"[A-Z]{3}_[A-Z]{3}", p)]
        if bad or not pairs:
            return err(f"通貨ペアの形式が不正です: {', '.join(bad) or '空'}")
        cfg["pairs"] = pairs
    for k in ("virtual_capital", "monthly_target_pct"):
        if k in body:
            v = float(body[k])
            if v <= 0:
                return err("正の数を入れてください")
            cfg[k] = v
    store.save(cfg)
    return ok(config=cfg)


@app.post("/api/test_connection")
@login_required
def test_connection():
    """GMOコインの公開レートに届くか確認（登録不要）"""
    cfg = store.load()
    try:
        tk = GMOPublic(min_interval=0, retries=2).ticker()
    except MarketError as e:
        return err(e)
    rows = [{"pair": p, "bid": tk[p]["bid"], "ask": tk[p]["ask"], "status": tk[p]["status"]}
            for p in cfg["pairs"] if p in tk]
    return ok(prices=rows, missing=[p for p in cfg["pairs"] if p not in tk])


@app.post("/api/command")
@login_required
def command():
    cmd = (request.get_json(force=True) or {}).get("cmd")
    if cmd not in ("pause", "resume", "close_all", "learn_now", "reset_virtual"):
        return err("不明な指示です")
    db.command_add(cmd)
    return ok()


@app.post("/api/password")
@login_required
def password():
    b = request.get_json(force=True) or {}
    if not store.check_password(b.get("current", "")):
        return err("現在のパスワードが違います")
    new = b.get("new", "")
    if len(new) < 8:
        return err("8文字以上にしてください")
    store.set_password(new)
    return ok()
