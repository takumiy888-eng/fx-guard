"""経済指標カレンダー

- 運用中: 無料の週間カレンダー（ForexFactory形式のJSON）を自動取得し、DBに蓄積
- バックテスト: 主要指標（FOMC・米雇用統計）の日程を再現 ＋ これまでに蓄積した実データ
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
ET = ZoneInfo("America/New_York")

# federalreserve.gov のFOMC日程（決定日 = 2日目、14:00 ET）
FOMC = {
    2021: ["01-27", "03-17", "04-28", "06-16", "07-28", "09-22", "11-03", "12-15"],
    2022: ["01-26", "03-16", "05-04", "06-15", "07-27", "09-21", "11-02", "12-14"],
    2023: ["02-01", "03-22", "05-03", "06-14", "07-26", "09-20", "11-01", "12-13"],
    2024: ["01-31", "03-20", "05-01", "06-12", "07-31", "09-18", "11-07", "12-18"],
    2025: ["01-29", "03-19", "05-07", "06-18", "07-30", "09-17", "10-29", "12-10"],
    2026: ["01-28", "03-18", "04-29", "06-17", "07-29", "09-16", "10-28", "12-09"],
    2027: ["01-27", "03-17", "04-28", "06-09", "07-28", "09-15", "10-27", "12-08"],
}


def historical_events(start: datetime, end: datetime) -> list[tuple[pd.Timestamp, str, str]]:
    """バックテスト用に再現する重要指標 (UTC時刻, 通貨, 名称)"""
    out = []
    for y in range(start.year, end.year + 1):
        for md in FOMC.get(y, []):
            m, d = map(int, md.split("-"))
            out.append((_et(y, m, d, 14, 0), "USD", "FOMC政策金利"))
        for m in range(1, 13):
            first = date(y, m, 1)
            fri = first + timedelta(days=(4 - first.weekday()) % 7)  # 第1金曜
            out.append((_et(y, m, fri.day, 8, 30), "USD", "米雇用統計"))
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    return [ev for ev in out if s <= ev[0] <= e]


def _et(y, m, d, hh, mm) -> pd.Timestamp:
    return pd.Timestamp(datetime(y, m, d, hh, mm, tzinfo=ET)).tz_convert("UTC")


class EventIndex:
    """高速な「この時間帯に、この通貨の重要指標があるか」判定"""

    def __init__(self, events: list[tuple[pd.Timestamp, str, str]]):
        self.by_ccy: dict[str, list] = {}
        for ts, ccy, title in events:
            self.by_ccy.setdefault(ccy, []).append((pd.Timestamp(ts), title))
        for v in self.by_ccy.values():
            v.sort(key=lambda x: x[0])
        self._ts = {k: pd.DatetimeIndex([t for t, _ in v]) for k, v in self.by_ccy.items()}

    def find(self, ccys, t0: pd.Timestamp, t1: pd.Timestamp):
        """t0〜t1 の間にある指標を1件返す（なければ None）"""
        for c in ccys:
            idx = self._ts.get(c)
            if idx is None or len(idx) == 0:
                continue
            i = idx.searchsorted(t0)
            if i < len(idx) and idx[i] <= t1:
                return self.by_ccy[c][i]
        return None


class LiveCalendar:
    """運用中の自動取得（3時間ごと）。取得失敗時は前回分を使い、7日以上古ければ警戒モード。"""

    def __init__(self, db, refresh_hours: float = 3.0):
        self.db = db
        self.refresh = timedelta(hours=refresh_hours)
        self.index = EventIndex([])
        self.last_ok: datetime | None = None
        self._load()

    def _load(self):
        rows = self.db.events_between(datetime.now(timezone.utc) - timedelta(days=3),
                                      datetime.now(timezone.utc) + timedelta(days=10))
        self.index = EventIndex([(pd.Timestamp(r["ts"]), r["ccy"], r["title"]) for r in rows])
        lo = self.db.kv_get("calendar_last_ok")
        self.last_ok = datetime.fromisoformat(lo) if lo else None

    def update(self, now: datetime, fetch=None) -> str | None:
        """必要なら取得。エラー時はメッセージを返す。"""
        last_try = self.db.kv_get("calendar_last_try")
        if last_try and now - datetime.fromisoformat(last_try) < self.refresh:
            return None
        self.db.kv_set("calendar_last_try", now.isoformat())
        try:
            items = (fetch or _fetch)()
        except Exception as e:  # noqa: BLE001
            return f"経済指標の取得に失敗（前回の取得分で継続）: {e}"
        rows = []
        for it in items:
            if it.get("impact") != "High":
                continue
            try:
                ts = pd.Timestamp(it["date"]).tz_convert("UTC")
            except Exception:  # noqa: BLE001
                continue
            rows.append((ts.isoformat(), it.get("country", ""), it.get("title", ""), "High"))
        self.db.events_upsert(rows)
        self.db.kv_set("calendar_last_ok", now.isoformat())
        self._load()
        return None

    @property
    def stale(self) -> bool:
        return self.last_ok is None or datetime.now(timezone.utc) - self.last_ok > timedelta(days=7)


def _fetch() -> list[dict]:
    r = requests.get(FEED_URL, timeout=15, headers={"User-Agent": "fx-guard/1.0"})
    r.raise_for_status()
    return json.loads(r.text)
