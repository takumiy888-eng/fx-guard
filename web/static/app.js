"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (v, d = 0) => (v == null || isNaN(v) ? "—" : Number(v).toLocaleString("ja-JP", { minimumFractionDigits: d, maximumFractionDigits: d }));
const pct = (v, d = 2) => (v == null || isNaN(v) ? "—" : (v > 0 ? "+" : "") + fmt(v, d) + "%");
const yen = (v) => (v == null ? "—" : "¥" + fmt(v));
const cls = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const jst = (iso) => (iso ? new Date(iso).toLocaleString("ja-JP", { timeZone: "Asia/Tokyo", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—");

async function api(url, opt = {}) {
  const r = await fetch(url, { headers: { "Content-Type": "application/json" }, credentials: "same-origin", ...opt });
  const j = await r.json().catch(() => ({ ok: false, error: `HTTP ${r.status}` }));
  if (r.status === 401 && !url.endsWith("/login")) { showLogin(); throw new Error("ログインが必要です"); }
  if (!j.ok) throw new Error(j.error || "エラー");
  return j;
}
const post = (url, body) => api(url, { method: "POST", body: JSON.stringify(body || {}) });

// ------------------------------------------------------------ ログイン
function showLogin() { $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); }
async function doLogin() {
  try { await post("/api/login", { password: $("#pw").value }); $("#pw").value = ""; start(); }
  catch (e) { $("#loginMsg").textContent = e.message; }
}
$("#btnLogin").onclick = doLogin;
$("#pw").addEventListener("keydown", (e) => e.key === "Enter" && doLogin());

// ------------------------------------------------------------ タブ
let VIEW = "home";
function go(v) {
  VIEW = v;
  document.querySelectorAll(".tabbar button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  document.querySelectorAll(".view").forEach((s) => s.classList.toggle("active", s.id === "v-" + v));
  refresh();
  window.scrollTo(0, 0);
}
document.querySelectorAll(".tabbar button").forEach((b) => (b.onclick = () => go(b.dataset.v)));
document.querySelectorAll("[data-go]").forEach((a) => (a.onclick = () => go(a.dataset.go)));

// ------------------------------------------------------------ ホーム
let OV = null;
async function loadHome() {
  const o = (OV = await api("/api/overview"));
  const s = o.status || {};
  const preparing = /初回|過去データ/.test(s.state || "") || (!o.learned_at && !s.state);
  $("#setupBanner").classList.toggle("hidden", !preparing);
  $("#setupState").textContent = preparing && s.state ? "いまの状態：" + s.state : "";
  $("#deadBanner").classList.toggle("hidden", !s.updated || o.alive);
  const chip = $("#stateChip");
  const st = !o.alive && s.updated ? "Bot応答なし" : s.state || "起動待ち";
  chip.textContent = /初回|過去データ/.test(st) ? "初回準備中" : st.length > 10 ? st.slice(0, 10) + "…" : st;
  chip.className = "chip " + (st === "稼働中" ? "ok" : /停止|応答なし/.test(st) ? "stop" : "wait");
  $("#hEquity").textContent = yen(s.equity);
  $("#hTotal").innerHTML = s.capital ? `<span class="${cls(s.total_pct)}">${pct(s.total_pct)}</span> <span class="muted small">（開始 ${yen(s.capital)}・${o.started_at ? jst(o.started_at).slice(0, 5) + "〜" : ""}）</span>` : "";
  const tgt = s.target_pct || 5, m = s.month_pct || 0;
  $("#hMonth").innerHTML = `<span class="${cls(m)}">${pct(m)}</span>`;
  $("#hTarget").textContent = tgt;
  const scale = Math.max(tgt * 1.5, Math.abs(m));
  const bar = $("#hMonthBar");
  bar.style.width = Math.min(100, (Math.abs(m) / scale) * 100) + "%";
  bar.className = m < 0 ? "neg" : "";
  $("#hTargetMark").style.left = (tgt / scale) * 100 + "%";
  $("#hDay").innerHTML = `<span class="${cls(s.day_pct)}">${pct(s.day_pct)}</span>`;
  $("#hWeek").innerHTML = `<span class="${cls(s.week_pct)}">${pct(s.week_pct)}</span>`;
  $("#hDD").innerHTML = `<span class="${s.dd_pct > 0 ? "neg" : ""}">${s.dd_pct > 0 ? "-" + fmt(s.dd_pct, 2) + "%" : "0%"}</span>`;

  const lv = s.levels || o.rules.levels;
  $("#ladder").innerHTML = lv.map((x, i) => `<div class="${i < (s.level ?? 0) ? "done" : ""} ${i === (s.level ?? 0) ? "cur" : ""}">${x}%</div>`).join("");
  const lim = s.limits || {};
  $("#riskNote").innerHTML = s.limits
    ? `現在 1回 ${fmt(s.risk_now, 2)}%${s.streak >= 3 ? `（${s.streak}連敗中のため半分）` : ""}。ストップ：1日 -${fmt(lim.day, 1)}%・1週 -${fmt(lim.week, 1)}%・1か月 -${fmt(lim.month, 1)}%・最高値から -${o.rules.dd_demote_pct}%で最低段階、-${o.rules.dd_halt_pct}%で完全停止。実績が基準を満たすと週1回だけ1段階上がります。`
    : "";
  drawLine($("#eqChart"), o.equity.map(([t, v]) => [new Date(t).getTime(), v]), s.capital);
  $("#eqNote").textContent = o.equity.length < 2 ? "運用が始まるとここに推移が出ます" : "";

  const ps = s.positions || [];
  $("#posList").innerHTML = ps.length ? ps.map((p) => `<div class="item"><div><div class="t">${p.pair} ${p.side}</div><div class="s">${fmt(p.units)}通貨・建値 ${p.entry}・${p.tp1_done ? "✂ 半分利確済み（残りは負けなし）" : "損切り " + fmt(p.sl, 3)}</div></div><div class="num ${cls(p.r_now)}">${p.r_now >= 0 ? "+" : ""}${fmt(p.r_now, 2)}R</div></div>`).join("") : `<p class="muted small">なし</p>`;
  const ev = s.next_events || [];
  $("#evList").innerHTML = ev.length ? ev.map((e) => `<div class="item"><div class="t small">${esc(e.title)}</div><div class="num small muted">${e.ccy}・${e.ts}</div></div>`).join("") + `<p class="muted small">前後は新規エントリーしません${s.calendar_stale ? `　<span class="warn">⚠ カレンダーの更新が7日以上止まっています</span>` : ""}</p>` : `<p class="muted small">4日以内の重要指標はありません${s.calendar_stale ? `（<span class="warn">カレンダー未取得</span>）` : ""}</p>`;
  $("#homeLog").innerHTML = renderLogs(o.logs.slice(0, 6));
  $("#btnPause").textContent = o.paused ? "再開する" : "一時停止";
}
$("#btnPause").onclick = async () => { await post("/api/command", { cmd: OV && OV.paused ? "resume" : "pause" }); setTimeout(refresh, 2000); };
$("#btnLearn").onclick = async () => {
  if (!confirm("今すぐ学習を実行しますか？（2〜3分かかります。その間は売買チェックが止まります）")) return;
  await post("/api/command", { cmd: "learn_now" }); alert("学習を予約しました。完了すると思考ログに結果が出ます。");
};

// ------------------------------------------------------------ 取引
async function loadTrades() {
  const { trades } = await api("/api/trades");
  const c = trades.filter((t) => t.status === "closed");
  const w = c.filter((t) => t.pnl_jpy > 0).length, pnl = c.reduce((a, t) => a + t.pnl_jpy, 0);
  const gw = c.filter((t) => t.r > 0).reduce((a, t) => a + t.r, 0), gl = -c.filter((t) => t.r < 0).reduce((a, t) => a + t.r, 0);
  const k = [["取引数", fmt(c.length)], ["勝率", c.length ? fmt((w / c.length) * 100, 1) + "%" : "—"], ["PF", gl ? fmt(gw / gl, 2) : "—"], ["損益", `<span class="${cls(pnl)}">${yen(pnl)}</span>`], ["平均R", c.length ? fmt(c.reduce((a, t) => a + t.r, 0) / c.length, 2) : "—"]];
  $("#trKpis").innerHTML = k.map(([l, v]) => `<div class="kpi"><div class="l">${l}</div><div class="v">${v}</div></div>`).join("");
  $("#trList").innerHTML = trades.length ? `<div class="card">${trades.map((t) => `<div class="item"><div><div class="t">${t.pair} ${t.side > 0 ? "買い" : "売り"} <span class="muted small">${t.status === "open" ? "保有中" : esc(t.reason || "")}</span></div><div class="s">${jst(t.open_time)} 〜 ${t.close_time ? jst(t.close_time) : ""}・${fmt(t.units)}通貨・リスク ${fmt(t.risk_pct, 2)}%</div></div><div class="num">${t.status === "closed" ? `<div class="${cls(t.pnl_jpy)}">${t.pnl_jpy >= 0 ? "+" : ""}${fmt(t.pnl_jpy)}円</div><div class="small muted">${t.r >= 0 ? "+" : ""}${fmt(t.r, 2)}R</div>` : "—"}</div></div>`).join("")}</div>` : `<div class="banner">まだ取引はありません。条件が揃わない時は「入らない」のが正常な動きです。</div>`;
}

// ------------------------------------------------------------ 思考ログ
let LOGK = "";
document.querySelectorAll("#logFilter button").forEach((b) => (b.onclick = () => { LOGK = b.dataset.k; document.querySelectorAll("#logFilter button").forEach((x) => x.classList.toggle("on", x === b)); loadLogs(); }));
function renderLogs(ls) { return ls.length ? `<div class="loglist">${ls.map((l) => `<div class="e ${l.kind}"><span class="ts">${jst(l.ts)}</span>${esc(l.msg)}</div>`).join("")}</div>` : `<p class="muted small">まだありません</p>`; }
async function loadLogs() { const { logs } = await api("/api/logs?limit=200&kind=" + (LOGK || "main")); $("#logList").innerHTML = renderLogs(logs); }

// ------------------------------------------------------------ 学習
async function loadLearn() {
  const { learning: L, history, param_labels } = await api("/api/learning");
  $("#learnEmpty").classList.toggle("hidden", !!L);
  $("#learnBody").classList.toggle("hidden", !L);
  if (!L) return;
  const R = L.replay;
  const card = (name, r, hl) => `<div class="kpi ${hl ? "hl" : ""}"><div class="l">${name}</div><div class="v">${yen(r.final)}</div><div class="small">月平均 <b class="${cls(r.monthly_avg_pct)}">${pct(r.monthly_avg_pct)}</b></div><div class="small muted">最大DD ${fmt(r.max_dd_pct, 1)}%・最悪月 ${pct(r.worst_month, 1)}</div><div class="small muted">プラス月 ${r.positive_months}/${r.months}・5%以上 ${r.months_5pct}回</div></div>`;
  $("#replayCards").innerHTML = card("段階リスク（採用中）", R.ladder, true) + card("固定0.25%（参考）", R.fixed) + card("直近12か月・段階リスク", R.last12m);
  drawBars($("#monthChart"), R.ladder.monthly.map((m) => [m.month, m.ret_pct]), 5);
  $("#replayNote").textContent = `期間 ${L.period[0]}〜${L.period[1]}・1万円スタート・スプレッドとスリッページ込み。棒は月ごとの損益率（点線は月5%）。過去の成績は将来を保証しません。`;
  $("#notes").innerHTML = (L.notes.length ? L.notes : ["変更なし（今のルールを維持）"]).map((n) => `<li>${esc(n)}</li>`).join("");
  $("#learnMeta").textContent = `${jst(L.ran_at)}・${L.reason}・所要 ${L.seconds}秒・ルール v${L.version}・前半/後半の境目 ${L.cut}`;
  const S = L.stats, row = (n, s) => [n, fmt(s.n), fmt(s.win_rate, 1) + "%", fmt(s.pf, 2), fmt(s.avg_r, 2), fmt(s.total_r, 1), fmt(s.max_dd_r, 1), s.max_losing_streak];
  $("#statTbl").innerHTML = table(["", "回数", "勝率", "PF", "平均R", "合計R", "最大DD(R)", "最大連敗"], [row("全期間", S.all), row("前半（学習用）", S.is), row("後半（検証用）", S.oos)]);
  $("#blockList").innerHTML = L.blocks.length ? L.blocks.map((b) => `<div class="item"><div class="t">🚫 ${esc((OV_FEAT[b.feature] || b.feature) + "＝" + b.value)}</div><div class="num small">${b.n}回・平均${fmt(b.avg_r, 2)}R</div></div>`).join("") : `<p class="muted small">なし（はっきり負けている条件は見つかっていません）</p>`;
  $("#condTbl").innerHTML = table(["条件", "回数", "勝率", "平均R", "合計R"], L.conditions.map((c) => [`${esc(c.feature)}＝${esc(c.value)}`, c.n, fmt(c.win_rate, 0) + "%", `<span class="${cls(c.avg_r)}">${fmt(c.avg_r, 2)}</span>`, fmt(c.total_r, 1)]));
  const sh = L.shadows || { by_reason: [] };
  $("#shadowTbl").innerHTML = sh.by_reason.length ? table(["見送り理由", "回数", "仮想平均R", "仮想合計R"], sh.by_reason.map((x) => [esc(x.reason), x.n, `<span class="${cls(x.avg_r)}">${fmt(x.avg_r, 2)}</span>`, fmt(x.total_r, 1)])) + (sh.taken_n ? `<p class="small">実際に入った取引の平均：<b>${fmt(sh.taken_avg_r, 2)}R</b>（${sh.taken_n}回）</p>` : "") : `<p class="muted small">運用が始まると、見送ったシグナルの「入っていたらどうなったか」が貯まります。</p>`;
  $("#paramTbl").innerHTML = table(["パラメータ", "現在値"], Object.entries(L.params).map(([k, v]) => [param_labels[k] || k, v]));
  $("#histTbl").innerHTML = history.length ? table(["版", "日時", "内容", ...Object.keys(L.params).map((k) => param_labels[k] || k)], history.slice().reverse().map((h) => ["v" + h.version, jst(h.adopted_at), esc(h.note || ""), ...Object.keys(L.params).map((k) => h.params[k])])) : "";
}
const OV_FEAT = { pair: "通貨ペア", side: "売買", hour: "時間帯", weekday: "曜日", vol: "ボラティリティ", trend: "トレンド強度" };

// ------------------------------------------------------------ 設定
async function loadSettings() {
  const r = await api("/api/settings"), c = r.config;
  $("#sPairs").value = c.pairs.join(", ");
  $("#sCapital").value = c.virtual_capital;
  $("#sTarget").value = c.monthly_target_pct;
  $("#sDiscord").value = c.discord_webhook || "";
}
$("#btnSaveConn").onclick = async () => {
  const m = $("#connMsg"); m.textContent = "確認中…"; m.className = "small muted";
  try {
    const r = await post("/api/test_connection");
    m.className = "small pos";
    m.textContent = "✓ 取得できました" + (r.missing.length ? `（GMOに無い通貨ペア: ${r.missing.join(", ")}）` : "");
    $("#connPrices").innerHTML = table(["通貨ペア", "売値", "買値", "市場"], r.prices.map((x) => [x.pair, x.bid, x.ask, x.status === "OPEN" ? "取引中" : "休止中"]));
  } catch (e) { m.className = "small neg"; m.textContent = "✕ " + e.message; $("#connPrices").innerHTML = ""; }
};
$("#btnSave").onclick = async () => {
  const m = $("#saveMsg");
  try {
    await post("/api/settings", { pairs: $("#sPairs").value, virtual_capital: $("#sCapital").value, monthly_target_pct: $("#sTarget").value, discord_webhook: $("#sDiscord").value.trim() });
    m.className = "small pos"; m.textContent = "保存しました";
  } catch (e) { m.className = "small neg"; m.textContent = "✕ " + e.message; }
};
$("#btnPw").onclick = async () => {
  try { await post("/api/password", { current: $("#pwCur").value, new: $("#pwNew").value }); $("#pwMsg").className = "small pos"; $("#pwMsg").textContent = "変更しました"; $("#pwCur").value = $("#pwNew").value = ""; }
  catch (e) { $("#pwMsg").className = "small neg"; $("#pwMsg").textContent = "✕ " + e.message; }
};
$("#btnCloseAll").onclick = async () => { if (confirm("Botの全ポジションを成行で決済します。よろしいですか？")) { await post("/api/command", { cmd: "close_all" }); alert("決済を指示しました"); } };
$("#btnReset").onclick = async () => { if (confirm("仮想資金を設定額に戻し、リスク段階も最低に戻します。取引履歴は残ります。よろしいですか？")) { await post("/api/command", { cmd: "reset_virtual" }); alert("リセットを指示しました"); } };
$("#btnLogout").onclick = async () => { await post("/api/logout"); showLogin(); };

// ------------------------------------------------------------ グラフ
function canvasCtx(cv, H) {
  const dpr = window.devicePixelRatio || 1, W = cv.clientWidth;
  cv.width = W * dpr; cv.height = H * dpr; cv.style.height = H + "px";
  const g = cv.getContext("2d"); g.scale(dpr, dpr); g.clearRect(0, 0, W, H);
  const css = getComputedStyle(document.documentElement);
  g.font = "10px " + css.getPropertyValue("--mono");
  return { g, W, H, c: (n) => css.getPropertyValue(n).trim() };
}
function drawLine(cv, pts, base) {
  if (!cv.clientWidth) return;
  const { g, W, H, c } = canvasCtx(cv, 180);
  if (pts.length < 2) { g.fillStyle = c("--muted"); g.fillText("データなし", W / 2 - 24, H / 2); return; }
  const L = 52, R = 6, T = 8, B = 20, xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]).concat(base ? [base] : []);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys), pad = (y1 - y0) * 0.1 || 1;
  const X = (x) => L + ((x - x0) / (x1 - x0 || 1)) * (W - L - R), Y = (y) => T + (1 - (y - y0 + pad) / (y1 - y0 + 2 * pad)) * (H - T - B);
  g.strokeStyle = c("--line"); g.fillStyle = c("--muted");
  [y0, (y0 + y1) / 2, y1].forEach((y) => { g.beginPath(); g.moveTo(L, Y(y)); g.lineTo(W - R, Y(y)); g.stroke(); g.fillText(Math.round(y).toLocaleString(), 2, Y(y) + 3); });
  [x0, x1].forEach((x, i) => g.fillText(new Date(x).toLocaleDateString("ja-JP", { month: "numeric", day: "numeric" }), i ? W - R - 34 : L, H - 5));
  if (base) { g.setLineDash([3, 3]); g.beginPath(); g.moveTo(L, Y(base)); g.lineTo(W - R, Y(base)); g.stroke(); g.setLineDash([]); }
  g.strokeStyle = c("--accent"); g.lineWidth = 1.8; g.beginPath();
  pts.forEach((p, i) => (i ? g.lineTo(X(p[0]), Y(p[1])) : g.moveTo(X(p[0]), Y(p[1])))); g.stroke();
}
function drawBars(cv, rows, target) {
  if (!cv.clientWidth || !rows.length) return;
  const { g, W, H, c } = canvasCtx(cv, 170);
  const L = 30, R = 4, T = 8, B = 18, vals = rows.map((r) => r[1]);
  const hi = Math.max(target * 1.3, ...vals), lo = Math.min(0, ...vals);
  const Y = (v) => T + (1 - (v - lo) / (hi - lo || 1)) * (H - T - B), bw = (W - L - R) / rows.length;
  g.strokeStyle = c("--line"); g.fillStyle = c("--muted");
  [lo, 0, target, hi].forEach((v) => g.fillText(Math.round(v) + "%", 0, Y(v) + 3));
  g.beginPath(); g.moveTo(L, Y(0)); g.lineTo(W - R, Y(0)); g.stroke();
  rows.forEach(([, v], i) => { g.fillStyle = v >= target ? c("--accent") : v >= 0 ? "#3d7f77" : c("--down"); const y = Y(Math.max(v, 0)), h = Math.abs(Y(v) - Y(0)); g.fillRect(L + i * bw + 1, y, Math.max(bw - 2, 1), Math.max(h, 1)); });
  g.setLineDash([3, 3]); g.strokeStyle = c("--text"); g.beginPath(); g.moveTo(L, Y(target)); g.lineTo(W - R, Y(target)); g.stroke(); g.setLineDash([]);
  g.fillStyle = c("--muted"); [0, rows.length - 1].forEach((i) => g.fillText(rows[i][0], Math.min(L + i * bw, W - R - 40), H - 4));
}
function table(head, rows) {
  return `<div class="tw"><table><thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((x, i) => `<td class="${i ? "n" : ""}">${x}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

// ------------------------------------------------------------ 起動
async function refresh() {
  try {
    if (VIEW === "home") await loadHome();
    else if (VIEW === "trades") await loadTrades();
    else if (VIEW === "log") await loadLogs();
    else if (VIEW === "learn") await loadLearn();
    else if (VIEW === "settings") await loadSettings();
  } catch (e) { console.warn(e); }
}
let timer = null;
async function start() {
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden");
  await refresh();
  clearInterval(timer);
  timer = setInterval(() => VIEW === "home" && refresh(), 15000);
}
window.addEventListener("resize", () => VIEW === "home" && OV && drawLine($("#eqChart"), OV.equity.map(([t, v]) => [new Date(t).getTime(), v]), OV.status.capital));
(async () => { const r = await api("/api/me"); r.auth ? start() : showLogin(); })();
