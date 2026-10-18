"""資金管理（方針B：0.25%から始め、実績が出た分だけ段階的にリスクを上げる）

- 上げる時は慎重（十分な取引数と安定した成績が条件・週1回・1段階ずつ）
- 下げる時は即座（DD・連敗・週次ストップで即降格）
- 何重ものストップ（1日・1週・1か月・最大DD）
"""
from __future__ import annotations

from dataclasses import dataclass

RISK_LEVELS = [0.25, 0.4, 0.6, 0.85, 1.25]   # 1回の損失（口座の%）
LOSS_R = -0.3                                  # これより悪い決済を「負け」として連敗に数える

RULES = {
    "streak_half": 3,          # 3連敗でリスク半分（勝つまで）
    "streak_demote": 4,        # 4連敗で1段階降格
    "streak_cooldown": 5,      # 5連敗で24時間休止
    "dd_demote_pct": 4.0,      # 最高値から-4%で最低段階へ
    "dd_halt_pct": 8.0,        # -8%で完全停止（週次学習で再検証に合格すれば最低段階で再開）
    "promote_min_trades": 15,  # 昇格に必要な取引数（前回の段階変更以降）
    "promote_min_pf": 1.3,
    "promote_min_avg_r": 0.15,
    "promote_max_dd_pct": 3.0,
    "promote_min_weeks": 2,    # 昇格の最短間隔
    "backtest_min_pf": 1.2,    # 直近バックテスト（後半期間）のPFもこれ以上
}


def limits(level: int) -> dict:
    """1日・1週・1か月の損失ストップ（%）。リスク段階に応じて少しだけ広げる"""
    base = RISK_LEVELS[level]
    return {"day": max(1.0, 1.5 * base), "week": max(2.0, 3.0 * base), "month": max(4.0, 5.0 * base)}


def effective_risk(level: int, losing_streak: int) -> float:
    r = RISK_LEVELS[level]
    if losing_streak >= RULES["streak_half"]:
        r *= 0.5
    return r


def promotion_check(trades_r: list[float], dd_pct_since: float, weeks_since: float,
                    last4w_pnl: float, backtest_oos_pf: float) -> tuple[bool, str]:
    """昇格してよいか（理由つき）"""
    n = len(trades_r)
    R = RULES
    if n < R["promote_min_trades"]:
        return False, f"取引数 {n}/{R['promote_min_trades']} 回で実績不足"
    gw = sum(x for x in trades_r if x > 0)
    gl = -sum(x for x in trades_r if x < 0)
    pf = gw / gl if gl > 0 else 99.0
    avg = sum(trades_r) / n
    if weeks_since < R["promote_min_weeks"]:
        return False, f"前回の変更から{weeks_since:.1f}週しか経っていない"
    if pf < R["promote_min_pf"]:
        return False, f"PF {pf:.2f} が基準 {R['promote_min_pf']} 未満"
    if avg < R["promote_min_avg_r"]:
        return False, f"平均 {avg:.2f}R が基準 {R['promote_min_avg_r']}R 未満"
    if dd_pct_since > R["promote_max_dd_pct"]:
        return False, f"この段階での最大DD {dd_pct_since:.1f}% が基準 {R['promote_max_dd_pct']}% 超"
    if last4w_pnl <= 0:
        return False, "直近4週の損益がマイナス"
    if backtest_oos_pf < R["backtest_min_pf"]:
        return False, f"最新バックテスト後半のPF {backtest_oos_pf:.2f} が基準未満"
    return True, f"{n}回・PF {pf:.2f}・平均{avg:.2f}R・DD {dd_pct_since:.1f}%で基準をすべて満たした"


def calc_units(equity: float, risk_pct: float, sl_distance: float, quote_to_account: float,
               price: float, max_leverage: float = 10.0) -> int:
    if equity <= 0 or sl_distance <= 0 or quote_to_account <= 0 or price <= 0:
        return 0
    units = equity * risk_pct / 100.0 / (sl_distance * quote_to_account)
    cap = equity * max_leverage / (price * quote_to_account)
    return int(max(0, min(units, cap)))


@dataclass
class LadderState:
    """リスク段階と各種ストップの状態（実運用とリプレイで共通）"""
    equity: float
    peak: float
    level: int = 0
    streak: int = 0
    level_since_equity: float = 0.0
    level_peak: float = 0.0
    level_max_dd: float = 0.0
    full_halt: bool = False

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "LadderState":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})

    @property
    def dd_pct(self) -> float:
        return (1 - self.equity / self.peak) * 100 if self.peak > 0 else 0.0

    def set_level(self, lv: int):
        self.level = max(0, min(lv, len(RISK_LEVELS) - 1))
        self.level_since_equity = self.level_peak = self.equity
        self.level_max_dd = 0.0

    def on_close(self, pnl: float, r: float) -> list[str]:
        """決済ごとに更新。発生したイベント（日本語）を返す"""
        msgs = []
        self.equity += pnl
        self.peak = max(self.peak, self.equity)
        self.level_peak = max(self.level_peak, self.equity)
        if self.level_peak > 0:
            self.level_max_dd = max(self.level_max_dd, (1 - self.equity / self.level_peak) * 100)
        # 「負け」は -0.3R より悪い決済だけ。ほぼ±0の撤退（指標前・時間切れなど）は連敗に数えずリセットもしない
        if r < LOSS_R:
            self.streak += 1
        elif r > 0.05:
            self.streak = 0
        if self.streak == RULES["streak_half"]:
            msgs.append(f"{self.streak}連敗：次から勝つまでリスクを半分にします")
        if self.streak == RULES["streak_demote"] and self.level > 0:
            self.set_level(self.level - 1)
            msgs.append(f"4連敗：リスク段階を {RISK_LEVELS[self.level]}% に下げました")
        if self.dd_pct >= RULES["dd_demote_pct"] and self.level > 0:
            self.set_level(0)
            msgs.append(f"最高値から-{self.dd_pct:.1f}%：リスクを最低の {RISK_LEVELS[0]}% に戻しました")
        if self.dd_pct >= RULES["dd_halt_pct"] and not self.full_halt:
            self.full_halt = True
            msgs.append(f"最高値から-{self.dd_pct:.1f}%：完全停止。週次学習で再検証します")
        return msgs
