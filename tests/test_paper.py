import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.backtest import DEFAULT_SPREAD, pip  # noqa: E402
from core.candles import CandleStore  # noqa: E402
from core.db import DB  # noqa: E402
from core.gmo import GMOPublic, MarketError, dates_between, gmo_date, klines_to_df  # noqa: E402
from core.paper import PaperAccount  # noqa: E402
from tests.fake_gmo import FakeGMO  # noqa: E402
from tests.synth import fx_hours, make  # noqa: E402

H = pd.Timedelta(hours=1)


def _setup(now="2025-03-05 06:20", n=6500, t0="2024-09-02", spread_by_jst_hour=None):
    mid = {"USD_JPY": fx_hours(make(n=n, seed=5, t0=t0)), "EUR_USD": fx_hours(make(n=n, seed=6, start=1.08, t0=t0))}
    fake = FakeGMO(mid, spread_by_jst_hour=spread_by_jst_hour)
    clock = {"t": pd.Timestamp(now, tz="UTC").to_pydatetime()}
    fake.set_now(clock["t"])
    gmo = GMOPublic(http_get=fake.get, min_interval=0, sleep=lambda s: None)
    tmp = Path(tempfile.mkdtemp())
    acct = PaperAccount(DB(tmp / "t.db"), tmp, gmo=gmo, clock=lambda: clock["t"])
    return fake, acct, clock, mid

def _go(fake, clock, t):
    clock["t"] = pd.Timestamp(t, tz="UTC").to_pydatetime()
    fake.set_now(clock["t"])


# ---------------------------------------------------------------- GMO 形式
def test_gmo_date_switches_at_6am_jst():
    assert gmo_date(pd.Timestamp("2024-03-05 20:59", tz="UTC")) == date(2024, 3, 5)   # 3/6 5:59 JST
    assert gmo_date(pd.Timestamp("2024-03-05 21:00", tz="UTC")) == date(2024, 3, 6)   # 3/6 6:00 JST
    assert all(d.weekday() != 6 for d in dates_between(date(2024, 3, 1), date(2024, 3, 31)))


def test_klines_parse_strings_and_ms():
    df = klines_to_df([{"openTime": "1709683200000", "open": "150.100", "high": "150.300", "low": "150.000",
                        "close": "150.200"}])
    assert df.index[0] == pd.Timestamp("2024-03-06 00:00", tz="UTC")
    assert df.iloc[0].close == pytest.approx(150.2)


def test_api_errors_and_retries():
    fake, acct, clock, _ = _setup()
    fake.fail_next = 2                       # 通信エラー2回 → 再試行で成功
    assert "USD_JPY" in acct.gmo.ticker()
    fake.maintenance = True
    with pytest.raises(MarketError):
        acct.gmo.ticker()
    fake.maintenance = False
    with pytest.raises(MarketError):         # 取得可能期間より前
        acct.gmo.klines_day("USD_JPY", date(2023, 1, 5))


# ---------------------------------------------------------------- ローソク足
def test_candles_never_store_unfinished_bar_and_resume():
    fake, acct, clock, mid = _setup(now="2025-03-05 06:20")
    cs = acct.candles
    df = cs.ensure("USD_JPY", pd.Timestamp("2025-01-06", tz="UTC"))
    assert df.index[-1] == pd.Timestamp("2025-03-05 05:00", tz="UTC")      # 06:00の足は進行中なので無い
    hs = DEFAULT_SPREAD["USD_JPY"] * pip("USD_JPY") / 2
    ref = mid["USD_JPY"].loc[df.index[-1]]
    assert df.iloc[-1].close == pytest.approx(ref.close - hs, abs=1e-3)    # Bidで保存
    n1 = fake.calls["/v1/klines"]
    _go(fake, clock, "2025-03-05 08:00:10")
    assert cs.refresh("USD_JPY") == 2                                       # 06:00, 07:00 が追加
    assert fake.calls["/v1/klines"] == n1 + 1                               # 今日の分だけ取得
    # 保存→読み直しでも同じ
    cs2 = CandleStore(cs.folder, acct.gmo, lambda: clock["t"])
    assert len(cs2.bid("USD_JPY")) == len(cs.bid("USD_JPY"))
    # 開始を広げたら足りない過去分だけ取りに行く
    df3 = cs.ensure("USD_JPY", pd.Timestamp("2024-10-01", tz="UTC"))
    assert df3.index[0] < pd.Timestamp("2024-10-03", tz="UTC")
    assert not df3.index.duplicated().any() and df3.index.is_monotonic_increasing


def test_no_polling_storm_on_weekend():
    fake, acct, clock, _ = _setup(now="2025-03-08 03:00")                 # 土曜（市場休み）
    acct.candles.ensure("USD_JPY", pd.Timestamp("2025-02-20", tz="UTC"))
    n = fake.calls["/v1/klines"]
    for m in range(0, 240, 1):                                             # 4時間、毎分呼ばれても
        _go(fake, clock, pd.Timestamp("2025-03-08 03:00") + pd.Timedelta(minutes=m))
        acct.recent_df("USD_JPY", "H1", 100)
    assert fake.calls["/v1/klines"] - n == 0


# ---------------------------------------------------------------- 仮想売買
def test_market_order_fills_at_ask_bid_with_slippage():
    fake, acct, clock, _ = _setup()
    bid, ask = acct.pricing(["USD_JPY"])["USD_JPY"]
    r = acct.market_order("USD_JPY", 100, f"{bid - 0.5:.3f}", f"{ask + 1:.3f}", tag="fxguard")
    assert float(r["orderFillTransaction"]["price"]) == pytest.approx(ask + 0.002)
    r2 = acct.market_order("EUR_USD", -100, "1.5", None, tag="fxguard")
    b2, _ = acct.pricing(["EUR_USD"])["EUR_USD"]
    assert float(r2["orderFillTransaction"]["price"]) == pytest.approx(b2 - 0.00002)
    with pytest.raises(MarketError):                                       # 損切りが逆側
        acct.market_order("USD_JPY", 100, f"{ask + 1:.3f}", None)


def test_closed_market_rejects_orders():
    fake, acct, clock, _ = _setup(now="2025-03-08 03:00")
    assert not acct.is_open("USD_JPY")
    with pytest.raises(MarketError):
        acct.market_order("USD_JPY", 100, "100.000", None)


def test_partial_close_and_breakeven_stop():
    fake, acct, clock, _ = _setup()
    bid, ask = acct.pricing(["USD_JPY"])["USD_JPY"]
    tid = acct.market_order("USD_JPY", 100, f"{bid - 1:.3f}", None, tag="fxguard")["orderFillTransaction"]["tradeOpened"]["tradeID"]
    entry = float(acct.open_trades()[0]["price"])
    acct.close_trade(tid, 50)
    assert acct.open_trades()[0]["currentUnits"] == "50"
    acct.set_stop_loss(tid, f"{entry + 5:.3f}")         # 現在値より上 → 次の同期で建値決済扱い
    assert acct.sync() == [tid]
    tr = acct.trade(tid)
    assert tr["closeReason"] == "建値決済" and tr["initialUnits"] == "100"
    assert acct.open_trades() == []


def test_stop_detected_from_candles_after_downtime():
    """Botが止まっていた間に損切りに届いていたら、再開時に確定足から検出して損切り値で約定"""
    fake, acct, clock, mid = _setup(now="2025-03-04 01:10")
    acct.candles.ensure("USD_JPY", pd.Timestamp("2025-02-20", tz="UTC"))
    bid, ask = acct.pricing(["USD_JPY"])["USD_JPY"]
    after = mid["USD_JPY"][mid["USD_JPY"].index >= pd.Timestamp("2025-03-04 02:00", tz="UTC")].iloc[:30]
    hs = DEFAULT_SPREAD["USD_JPY"] * pip("USD_JPY") / 2
    lows = (after.low - hs).cummin()
    sl = float(lows.iloc[20]) + 0.001                   # 約20時間後に初めて届く損切り値
    assert sl < bid
    tid = acct.market_order("USD_JPY", 100, f"{sl:.3f}", None, tag="fxguard")["orderFillTransaction"]["tradeOpened"]["tradeID"]
    _go(fake, clock, "2025-03-05 06:10")                # 29時間後に再開
    acct.candles.refresh("USD_JPY")
    assert tid in acct.sync()
    tr = acct.trade(tid)
    hit = after.index[(after.low - hs) <= float(f"{sl:.3f}")][0]
    assert pd.Timestamp(tr["closeTime"]) == hit + H
    assert float(tr["averageClosePrice"]) <= float(f"{sl:.3f}") - 0.002 + 1e-9 or tr["closeReason"].endswith("(窓)")


def test_take_profit_by_ticker():
    fake, acct, clock, _ = _setup()
    bid, ask = acct.pricing(["USD_JPY"])["USD_JPY"]
    tid = acct.market_order("USD_JPY", -100, f"{ask + 5:.3f}", f"{ask + 0.5:.3f}", tag="fxguard")["orderFillTransaction"]["tradeOpened"]["tradeID"]
    assert acct.sync() == [tid]                          # 売りの利確値が現在のAskより上 → すぐ利確
    assert acct.trade(tid)["closeReason"] == "利確"


# ---------------------------------------------------------------- 売値・買値の両方
def test_mid_and_actual_spread_from_bid_and_ask():
    """早朝（日本時間6〜7時）だけスプレッドが5pipsに広がる相場で、仲値と実際のスプレッドを正しく再現する"""
    fake, acct, clock, mid = _setup(now="2025-03-05 06:20", spread_by_jst_hour={6: 5.0, 7: 5.0})
    df = acct.history_df("USD_JPY", "H1", pd.Timestamp("2025-02-03", tz="UTC"), pd.Timestamp("2025-03-05", tz="UTC"))
    assert acct.ask_ready(["USD_JPY"])
    ref = mid["USD_JPY"].reindex(df.index)
    assert (df.close - ref.close).abs().max() < 0.002               # 仲値は元の値と一致（表示桁の丸めのみ）
    jh = df.index.tz_convert("Asia/Tokyo").hour
    wide, normal = df.spread[(jh == 6) | (jh == 7)], df.spread[(jh >= 10) & (jh <= 20)]
    assert abs(wide.mean() - 0.05) < 0.002 and abs(normal.mean() - 0.004) < 0.001


def test_backtest_charges_actual_spread():
    from core import backtest as B
    fake, acct, clock, mid = _setup(now="2025-05-01 06:20", spread_by_jst_hour={h: 3.0 for h in range(24)})
    df = acct.history_df("USD_JPY", "H1", pd.Timestamp("2024-09-02", tz="UTC"), pd.Timestamp("2025-05-01", tz="UTC"))
    real = B.stats(B.run({"USD_JPY": df}, gates={"session_start": 0, "session_end": 0})["trades"])
    cheap = B.stats(B.run({"USD_JPY": df}, gates={"session_start": 0, "session_end": 0}, spread={"USD_JPY": 0.4})["trades"])
    assert real["n"] > 20 and real["avg_r"] < cheap["avg_r"]       # 実際の3pipsは標準0.4pipsより不利になる
