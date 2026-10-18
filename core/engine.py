"""常駐Bot本体（VPSで24時間動かす）

ループ（既定15秒ごと）:
  画面からの指示 → 設定確認 → 初回/週次学習 → 経済指標の更新 → 売買チェック
売買チェック:
  決済の検知と仮想資金の更新 → 保有管理（半分利確・建値ストップ・時間切れ・指標前撤退・週末決済）
  → 各種ストップ判定 → 新しい足の確定でシグナル → 入らない理由 → 発注 or 影のトレード記録
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from .config import Store
from .db import DB
from .events import EventIndex, LiveCalendar, historical_events
from .gates import DEFAULT_GATES, check_entry, currencies, event_exit_due, weekend_close_due
from .learner import Learner
from .gmo import MarketError
from .paper import PaperAccount
from .risk import RISK_LEVELS, RULES, LadderState, calc_units, effective_risk, limits, promotion_check
from .strategy import BAR, features, lookback_bars, params_with_defaults, prepare

TAG = "fxguard"
EDGE = {"min_trades": 30, "min_avg_r": 0.05, "min_pf": 1.15}   # 取引してよい最低条件（学習の後半期間）
JST = timezone(timedelta(hours=9))


class Engine:
    def __init__(self, base_dir: Path, client_factory=None, calendar_fetch=None, clock=None):
        self.base = Path(base_dir)
        self.store = Store(self.base)
        self.db = DB(self.base / "data" / "fxguard.db")
        self.client_factory = client_factory or (lambda cfg: PaperAccount(self.db, self.base, clock=self.clock))
        self.calendar = LiveCalendar(self.db)
        self.calendar_fetch = calendar_fetch
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.learner = Learner(self.db, self.store, self.client_factory, self.notify, self.base, clock=self.clock)
        self.learner.progress = self._progress
        self.client = None
        self.pairs: list[str] = []
        self._cfg: dict = {}
        self._progress_at = None
        self.client_key = None
        self.meta: dict = {}
        self.last_processed: dict[str, pd.Timestamp] = {}
        self.events = EventIndex([])
        self.last_error = None
        self.last_error_at = None
        self.started_at = self.clock()
        self._ensure_state()

    # ------------------------------------------------------------ 状態
    def _ensure_state(self):
        cfg = self.store.load()
        if self.db.kv_get("ladder") is None:
            cap = float(cfg["virtual_capital"])
            st = LadderState(equity=cap, peak=cap)
            st.set_level(0)
            self.db.kv_set("ladder", st.to_dict())
            self.db.kv_set("level_changed_at", self.clock().isoformat())
            self.db.kv_set("virtual_started_at", self.clock().isoformat())
            self.db.log("system", f"仮想資金 {cap:,.0f}円 でスタート（リスク {RISK_LEVELS[0]}%）")

    @property
    def ladder(self) -> LadderState:
        return LadderState.from_dict(self.db.kv_get("ladder"))

    def save_ladder(self, st: LadderState):
        self.db.kv_set("ladder", st.to_dict())

    def notify(self, msg: str):
        url = self.store.load().get("discord_webhook") or ""
        if not url:
            return
        try:
            requests.post(url, json={"content": msg[:1900]}, timeout=8)
        except requests.RequestException:
            pass

    def _err(self, msg: str):
        now = self.clock()
        if msg != self.last_error or not self.last_error_at or now - self.last_error_at > timedelta(minutes=30):
            self.db.log("error", msg)
            if self.last_error != msg:
                self.notify(f"⚠ {msg}")
        self.last_error, self.last_error_at = msg, now

    # ------------------------------------------------------------ メインループ
    def run_forever(self):
        self.db.log("system", "Bot起動")
        while True:
            try:
                self.step()
            except Exception as e:  # noqa: BLE001  何があっても止まらない
                self._err(f"{type(e).__name__}: {e}")
                traceback.print_exc()
            time.sleep(max(5, int(self.store.load().get("poll_seconds", 15))))

    def step(self):
        now = self.clock()
        cfg = self.store.load()
        self._cfg = cfg
        self._commands(cfg)
        self._client(cfg)
        if self.db.kv_get("learning") is None:
            last = self.db.kv_get("last_learn_at")
            if not last or now - datetime.fromisoformat(last) > timedelta(hours=1):
                self._status(cfg, state="初回準備中（過去データ取得・学習）", now=now)
                self._learn("初回学習")
            if self.db.kv_get("learning") is None:  # 初回学習が終わるまでは売買しない
                self._status(cfg, state="初回学習の再試行待ち", now=now)
                return
        elif not self.db.kv_get("ask_ready") and hasattr(self.client, "ask_ready"):
            # 買値（ASK）の過去データがまだ無ければ、取得して実際のスプレッドで学習し直す（初回だけ・約30分）
            need_from = now - timedelta(days=int(365 * float(cfg["history_years"])))
            if self.client.ask_ready(self.pairs, need_from):
                self.db.kv_set("ask_ready", True)
            else:
                last = self.db.kv_get("ask_attempt_at")
                if not last or now - datetime.fromisoformat(last) > timedelta(hours=6):
                    self.db.kv_set("ask_attempt_at", now.isoformat())
                    self.db.log("system", "買値の過去データを取得して、実際のスプレッドで学習し直します（30分ほど）")
                    before = (self.db.kv_get("learning") or {}).get("ran_at")
                    self._learn("買値データの取得と再学習")
                    learned = (self.db.kv_get("learning") or {}).get("ran_at") != before
                    if learned or self.client.ask_ready(self.pairs, need_from):
                        # 学習まで終われば完了扱い（取れなかった日は週次学習のたびに自動で補う）
                        self.db.kv_set("ask_ready", True)
                        self.db.log("system", "買値の過去データがそろいました。以後は実際の売値・買値で検証します")
        elif self._learn_due(cfg, now):
            self._status(cfg, state="週次学習中", now=now)
            self._learn("週次学習")
        err = self.calendar.update(now, self.calendar_fetch)
        if err:
            self._err(err)
        self._rebuild_events(now)
        self.tick(cfg, now)
        self.last_error = None

    def _client(self, cfg):
        key = tuple(cfg["pairs"])
        if key != self.client_key or self.client is None:
            client = self.client_factory(cfg)
            meta = client.instruments(cfg["pairs"])
            missing = [p for p in cfg["pairs"] if p not in meta]
            if missing:
                self.db.log("system", f"GMOで扱っていない通貨ペアを除外しました: {', '.join(missing)}")
            if not meta:
                raise MarketError("取引できる通貨ペアがありません（設定を確認してください）")
            self.client, self.meta, self.client_key = client, meta, key
            self.pairs = [p for p in cfg["pairs"] if p in meta]
            self.last_processed = {}
            self.db.log("system", f"GMOコインの公開レートに接続（仮想売買 / {', '.join(self.pairs)}）")

    def _progress(self, pair, d, i, n):
        """初回の過去データ取得の進み具合を画面に出す（見張り番にも生きていることを伝える）"""
        now = self.clock()
        if pair == "学習":
            first = self.db.kv_get("learning") is None
            self._status(self._cfg or self.store.load(),
                         state="初回準備中（学習・数分かかります）" if first else "週次学習中", now=now)
            return
        if self._progress_at and (now - self._progress_at).total_seconds() < 5 and i < n:
            return
        self._progress_at = now
        k = self.pairs.index(pair) if pair in self.pairs else 0
        total = max(len(self.pairs), 1)
        pct = int((k + i / max(n, 1)) / total * 100)
        self._status(self._cfg or self.store.load(),
                     state=f"過去データ取得中 {pct}%（{pair} {d:%Y-%m}）", now=now)

    def _rebuild_events(self, now):
        rows = self.db.events_between(now - timedelta(days=2), now + timedelta(days=10))
        evs = [(pd.Timestamp(r["ts"]), r["ccy"], r["title"]) for r in rows]
        evs += historical_events(now - timedelta(days=2), now + timedelta(days=10))
        self.events = EventIndex(evs)

    def _learn_due(self, cfg, now) -> bool:
        j = now.astimezone(JST)
        last = self.db.kv_get("last_learn_at")
        if last and now - datetime.fromisoformat(last) > timedelta(days=7, hours=12):
            return True  # 土曜にPCが止まっていた週は、次に動いた時に学習する
        if j.weekday() != int(cfg["learn_weekday"]) or j.hour < int(cfg["learn_hour_jst"]):
            return False
        return not last or datetime.fromisoformat(last).astimezone(JST).date() != j.date()

    def _learn(self, reason: str):
        now = self.clock()
        self.db.kv_set("last_learn_at", now.isoformat())  # 失敗しても同じ日に何度も繰り返さない
        if reason != "初回学習":
            self._status(self._cfg or self.store.load(), state=f"{reason}中", now=now)
        try:
            summary = self.learner.run(reason, client=self.client)
        except Exception as e:  # noqa: BLE001
            self._err(f"学習に失敗（前回の学習結果で運用継続）: {e}")
            return
        self._weekly_risk_review(summary, now)

    # ------------------------------------------------------------ 優位性の確認
    def _edge_ok(self) -> bool:
        """最新の学習で「勝てる根拠」が確認できている時だけ新規エントリーする（負けているルールでは取引しない）"""
        L = self.db.kv_get("learning") or {}
        st = L.get("stats") or {}
        oos, whole = st.get("oos") or {}, st.get("all") or {}
        ok = (oos.get("n", 0) >= EDGE["min_trades"] and oos.get("avg_r", 0) >= EDGE["min_avg_r"]
              and oos.get("pf", 0) >= EDGE["min_pf"] and whole.get("avg_r", 0) > 0)
        prev = self.db.kv_get("edge_ok")
        if prev is None or bool(prev) != ok:
            self.db.kv_set("edge_ok", ok)
            if ok:
                msg = (f"✅ 検証で優位性を確認（後半期間 {oos.get('n', 0)}回・PF {oos.get('pf', 0):.2f}・"
                       f"平均{oos.get('avg_r', 0):+.2f}R）。条件が揃えば取引します")
            else:
                msg = (f"⏸ 検証で勝てる根拠が確認できないため、新規の取引を止めて様子見します（後半期間 PF "
                       f"{oos.get('pf', 0):.2f}・平均{oos.get('avg_r', 0):+.2f}R）。シグナルは影のトレードとして記録し、"
                       f"毎週の学習で再判定します")
            self.db.log("risk", msg)
            self.notify(msg)
        return ok

    # ------------------------------------------------------------ 週次のリスク段階見直し
    def _weekly_risk_review(self, summary: dict, now: datetime):
        st = self.ladder
        oos = summary["stats"]["oos"]
        if st.full_halt:
            if oos["pf"] >= RULES["backtest_min_pf"] and oos["avg_r"] > 0:
                st.full_halt = False
                st.peak = st.equity
                st.set_level(0)
                self.db.kv_set("level_changed_at", now.isoformat())
                msg = f"▶ 再検証に合格（後半PF {oos['pf']:.2f}）。最低リスク {RISK_LEVELS[0]}% で運用を再開します"
            else:
                msg = f"⏸ 再検証で基準未達（後半PF {oos['pf']:.2f}）。停止を継続します"
            self.db.log("risk", msg)
            self.notify(msg)
            self.save_ladder(st)
            return
        if st.level >= len(RISK_LEVELS) - 1:
            self.db.log("risk", f"リスクは上限の {RISK_LEVELS[-1]}% です")
            return
        changed = pd.Timestamp(self.db.kv_get("level_changed_at"))
        closed = [t for t in self.db.trades("closed") if t["close_time"] and pd.Timestamp(t["close_time"]) >= changed]
        rs = [float(t["r"]) for t in closed]
        last4 = sum(float(t["pnl_jpy"]) for t in self.db.trades("closed")
                    if t["close_time"] and pd.Timestamp(t["close_time"]) >= pd.Timestamp(now) - pd.Timedelta(days=28))
        weeks = (pd.Timestamp(now) - changed).days / 7
        ok, why = promotion_check(rs, st.level_max_dd, weeks, last4, oos["pf"])
        if ok:
            st.set_level(st.level + 1)
            self.db.kv_set("level_changed_at", now.isoformat())
            msg = f"⬆ リスクを {RISK_LEVELS[st.level - 1]}% → {RISK_LEVELS[st.level]}% に引き上げ（{why}）"
            self.notify(msg)
        else:
            msg = f"リスクは {RISK_LEVELS[st.level]}% のまま（{why}）"
        self.db.log("risk", msg)
        self.save_ladder(st)

    # ------------------------------------------------------------ 画面からの指示
    def _commands(self, cfg):
        for c in self.db.commands_pending():
            cmd, res = c["cmd"], "ok"
            try:
                if cmd == "pause":
                    self.db.kv_set("paused", True)
                    self.db.log("system", "⏸ 画面から一時停止（新規エントリーなし・保有は管理継続）")
                elif cmd == "resume":
                    self.db.kv_set("paused", False)
                    self.db.log("system", "▶ 画面から再開")
                elif cmd == "close_all":
                    n = self._close_all("画面から全決済")
                    res = f"{n}件"
                elif cmd == "learn_now":
                    self._client(cfg)
                    self._learn("手動実行")
                elif cmd == "reset_virtual":
                    cap = float(cfg["virtual_capital"])
                    st = LadderState(equity=cap, peak=cap)
                    st.set_level(0)
                    self.save_ladder(st)
                    self.db.kv_set("level_changed_at", self.clock().isoformat())
                    self.db.kv_set("virtual_started_at", self.clock().isoformat())
                    self.db.kv_set("periods", None)
                    self.db.log("system", f"仮想資金を {cap:,.0f}円 にリセット")
            except Exception as e:  # noqa: BLE001
                res = f"失敗: {e}"
                self._err(f"指示「{cmd}」の実行に失敗: {e}")
            self.db.command_done(c["id"], res)

    def _close_all(self, reason: str) -> int:
        if not self.client:
            return 0
        n = 0
        for t in self.client.open_trades():
            if (t.get("clientExtensions") or {}).get("tag") == TAG:
                if self._close(t["id"], reason):
                    n += 1
        return n

    def _close(self, tid: str, reason: str) -> bool:
        try:
            self.client.close_trade(tid)
        except MarketError as e:
            self._err(f"決済できませんでした（次のチェックで再試行）: {e}")
            return False
        self.db.trade_update(tid, reason=reason)
        return True

    # ------------------------------------------------------------ 売買チェック
    def tick(self, cfg: dict, now: datetime):
        client = self.client
        p = params_with_defaults(self.db.kv_get("params", {}))
        blocks = self.db.kv_get("blocks", [])
        g = dict(DEFAULT_GATES)
        pairs = self.pairs
        quotes = {currencies(x)[1] for x in pairs} - {"JPY"}
        conv = [f"{q}_JPY" for q in quotes]
        prices = client.pricing(list(dict.fromkeys(pairs + conv)))
        is_open = getattr(client, "is_open", lambda _p: True)
        if hasattr(client, "sync"):
            client.sync()  # 仮想口座：損切り・利確に届いた注文を約定させる

        def q2jpy(pair):
            q = currencies(pair)[1]
            if q == "JPY":
                return 1.0
            b, a = prices[f"{q}_JPY"]
            return (b + a) / 2

        # --- 決済の検知 → 仮想資金を更新
        live_open = {t["id"]: t for t in client.open_trades()
                     if (t.get("clientExtensions") or {}).get("tag") == TAG}
        db_open = {t["id"]: t for t in self.db.trades("open")}
        st = self.ladder
        for tid in set(db_open) - set(live_open):
            self._finalize(db_open.pop(tid), st, q2jpy, now)
        self.save_ladder(st)

        # --- 保有管理
        wk_close = weekend_close_due(pd.Timestamp(now))
        for tid, t in list(db_open.items()):
            lt = live_open.get(tid)
            if lt is None or not is_open(t["pair"]):
                continue
            d = int(t["side"])
            bid, ask = prices[t["pair"]]
            px = bid if d == 1 else ask
            prec = int(self.meta.get(t["pair"], {}).get("displayPrecision", 3))
            opened = pd.Timestamp(t["open_time"])
            reason = None
            if wk_close:
                reason = "週末決済"
            elif now - opened.to_pydatetime() >= timedelta(hours=float(p["max_hold_hours"])):
                reason = "時間切れ"
            elif not t["tp1_done"] and event_exit_due(t["pair"], pd.Timestamp(now), g, self.events):
                reason = "指標前に撤退"
            if reason:
                if self._close(tid, reason):
                    self.db.log("trade", f"{t['pair']} を決済：{reason}")
                continue
            if not t["tp1_done"] and (px - float(t["tp1"])) * d >= 0:
                cur = abs(int(float(lt["currentUnits"])))
                half = cur // 2
                try:
                    if half >= 1:
                        client.close_trade(tid, half)
                    client.set_stop_loss(tid, f"{float(t['entry']):.{prec}f}")
                except MarketError as e:
                    self._err(f"{t['pair']} 半分利確に失敗（次のチェックで再試行）: {e}")
                    continue
                self.db.trade_update(tid, tp1_done=1)
                t["tp1_done"] = 1
                msg = f"✂ {t['pair']} +1R到達：{half:,}通貨を利確し、残りは建値ストップ（負けなし確定）"
                self.db.log("trade", msg)
                self.notify(msg)

        # --- 時価の仮想資金と各種ストップ
        unreal = 0.0
        for t in db_open.values():
            lt = live_open.get(t["id"])
            if not lt:
                continue
            d = int(t["side"])
            bid, ask = prices[t["pair"]]
            px = bid if d == 1 else ask
            cur = abs(int(float(lt["currentUnits"])))
            unreal += (px - float(t["entry"])) * d * cur * q2jpy(t["pair"])
            if t["tp1_done"]:
                unreal += (float(t["tp1"]) - float(t["entry"])) * d * (int(t["units"]) - cur) * q2jpy(t["pair"])
        mtm = st.equity + unreal
        per = self._periods(now, mtm)
        lim = limits(st.level)
        halt = None
        if st.full_halt:
            halt = "完全停止中"
        elif self.db.kv_get("cooldown_until") and now < datetime.fromisoformat(self.db.kv_get("cooldown_until")):
            halt = "連敗クールダウン中"
        else:
            for k, name in (("day", "日次"), ("week", "週次"), ("month", "月次")):
                loss = (per[k + "_start"] - mtm) / per[k + "_start"] * 100
                if loss >= lim[k]:
                    halt = f"{name}損失ストップ中"
                    if self.db.kv_get(f"halt_noted_{k}") != per[k]:
                        self.db.kv_set(f"halt_noted_{k}", per[k])
                        msg = f"🛑 {name}の損失が -{loss:.2f}%（上限 {lim[k]:.1f}%）。期間が変わるまで新規エントリーを止めます"
                        self.db.log("risk", msg)
                        self.notify(msg)
                        if k == "week" and st.level > 0:
                            st.set_level(st.level - 1)
                            self.db.kv_set("level_changed_at", now.isoformat())
                            self.save_ladder(st)
                            self.db.log("risk", f"週次ストップのためリスクを {RISK_LEVELS[st.level]}% に下げました")
                    break
        if self.db.kv_get("paused"):
            halt = halt or "一時停止中"
        if not self._edge_ok():
            halt = halt or "様子見（検証で優位性なし）"

        # --- 新しい足の確定 → シグナル
        positions = [(t["pair"], int(t["side"])) for t in db_open.values() if t["id"] in live_open]
        need = lookback_bars(p)
        for pair in pairs:
            last = self.last_processed.get(pair)
            if last is not None and now < (last + 2 * BAR).to_pydatetime() + timedelta(seconds=5):
                continue
            df = client.recent_df(pair, "H1", min(need, 4999))
            if df.empty:
                continue
            last_t = df.index[-1]
            if last is not None and last_t <= last:
                continue
            self.last_processed[pair] = last_t
            if last is None:
                continue  # 起動直後の足では入らない
            close_t = last_t + BAR
            if now - close_t.to_pydatetime() > timedelta(minutes=15):
                continue
            s = prepare(df, p).iloc[-1]
            d = int(s["signal"])
            if d == 0:
                continue
            feats = features(pair, d, close_t, float(s["vol_ratio"]), float(s["adx"]))
            sl_dist = float(s["atr"]) * p["sl_atr_mult"]
            why = halt or check_entry(pair, d, close_t, float(s["vol_ratio"]), feats, p, g, self.events,
                                      positions, blocks)
            bid, ask = prices[pair]
            pip_sz = 10 ** int(self.meta.get(pair, {}).get("pipLocation", -2 if pair.endswith("JPY") else -4))
            if not why and (ask - bid) / pip_sz > float(cfg["max_spread_pips"]):
                why = "スプレッド拡大"
            if not why and wk_close:
                why = "週末・週明け"
            if not why and not is_open(pair):
                why = "市場休止中"
            units = 0
            if not why:
                entry = ask if d == 1 else bid
                risk_pct = effective_risk(st.level, st.streak)
                units = calc_units(mtm, risk_pct, sl_dist, q2jpy(pair), entry, float(cfg["max_leverage"]))
                if units < int(float(self.meta.get(pair, {}).get("minimumTradeSize", 1))):
                    why = "数量不足"
            side = "買い" if d == 1 else "売り"
            if why:
                self.db.shadow_insert(pair, d, close_t.isoformat(), float(s["close"]), sl_dist, why, feats)
                self.db.log("skip", f"{pair} {side}シグナル → 見送り（{why}）")
                continue
            try:
                self._enter(pair, d, units, entry, sl_dist, p, risk_pct, q2jpy(pair), feats, now)
                positions.append((pair, d))
            except MarketError as e:
                self._err(f"{pair} {side} 発注失敗: {e}")

        self._status(cfg, state=halt or "稼働中", now=now, mtm=mtm, per=per, prices=prices,
                     live_open=live_open, db_open=db_open)
        if now.minute < 5 or not self.db.kv_get("last_snap") or \
                now - datetime.fromisoformat(self.db.kv_get("last_snap")) > timedelta(minutes=60):
            self.db.equity_snap(now.replace(second=0, microsecond=0).isoformat(), round(mtm, 1), st.peak)
            self.db.kv_set("last_snap", now.isoformat())

    def _enter(self, pair, d, units, entry, sl_dist, p, risk_pct, q2a, feats, now):
        prec = int(self.meta.get(pair, {}).get("displayPrecision", 3))
        sl = entry - d * sl_dist
        tp1 = entry + d * sl_dist * p["tp1_r"]
        tp2 = entry + d * sl_dist * p["tp2_r"]
        body = self.client.market_order(pair, d * units, f"{sl:.{prec}f}", f"{tp2:.{prec}f}", None,
                                        tag=TAG, comment=f"v{self.db.kv_get('params_version', 1)}")
        fill = body.get("orderFillTransaction", {})
        tid = (fill.get("tradeOpened") or {}).get("tradeID")
        px = float(fill.get("price", entry))
        if not tid:
            raise MarketError("約定情報が取得できませんでした")
        # 実際の約定価格で利確・損切りの基準を引き直す（損切り幅は維持）
        sl, tp1, tp2 = px - d * sl_dist, px + d * sl_dist * p["tp1_r"], px + d * sl_dist * p["tp2_r"]
        risk_jpy = units * sl_dist * q2a
        self.db.trade_insert({
            "id": tid, "pair": pair, "side": d, "units": units, "entry": px, "sl": sl, "tp1": tp1, "tp2": tp2,
            "sl_dist": sl_dist, "risk_pct": risk_pct, "risk_jpy": risk_jpy, "open_time": now.isoformat(),
            "status": "open", "tp1_done": 0, "features": feats,
            "params_version": int(self.db.kv_get("params_version", 1)),
        })
        side = "買い" if d == 1 else "売り"
        msg = (f"📥 {pair} {side} {units:,}通貨 @ {px:.{prec}f}（損切り {sl:.{prec}f}・最大損失 約{risk_jpy:,.0f}円 = "
               f"{risk_pct:.2f}%）")
        self.db.log("trade", msg)
        self.notify(msg)

    def _finalize(self, t: dict, st: LadderState, q2jpy, now):
        try:
            tr = self.client.trade(t["id"])
        except MarketError as e:
            self._err(f"決済情報の取得に失敗 #{t['id']}: {e}")
            return
        d = int(t["side"])
        price = float(tr.get("price", t["entry"]))
        avg = float(tr.get("averageClosePrice") or price)
        init = abs(int(float(tr.get("initialUnits", t["units"]))))
        q2 = q2jpy(t["pair"])
        pnl = (avg - price) * d * init * q2
        r = pnl / (init * float(t["sl_dist"]) * q2) if t["sl_dist"] else 0.0
        reason = t.get("reason") or tr.get("closeReason")
        if not reason:
            if r <= -0.5:
                reason = "損切り"
            elif t["tp1_done"] and r < 1.0:
                reason = "建値決済"
            else:
                reason = "利確"
        ct = tr.get("closeTime") or now.isoformat()
        self.db.trade_update(t["id"], status="closed", close_time=ct, exit_avg=avg,
                             pnl_jpy=pnl, r=r, reason=reason)
        prev_level = st.level
        msgs = st.on_close(pnl, r)
        if st.level != prev_level:
            self.db.kv_set("level_changed_at", now.isoformat())
        if st.streak >= RULES["streak_cooldown"]:
            self.db.kv_set("cooldown_until", (now + timedelta(hours=24)).isoformat())
            msgs.append(f"{st.streak}連敗：24時間エントリーを休みます")
        mark = "🟢" if pnl > 0 else ("⚪" if abs(r) < 0.05 else "🔴")
        msg = f"{mark} {t['pair']} 決済（{reason}）{pnl:+,.0f}円 / {r:+.2f}R → 仮想資金 {st.equity:,.0f}円"
        self.db.log("trade", msg)
        self.notify(msg)
        for m in msgs:
            self.db.log("risk", m)
            self.notify("⚠ " + m)

    def _periods(self, now: datetime, mtm: float) -> dict:
        j = now.astimezone(JST)
        keys = {"day": j.date().isoformat(),
                "week": (j.date() - timedelta(days=j.weekday())).isoformat(),
                "month": j.strftime("%Y-%m")}
        per = self.db.kv_get("periods") or {}
        changed = False
        for k, v in keys.items():
            if per.get(k) != v:
                per[k], per[k + "_start"] = v, mtm
                changed = True
        if changed:
            self.db.kv_set("periods", per)
        return per

    # ------------------------------------------------------------ 画面用ステータス
    def _status(self, cfg, state, now, mtm=None, per=None, prices=None, live_open=None, db_open=None):
        st = self.ladder
        mtm = st.equity if mtm is None else mtm
        per = per or self.db.kv_get("periods") or {}
        cap = float(cfg["virtual_capital"])
        positions = []
        for t in (db_open or {}).values():
            lt = (live_open or {}).get(t["id"])
            if not lt:
                continue
            d = int(t["side"])
            bid, ask = prices[t["pair"]]
            px = bid if d == 1 else ask
            positions.append({
                "pair": t["pair"], "side": "買い" if d == 1 else "売り", "units": abs(int(float(lt["currentUnits"]))),
                "entry": t["entry"], "now": px, "sl": float(lt.get("stopLossOrder", {}).get("price", t["sl"]))
                if isinstance(lt.get("stopLossOrder"), dict) else t["sl"],
                "r_now": (px - float(t["entry"])) * d / float(t["sl_dist"]), "tp1_done": bool(t["tp1_done"]),
                "open_time": t["open_time"],
            })
        self.db.kv_set("status", {
            "state": state,
            "updated": now.isoformat(timespec="seconds"),
            "equity": round(mtm, 1),
            "realized": round(st.equity, 1),
            "peak": st.peak,
            "dd_pct": round((1 - mtm / st.peak) * 100, 2) if st.peak else 0,
            "capital": cap,
            "total_pct": round((mtm / cap - 1) * 100, 2),
            "month_pct": round((mtm / per["month_start"] - 1) * 100, 2) if per.get("month_start") else 0,
            "week_pct": round((mtm / per["week_start"] - 1) * 100, 2) if per.get("week_start") else 0,
            "day_pct": round((mtm / per["day_start"] - 1) * 100, 2) if per.get("day_start") else 0,
            "target_pct": float(cfg["monthly_target_pct"]),
            "level": st.level,
            "levels": RISK_LEVELS,
            "risk_now": effective_risk(st.level, st.streak),
            "streak": st.streak,
            "limits": limits(st.level),
            "positions": positions,
            "calendar_stale": self.calendar.stale,
            "last_error": self.last_error,
            "next_events": self._upcoming(cfg, now),
        })

    def _upcoming(self, cfg, now) -> list[dict]:
        ccys = {c for p in (self.pairs or cfg["pairs"]) for c in currencies(p)}
        rows = self.db.events_between(now, now + timedelta(days=4))
        out = [{"ts": pd.Timestamp(r["ts"]).tz_convert("Asia/Tokyo").strftime("%m/%d %H:%M"), "ccy": r["ccy"],
                "title": r["title"]} for r in rows if r["ccy"] in ccys]
        return out[:8]
