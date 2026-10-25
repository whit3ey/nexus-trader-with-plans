const $ = id => document.getElementById(id);
const fmt = (x, d = 2) => (x === null || x === undefined || x === "") ? "–" : (typeof x === "number" ? x.toLocaleString("en-GB", {maximumFractionDigits: d}) : String(x));
const cls = ok => ok === true ? "ok" : ok === false ? "bad" : "warn";
function rows(el, items) {            // all values inserted with textContent => no HTML injection from AI text
  el.textContent = "";
  for (const [k, v, c] of items) {
    const a = document.createElement("div"); a.className = "k"; a.textContent = k;
    const b = document.createElement("div"); b.className = "v " + (c || ""); b.textContent = v;
    el.append(a, b);
  }
}
const london = t => new Date(t * 1000).toLocaleString("en-GB", {timeZone: "Europe/London"});
async function load() {
  try {
    const s = await (await fetch("/api/state")).json();
    const b = $("banner");
    b.className = "banner " + String(s.mode || "paper").toLowerCase();
    b.querySelector("strong").textContent = s.display_mode + " — " + (s.display_symbol || s.symbol);
    $("clock").textContent = london(s.time) + " UK";
    $("connection-label").textContent = "BOT FEED CONNECTED";
    const sy = s.system, h = sy.halted;
    rows($("system"), [["Bot", "ONLINE", "ok"], ["Ollama", sy.ollama === null ? "not checked" : (sy.ollama ? "OK" : "DOWN"), cls(sy.ollama)],
      ["AI model", sy.model], ["Bybit", sy.bybit + " (" + sy.bybit_env + ")", cls(sy.bybit === "ok")], ["WebSocket", sy.websocket, cls(sy.websocket === "healthy" ? true : (String(sy.websocket).startsWith("n/a") ? null : false))],
      ["Trading", h.halted ? "HALTED: " + h.reason : (h.paused ? "PAUSED" : "active"), h.halted ? "bad" : (h.paused ? "warn" : "ok")],
      ["Orders go to", sy.permission.destination, sy.permission.can_place_orders ? "ok" : "warn"], ["Uptime", fmt(s.uptime_s, 0) + " s"]]);
    $("gate").textContent = "EXECUTION GATE\n" + sy.live_gate.join("  |  ") + (sy.permission.blockers.length ? "\nBLOCKERS: " + sy.permission.blockers.join(" ") : "");
    $("uptime-label").textContent = "UPTIME " + fmt(s.uptime_s, 0) + " SEC";
    // Agent floor renders reported observations and conclusions, not hidden chain-of-thought.
    const scan = s.scanner || {}, intelState = s.intelligence || {}, aiState = s.ai || {}, multi = s.multi_agent || {};
    const m = s.market || {}, riskState = s.risk || {}, halted = Boolean((s.system || {}).halted && ((s.system.halted.halted) || (s.system.halted.paused)));
    const floorStatus = halted ? "TRADING HALTED" : (multi.status === "complete" ? "REVIEW COMPLETE" : (scan.selected_symbol ? "MONITORING " + scan.selected_symbol : "SCANNING MARKETS"));
    $("floor-status").textContent = floorStatus;
    $("floor-equity").textContent = fmt(riskState.equity, 2) + " USDT";
    $("floor-pnl").textContent = fmt(riskState.daily_pnl, 2) + " USDT";
    $("floor-pnl").className = (Number(riskState.daily_pnl || 0) >= 0 ? "positive" : "negative");
    $("floor-winrate").textContent = fmt(riskState.win_rate, 1) + "%";
    $("floor-symbol").textContent = s.display_symbol || scan.selected_symbol || s.symbol || "—";
    $("floor-chart-symbol").textContent = s.display_symbol || scan.selected_symbol || s.symbol || "NO SYMBOL";
    $("floor-chart-price").textContent = m.price == null ? "—" : fmt(m.price, 6);
    $("floor-chart-change").textContent = m.price == null ? "WAITING FOR LIVE PRICE" : "CURRENT REPORTED PRICE · NO SYNTHETIC CANDLES";
    $("floor-trend-1h").textContent = "1H " + String(m.trend_1h || "—").toUpperCase();
    $("floor-trend-5m").textContent = "5M " + String(m.trend_5m || "—").toUpperCase();
    $("floor-setup").textContent = "SETUP: " + (m.setup && m.setup.has_setup ? String(m.setup.direction || "CONFIRMED") + (m.setup.kind === "plan" ? " PLAN" : "") + " / " + (m.setup.reasons || []).join(", ") : "NO CONFIRMED SETUP");
    const plan = s.plan && s.plan.entry != null ? s.plan : null;
    if (plan && plan.kind === "plan") {
      $("floor-setup").textContent = "PLAN " + String(plan.direction || "").toUpperCase() + " · entry " + fmt(plan.entry) + " · " + fmt(plan.minutes_left, 0) + " min left";
    }
    $("floor-volatility").textContent = "ATR: " + fmt(m.atr_5m, 6);
    $("floor-funding").textContent = "FUNDING: " + fmt(m.funding_rate, 6);
    const chart = $("floor-chart");
    const candles = Array.isArray(m.candles) ? m.candles : (Array.isArray(m.ohlcv) ? m.ohlcv : []);
    if (candles.length >= 2) {
      const data = candles.slice(-60).map(c => Array.isArray(c) ? {open:Number(c[1]),high:Number(c[2]),low:Number(c[3]),close:Number(c[4])} : {open:Number(c.open),high:Number(c.high),low:Number(c.low),close:Number(c.close)}).filter(c => [c.open,c.high,c.low,c.close].every(Number.isFinite));
      if (data.length >= 2) {
        const levels = [];
        if (plan) for (const v of [plan.entry, plan.stop, plan.target, plan.abandon]) if (Number.isFinite(Number(v))) levels.push(Number(v));
        const lo = Math.min(...data.map(c=>c.low), ...(levels.length ? levels : [Infinity]));
        const hi = Math.max(...data.map(c=>c.high), ...(levels.length ? levels : [-Infinity]));
        const span = (hi-lo) || 1, w=720, h=230, pad=16, slot=(w-pad*2)/data.length;
        const y = v => h-pad-(v-lo)/span*(h-pad*2);
        const shapes = data.map((c,i)=>{const x=pad+i*slot+slot/2, up=c.close>=c.open, top=Math.min(y(c.open),y(c.close)), bh=Math.max(2,Math.abs(y(c.open)-y(c.close)));return `<line x1="${x}" y1="${y(c.high)}" x2="${x}" y2="${y(c.low)}" class="candle-wick ${up?'up':'down'}"/><rect x="${x-slot*.29}" y="${top}" width="${Math.max(2,slot*.58)}" height="${bh}" class="candle-body ${up?'up':'down'}"/>`;}).join("");
        const lines = (plan && plan.kind === "plan") ? [["entry", plan.entry, "ENTRY"], ["stop", plan.stop, "STOP"], ["target", plan.target, "TARGET"], ["abandon", plan.abandon, "ABANDON"]].filter(([,v]) => Number.isFinite(Number(v))).map(([cls, val, label]) => {
          const yy = y(Number(val));
          return `<line x1="${pad}" y1="${yy}" x2="${w-pad}" y2="${yy}" class="plan-line ${cls}" stroke-dasharray="6 4"/><text x="${w-pad}" y="${Math.max(11, yy-3)}" text-anchor="end" class="plan-label ${cls}">${label}</text>`;
        }).join("") : "";
        chart.innerHTML = `<svg class="real-candles" viewBox="0 0 ${w} ${h}" role="img" aria-label="Candles supplied by the bot">${[.25,.5,.75].map(n=>`<line x1="0" y1="${h*n}" x2="${w}" y2="${h*n}" class="chart-gridline"/>`).join("")}${shapes}${lines}</svg>`;
      }
    } else {
      chart.innerHTML = '<div class="chart-placeholder"><span class="chart-cross">＋</span><strong>'+(m.price == null ? 'Waiting for market data' : 'Candle history not exposed by bot state')+'</strong><small>Price and indicator readings are live when available. The dashboard will not invent chart candles.</small></div>';
    }
    const agentDefaults = [
      {codename:"BRAM",name:"Scanner",role:"Pair selection and candidate context"},
      {codename:"ILSA",name:"News & data",role:"News and external context"},
      {codename:"HOLT",name:"Technical review",role:"Structure, trend and setup checks"},
      {codename:"KETT",name:"Risk pre-check",role:"Risk flags and volatility review"},
      {codename:"RIGO",name:"Market regime",role:"Multi-timeframe market regime"},
      {codename:"TESS",name:"Execution check",role:"Pre-flight data completeness, no order placement"}
    ];
    const reported = multi.agents || [];
    const agentData = agentDefaults.map(d => ({...d, ...(reported.find(a => a.codename === d.codename || a.name === d.name) || {})}));
    const deskRoot = $("agent-desks"); deskRoot.textContent = "";
    const statusClass = a => (a.status === "complete" || a.status === "clear" || a.status === "aligned") ? "clear" : (["review","limited","error","partial"].includes(a.status) ? "caution" : "working");
    function inspectAgent(a) {
      $("inspector-avatar").textContent = a.codename || "NX";
      $("inspector-name").textContent = a.codename + " / " + (a.name || "Agent");
      $("inspector-role").textContent = a.role || "Specialist review";
      $("inspector-status").textContent = String(a.status || "WAITING").toUpperCase();
      $("inspector-action").textContent = a.detail || a.state || "No report yet.";
      const flags = Array.isArray(a.flags) && a.flags.length ? a.flags.join("; ") : (a.directional_alignment === false ? "Timeframes are not aligned." : (a.setup_confirmed === false ? "No deterministic setup confirmed." : "No explicit warning returned."));
      $("inspector-findings").textContent = flags;
      $("inspector-gate").textContent = "Advisory status only. " + (multi.decision_authority || "The independent deterministic risk engine remains authoritative.");
      document.querySelectorAll(".agent-desk").forEach(el=>el.classList.toggle("selected", el.dataset.codename === a.codename));
    }
    for (const a of agentData) {
      const btn = document.createElement("button"); btn.type="button"; btn.className="agent-desk " + statusClass(a); btn.dataset.codename=a.codename;
      const avatar=document.createElement("span"); avatar.className="desk-avatar avatar-"+a.codename.toLowerCase(); avatar.textContent=a.codename;
      const title=document.createElement("strong"); title.textContent=a.name;
      const role=document.createElement("span"); role.className="desk-role"; role.textContent=a.role;
      const state=document.createElement("em"); state.textContent=a.state || a.status || (multi.enabled === false ? "DISABLED" : "WAITING FOR REVIEW");
      const detail=document.createElement("small"); detail.textContent=a.detail || (multi.enabled === false ? "Agent layer disabled" : "Waiting for a report from the review cycle.");
      btn.append(avatar,title,role,state,detail); btn.addEventListener("click",()=>{window.__nexusSelectedAgent=a.codename;inspectAgent(a);}); deskRoot.append(btn);
    }
    const current = agentData.find(a => a.codename === (window.__nexusSelectedAgent || "")) || agentData.find(a=>a.status === "review" || a.status === "limited" || a.status === "error") || agentData.find(a=>a.status === "complete" || a.status === "clear") || agentData[0];
    if (!window.__nexusSelectedAgent || !agentData.some(a=>a.codename===window.__nexusSelectedAgent)) inspectAgent(current);
    else inspectAgent(agentData.find(a=>a.codename===window.__nexusSelectedAgent));
    window.__nexusSelectedAgent = $("inspector-avatar").textContent;
    const floorDecisionText = String(aiState.decision || aiState.error || "WAITING FOR AI DECISION").toUpperCase();
    $("floor-decision-chip").textContent = aiState.accepted === true ? "ACCEPTED" : (aiState.accepted === false ? "REJECTED" : "WAIT");
    $("floor-decision").textContent = floorDecisionText + " · " + (aiState.reason || aiState.rejection || "No decision explanation reported yet.");
    $("floor-event-count").textContent = (s.events || []).length + " EVENTS";
    const activity = $("floor-activity"); activity.textContent = "";
    const eventItems = (s.events || []).slice(-9).reverse();
    const agentItems = agentData.filter(a=>a.detail && a.detail !== "Waiting for a report from the review cycle.").map(a=>({label:a.codename, text:(a.state || a.status || "REPORT")+" · "+a.detail, status:statusClass(a)}));
    const combined = [...agentItems.slice(0,6), ...eventItems.map(e=>({label:"BOT",text:String(e),status:"working"}))].slice(0,12);
    for (const item of combined) { const row=document.createElement("div"); row.className="floor-log-entry"; const dot=document.createElement("i"); dot.className="legend-dot "+(item.status||"working"); const label=document.createElement("b"); label.textContent=item.label || "EVENT"; const body=document.createElement("span"); body.textContent=item.text; row.append(dot,label,body); activity.append(row); }
    if (!combined.length) activity.innerHTML='<div class="floor-log-entry muted">Waiting for bot events and specialist reports…</div>';
    $("metric-equity").textContent = fmt((s.risk || {}).equity, 2);
    $("metric-pnl").textContent = fmt((s.risk || {}).daily_pnl, 2);
    $("metric-pnl").className = ((s.risk || {}).daily_pnl || 0) >= 0 ? "ok" : "bad";
    $("metric-winrate").textContent = fmt((s.risk || {}).win_rate, 1) + "%";
    $("metric-symbol").textContent = s.display_symbol || s.symbol || "—";
    $("metric-symbol").style.fontSize = String(s.display_symbol || s.symbol || "").length > 14 ? "15px" : "21px";
    $("metric-scan").textContent = (s.scanner || {}).selected_symbol ? "PAIR SELECTED" : ((s.scanner || {}).selection_required ? "SCANNER GATED" : "CONFIGURED PAIR");
    rows($("market"), [["Price", fmt(m.price)], ["Bid / Ask", fmt(m.bid) + " / " + fmt(m.ask)], ["1H trend", fmt(m.trend_1h) + " (" + fmt(m.structure_1h) + ")"],
      ["5M trend", fmt(m.trend_5m)], ["ATR 5M", fmt(m.atr_5m)], ["Volume", fmt(m.volume)], ["Funding", fmt(m.funding_rate, 6)], ["Open interest", fmt(m.open_interest, 0)],
      ["Setup", m.setup && m.setup.has_setup ? m.setup.direction + ": " + m.setup.reasons.join(", ") : "none"],
      ["FVG (5M)", (m.fvg || []).map(f => f.direction + " " + fmt(f.lower) + "-" + fmt(f.upper)).join("; ") || "none"],
      ["Order block (5M)", (m.order_block || []).map(o => o.direction + " " + fmt(o.low) + "-" + fmt(o.high)).join("; ") || "none"],
      ["Sweep", m.sweep && m.sweep.swept ? m.sweep.side + " @ " + fmt(m.sweep.level) : "none"],
      ["Liquidity", m.liquidity ? "eq.highs " + fmt((m.liquidity.equal_highs || []).join(",") || "–") + " | eq.lows " + fmt((m.liquidity.equal_lows || []).join(",") || "–") : "–"]]);
    const a = s.ai || {};
    const dc = $("decision-chip");
    const decisionText = String(a.decision || a.error || "AWAITING DATA").toUpperCase();
    dc.textContent = decisionText;
    dc.className = "decision-chip " + (decisionText.includes("LONG") ? "long" : decisionText.includes("SHORT") ? "short" : "wait");
    rows($("ai"), [["Decision", fmt(a.decision || a.error)], ["Confidence", fmt(a.confidence, 0)], ["Setup", fmt(a.setup)],
      ["Market regime", fmt(a.market_regime)], ["Reasoning", fmt(a.reason)], ["Entry type / price", fmt(a.entry_type) + " / " + fmt(a.entry_price)],
      ["Stop loss", fmt(a.stop_loss)], ["Take profit", fmt(a.take_profit)], ["Risk / reward", fmt(a.risk_reward)],
      ["Invalidations", Array.isArray(a.invalidations) ? (a.invalidations.join("; ") || "–") : "–"],
      ["Time", a.time ? london(a.time) : "–"], ["Result", a.accepted === undefined ? "–" : (a.accepted ? "ACCEPTED" : "REJECTED"), a.accepted ? "ok" : "warn"], ["Why rejected", fmt(a.rejection)]]);
    const sc = s.scanner || {};
    $("scanner-note").textContent = (sc.note || "Scanner not available") + (sc.last_scan ? " Last scan: " + london(sc.last_scan) : "");
    const candidates = sc.candidates || [];
    const scannerEl = $("scanner"); scannerEl.textContent = "";
    if (!candidates.length) {
      scannerEl.textContent = "No candidates passed the current liquidity/spread screen yet.";
    } else {
      for (const c of candidates.slice(0, 10)) {
        const card = document.createElement("div"); card.className = "scanner-card";
        const title = document.createElement("strong"); title.textContent = c.symbol + "  ·  score " + fmt(c.score);
        const detail = document.createElement("div"); detail.className = "small";
        detail.textContent = "Price " + fmt(c.price) + " | 24h turnover " + fmt(c.turnover_24h, 0) + " USDT | spread " + fmt(c.spread_pct, 4) + "% | funding " + fmt(c.funding_rate, 6);
        const reason = document.createElement("div"); reason.className = "small"; reason.textContent = c.reason || "";
        card.append(title, detail, reason); scannerEl.append(card);
      }
    }
    const intel = s.intelligence || {};
    $("intel-note").textContent = (intel.status || "Not available") + (intel.last_update ? " | Updated: " + london(intel.last_update) : "") + " | " + (intel.note || "");
    const intelEl = $("intel"); intelEl.textContent = "";
    const headlines = intel.news || [];
    const network = intel.bitcoin_network || {};
    const newsAnalysis = intel.news_analysis || {};
    rows(intelEl, []);
    if (newsAnalysis.headline_count !== undefined) {
      const card = document.createElement("div"); card.className = "scanner-card";
      const title = document.createElement("strong"); title.textContent = "Headline sentiment (heuristic)";
      const detail = document.createElement("div"); detail.className = "small";
      detail.textContent = "Score " + fmt(newsAnalysis.heuristic_sentiment_score) + " | headlines " + fmt(newsAnalysis.headline_count, 0) + " | " + (newsAnalysis.sentiment_method || "Unvalidated heuristic");
      card.append(title, detail); intelEl.append(card);
    }
    const tokenNews = intel.token_news || {};
    if (tokenNews.symbol) {
      const card = document.createElement("div"); card.className = "scanner-card";
      const title = document.createElement("strong"); title.textContent = tokenNews.symbol + " headline context";
      const detail = document.createElement("div"); detail.className = "small";
      detail.textContent = "Matching headlines " + fmt(tokenNews.headline_count, 0) + " | heuristic score " + fmt(tokenNews.heuristic_sentiment_score);
      card.append(title, detail); intelEl.append(card);
    }
    const dex = intel.token_dex_market || {};
    if (dex.symbol) {
      const card = document.createElement("div"); card.className = "scanner-card";
      const title = document.createElement("strong"); title.textContent = dex.symbol + " DEX market proxy: " + (dex.status || "unknown");
      const detail = document.createElement("div"); detail.className = "small";
      const pool = dex.best_pool || {};
      detail.textContent = pool.chain ? (pool.chain + " / " + (pool.dex || "DEX") + " | liquidity $" + fmt(pool.liquidity_usd, 0) + " | 24h volume $" + fmt(pool.volume_24h_usd, 0) + " | 1h buys/sells " + fmt((pool.transactions_1h || {}).buys, 0) + "/" + fmt((pool.transactions_1h || {}).sells, 0) + " | 24h change " + fmt(pool.price_change_24h_pct) + "%") : (dex.note || "No token-specific DEX data available.");
      card.append(title, detail);
      if (pool.url) { const link = document.createElement("a"); link.href = pool.url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = "Open DEX pool"; card.append(link); }
      if (dex.match_warning) { const warn = document.createElement("div"); warn.className = "small"; warn.textContent = dex.match_warning; card.append(warn); }
      intelEl.append(card);
    }
    for (const item of headlines.slice(0, 8)) {
      const card = document.createElement("div"); card.className = "scanner-card";
      const title = document.createElement("strong"); title.textContent = "[" + (item.source || "News") + "] " + (item.title || "Untitled");
      const meta = document.createElement("div"); meta.className = "small"; meta.textContent = item.published || "Publication time unavailable";
      const link = document.createElement("a"); link.href = item.url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = "Open source";
      card.append(title, meta, link); intelEl.append(card);
    }
    for (const [key, val] of Object.entries(network)) {
      const card = document.createElement("div"); card.className = "scanner-card";
      const title = document.createElement("strong"); title.textContent = "Bitcoin network: " + key.replaceAll("_", " ");
      const detail = document.createElement("div"); detail.className = "small"; detail.textContent = "Latest " + fmt(val.latest) + " " + (val.unit || "") + " | previous " + fmt(val.previous) + " | source timestamp " + fmt(val.timestamp, 0);
      card.append(title, detail); intelEl.append(card);
    }
    if (!headlines.length && !Object.keys(network).length) intelEl.textContent = "No current intelligence data. Trading logic must not treat missing feeds as confirmation.";
    const p = s.position;
    if (p) {
      rows($("position"), [["Side", p.side], ["Entry", fmt(p.avgPrice)], ["Price", fmt(p.markPrice)], ["Size", fmt(p.size, 4)], ["Leverage", fmt(p.leverage, 1) + "x"],
        ["Unrealised P/L", fmt(p.unrealisedPnl), p.unrealisedPnl >= 0 ? "ok" : "bad"], ["Stop loss", fmt(p.stopLoss)], ["Take profit", fmt(p.takeProfit)], ["R multiple", fmt(p.r_multiple)]]);
    } else if (plan && plan.kind === "plan") {
      const side = String(plan.direction || "long").toLowerCase();
      const mins = plan.minutes_left == null ? "—" : fmt(plan.minutes_left, 0);
      rows($("position"), [
        ["Open trade", "Plan " + side + ", entry " + fmt(plan.entry) + ", " + mins + " min left"],
        ["Stop", fmt(plan.stop)], ["Target", fmt(plan.target)], ["Abandon", fmt(plan.abandon)],
        ["Take-profit style", fmt(plan.tp_management || "configured")],
        ["Risk used", plan.risk_scale == null ? "—" : Math.round(Number(plan.risk_scale) * 100) + "% of normal"]]);
    } else {
      rows($("position"), [["Position", s.pending_entry ? "entry order pending" : "none"]]);
    }
    const r = s.risk;
    rows($("risk"), [["Daily trades", r.daily_trades + " / " + r.max_daily_trades], ["Daily P/L", fmt(r.daily_pnl), r.daily_pnl >= 0 ? "ok" : "bad"],
      ["Daily loss", fmt(r.daily_loss_pct, 2) + "% / " + r.max_daily_loss_pct + "%", r.daily_loss_pct >= r.max_daily_loss_pct ? "bad" : ""],
      ["Drawdown", fmt(r.drawdown_pct, 2) + "% / " + r.max_drawdown_pct + "%"], ["Risk per trade", r.risk_per_trade_pct + "%"],
      ["Equity", fmt(r.equity)], ["Available", fmt(r.available)], ["Wins / losses", r.wins + " / " + r.losses], ["Win rate", fmt(r.win_rate, 1) + "%"], ["Average R", fmt(r.average_r)],
      ["Consecutive losses", r.consecutive_losses], ["Stopped by", (r.halted_by || []).join("; ") || "–"]]);
    $("events").textContent = (s.events || []).join("\n") || "–";
    const t = $("trades"); t.textContent = (s.recent_trades || []).map(x => `#${x.id} ${x.direction} ${fmt(x.entry)} → ${fmt(x.exit)}  P/L ${fmt(x.pnl)}  ${fmt(x.r_multiple)}R  ${x.exit_reason}`).join("\n") || "–";
  } catch (e) { const b = $("banner"); b.className = "banner live"; const label = b.querySelector("strong"); if (label) label.textContent = "DASHBOARD CANNOT REACH THE BOT"; $("connection-label").textContent = "FEED OFFLINE"; }
}
async function ctl(action) {
  const mode = $("banner").className.includes("paper") ? "PAPER" : ($("banner").className.includes("testnet") ? "TESTNET" : "LIVE");
  let confirm = null;
  if (action === "emergency_close" && !window.confirm("EMERGENCY CLOSE: cancel opening orders, close the position and stop autonomous trading. Continue?")) return;
  if (action === "resume" && !window.confirm("Resume trading after verifying the exchange state?")) return;
  if ((action === "emergency_close" || action === "resume") && mode !== "PAPER") confirm = mode;
  const r = await fetch("/api/control", {method: "POST", headers: {"Content-Type": "application/json", "X-Requested-By": "ai-trader"}, body: JSON.stringify({action, confirm})});
  $("ctlout").textContent = JSON.stringify(await r.json(), null, 1).slice(0, 1500);
  load();
}
load(); setInterval(load, 3000);
