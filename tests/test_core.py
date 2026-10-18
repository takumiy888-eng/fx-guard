import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import backtest as B  # noqa: E402
from core.db import DB  # noqa: E402
from core.events import EventIndex, LiveCalendar, historical_events  # noqa: E402
from core.gates import DEFAULT_GATES, check_entry, weekend_close_due  # noqa: E402
from core.learner import Learner  # noqa: E402
from core.position import Position  # noqa: E402
from core.replay import replay  # noqa: E402
from core.risk import RISK_LEVELS, LadderState, calc_units  # noqa: E402
from core.strategy import DEFAULT_PARAMS, PARAM_GRID  # noqa: E402
from tests.synth import make  # noqa: E402

T = pd.Timestamp("2024-03-05 06:00", tz="UTC")  # 火曜15時JST


# ---------------------------------------------------------------- 決済ルール
def test_position_half_profit_then_breakeven():
    p = Position("USD_JPY", 1, 150.0, 0.5, 1.0, 2.5, T)
    assert not p.step(150.0, 150.6, 150.05, 150.5, 0, 0)      # +1R到達 → 半分利確・建値へ
    assert p.tp1_done and p.stop == 150.0
    assert p.step(150.4, 150.45, 149.95, 150.0, 0, 0)          # 建値で残り決済
    assert p.reason == "建値決済" and p.r == pytest.approx(0.5)


def test_position_full_win_and_loss():
    p = Position("USD_JPY", -1, 150.0, 0.5, 1.0, 2.5, T)
    p.step(149.9, 149.95, 149.4, 149.5, 0, 0)
    p.step(149.5, 149.6, 148.7, 148.8, 0, 0)
    assert p.reason == "利確" and p.r == pytest.approx(1.75)
    q = Position("USD_JPY", 1, 150.0, 0.5, 1.0, 2.5, T)
    q.step(150.0, 150.2, 149.4, 149.6, 0, 0)
    assert q.reason == "損切り" and q.r == pytest.approx(-1.0)


def test_position_tp1_bar_that_dipped_below_entry_exits_at_breakeven():
    p = Position("USD_JPY", 1, 150.0, 0.5, 1.0, 2.5, T)
    assert p.step(150.0, 150.6, 149.9, 150.5, 0, 0)            # 同じ足で建値より下も付けた → 建値決済扱い
    assert p.reason == "建値決済" and p.r == pytest.approx(0.5)


def test_position_same_bar_assumes_worst():
    # 同じ足で +1R も 損切りも届く → 損切りが先
    p = Position("USD_JPY", 1, 150.0, 0.5, 1.0, 2.5, T)
    p.step(150.0, 150.6, 149.4, 150.0, 0, 0)
    assert p.reason == "損切り" and p.r == pytest.approx(-1.0)


def test_position_gap_through_stop():
    p = Position("USD_JPY", 1, 150.0, 0.5, 1.0, 2.5, T)
    p.step(149.0, 149.2, 148.9, 149.1, 0, 0)
    assert p.reason.startswith("損切り(窓)") and p.r == pytest.approx(-2.0)


# ---------------------------------------------------------------- 入らない理由
def _check(pair="USD_JPY", d=1, t=T, vol=1.0, open_pos=(), events=None, blocks=()):
    feats = {"pair": pair, "hour": "15-18時"}
    return check_entry(pair, d, t, vol, feats, DEFAULT_PARAMS, DEFAULT_GATES, events, list(open_pos), list(blocks))


def test_gates():
    assert _check() is None
    assert _check(t=pd.Timestamp("2024-03-09 03:00", tz="UTC")) == "週末・週明け"      # 土曜
    assert _check(t=pd.Timestamp("2024-03-04 00:00", tz="UTC")) == "週末・週明け"      # 月曜9時JST
    assert _check(t=pd.Timestamp("2024-03-05 19:00", tz="UTC")) == "時間帯外"          # 4時JST
    assert _check(vol=2.5) == "ボラ異常"
    assert _check(open_pos=[("EUR_JPY", 1)]) == "通貨の偏り"     # どちらも円売り
    assert _check(open_pos=[("EUR_JPY", -1)]) is None            # 円は相殺、EURは別
    assert _check(open_pos=[("USD_JPY", -1)]) == "同ペア保有中"
    ev = EventIndex([(T + pd.Timedelta(minutes=40), "USD", "CPI")])
    assert _check(events=ev) == "経済指標の前後"
    assert _check(pair="EUR_GBP", events=ev) is None
    assert _check(blocks=[{"feature": "hour", "value": "15-18時"}]) == "学習した回避条件"
    assert weekend_close_due(pd.Timestamp("2024-03-08 19:30", tz="UTC"))       # 土曜4:30JST
    assert not weekend_close_due(pd.Timestamp("2024-03-08 14:00", tz="UTC"))   # 金曜23時JST


def test_historical_events_are_utc_and_cover_fomc_nfp():
    ev = historical_events(datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 12, 31, tzinfo=timezone.utc))
    fomc = [e for e in ev if e[2].startswith("FOMC")]
    nfp = [e for e in ev if e[2] == "米雇用統計"]
    assert len(fomc) == 8 and len(nfp) == 12
    assert fomc[0][0] == pd.Timestamp("2024-01-31 19:00", tz="UTC")   # 14:00 ET（冬時間）
    assert nfp[6][0] == pd.Timestamp("2024-07-05 12:30", tz="UTC")     # 8:30 ET（夏時間）


def test_live_calendar_parses_feed():
    db = DB(Path(tempfile.mkdtemp()) / "t.db")
    cal = LiveCalendar(db)
    feed = [{"title": "CPI y/y", "country": "USD", "date": "2026-10-14T08:30:00-04:00", "impact": "High"},
            {"title": "x", "country": "EUR", "date": "2026-10-14T05:00:00-04:00", "impact": "Low"}]
    assert cal.update(datetime(2026, 10, 12, tzinfo=timezone.utc), fetch=lambda: feed) is None
    rows = db.events_between(datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 10, 30, tzinfo=timezone.utc))
    assert [(r["ccy"], r["ts"][:16]) for r in rows] == [("USD", "2026-10-14T12:30")]
    # 3時間以内は再取得しない・失敗しても落ちない
    assert cal.update(datetime(2026, 10, 12, 1, tzinfo=timezone.utc), fetch=lambda: 1 / 0) is None
    msg = cal.update(datetime(2026, 10, 12, 5, tzinfo=timezone.utc), fetch=lambda: 1 / 0)
    assert msg and "失敗" in msg


# ---------------------------------------------------------------- バックテスト
def test_no_lookahead_on_random_walk():
    avg = []
    for s in range(6):
        data = {"USD_JPY": make(n=12000, seed=700 + s, drift_regimes=False),
                "EUR_USD": make(n=12000, seed=800 + s, start=1.08, drift_regimes=False)}
        res = B.run(data, spread={k: 0 for k in B.DEFAULT_SPREAD}, slippage_pips=0)
        avg.append(B.stats(res["trades"])["avg_r"])
    # 先読みがあれば大きくプラスになる。不利側に寄せた約定仮定のため、わずかにマイナスが正常
    assert -0.15 < np.mean(avg) < 0.05


def test_no_same_direction_currency_exposure():
    data = {p: make(n=8000, seed=i, start=s) for i, (p, s) in
            enumerate([("USD_JPY", 150), ("EUR_JPY", 160), ("GBP_JPY", 190)])}
    tr = B.run(data)["trades"]
    for a in tr:
        for b in tr:
            if a is b or a["side"] != b["side"]:
                continue
            overlap = a["entry_time"] < b["exit_time"] and b["entry_time"] < a["exit_time"]
            assert not overlap, "円を同じ向きに二重で持っている"


# ---------------------------------------------------------------- 資金管理
def test_units_for_10k_yen():
    # 1万円・0.25%=25円、損切り15銭 → 166通貨
    assert calc_units(10000, 0.25, 0.15, 1.0, 150, 10) == 166
    # EUR_USD 損切り0.0012ドル、USDJPY150 → 25/(0.0012*150)=138
    assert calc_units(10000, 0.25, 0.0012, 150, 1.08, 10) == 138


def test_ladder_demotes_immediately():
    st = LadderState(equity=10000, peak=10000)
    st.set_level(3)
    for _ in range(4):
        st.on_close(-50, -1.0)
    assert st.level < 3 and st.streak == 4
    st.set_level(4)
    st.on_close(-500, -1.0)  # 最高値から-4%以上
    assert st.level == 0


def test_replay_losses_are_capped():
    t0 = pd.Timestamp("2024-01-08 06:00", tz="UTC")
    trades = [{"entry_time": t0 + pd.Timedelta(hours=7 * i), "exit_time": t0 + pd.Timedelta(hours=7 * i + 3),
               "r": -1.0} for i in range(300)]
    r = replay(trades, 10000, ladder=True, oos_pf=0.8)
    # 全敗でも -8% 付近で完全停止し、再検証に通らないので再開しない
    assert -9.5 < r["max_dd_pct"] < -7.0
    assert r["skipped_by_stops"] > 200


# ---------------------------------------------------------------- 学習
def test_learner_blocks_clearly_losing_condition_only():
    L = Learner.__new__(Learner)
    cond = {
        ("pair", "GBP_JPY"): {"n": 80, "avg_r": -0.4, "ub": -0.15},
        ("hour", "00-03時"): {"n": 25, "avg_r": -0.9, "ub": -0.3},   # 回数不足 → 対象外
        ("side", "売り"): {"n": 200, "avg_r": -0.2, "ub": -0.05},    # 売買方向は対象外
        ("pair", "USD_JPY"): {"n": 100, "avg_r": 0.3, "ub": 0.5},
    }
    b = L._choose_blocks(cond, [], 400)
    assert [(x["feature"], x["value"]) for x in b] == [("pair", "GBP_JPY")]
    # 改善したら解除
    cond[("pair", "GBP_JPY")] = {"n": 90, "avg_r": 0.1, "ub": 0.3}
    assert L._choose_blocks(cond, b, 400) == []


def test_learner_moves_params_one_step_only():
    cur = dict(DEFAULT_PARAMS)
    for p in Learner._neighbors(cur):
        for k, grid in PARAM_GRID.items():
            assert abs(grid.index(p[k]) - grid.index(min(grid, key=lambda g: abs(g - cur[k])))) <= 1
