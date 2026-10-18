import numpy as np, pandas as pd
def make(pair="USD_JPY", n=8000, start=150.0, vol=0.0012, seed=1, freq="h", drift_regimes=True, t0="2022-01-03"):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(t0, periods=n, freq=freq, tz="UTC")
    drift = np.zeros(n)
    if drift_regimes:
        regime = np.repeat(rng.choice([-1, 0, 1], size=n // 400 + 1), 400)[:n]
        drift = regime * vol * 0.15
    r = rng.normal(drift, vol, n)
    close = start * np.exp(np.cumsum(r))
    open_ = np.r_[start, close[:-1]]
    wick = np.abs(rng.normal(0, vol * start * 0.6, n))
    high = np.maximum(open_, close) + wick
    low = np.minimum(open_, close) - np.abs(rng.normal(0, vol * start * 0.6, n))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close}, index=idx)


def fx_hours(df):
    """実際のFX市場と同じく、金曜21時〜日曜22時（UTC）を除く"""
    wd, h = df.index.dayofweek, df.index.hour
    closed = ((wd == 4) & (h >= 21)) | (wd == 5) | ((wd == 6) & (h < 22))
    return df[~closed]
