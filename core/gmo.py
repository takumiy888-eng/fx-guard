"""GMOコイン 外国為替FX の公開API（登録・APIキー不要）

https://api.coin.z.com/fxdocs/ の Public API を使う:
  /v1/status   … 市場の状態（OPEN / CLOSE / MAINTENANCE）
  /v1/ticker   … 全銘柄の最新 Bid/Ask
  /v1/klines   … ローソク足（1時間足は 2023-10-28 以降、日付は日本時間6:00切り替え）
  /v1/symbols  … 取扱銘柄
値はすべて文字列、openTime はミリ秒のUnix時刻で返る。
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

BASE_URL = "https://forex-api.coin.z.com/public"
FIRST_H1_DATE = date(2023, 10, 28)   # 1時間足を取得できる最初の日付


class MarketError(Exception):
    """価格取得・仮想売買のエラー（Botは止めずに記録して次の機会に再試行する）"""


def gmo_date(ts) -> date:
    """UTC時刻 → GMOの日付（日本時間6:00切り替え = UTC 21:00 切り替え）"""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return (t.tz_convert("UTC") + pd.Timedelta(hours=3)).date()


class GMOPublic:
    def __init__(self, http_get=None, min_interval: float = 0.25, timeout: int = 15, sleep=time.sleep, retries: int = 4):
        """http_get(path, params) -> dict を差し替えるとテストできる"""
        self.http_get = http_get or self._http_get
        self.min_interval = min_interval
        self.timeout = timeout
        self.sleep = sleep
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "fx-guard/1.0 (personal paper trading)"})
        self.requests = 0
        self.retries = retries

    # ------------------------------------------------------------ 通信
    def _http_get(self, path: str, params: dict | None) -> dict:
        r = self.session.get(BASE_URL + path, params=params, timeout=self.timeout)
        if r.status_code == 429 or r.status_code >= 500:
            raise _Retryable(f"HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise MarketError(f"HTTP {r.status_code}: 応答を読み取れません") from e

    def _get(self, path: str, params: dict | None = None):
        last = None
        for i in range(self.retries):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            try:
                body = self.http_get(path, params)
            except (_Retryable, requests.RequestException, OSError) as e:
                last = e
                self.sleep(min(2 ** i, 20))
                continue
            st = body.get("status")
            if st == 0:
                return body.get("data")
            msgs = body.get("messages") or []
            text = "; ".join(f"{m.get('message_code', '')} {m.get('message_string', '')}".strip() for m in msgs)
            if st == 5:  # メンテナンス中
                raise MarketError(f"GMO側がメンテナンス中です（{text}）")
            raise MarketError(f"GMO APIエラー status={st} {text}".strip())
        detail = "応答なし" if isinstance(last, requests.Timeout) else type(last).__name__ if last else "不明"
        raise MarketError(f"GMOに接続できません（ネット接続またはGMO側の障害: {detail}）")

    # ------------------------------------------------------------ API
    def status(self) -> str:
        return str((self._get("/v1/status") or {}).get("status", "UNKNOWN"))

    def ticker(self) -> dict[str, dict]:
        out = {}
        for row in self._get("/v1/ticker") or []:
            try:
                out[row["symbol"]] = {
                    "bid": float(row["bid"]),
                    "ask": float(row["ask"]),
                    "status": row.get("status", "OPEN"),
                    "timestamp": pd.Timestamp(row["timestamp"]).tz_convert("UTC")
                    if pd.Timestamp(row["timestamp"]).tzinfo else pd.Timestamp(row["timestamp"]).tz_localize("UTC"),
                }
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def symbols(self) -> list[str]:
        return [row["symbol"] for row in (self._get("/v1/symbols") or []) if "symbol" in row]

    def klines_day(self, pair: str, d: date, price_type: str = "BID") -> pd.DataFrame:
        """GMOの日付 d の1時間足（未確定の足も含まれうるので、使う側で除外する）"""
        rows = self._get("/v1/klines", {"symbol": pair, "priceType": price_type, "interval": "1hour",
                                         "date": d.strftime("%Y%m%d")}) or []
        return klines_to_df(rows)


def klines_to_df(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close"],
                            index=pd.DatetimeIndex([], tz="UTC", name="time"))
    df = pd.DataFrame(rows)
    idx = pd.to_datetime(df["openTime"].astype("int64"), unit="ms", utc=True)
    out = pd.DataFrame({k: df[k].astype(float).to_numpy() for k in ("open", "high", "low", "close")},
                       index=pd.DatetimeIndex(idx, name="time"))
    return out.sort_index()


class _Retryable(Exception):
    pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def dates_between(d0: date, d1: date, descending: bool = False) -> list[date]:
    """d0〜d1（両端含む）。日曜はGMOの日付で取引がないので除く"""
    if d1 < d0:
        return []
    out = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    out = [d for d in out if d.weekday() != 6]
    return out[::-1] if descending else out
