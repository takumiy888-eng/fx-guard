"""バックテストのRの並びを、資金管理ルール（リスク段階・各種ストップ）に通して
「1万円がいくらになるか」を計算する。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .risk import RISK_LEVELS, RULES, LadderState, effective_risk, limits, promotion_check


def replay(trades: list[dict], initial: float = 10000, ladder: bool = True, oos_pf: float = 1.5,
           max_level: int | None = None) -> dict:
    """ladder=False なら常に0.25%（方針A相当）"""
    tr = sorted(trades, key=lambda t: t["entry_time"])
    st = LadderState(equity=initial, peak=initial)
    st.set_level(0)
    top = len(RISK_LEVELS) - 1 if max_level is None else max_level
    curve = []
    events = []
    level_hist = []
    since: list[float] = []
    level_changed_at = None
    week_key = day_key = month_key = None
    day_start = week_start = month_start = initial
    cooldown_until = None
    pnl_by_week: dict = {}
    skipped = 0

    for t in tr:
        et = pd.Timestamp(t["entry_time"]).tz_convert("Asia/Tokyo")
        dk, wk, mk = et.date(), (et - pd.Timedelta(days=et.weekday())).date(), (et.year, et.month)
        if wk != week_key:
            # 週の切り替わり＝週次学習のタイミング：昇格判定・完全停止からの再開
            if week_key is not None and ladder:
                if st.full_halt and oos_pf >= RULES["backtest_min_pf"]:
                    st.full_halt = False
                    st.peak = st.equity
                    st.set_level(0)
                    since, level_changed_at = [], et
                    events.append((et, "完全停止から最低リスクで再開"))
                elif not st.full_halt and st.level < top:
                    weeks = (et - level_changed_at).days / 7 if level_changed_at is not None else 99
                    last4 = sum(v for k, v in pnl_by_week.items() if k >= wk - pd.Timedelta(days=28))
                    ok, why = promotion_check(since, st.level_max_dd, weeks, last4, oos_pf)
                    if ok:
                        st.set_level(st.level + 1)
                        since, level_changed_at = [], et
                        events.append((et, f"{RISK_LEVELS[st.level]}%へ昇格"))
            week_key, week_start = wk, st.equity
        if dk != day_key:
            day_key, day_start = dk, st.equity
        if mk != month_key:
            month_key, month_start = mk, st.equity

        lim = limits(st.level)
        halted = (
            st.full_halt
            or (cooldown_until is not None and et < cooldown_until)
            or (day_start - st.equity) / day_start * 100 >= lim["day"]
            or (week_start - st.equity) / week_start * 100 >= lim["week"]
            or (month_start - st.equity) / month_start * 100 >= lim["month"]
        )
        if halted:
            skipped += 1
            continue

        risk = effective_risk(st.level, st.streak) if ladder else RISK_LEVELS[0]
        pnl = st.equity * risk / 100 * t["r"]
        prev_level = st.level
        msgs = st.on_close(pnl, t["r"])
        if not ladder:
            st.level = 0
        if st.level != prev_level:
            since, level_changed_at = [], et
        else:
            since.append(t["r"])
        if st.streak >= RULES["streak_cooldown"]:
            cooldown_until = et + pd.Timedelta(hours=24)
        for m in msgs:
            events.append((et, m))
        pnl_by_week[wk] = pnl_by_week.get(wk, 0.0) + pnl
        xt = pd.Timestamp(t["exit_time"])
        curve.append((xt, st.equity))
        level_hist.append(RISK_LEVELS[st.level])

    eq = pd.Series([e for _, e in curve], index=pd.DatetimeIndex([x for x, _ in curve]))
    monthly = []
    if len(eq):
        first = tr[0]["entry_time"]
        m_end = eq.tz_convert("Asia/Tokyo").resample("ME").last().ffill()
        prev = initial
        for ts, v in m_end.items():
            monthly.append({"month": ts.strftime("%Y-%m"), "equity": round(float(v)), "ret_pct": (v / prev - 1) * 100})
            prev = v
    peak = np.maximum.accumulate(np.r_[initial, eq.to_numpy()]) if len(eq) else np.array([initial])
    vals = np.r_[initial, eq.to_numpy()] if len(eq) else np.array([initial])
    max_dd = float(((vals - peak) / peak).min() * 100)
    rets = [m["ret_pct"] for m in monthly]
    final = float(vals[-1])
    months = max(len(monthly), 1)
    return {
        "initial": initial,
        "final": final,
        "return_pct": (final / initial - 1) * 100,
        "monthly_avg_pct": ((final / initial) ** (1 / months) - 1) * 100,
        "max_dd_pct": max_dd,
        "best_month": max(rets) if rets else 0,
        "worst_month": min(rets) if rets else 0,
        "positive_months": sum(1 for r in rets if r > 0),
        "months": len(monthly),
        "months_5pct": sum(1 for r in rets if r >= 5),
        "monthly": monthly,
        "curve": [[int(ts.timestamp() * 1000), round(float(v), 1)] for ts, v in eq.items()][:: max(1, len(eq) // 800)],
        "events": [(str(ts)[:16], m) for ts, m in events][-60:],
        "max_level_reached": max(level_hist) if level_hist else RISK_LEVELS[0],
        "skipped_by_stops": skipped,
    }
