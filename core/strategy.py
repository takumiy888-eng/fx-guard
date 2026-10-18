"""売買シグナルと、学習に使う「その時の状況（特徴）」の計算。

戦略: 上位足(4時間足)と1時間足のトレンドが揃っている時だけ、短期EMAまでの押し目・戻りで入る。
バックテスト・実運用・学習のすべてがこのファイルの同じ関数を使う。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import adx, atr, ema

BAR = pd.Timedelta(hours=1)  # 1時間足で運用

DEFAULT_PARAMS = {
    "ema_fast": 20,        # 押し目の基準
    "ema_mid": 50,
    "ema_slow": 200,       # 1時間足の方向
    "h4_ema": 50,          # 4時間足の方向
    "adx_period": 14,
    "adx_min": 20.0,       # トレンド強度の下限
    "atr_period": 14,
    "sl_atr_mult": 1.5,    # 損切り幅 = ATR × これ
    "tp1_r": 1.0,          # ここで半分利確し、残りは建値ストップ
    "tp2_r": 2.5,          # 残り半分の利確
    "vol_min": 0.6,        # ボラ（平常比）がこれ未満なら見送り
    "vol_max": 2.0,        # これを超えたら見送り（荒れ相場）
    "max_hold_hours": 48,  # 伸びなければ撤退
}

# 学習で動かすパラメータと候補（1回の学習では隣の値にしか動かさない）
PARAM_GRID = {
    "sl_atr_mult": [1.2, 1.5, 1.8, 2.2],
    "tp2_r": [2.0, 2.5, 3.0, 3.5],
    "ema_fast": [13, 20, 30],
    "adx_min": [16.0, 20.0, 24.0, 28.0],
}

PARAM_LABELS = {
    "sl_atr_mult": "損切り幅（ATR×）",
    "tp2_r": "最終利確（R）",
    "ema_fast": "押し目の基準EMA",
    "adx_min": "トレンド強度の下限（ADX）",
}

FEATURE_LABELS = {
    "pair": "通貨ペア",
    "side": "売買",
    "hour": "時間帯",
    "weekday": "曜日",
    "vol": "ボラティリティ",
    "trend": "トレンド強度",
}
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]


def params_with_defaults(p: dict | None) -> dict:
    out = dict(DEFAULT_PARAMS)
    for k, v in (p or {}).items():
        if k in out:
            out[k] = type(out[k])(v)
    return out


def lookback_bars(p: dict) -> int:
    p = params_with_defaults(p)
    return max(p["ema_slow"] * 3, p["h4_ema"] * 4 * 3, 600) + 100


def prepare(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """1時間足（確定足のみ）から指標とシグナルを計算。各行は「その足が確定した時点」で分かる情報だけを使う。"""
    p = params_with_defaults(params)
    out = df[["open", "high", "low", "close"]].astype(float).copy()
    c = out["close"]
    out["atr"] = atr(out, p["atr_period"])
    out["adx"] = adx(out, p["adx_period"])
    fast, mid, slow = ema(c, p["ema_fast"]), ema(c, p["ema_mid"]), ema(c, p["ema_slow"])
    out["ema_fast"] = fast
    # 平常時に対するボラの大きさ（過去約1か月の中央値比）
    out["vol_ratio"] = out["atr"] / out["atr"].rolling(500, min_periods=200).median()

    # 4時間足: 確定した4時間足だけを使う（完成時刻 <= 1時間足の確定時刻）
    h4 = out.resample("4h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    h4e = ema(h4["close"], p["h4_ema"])
    h4_trend = pd.Series(
        np.where((h4["close"] > h4e) & (h4e.diff() > 0), 1, np.where((h4["close"] < h4e) & (h4e.diff() < 0), -1, 0)),
        index=h4.index + pd.Timedelta(hours=4),
    )
    close_times = out.index + BAR
    out["h4_trend"] = h4_trend.reindex(close_times, method="ffill").fillna(0).to_numpy()

    strong = out["adx"] >= p["adx_min"]
    up = (c > slow) & (fast > mid) & strong & (out["h4_trend"] == 1)
    dn = (c < slow) & (fast < mid) & strong & (out["h4_trend"] == -1)
    long_ = up & (out["low"] <= fast) & (c > fast) & (c > out["open"])
    short_ = dn & (out["high"] >= fast) & (c < fast) & (c < out["open"])
    sig = np.where(long_, 1, np.where(short_, -1, 0))
    warm = min(len(out), lookback_bars(p) // 2)
    sig[:warm] = 0
    sig[out["vol_ratio"].isna().to_numpy()] = 0
    out["signal"] = sig.astype(int)
    return out


def features(pair: str, side: int, close_time: pd.Timestamp, vol_ratio: float, adx_v: float) -> dict:
    """学習で使う「その時の状況」。値は人が読める日本語ラベル。"""
    jst = pd.Timestamp(close_time).tz_convert("Asia/Tokyo")
    h = (jst.hour // 3) * 3
    vol = "低い" if vol_ratio < 0.85 else ("高い" if vol_ratio > 1.25 else "普通")
    trend = "弱い" if adx_v < 25 else ("強い" if adx_v > 35 else "普通")
    return {
        "pair": pair,
        "side": "買い" if side == 1 else "売り",
        "hour": f"{h:02d}-{h + 3:02d}時",
        "weekday": WEEKDAYS[jst.weekday()],
        "vol": vol,
        "trend": trend,
    }
