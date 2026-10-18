"""1ポジションの決済ルール（バックテスト・影のトレード評価で共通）

- 損切り: ATR×倍率（常に設定）
- +1R で半分利確 → 残りは建値ストップ（勝ちかけを負けにしない）
- 残りは +tp2R で利確、伸びなければ時間切れで撤退
- 1本の足の中の順番が分からない時は、常に「不利な方が先に起きた」とみなす
"""
from __future__ import annotations


class Position:
    def __init__(self, pair: str, d: int, entry: float, sl_dist: float, tp1_r: float, tp2_r: float,
                 entry_time, max_hold_bars: int = 0, features: dict | None = None):
        self.pair, self.d, self.entry, self.sl_dist = pair, d, entry, sl_dist
        self.stop = entry - d * sl_dist
        self.tp1 = entry + d * sl_dist * tp1_r
        self.tp2 = entry + d * sl_dist * tp2_r
        self.entry_time = entry_time
        self.max_hold_bars = max_hold_bars
        self.features = features or {}
        self.open_frac = 1.0        # 残っている割合
        self.realized = 0.0         # 決済済み分の値幅（価格 × 割合）
        self.tp1_done = False
        self.bars = 0
        self.closed = False
        self.reason = ""
        self.exit_time = None

    # 値幅をRに換算
    @property
    def r(self) -> float:
        return self.realized / self.sl_dist if self.sl_dist > 0 else 0.0

    @property
    def risk_free(self) -> bool:
        return self.tp1_done

    def _fav(self, a: float, b: float) -> float:
        """a が b より有利なら正"""
        return (a - b) * self.d

    def _take(self, frac: float, px: float):
        self.realized += (px - self.entry) * self.d * frac
        self.open_frac -= frac

    def close(self, px: float, reason: str, t=None):
        if self.closed:
            return
        self._take(self.open_frac, px)
        self.open_frac = 0.0
        self.closed, self.reason, self.exit_time = True, reason, t

    def step(self, o: float, h: float, l: float, c: float, hs: float, slip: float, t=None) -> bool:
        """仲値の1本の足で状態を進める。hs=スプレッド半分。決済されたら True"""
        if self.closed:
            return True
        self.bars += 1
        d = self.d
        if d == 1:   # 買いは Bid で決済
            op, fav, adv, cl = o - hs, h - hs, l - hs, c - hs
        else:        # 売りは Ask で決済
            op, fav, adv, cl = o + hs, l + hs, h + hs, c + hs
        stop_name = "建値決済" if self.tp1_done else "損切り"

        if self._fav(op, self.stop) <= 0:                 # 窓で飛び越えた
            self.close(op - d * slip, stop_name + "(窓)", t)
            return True
        if self._fav(adv, self.stop) <= 0:
            self.close(self.stop - d * slip, stop_name, t)
            return True
        if not self.tp1_done and self._fav(fav, self.tp1) >= 0:
            self._take(0.5, self.tp1)
            self.tp1_done = True
            self.stop = self.entry
            if self._fav(adv, self.stop) <= 0:           # 同じ足で建値まで戻った可能性 → 戻ったとみなす
                self.close(self.stop - d * slip, "建値決済", t)
                return True
        if self._fav(fav, self.tp2) >= 0:
            self.close(self.tp2, "利確", t)
            return True
        if self.max_hold_bars and self.bars >= self.max_hold_bars:
            self.close(cl, "時間切れ", t)
            return True
        return False

    def mark(self, px_mid: float, hs: float) -> float:
        """現在値で全決済した場合のR"""
        px = px_mid - self.d * hs
        return (self.realized + (px - self.entry) * self.d * self.open_frac) / self.sl_dist
