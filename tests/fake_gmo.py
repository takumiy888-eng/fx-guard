"""GMOコイン 公開API の模擬サーバー（HTTP応答の形をドキュメントどおりに再現）

- 値はすべて文字列、openTime はミリ秒の文字列
- klines の date は日本時間6:00切り替え。進行中の足も「最終的な値」で返す
  （本物は途中の値だが、ここで最終値を返すことで、使う側が未確定足を除外していないと未来が見えてしまう＝テストで検出できる）
- ticker は現在時刻の足の中を 始値→安値→高値→終値（陰線は高値→安値）の順に動く
- 市場が閉まっている時間は ticker の status が CLOSE
"""
from datetime import timedelta

import numpy as np
import pandas as pd

from core.backtest import DEFAULT_SPREAD, pip
from core.gmo import FIRST_H1_DATE, gmo_date

H = pd.Timedelta(hours=1)


class FakeGMO:
    def __init__(self, mid: dict[str, pd.DataFrame], extra_symbols=("NZD_JPY",), spread_by_jst_hour=None):
        """spread_by_jst_hour: {日本時間の時: スプレッド(pips)} 指定した時間だけスプレッドを変える（早朝の拡大など）"""
        self.mid = mid
        self.sp_hour = spread_by_jst_hour or {}
        self.now = None
        self.calls: dict[str, int] = {}
        self.extra = list(extra_symbols)
        self.maintenance = False
        self.fail_next = 0
        # 日付ごとに事前に分割（高速化）
        self.by_date = {}
        self.ask_by_date = {}
        for p, df in mid.items():
            sp = self._spread_series(p, df.index)
            bid = df.sub(sp / 2, axis=0)
            ask = df.add(sp / 2, axis=0)
            keys = np.array([gmo_date(t) for t in bid.index])
            self.by_date[p] = {d: bid[keys == d] for d in sorted(set(keys))}
            self.ask_by_date[p] = {d: ask[keys == d] for d in sorted(set(keys))}

    def _spread_series(self, p, idx):
        base = DEFAULT_SPREAD.get(p, 1.0)
        jh = idx.tz_convert("Asia/Tokyo").hour
        return pd.Series([self.sp_hour.get(h, base) * pip(p) for h in jh], index=idx)

    def set_now(self, t):
        self.now = pd.Timestamp(t)

    def _ok(self, data):
        return {"status": 0, "data": data, "responsetime": self.now.isoformat()}

    def _price_now(self, pair):
        df = self.mid[pair]
        bar_t = self.now.floor("h")
        if bar_t not in df.index:
            past = df[df.index < bar_t]
            return float(past.close.iloc[-1]), False
        b = df.loc[bar_t]
        f = (self.now - bar_t) / H
        pts = [b.open, b.low, b.high, b.close] if b.close >= b.open else [b.open, b.high, b.low, b.close]
        x = f * 3
        i = min(int(x), 2)
        return float(pts[i] + (pts[i + 1] - pts[i]) * (x - i)), True

    def get(self, path, params):
        self.calls[path] = self.calls.get(path, 0) + 1
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("simulated network error")
        if self.maintenance:
            return {"status": 5, "messages": [{"message_code": "ERR-5201", "message_string": "MAINTENANCE."}],
                    "responsetime": self.now.isoformat()}
        if path == "/v1/status":
            any_open = any(self._price_now(p)[1] for p in self.mid)
            return self._ok({"status": "OPEN" if any_open else "CLOSE"})
        if path == "/v1/symbols":
            return self._ok([{"symbol": s, "minOpenOrderSize": "10000", "maxOrderSize": "500000",
                              "sizeStep": "1", "tickSize": "0.001"} for s in list(self.mid) + self.extra])
        if path == "/v1/ticker":
            rows = []
            for p in self.mid:
                m, is_open = self._price_now(p)
                hs = self._spread_series(p, pd.DatetimeIndex([self.now.floor("h")])).iloc[0] / 2
                prec = 3 if p.endswith("JPY") else 5
                rows.append({"symbol": p, "bid": f"{m - hs:.{prec}f}", "ask": f"{m + hs:.{prec}f}",
                             "timestamp": self.now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                             "status": "OPEN" if is_open else "CLOSE"})
            return self._ok(rows)
        if path == "/v1/klines":
            d = pd.Timestamp(params["date"]).date()
            if d < FIRST_H1_DATE:
                return {"status": 1, "messages": [{"message_code": "ERR-5106", "message_string": "Invalid date."}],
                        "responsetime": self.now.isoformat()}
            src = self.ask_by_date if params["priceType"] == "ASK" else self.by_date
            day = src.get(params["symbol"], {}).get(d)
            if day is None:
                return self._ok([])
            day = day[day.index <= self.now]          # 進行中の足まで（最終値のまま）
            prec = 3 if params["symbol"].endswith("JPY") else 5
            return self._ok([{"openTime": str(int(t.value // 10**6)), "open": f"{r.open:.{prec}f}",
                              "high": f"{r.high:.{prec}f}", "low": f"{r.low:.{prec}f}",
                              "close": f"{r.close:.{prec}f}"} for t, r in day.iterrows()])
        return {"status": 1, "messages": [{"message_code": "ERR-404", "message_string": "not found"}]}
