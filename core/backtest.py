"""バックテスト（成績はR＝「1回の損切り額」を1とした単位で計算）

R単位にすることで、資金量やリスク%に関係なく「ルールそのものの実力」を測れる。
資金1万円でいくらになるかは replay.py が、このRの並びを資金管理ルールに通して計算する。

約定ルールは実運用と同じ:
- シグナルは足の確定時 → 入らない理由のチェック → 次の足の始値で成行
- 仲値 ± スプレッド/2 で Bid/Ask、成行・逆指値にはスリッページを加算
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .gates import DEFAULT_GATES, check_entry, event_exit_due, weekend_close_due
from .position import Position
from .strategy import BAR, features, params_with_defaults, prepare

DEFAULT_SPREAD = {"USD_JPY": 0.4, "EUR_JPY": 0.6, "GBP_JPY": 1.1, "AUD_JPY": 0.7,
                  "EUR_USD": 0.4, "GBP_USD": 0.9, "AUD_USD": 0.6}


def pip(pair: str) -> float:
    return 0.01 if pair.endswith("JPY") else 0.0001


def run(data: dict[str, pd.DataFrame], params: dict | None = None, gates: dict | None = None,
        events=None, blocks: list[dict] | None = None, spread: dict | None = None,
        slippage_pips: float = 0.2, prepared: dict | None = None) -> dict:
    p = params_with_defaults(params)
    g = {**DEFAULT_GATES, **(gates or {})}
    blocks = blocks or []
    actual = spread is None          # spread を指定しなければ、データにある実際のスプレッドを使う
    spread = {**DEFAULT_SPREAD, **(spread or {})}
    max_hold = int(math.ceil(p["max_hold_hours"] * 3600 / BAR.total_seconds()))

    prep = prepared or {pair: prepare(df, p) for pair, df in data.items()}
    rows = {}
    for pair, s in prep.items():
        src = data.get(pair)
        if actual and src is not None and "spread" in src.columns:
            sp = src["spread"].reindex(s.index).to_numpy(dtype=float)
        else:
            sp = np.full(len(s), np.nan)
        rows[pair] = dict(zip(s.index, zip(s["open"], s["high"], s["low"], s["close"], s["atr"],
                                           s["signal"], s["vol_ratio"], s["adx"], sp)))
    times = sorted(set().union(*[r.keys() for r in rows.values()])) if rows else []

    open_pos: dict[str, Position] = {}
    pending: dict[str, tuple] = {}
    trades: list[Position] = []
    skipped: dict[str, int] = {}
    signals = 0

    for t in times:
        ct = t + BAR
        wk_close = weekend_close_due(ct)
        for pair, R in rows.items():
            r = R.get(t)
            if r is None:
                continue
            o, h, l, c, a, sg, vr, ax, spb = r
            hs = spb / 2 if np.isfinite(spb) else spread.get(pair, 1.0) * pip(pair) / 2
            slip = slippage_pips * pip(pair)

            # 1) 前の足で決めたエントリーを始値で約定
            if pair in pending:
                d, sig_atr, feats, sig_t = pending.pop(pair)
                entry = o + d * (hs + slip)
                pos = Position(pair, d, entry, sig_atr * p["sl_atr_mult"], p["tp1_r"], p["tp2_r"],
                               t, max_hold, feats)
                open_pos[pair] = pos

            # 2) 保有中の決済判定
            pos = open_pos.get(pair)
            if pos is not None:
                done = pos.step(o, h, l, c, hs, slip, ct)
                if not done and wk_close:
                    pos.close(c - pos.d * hs, "週末決済", ct)
                    done = True
                if not done and not pos.risk_free and event_exit_due(pair, ct, g, events):
                    pos.close(c - pos.d * hs, "指標前に撤退", ct)
                    done = True
                if done:
                    trades.append(open_pos.pop(pair))

            # 3) この足の確定でシグナル判定
            if sg != 0 and not np.isnan(a):
                signals += 1
                feats = features(pair, int(sg), ct, vr, ax)
                current = [(pp, x.d) for pp, x in open_pos.items()] + [(pp, v[0]) for pp, v in pending.items()]
                why = check_entry(pair, int(sg), ct, vr, feats, p, g, events, current, blocks)
                if why is None and wk_close:
                    why = "週末・週明け"
                if why:
                    skipped[why] = skipped.get(why, 0) + 1
                    continue
                pending[pair] = (int(sg), float(a), feats, ct)

    for pair, pos in list(open_pos.items()):
        last = max(rows[pair])
        c = rows[pair][last][3]
        pos.close(c - pos.d * spread.get(pair, 1.0) * pip(pair) / 2, "期末", last + BAR)
        trades.append(pos)

    out = [
        {
            "pair": x.pair,
            "side": "買い" if x.d == 1 else "売り",
            "entry_time": x.entry_time,
            "exit_time": x.exit_time,
            "r": float(x.r),
            "reason": x.reason,
            "tp1": x.tp1_done,
            "bars": x.bars,
            "features": x.features,
        }
        for x in trades
    ]
    out.sort(key=lambda z: z["exit_time"])
    return {"trades": out, "signals": signals, "skipped": skipped,
            "start": times[0] if times else None, "end": (times[-1] + BAR) if times else None}


def stats(trades: list[dict]) -> dict:
    """R単位の成績"""
    rs = np.array([t["r"] for t in trades], dtype=float)
    n = len(rs)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "pf": 0.0, "avg_r": 0.0, "total_r": 0.0,
                "max_dd_r": 0.0, "score": 0.0, "max_losing_streak": 0}
    gw, gl = rs[rs > 0].sum(), -rs[rs < 0].sum()
    cum = np.cumsum(rs)
    dd = float((np.maximum.accumulate(np.r_[0, cum]) - np.r_[0, cum]).max())
    streak = worst = 0
    for x in rs:
        streak = streak + 1 if x < 0 else 0
        worst = max(worst, streak)
    total = float(rs.sum())
    return {
        "n": n,
        "win_rate": float((rs > 0).mean() * 100),
        "pf": float(gw / gl) if gl > 0 else (99.0 if gw > 0 else 0.0),
        "avg_r": float(rs.mean()),
        "total_r": total,
        "max_dd_r": dd,
        # 「どれだけ増えたか ÷ 最大でどれだけ減ったか」— 負けにくさを重視した評価値
        "score": total / max(dd, 3.0),
        "max_losing_streak": worst,
    }


def split(trades: list[dict], start, end, oos_ratio: float = 0.25):
    cut = pd.Timestamp(start) + (pd.Timestamp(end) - pd.Timestamp(start)) * (1 - oos_ratio)
    return [t for t in trades if t["entry_time"] < cut], [t for t in trades if t["entry_time"] >= cut], cut
