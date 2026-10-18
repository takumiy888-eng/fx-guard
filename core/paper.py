"""仮想売買口座（ペーパートレード）

GMOコインの実際の Bid/Ask を使って、Bot内で約定・決済を再現する。
- 成行: 買いは Ask、売りは Bid ＋ スリッページ（不利な方向）
- 損切り: 価格が損切り値を越えたら、その時の価格で約定（窓開けなら損切り値より悪くなる）
- 利確: 価格が利確値に届いたら利確値で約定
- 15秒ごとの最新レートに加えて、確定した1時間足の高値・安値でも判定するので、
  チェックの合間のヒゲやBot停止中の値動きでも損切り・利確が漏れない（同じ足で両方届いたら損切り扱い）
- 市場が閉まっている間（週末・メンテナンス）は約定しない

エンジンからは証券会社と同じように使える（open_trades / market_order / close_trade / set_stop_loss / trade）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .backtest import DEFAULT_SPREAD, pip
from .candles import CandleStore
from .gmo import GMOPublic, MarketError

H = pd.Timedelta(hours=1)
BOOK_KEY = "paper_book"


class PaperAccount:
    def __init__(self, db, base_dir: Path, gmo: GMOPublic | None = None, clock=None, slippage_pips: float = 0.2):
        self.db = db
        self.gmo = gmo or GMOPublic()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.candles = CandleStore(Path(base_dir) / "data" / "candles", self.gmo, self.clock)
        self.slip_pips = slippage_pips
        self._ticker: dict = {}
        self._ticker_at = None
        self._next_refresh: dict[str, datetime] = {}
        self._symbols: list[str] | None = None

    # ------------------------------------------------------------ 価格
    def _tick(self, max_age_s: float = 5) -> dict:
        now = self.clock()
        if self._ticker_at is None or (now - self._ticker_at).total_seconds() > max_age_s or not self._ticker:
            self._ticker = self.gmo.ticker()
            self._ticker_at = now
        return self._ticker

    def is_open(self, pair: str) -> bool:
        t = self._tick().get(pair)
        if not t or t["status"] != "OPEN":
            return False
        # 価格が5分以上更新されていなければ止まっているとみなす
        return (pd.Timestamp(self.clock()) - t["timestamp"]).total_seconds() < 300

    @property
    def market_open(self) -> bool:
        tk = self._tick()
        return any(v["status"] == "OPEN" for v in tk.values()) if tk else False

    def pricing(self, pairs: list[str]) -> dict[str, tuple[float, float]]:
        tk = self._tick()
        out = {}
        for p in pairs:
            if p not in tk:
                raise MarketError(f"{p} のレートがGMOにありません")
            out[p] = (tk[p]["bid"], tk[p]["ask"])
        return out

    def instruments(self, pairs: list[str]) -> dict[str, dict]:
        if self._symbols is None:
            self._symbols = self.gmo.symbols()
        out = {}
        for p in pairs:
            if p in self._symbols:
                jpy = p.endswith("JPY")
                out[p] = {"name": p, "displayPrecision": 3 if jpy else 5, "pipLocation": -2 if jpy else -4,
                          "minimumTradeSize": "1"}
        return out

    # ------------------------------------------------------------ ローソク足（仲値と実際のスプレッド）
    def _half_spread(self, pair: str) -> float:
        return DEFAULT_SPREAD.get(pair, 1.0) * pip(pair) / 2

    def _mid(self, pair: str, bid: pd.DataFrame) -> pd.DataFrame:
        """売値と買値の足から仲値を作る。買値が無い時間は 売値＋標準スプレッドの半分 で補う。
        spread 列 = その足の始値時点の実際のスプレッド（価格差）。バックテストで使う"""
        bid = bid[["open", "high", "low", "close"]]
        ask = self.candles.ask(pair).reindex(bid.index)
        dflt = self._half_spread(pair) * 2
        filled = ask.isna().any(axis=1)
        ask = ask.where(~filled, bid + dflt)
        mid = (bid + ask) / 2
        # 高値・安値は売値と買値で同時刻とは限らないので、四本値の整合を取り直す
        mid["high"] = mid[["open", "high", "low", "close"]].max(axis=1)
        mid["low"] = mid[["open", "high", "low", "close"]].min(axis=1)
        mid["spread"] = (ask["open"] - bid["open"]).clip(lower=0)
        mid.loc[filled, "spread"] = dflt
        return mid

    def ask_ready(self, pairs: list[str], start=None) -> bool:
        """学習に使う期間（start 以降）の買値データが揃っているか"""
        for p in pairs:
            b, a = self.candles.bid(p), self.candles.ask(p)
            if len(a) == 0 or len(b) == 0:
                return False
            need = max(b.index[0], pd.Timestamp(start)) if start is not None else b.index[0]
            if a.index[0] > need + pd.Timedelta(days=7) or a.index[-1] < b.index[-1] - pd.Timedelta(days=7):
                return False
        return True

    def recent_df(self, pair: str, granularity: str, count: int) -> pd.DataFrame:
        now = self.clock()
        due = self._next_refresh.get(pair)
        have = len(self.candles.bid(pair)) > 0
        if have and not self.market_open:
            # 市場が閉まっている間は新しい足が出ないので、GMOに問い合わせない
            self._next_refresh[pair] = now
        elif due is None or now >= due:
            new = 0
            try:
                new = self.candles.refresh(pair, side="BID")
                self.candles.refresh(pair, side="ASK")
            finally:
                # 新しい足がまだ無ければ1分後にもう一度
                self._next_refresh[pair] = now + timedelta(minutes=0 if new else 1)
        return self._mid(pair, self.candles.bid(pair).tail(count))

    def history_df(self, pair: str, granularity: str, start, end, progress=None) -> pd.DataFrame:
        def stage(k):  # 売値（前半）・買値（後半）で進み具合を分ける
            if not progress:
                return None
            return lambda pr, d, i, n: progress(pr, d, i + k * n, 2 * n)
        df = self.candles.ensure(pair, start, stage(0), side="BID")
        self.candles.ensure(pair, start, stage(1), side="ASK")
        df = df[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
        return self._mid(pair, df)

    def cached_df(self, pair: str) -> pd.DataFrame:
        return self._mid(pair, self.candles.bid(pair))

    # ------------------------------------------------------------ 仮想口座の帳簿（DBに保存）
    def _book(self) -> dict:
        return self.db.kv_get(BOOK_KEY) or {"next_id": 1, "open": {}, "closed": {}}

    def _save(self, b: dict) -> None:
        if len(b["closed"]) > 300:
            for k in sorted(b["closed"], key=lambda x: int(x))[:-300]:
                b["closed"].pop(k)
        self.db.kv_set(BOOK_KEY, b)

    def _slip(self, pair: str) -> float:
        return self.slip_pips * pip(pair)

    def _fill(self, b: dict, tid: str, n: int, px: float, reason: str | None, when) -> None:
        t = b["open"][tid]
        n = min(n, abs(t["current"]))
        sgn = 1 if t["current"] > 0 else -1
        t["closed_units"] += n
        t["closed_value"] += px * n
        t["current"] -= sgn * n
        if t["current"] == 0:
            b["open"].pop(tid)
            b["closed"][tid] = {
                "id": tid, "instrument": t["instrument"], "price": str(t["price"]),
                "initialUnits": str(t["units"]), "state": "CLOSED",
                "averageClosePrice": str(t["closed_value"] / t["closed_units"]),
                "closeReason": reason, "closeTime": pd.Timestamp(when).isoformat(),
            }

    def _stop_reason(self, t: dict) -> str:
        d = 1 if t["units"] > 0 else -1
        return "建値決済" if (t["sl"] - t["price"]) * d >= -1e-9 else "損切り"

    # ------------------------------------------------------------ 損切り・利確の判定
    def sync(self) -> list[str]:
        """保有中の注文について、損切り・利確に届いたかを判定して約定させる。決済したIDを返す"""
        b = self._book()
        if not b["open"]:
            return []
        done: list[str] = []
        now = self.clock()
        tk = self._tick()
        for tid, t in list(b["open"].items()):
            pair, d = t["instrument"], (1 if t["units"] > 0 else -1)
            sp = DEFAULT_SPREAD.get(pair, 1.0) * pip(pair)
            slip = self._slip(pair)
            # 1) 確定した1時間足で判定（チェックの合間・停止中の値動きを拾う）
            #    買いは売値の足、売りは買値の足（無い時間は 売値＋標準スプレッド）で判定
            bars = self.candles.bid(pair)
            asks = self.candles.ask(pair)
            since = pd.Timestamp(max(t["sl_since"], t["checked_until"]))
            first_bar = since.ceil("h")
            bars = bars[bars.index >= first_bar]
            for ts, bar in bars.iterrows():
                o, hi, lo = bar.open, bar.high, bar.low
                if d == -1 and ts in asks.index:
                    ab = asks.loc[ts]
                    sp_o, sp_h, sp_l = ab.open - o, ab.high - hi, ab.low - lo
                else:
                    sp_o = sp_h = sp_l = sp
                if d == 1:            # 買いは Bid で決済
                    if o <= t["sl"]:
                        px, why = o - slip, self._stop_reason(t) + "(窓)"
                    elif lo <= t["sl"]:
                        px, why = t["sl"] - slip, self._stop_reason(t)
                    elif t["tp"] is not None and hi >= t["tp"]:
                        px, why = t["tp"], "利確"
                    else:
                        px = None
                else:                 # 売りは Ask（Bid＋標準スプレッド）で決済
                    ao, ah, al = o + sp_o, hi + sp_h, lo + sp_l
                    if ao >= t["sl"]:
                        px, why = ao + slip, self._stop_reason(t) + "(窓)"
                    elif ah >= t["sl"]:
                        px, why = t["sl"] + slip, self._stop_reason(t)
                    elif t["tp"] is not None and al <= t["tp"]:
                        px, why = t["tp"], "利確"
                    else:
                        px = None
                t["checked_until"] = (ts + H).isoformat()
                if px is not None:
                    self._fill(b, tid, abs(t["current"]), px, why, ts + H)
                    done.append(tid)
                    break
            if tid in done:
                continue
            # 2) 最新レートで判定
            q = tk.get(pair)
            if not q or q["status"] != "OPEN":
                continue
            if d == 1:
                px_now = q["bid"]
                if px_now <= t["sl"]:
                    self._fill(b, tid, abs(t["current"]), px_now - slip, self._stop_reason(t), now)
                    done.append(tid)
                elif t["tp"] is not None and px_now >= t["tp"]:
                    self._fill(b, tid, abs(t["current"]), t["tp"], "利確", now)
                    done.append(tid)
            else:
                px_now = q["ask"]
                if px_now >= t["sl"]:
                    self._fill(b, tid, abs(t["current"]), px_now + slip, self._stop_reason(t), now)
                    done.append(tid)
                elif t["tp"] is not None and px_now <= t["tp"]:
                    self._fill(b, tid, abs(t["current"]), t["tp"], "利確", now)
                    done.append(tid)
        self._save(b)
        return done

    # ------------------------------------------------------------ 証券会社と同じ形の操作
    def open_trades(self) -> list[dict]:
        return [{"id": tid, "instrument": t["instrument"], "currentUnits": str(t["current"]),
                 "price": str(t["price"]), "openTime": t["open_time"],
                 "clientExtensions": {"tag": t["tag"], "comment": t["comment"]},
                 "stopLossOrder": {"price": str(t["sl"])}}
                for tid, t in self._book()["open"].items()]

    def trade(self, tid: str) -> dict:
        b = self._book()
        if tid in b["closed"]:
            return b["closed"][tid]
        raise MarketError(f"取引 #{tid} は決済されていません")

    def market_order(self, pair: str, units: int, stop_loss: str, take_profit: str | None = None,
                     trailing_distance=None, tag: str = "", comment: str = "") -> dict:
        if units == 0:
            raise MarketError("数量が0です")
        if not self.is_open(pair):
            raise MarketError(f"{pair} は現在取引できません（市場休止中）")
        bid, ask = self.pricing([pair])[pair]
        d = 1 if units > 0 else -1
        px = ask + self._slip(pair) if d == 1 else bid - self._slip(pair)
        sl = float(stop_loss)
        tp = float(take_profit) if take_profit else None
        if (px - sl) * d <= 0:
            raise MarketError("損切り値が約定価格の反対側にありません")
        now = self.clock()
        b = self._book()
        tid = str(b["next_id"])
        b["next_id"] += 1
        b["open"][tid] = {
            "instrument": pair, "units": int(units), "current": int(units), "price": px, "sl": sl, "tp": tp,
            "tag": tag, "comment": comment, "open_time": pd.Timestamp(now).isoformat(),
            "sl_since": pd.Timestamp(now).isoformat(), "checked_until": pd.Timestamp(now).isoformat(),
            "closed_units": 0, "closed_value": 0.0,
        }
        self._save(b)
        return {"orderFillTransaction": {"price": str(px), "tradeOpened": {"tradeID": tid}}}

    def close_trade(self, tid: str, units: int | None = None) -> dict:
        b = self._book()
        if tid not in b["open"]:
            raise MarketError(f"取引 #{tid} は保有していません")
        t = b["open"][tid]
        pair = t["instrument"]
        if not self.is_open(pair):
            raise MarketError(f"{pair} は現在決済できません（市場休止中）")
        bid, ask = self.pricing([pair])[pair]
        px = bid - self._slip(pair) if t["current"] > 0 else ask + self._slip(pair)
        n = abs(t["current"]) if units is None else int(units)
        self._fill(b, tid, n, px, None, self.clock())
        self._save(b)
        return {}

    def set_stop_loss(self, tid: str, price: str) -> dict:
        b = self._book()
        if tid not in b["open"]:
            raise MarketError(f"取引 #{tid} は保有していません")
        b["open"][tid]["sl"] = float(price)
        b["open"][tid]["sl_since"] = pd.Timestamp(self.clock()).isoformat()
        self._save(b)
        return {}
