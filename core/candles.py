"""1時間足のローカル保存（CSV）と差分取得

- 保存するのは確定したBid足だけ（未確定の足は絶対に保存しない＝未来の値を使わない）
- 初回は過去約2.5年分を1日ずつ取得（約30分）。以降は新しい日付だけを取得
- 途中で失敗しても、取得済みの分は保存されているので次回は続きから
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .gmo import FIRST_H1_DATE, GMOPublic, MarketError, dates_between, gmo_date

H = pd.Timedelta(hours=1)
COLS = ["open", "high", "low", "close"]


class CandleStore:
    def __init__(self, folder: Path, gmo: GMOPublic, clock=None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.gmo = gmo
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._mem: dict[str, pd.DataFrame] = {}

    # ------------------------------------------------------------ 読み書き
    def _path(self, pair: str) -> Path:
        return self.folder / f"{pair}_H1_BID.csv"

    def bid(self, pair: str) -> pd.DataFrame:
        if pair not in self._mem:
            f = self._path(pair)
            if f.exists():
                df = pd.read_csv(f, index_col=0)
                df.index = pd.DatetimeIndex(pd.to_datetime(df.index, utc=True), name="time")
                self._mem[pair] = df[COLS].astype(float).sort_index()
            else:
                self._mem[pair] = pd.DataFrame(columns=COLS, index=pd.DatetimeIndex([], tz="UTC", name="time"))
        return self._mem[pair]

    def _save(self, pair: str, df: pd.DataFrame) -> None:
        df = df[~df.index.duplicated(keep="last")].sort_index()
        self._mem[pair] = df
        tmp = self._path(pair).with_suffix(".tmp")
        df.to_csv(tmp)
        os.replace(tmp, self._path(pair))

    # ------------------------------------------------------------ 取得
    def _fetch_dates(self, pair: str, dates, now, progress=None) -> int:
        df = self.bid(pair)
        added, buf = 0, []
        for i, d in enumerate(dates):
            try:
                day = self.gmo.klines_day(pair, d, "BID")
            except MarketError as e:
                if "status=" in str(e) and d < FIRST_H1_DATE + timedelta(days=3):
                    continue  # 取得可能期間の端
                if buf:
                    self._save(pair, pd.concat([df] + buf))
                raise
            day = day[day.index + H <= pd.Timestamp(now)]  # 未確定の足を除外
            if len(day):
                buf.append(day)
                added += len(day)
            if progress and (i % 20 == 0 or i == len(dates) - 1):
                progress(pair, d, i + 1, len(dates))
            if len(buf) >= 30:
                df = pd.concat([df] + buf)
                self._save(pair, df)
                buf = []
        if buf:
            self._save(pair, pd.concat([df] + buf))
        return added

    def ensure(self, pair: str, start, progress=None) -> pd.DataFrame:
        """start 以降の足が揃っている状態にする（足りない過去分と、新しい分を取得）"""
        now = self.clock()
        start = max(pd.Timestamp(start), pd.Timestamp(FIRST_H1_DATE, tz="UTC") + pd.Timedelta(hours=21))
        df = self.bid(pair)
        if len(df) and df.index[0] > start + pd.Timedelta(days=4):
            # 足りない過去分：新しい日付から古い方へ（途中で止まっても隙間ができない）
            ds = dates_between(gmo_date(start), gmo_date(df.index[0]) - timedelta(days=1), descending=True)
            self._fetch_dates(pair, ds, now, progress)
        self.refresh(pair, force=True, progress=progress, start=start)
        return self.bid(pair)

    def refresh(self, pair: str, force: bool = False, progress=None, start=None) -> int:
        """最後に保存した足の日付から今日までを取得。新しい確定足の本数を返す"""
        now = self.clock()
        df = self.bid(pair)
        if len(df):
            d0 = gmo_date(df.index[-1])
        else:
            st = pd.Timestamp(start) if start is not None else pd.Timestamp(now) - pd.Timedelta(days=60)
            d0 = max(gmo_date(st), FIRST_H1_DATE)
        before = df.index[-1] if len(df) else None
        self._fetch_dates(pair, dates_between(d0, gmo_date(now)), now, progress)
        after = self.bid(pair)
        if before is None:
            return len(after)
        return int((after.index > before).sum())
