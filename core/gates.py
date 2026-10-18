"""「入らない理由」のチェック（バックテストと実運用で共通）

最初に引っかかった理由を返す。None ならエントリー可。
見送ったシグナルは「影のトレード」として記録し、フィルターが本当に役立っているかを学習で検証する。
"""
from __future__ import annotations

import pandas as pd

DEFAULT_GATES = {
    "session_start": 9,         # エントリー可能時間（日本時間）
    "session_end": 2,           # 翌2時まで（東京昼〜NY前半）
    "event_before_min": 60,     # 重要指標の60分前から
    "event_after_min": 30,      # 30分後までは入らない
    "event_exit_before_min": 30,  # 保有中は30分前に撤退（建値ストップ済みなら継続）
    "monday_start_hour": 10,    # 月曜は10時まで入らない（窓開け直後）
    "weekend_close": "土 04:00",  # 週末持ち越しなし
    "max_positions": 3,
}


def currencies(pair: str) -> tuple[str, str]:
    b, q = pair.split("_")
    return b, q


def exposure(pair: str, d: int) -> dict[str, int]:
    """買い USD_JPY = USDを買い・JPYを売り"""
    b, q = currencies(pair)
    return {b: d, q: -d}


def in_session(t_jst: pd.Timestamp, g: dict) -> bool:
    s, e, h = int(g["session_start"]), int(g["session_end"]), t_jst.hour
    if s == e:
        return True
    return s <= h < e if s < e else (h >= s or h < e)


def weekend_block(t_jst: pd.Timestamp, g: dict) -> bool:
    wd = t_jst.weekday()
    if wd >= 5:  # 土日
        return True
    if wd == 0 and t_jst.hour < int(g["monday_start_hour"]):
        return True
    if wd == 4 and t_jst.hour >= 23:  # 金曜深夜は週末前なので新規なし
        return True
    return False


def weekend_close_due(t_utc: pd.Timestamp) -> bool:
    j = pd.Timestamp(t_utc).tz_convert("Asia/Tokyo")
    return (j.weekday() == 5 and j.hour >= 4) or j.weekday() == 6


def check_entry(pair: str, d: int, close_time: pd.Timestamp, vol_ratio: float, feats: dict,
                params: dict, gates: dict, events, open_positions: list[tuple[str, int]],
                blocks: list[dict]) -> str | None:
    """close_time: シグナルの足が確定した時刻（UTC）"""
    t = pd.Timestamp(close_time)
    j = t.tz_convert("Asia/Tokyo")
    if weekend_block(j, gates):
        return "週末・週明け"
    if not in_session(j, gates):
        return "時間帯外"
    ev = events.find(currencies(pair), t - pd.Timedelta(minutes=gates["event_after_min"]),
                     t + pd.Timedelta(minutes=gates["event_before_min"])) if events else None
    if ev:
        return "経済指標の前後"
    if not (params["vol_min"] <= vol_ratio <= params["vol_max"]):
        return "ボラ異常"
    for b in blocks:
        if feats.get(b["feature"]) == b["value"]:
            return "学習した回避条件"
    if any(p == pair for p, _ in open_positions):
        return "同ペア保有中"
    if len(open_positions) >= int(gates["max_positions"]):
        return "同時保有上限"
    mine = exposure(pair, d)
    for p, dd in open_positions:
        for ccy, sgn in exposure(p, dd).items():
            if mine.get(ccy) == sgn:
                return "通貨の偏り"
    return None


def event_exit_due(pair: str, t: pd.Timestamp, gates: dict, events) -> bool:
    if not events:
        return False
    t = pd.Timestamp(t)
    return events.find(currencies(pair), t, t + pd.Timedelta(minutes=gates["event_exit_before_min"])) is not None
