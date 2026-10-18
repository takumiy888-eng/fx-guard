"""週次の自己学習（土曜・市場休み中）

1. ロールバック確認  … 前回変えたパラメータで実運用が悪化していたら元に戻す
2. 回避条件の学習    … 条件別（ペア・時間帯・曜日・ボラ・トレンド強度・売買）の成績から、
                        はっきり負けている条件を「入らない条件」に追加／改善したら解除
3. パラメータ調整    … 今の値の「隣」だけを試し、前半期間で良く・後半期間（未使用データ）でも
                        悪化しない場合だけ採用（過剰最適化の防止）
4. 影のトレード評価  … 見送ったシグナルを仮想で決済し、フィルターが役立っているか確認
5. 1万円リプレイ    … 最新ルールで1万円がどうなるか（固定0.25% と 段階リスク）
変更はすべて日本語で「思考ログ」に理由つきで残す。
"""
from __future__ import annotations

import itertools
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest as B
from .events import EventIndex, historical_events
from .position import Position
from .replay import replay
from .strategy import (BAR, DEFAULT_PARAMS, FEATURE_LABELS, PARAM_GRID, PARAM_LABELS,
                       params_with_defaults, prepare)

CRIT = {
    "block_min_n": 40,        # この回数以上の実績がある条件だけ判断する
    "block_z": 1.28,          # 「平均R + 1.28×標準誤差 < 0」＝90%の確からしさで負けている
    "unblock_avg_r": 0.02,
    "max_blocks": 5,
    "min_coverage": 0.6,      # 回避条件で取引数を4割以上減らさない
    "is_min_n": 60,
    "oos_min_n": 20,
    "oos_min_pf": 1.1,
    "is_improve": 1.10,       # 前半の評価値が1割以上良くなる時だけ変える
    "rollback_min_trades": 12,
    "rollback_avg_r": -0.10,
}


def _jp_cond(b: dict) -> str:
    return f"{FEATURE_LABELS.get(b['feature'], b['feature'])}＝{b['value']}"


class Learner:
    def __init__(self, db, store, client_factory, notify=lambda m: None, base_dir: Path | None = None, clock=None):
        self.db, self.store, self.client_factory, self.notify = db, store, client_factory, notify
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.base = Path(base_dir or store.base)
        self.progress = None  # 過去データ取得の進み具合を受け取る関数（エンジンが設定）

    # ------------------------------------------------------------ データ
    def history(self, cfg: dict, client) -> dict[str, pd.DataFrame]:
        """学習用の1時間足（仲値）。保存済みの分は再利用し、足りない分だけGMOから取得する"""
        end = self.clock()
        start = pd.Timestamp(end) - pd.Timedelta(days=int(365 * float(cfg["history_years"])))
        out = {}
        for pair in cfg["pairs"]:
            try:
                df = client.history_df(pair, "H1", start, end, progress=self.progress)
            except Exception as e:  # noqa: BLE001
                df = client.cached_df(pair) if hasattr(client, "cached_df") else pd.DataFrame()
                if df is None or df.empty:
                    self.db.log("system", f"{pair} の価格データが取得できなかったため、学習から外しました（{e}）")
                    continue
                self.db.log("system", f"{pair} の価格データ更新に失敗。保存済みデータで学習します（{e}）")
            out[pair] = df[df.index >= start][["open", "high", "low", "close"]]
        return out

    def events_for(self, start, end) -> EventIndex:
        evs = historical_events(pd.Timestamp(start).to_pydatetime(), pd.Timestamp(end).to_pydatetime())
        for r in self.db.events_between(pd.Timestamp(start).to_pydatetime(), pd.Timestamp(end).to_pydatetime()):
            evs.append((pd.Timestamp(r["ts"]), r["ccy"], r["title"]))
        return EventIndex(evs)

    # ------------------------------------------------------------ 本体
    def run(self, reason: str = "定期学習", data: dict | None = None, client=None) -> dict:
        cfg = self.store.load()
        wall0 = time.time()
        t0 = self.clock()
        self.db.log("learn", f"🧠 学習開始（{reason}）")
        if data is None:
            data = self.history(cfg, client or self.client_factory(cfg))
        if self.progress:
            try:
                self.progress("学習", None, 1, 1)
            except Exception:  # noqa: BLE001
                pass
        data = {p: d for p, d in data.items() if len(d) > 1000}
        if not data:
            raise RuntimeError("学習に使える価格データがありません")
        start = min(d.index[0] for d in data.values())
        end = max(d.index[-1] for d in data.values()) + BAR
        events = self.events_for(start, end)

        params = params_with_defaults(self.db.kv_get("params", {}))
        blocks = self.db.kv_get("blocks", [])
        hist = self.db.kv_get("params_history", [])
        version = int(self.db.kv_get("params_version", 1))
        live = [t for t in self.db.trades("closed") if t.get("r") is not None]
        notes: list[str] = []
        prep_cache: dict = {}

        def prep(p):
            key = (p["ema_fast"], p["adx_min"])
            if key not in prep_cache:
                prep_cache[key] = {pair: prepare(df, p) for pair, df in data.items()}
            return prep_cache[key]

        # 1) ロールバック確認
        rolled_back = False
        if hist:
            last = hist[-1]
            since = [t["r"] for t in live if t["open_time"] >= last["adopted_at"]]
            if (len(hist) >= 2 and len(since) >= CRIT["rollback_min_trades"]
                    and np.mean(since) < CRIT["rollback_avg_r"]
                    and np.mean(since) < float(last.get("pre_live_avg_r", 0))):
                prev = hist[-2]["params"]
                params = params_with_defaults(prev)
                version += 1
                hist.append({"version": version, "params": {k: params[k] for k in PARAM_GRID},
                             "adopted_at": t0.isoformat(), "pre_live_avg_r": float(np.mean(since)),
                             "note": "ロールバック"})
                rolled_back = True
                notes.append(f"↩ 前回の変更後、実運用 {len(since)}回の平均が {np.mean(since):+.2f}R と悪化したため、"
                             f"パラメータを1つ前に戻しました")

        # 2) 回避条件の学習（前半で判断 → 後半で検証）
        base = B.run(data, params, events=events, blocks=[], prepared=prep(params))
        is_all, oos_all, cut = B.split(base["trades"], start, end)
        cond = self._conditions(is_all + [self._live_as_trade(t) for t in live])
        new_blocks = self._choose_blocks(cond, blocks, len(is_all))
        if self._block_key(new_blocks) != self._block_key(blocks):
            cur_oos = B.stats(self._apply_blocks(oos_all, blocks))
            new_oos = B.stats(self._apply_blocks(oos_all, new_blocks))
            if new_oos["avg_r"] + 0.02 >= cur_oos["avg_r"] and new_oos["n"] >= CRIT["oos_min_n"] // 2:
                added = [b for b in new_blocks if self._bk(b) not in {self._bk(x) for x in blocks}]
                removed = [b for b in blocks if self._bk(b) not in {self._bk(x) for x in new_blocks}]
                for b in added:
                    notes.append(f"🚫 回避条件を追加：{_jp_cond(b)}（{b['n']}回・平均{b['avg_r']:+.2f}R。"
                                 f"後半期間でも平均 {cur_oos['avg_r']:+.2f}R → {new_oos['avg_r']:+.2f}R に改善）")
                for b in removed:
                    c = cond.get((b["feature"], b["value"]), {})
                    notes.append(f"✅ 回避条件を解除：{_jp_cond(b)}（最近は平均{c.get('avg_r', 0):+.2f}Rで改善）")
                blocks = new_blocks
            else:
                notes.append(f"回避条件の候補はあったが、後半期間で効果を確認できなかったため見送り"
                             f"（平均 {cur_oos['avg_r']:+.2f}R → {new_oos['avg_r']:+.2f}R）")

        # 3) パラメータ調整（隣の値だけ・前半で選んで後半で確認）
        search_rows = []
        cur_res = B.run(data, params, events=events, blocks=blocks, prepared=prep(params))
        cur_is, cur_oos, _ = B.split(cur_res["trades"], start, end)
        cur_is_s, cur_oos_s = B.stats(cur_is), B.stats(cur_oos)
        best = None
        if not rolled_back:
            for combo in self._neighbors(params):
                if all(combo[k] == params[k] for k in PARAM_GRID):
                    continue
                res = B.run(data, combo, events=events, blocks=blocks, prepared=prep(combo))
                i_tr, o_tr, _ = B.split(res["trades"], start, end)
                si, so = B.stats(i_tr), B.stats(o_tr)
                ok = (si["n"] >= CRIT["is_min_n"] and so["n"] >= CRIT["oos_min_n"]
                      and so["pf"] >= CRIT["oos_min_pf"]
                      and si["score"] >= cur_is_s["score"] * CRIT["is_improve"] + 1e-9
                      and so["score"] >= cur_oos_s["score"]
                      and so["max_dd_r"] <= cur_oos_s["max_dd_r"] * 1.1 + 0.5)
                search_rows.append({"params": {k: combo[k] for k in PARAM_GRID}, "is": si, "oos": so, "ok": ok})
                if ok and (best is None or si["score"] > best[1]["score"]):
                    best = (combo, si, so)
            if best:
                combo, si, so = best
                diff = [f"{PARAM_LABELS[k]} {params[k]}→{combo[k]}" for k in PARAM_GRID if combo[k] != params[k]]
                live_avg = float(np.mean([t["r"] for t in live[-30:]])) if live else 0.0
                version += 1
                hist.append({"version": version, "params": {k: combo[k] for k in PARAM_GRID},
                             "adopted_at": t0.isoformat(), "pre_live_avg_r": live_avg, "note": "週次調整"})
                notes.append(f"🔧 パラメータ変更：{'、'.join(diff)}（評価値 前半 {cur_is_s['score']:.2f}→{si['score']:.2f}、"
                             f"後半 {cur_oos_s['score']:.2f}→{so['score']:.2f}、後半の最大DD {so['max_dd_r']:.1f}R）")
                params = combo
            else:
                tried = len(search_rows)
                notes.append(f"パラメータは据え置き（{tried}通り試したが、前半・後半の両方で今より良い組み合わせなし）")
        if not hist:
            hist.append({"version": version, "params": {k: params[k] for k in PARAM_GRID},
                         "adopted_at": t0.isoformat(), "pre_live_avg_r": 0.0, "note": "初期値"})

        # 4) 最終評価と1万円リプレイ
        final = B.run(data, params, events=events, blocks=blocks, prepared=prep(params))
        f_is, f_oos, _ = B.split(final["trades"], start, end)
        s_all, s_is, s_oos = B.stats(final["trades"]), B.stats(f_is), B.stats(f_oos)
        capital = float(cfg["virtual_capital"])
        rep_fixed = replay(final["trades"], capital, ladder=False, oos_pf=s_oos["pf"])
        rep_ladder = replay(final["trades"], capital, ladder=True, oos_pf=s_oos["pf"])
        y1 = [t for t in final["trades"] if t["entry_time"] >= end - pd.Timedelta(days=365)]
        rep_y1 = replay(y1, capital, ladder=True, oos_pf=s_oos["pf"])

        # 5) 影のトレード
        shadow_stats = self._eval_shadows(data, params, events)

        # 保存
        self.db.kv_set("params", {k: params[k] for k in PARAM_GRID})
        self.db.kv_set("params_version", version)
        self.db.kv_set("params_history", hist[-30:])
        self.db.kv_set("blocks", blocks)
        cond_view = sorted(
            [{"feature": FEATURE_LABELS[f], "key": f, "value": v, **s} for (f, v), s in cond.items()],
            key=lambda x: x["avg_r"])
        summary = {
            "ran_at": t0.isoformat(timespec="seconds"),
            "reason": reason,
            "seconds": round(time.time() - wall0, 1),
            "period": [str(start)[:10], str(end)[:10]],
            "cut": str(cut)[:10],
            "pairs": list(data),
            "params": {k: params[k] for k in PARAM_GRID},
            "param_labels": PARAM_LABELS,
            "version": version,
            "blocks": blocks,
            "stats": {"all": s_all, "is": s_is, "oos": s_oos},
            "skipped": final["skipped"],
            "conditions": cond_view,
            "search": sorted(search_rows, key=lambda r: -r["is"]["score"])[:15],
            "replay": {"fixed": rep_fixed, "ladder": rep_ladder, "last12m": rep_y1},
            "shadows": shadow_stats,
            "notes": notes,
        }
        self.db.kv_set("learning", summary)
        for n in notes:
            self.db.log("learn", n)
        msg = (f"🧠 学習完了：後半期間 PF {s_oos['pf']:.2f}・平均{s_oos['avg_r']:+.2f}R・最大DD {s_oos['max_dd_r']:.1f}R。"
               f"1万円リプレイ（段階リスク）{rep_ladder['final']:,.0f}円・月平均{rep_ladder['monthly_avg_pct']:+.1f}%・"
               f"最大DD {rep_ladder['max_dd_pct']:.1f}%")
        self.db.log("learn", msg)
        self.notify(msg + ("\n" + "\n".join(notes) if notes else ""))
        return summary

    # ------------------------------------------------------------ 部品
    @staticmethod
    def _live_as_trade(t: dict) -> dict:
        return {"r": float(t["r"]), "features": t["features"], "entry_time": pd.Timestamp(t["open_time"])}

    @staticmethod
    def _conditions(trades: list[dict]) -> dict:
        groups: dict = {}
        for t in trades:
            for f, v in (t.get("features") or {}).items():
                groups.setdefault((f, v), []).append(t["r"])
        out = {}
        for k, rs in groups.items():
            a = np.array(rs, dtype=float)
            n = len(a)
            se = a.std(ddof=1) / math.sqrt(n) if n > 1 else 9.9
            out[k] = {"n": n, "avg_r": float(a.mean()), "ub": float(a.mean() + CRIT["block_z"] * se),
                      "win_rate": float((a > 0).mean() * 100), "total_r": float(a.sum())}
        return out

    def _choose_blocks(self, cond: dict, current: list[dict], n_total: int) -> list[dict]:
        keep = []
        for b in current:
            c = cond.get((b["feature"], b["value"]))
            if c and c["n"] >= CRIT["block_min_n"] and c["avg_r"] > CRIT["unblock_avg_r"]:
                continue  # 改善したので解除
            keep.append(b)
        cands = sorted(
            [(k, v) for k, v in cond.items() if v["n"] >= CRIT["block_min_n"] and v["ub"] < 0],
            key=lambda kv: kv[1]["ub"])
        chosen = list(keep)
        removed_n = sum(cond.get((b["feature"], b["value"]), {}).get("n", 0) for b in chosen)
        for (f, v), s in cands:
            if any(b["feature"] == f and b["value"] == v for b in chosen):
                continue
            if f == "side":  # 「買いだけ/売りだけ」にはしない（相場の向きに依存しすぎるため）
                continue
            if len(chosen) >= CRIT["max_blocks"]:
                break
            if n_total and (removed_n + s["n"]) / n_total > 1 - CRIT["min_coverage"]:
                continue
            chosen.append({"feature": f, "value": v, "n": s["n"], "avg_r": round(s["avg_r"], 3)})
            removed_n += s["n"]
        return chosen

    @staticmethod
    def _bk(b):
        return (b["feature"], b["value"])

    def _block_key(self, bl):
        return sorted(self._bk(b) for b in bl)

    @staticmethod
    def _apply_blocks(trades, blocks):
        keys = {(b["feature"], b["value"]) for b in blocks}
        return [t for t in trades if not any((f, v) in keys for f, v in t["features"].items())]

    @staticmethod
    def _neighbors(params: dict):
        opts = []
        for k, grid in PARAM_GRID.items():
            i = int(np.argmin([abs(g - params[k]) for g in grid]))
            opts.append([grid[j] for j in (i - 1, i, i + 1) if 0 <= j < len(grid)])
        for vals in itertools.product(*opts):
            p = dict(params)
            p.update(dict(zip(PARAM_GRID, vals)))
            yield params_with_defaults(p)

    def _eval_shadows(self, data, params, events) -> dict:
        p = params_with_defaults(params)
        max_bars = int(p["max_hold_hours"])
        for s in self.db.shadows(evaluated=False):
            df = data.get(s["pair"])
            if df is None:
                continue
            st = pd.Timestamp(s["signal_time"])
            after = df[df.index >= st]
            if len(after) < max_bars + 1:
                continue  # まだ結果が出ていない
            first = after.iloc[0]
            hs = B.DEFAULT_SPREAD.get(s["pair"], 1.0) * B.pip(s["pair"]) / 2
            entry = first.open + s["side"] * hs
            pos = Position(s["pair"], s["side"], entry, s["sl_dist"], p["tp1_r"], p["tp2_r"], st, max_bars)
            for ts, b in after.iterrows():
                if pos.step(b.open, b.high, b.low, b.close, hs, 0.2 * B.pip(s["pair"]), ts + BAR):
                    break
            if not pos.closed:
                pos.close(after.iloc[-1].close - s["side"] * hs, "期限", after.index[-1])
            self.db.x("UPDATE shadows SET evaluated=1, r=?, outcome=? WHERE id=?", (float(pos.r), pos.reason, s["id"]))
        rows = self.db.shadows(evaluated=True)
        out = {}
        for r in rows:
            out.setdefault(r["reason"], []).append(r["r"])
        live = [t["r"] for t in self.db.trades("closed") if t.get("r") is not None]
        return {
            "by_reason": [{"reason": k, "n": len(v), "avg_r": float(np.mean(v)), "total_r": float(np.sum(v))}
                          for k, v in sorted(out.items(), key=lambda kv: -len(kv[1]))],
            "taken_avg_r": float(np.mean(live)) if live else None,
            "taken_n": len(live),
        }
