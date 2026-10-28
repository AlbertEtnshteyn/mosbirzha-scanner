/* Пульт робота Мосбиржи: телефон/браузер ⇄ сервер робота */
"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const NATIVE = window.Android || null;          // мост приложения для Андроида (если открыто в нём)

// ---------------------------------------------------------------- ключ доступа
function store(k, v) { try { if (v === undefined) return localStorage.getItem(k); if (v === null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) { return null; } }
let KEY = null;
(function initKey() {
  const m = location.hash.match(/k=([A-Za-z0-9_\-]+)/);
  if (m) { KEY = m[1]; store("key", KEY); history.replaceState(null, "", location.pathname); }
  else KEY = store("key");
})();

function parseCode(s) {
  const m = String(s).trim().match(/^moexrobot:\/\/([^\/:]+):(\d+)\/([A-Za-z0-9_\-]+)\/([0-9a-f]{64})$/i);
  return m ? { host: m[1], port: m[2], key: m[3], fp: m[4] } : null;
}

async function api(path, body) {
  const opt = { headers: { "X-Key": KEY || "" } };
  if (body !== undefined) { opt.method = "POST"; opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (r.status === 401) { showLogin("Код подключения не подошёл."); throw new Error("401"); }
  const js = await r.json();
  if (!r.ok) throw new Error(js.error || ("HTTP " + r.status));
  return js;
}

// ---------------------------------------------------------------- форматирование
const fmt = (v) => v == null || v === "" || isNaN(v) ? "—" : (v >= 1000 ? (+v).toFixed(1) : v >= 10 ? (+v).toFixed(2) : v >= 1 ? (+v).toFixed(3) : (+v).toFixed(5));
const pct = (v, d = 2) => v == null || isNaN(v) ? "—" : (v > 0 ? "+" : "") + (+v).toFixed(d) + "%";
const cls = (v) => v > 0 ? "up" : v < 0 ? "down" : "";
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function toast(text, ms = 3500) {
  const t = $("#toast"); t.textContent = text; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms);
}
function ask(text, buttons) {
  return new Promise((res) => {
    $("#modalText").textContent = text;
    const box = $("#modalBtns"); box.innerHTML = "";
    buttons.forEach(([label, val, kind]) => {
      const b = document.createElement("button"); b.className = "btn " + (kind || ""); b.textContent = label;
      b.onclick = () => { $("#modal").hidden = true; res(val); }; box.appendChild(b);
    });
    $("#modal").hidden = false;
  });
}

// ---------------------------------------------------------------- звук
let actx = null;
function beep(kind) {
  if (store("sound") === "0") return;
  try {
    actx = actx || new (window.AudioContext || window.webkitAudioContext)();
    const notes = kind === "open" ? [660, 880] : kind === "close" ? [880, 660] : [740];
    notes.forEach((f, i) => {
      const o = actx.createOscillator(), g = actx.createGain();
      o.frequency.value = f; o.type = "sine"; o.connect(g); g.connect(actx.destination);
      const t0 = actx.currentTime + i * 0.18;
      g.gain.setValueAtTime(0.0001, t0); g.gain.exponentialRampToValueAtTime(0.3, t0 + 0.02); g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.16);
      o.start(t0); o.stop(t0 + 0.17);
    });
  } catch (e) {}
}
document.addEventListener("click", () => { try { actx = actx || new (window.AudioContext || window.webkitAudioContext)(); actx.resume(); } catch (e) {} }, { once: true });

// ---------------------------------------------------------------- вкладки
let TAB = "robot";
function showTab(t) {
  TAB = t;
  $$("nav.bottom button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  $$(".tab").forEach((s) => (s.hidden = s.id !== "tab-" + t));
  if (t === "trades") loadTrades();
  if (t === "settings") loadSettings();
  if (t === "chart") { if (!CH.chart) loadChart(); else CH.chart.applyOptions({ width: $("#chart").clientWidth }); }
  window.scrollTo(0, 0);
}
$$("nav.bottom button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

// ---------------------------------------------------------------- состояние робота
let EV = -1, LOGSEQ = 0, ST = null;
async function poll() {
  try {
    const st = await api(`/api/state?ev=${Math.max(EV, 0)}&log=${LOGSEQ}`);
    ST = st;
    renderState(st);
    if (EV >= 0) for (const e of st.events) onEvent(e);
    EV = st.ev_seq;
    if (st.log.length) {
      const pre = $("#log"), atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
      pre.textContent = (pre.textContent + st.log.join("\n") + "\n").split("\n").slice(-600).join("\n");
      if (atEnd) pre.scrollTop = pre.scrollHeight;
    }
    LOGSEQ = st.log_seq;
  } catch (e) {
    if (e.message !== "401") { $("#tradeBadge").textContent = "нет связи"; $("#tradeBadge").className = "badge dead"; }
  }
}

function onEvent(e) {
  if (e.kind === "open" || e.kind === "close") { beep(e.kind); toast(e.text, 6000); }
  else if (e.kind === "formation") toast(e.text, 4000);
  if (NATIVE && NATIVE.notify && (e.kind === "open" || e.kind === "close" || e.kind === "carry")) {
    try { NATIVE.notify(e.kind === "open" ? "Вход в позицию" : e.kind === "close" ? "Выход из позиции" : "Перенос", e.text); } catch (x) {}
  }
}

function renderState(st) {
  $("#clock").textContent = "МСК " + st.time + " · v" + st.version;
  const b = $("#tradeBadge");
  if (!st.running) { b.textContent = st.token_set ? "робот запускается" : "нужен токен"; b.className = "badge dead"; }
  else if (st.trade) { b.textContent = "● ТОРГОВЛЯ ВКЛ"; b.className = "badge on"; }
  else { b.textContent = "наблюдение"; b.className = "badge off"; }
  $("#status").textContent = st.status || "";
  $("#market").innerHTML = st.market == null ? "—" : `<span class="${cls(st.market)}">${pct(st.market * 100)}</span>`;
  const sb = $("#startBtn");
  if (st.trade) { sb.textContent = "Стоп"; sb.className = "btn stop"; }
  else { sb.textContent = "Старт"; sb.className = "btn start"; }
  $("#robotHint").textContent = st.trade ? "Робот сам входит и выходит по стратегиям. «Стоп» — выключить новые сделки."
    : st.token_set ? "Робот следит за рынком и шлёт формации, но не торгует. «Старт» — включить торговлю на демо-счёте."
      : "Введите токен песочницы Т-Инвестиций во вкладке «Настройки».";

  // позиции
  const P = st.positions || [];
  $("#posCount").textContent = P.length ? `(${P.length})` : "";
  $("#closeAllBtn").hidden = P.length < 2;
  $("#positions").innerHTML = P.length ? P.map((p) => {
    const res = p.last ? p.side * (p.last / p.entry - 1) * 100 : null;
    const take = p.take ? ((p.stage || 0) === 0 ? fmt(p.take) : (p.stage === 1 && p.take2 ? "т2 " + fmt(p.take2) : "трейл")) : "—";
    const strat = p.strat === "spike" ? "всплеск" + (p.took ? " (б/у)" : "") : "15:00";
    return `<div class="item ${p.side > 0 ? "l" : "s"}">
      <div class="hd"><span class="tk">${esc(p.ticker)} <span class="small ${p.side > 0 ? "up" : "down"}">${p.side > 0 ? "ЛОНГ" : "ШОРТ"}</span></span>
        <span class="big ${cls(res)}" style="font-size:18px">${pct(res)}</span></div>
      <div class="meta">${strat} · ${p.lots} лот.${p.carried ? " · перенос " + p.carried : ""}</div>
      <div class="grid4"><div><span>вход</span><b>${fmt(p.entry)}</b></div><div><span>сейчас</span><b>${fmt(p.last)}</b></div>
        <div><span>стоп</span><b class="down">${fmt(p.stop)}</b></div><div><span>тейк</span><b class="up">${take}</b></div></div>
      <div class="acts"><button class="btn small" data-chart="${esc(p.ticker)}" data-tf="${p.tf || 15}">График</button>
        <button class="btn small danger" data-close="${esc(p.ticker)}">Закрыть</button></div></div>`;
  }).join("") : `<div class="empty">Открытых позиций нет</div>`;

  // кандидаты «движения дня»
  const S = st.scan || [];
  $("#scanInfo").textContent = st.last_scan ? `порог ${(st.threshold * 100).toFixed(1)}%` : "";
  $("#scan").innerHTML = S.length ? S.map(([tk, mv, why]) =>
    `<div class="item tap" data-chart="${esc(tk)}" data-tf="5"><div class="hd"><span class="tk" style="font-size:14px">${esc(tk)}</span>
      <span class="${why === "СИГНАЛ" ? cls(mv) : "muted"}">${pct(mv * 100)}</span></div><div class="meta">${esc(why)}</div></div>`).join("")
    : `<div class="empty">Данные появятся после 10:01</div>`;

  // формации
  const K = st.spikes || [];
  $("#spikes").innerHTML = K.length ? K.map(([tm, tk, tfn, val, mult, body, why]) => {
    let w = why || "";
    if (w.startsWith("жду") && st.market != null && st.market !== 0 && (st.market > 0) !== (body > 0)) w += " (индекс против)";
    const tf = { "5м": 5, "15м": 15, "1ч": 60 }[tfn] || 15;
    return `<div class="item tap ${body > 0 ? "l" : "s"}" data-chart="${esc(tk)}" data-tf="${tf}">
      <div class="hd"><span class="tk">${esc(tk)} <span class="muted small">${esc(tfn)} · ${esc(tm)}</span></span>
        <span class="${body > 0 ? "up" : "down"}" style="font-weight:700">${body > 0 ? "ЛОНГ ⬆" : "ШОРТ ⬇"}</span></div>
      <div class="meta">оборот ${(val / 1e6).toFixed(0)} млн · ×${(+mult).toFixed(1)} · импульс ${pct(body * 100)}</div>
      <div class="meta" style="color:#c5cad6">${esc(w)}</div></div>`;
  }).join("") : `<div class="empty">Всплесков пока нет</div>`;
}

document.addEventListener("click", async (ev) => {
  const c = ev.target.closest("[data-chart]");
  if (c) { openChart(c.dataset.chart, +c.dataset.tf || 15); return; }
  const x = ev.target.closest("[data-close]");
  if (x) {
    const tk = x.dataset.close;
    if (await ask(`Закрыть позицию ${tk} по рынку?`, [["Закрыть", true, "danger"], ["Отмена", false]])) {
      const r = await api("/api/close", { ticker: tk }); toast(r.ok ? `Закрытие ${tk} запрошено` : r.msg);
    }
  }
});

$("#closeAllBtn").onclick = async () => {
  if (await ask("Закрыть ВСЕ открытые позиции по рынку?", [["Закрыть все", true, "danger"], ["Отмена", false]])) {
    await api("/api/close", { ticker: null }); toast("Закрытие всех позиций запрошено");
  }
};

$("#startBtn").onclick = async () => {
  if (!ST) return;
  if (!ST.trade) {
    const r = await api("/api/start", {}); toast(r.ok ? "Торговля включена" : r.msg); poll(); return;
  }
  let close = false;
  if ((ST.positions || []).length) {
    const a = await ask("Есть открытые позиции. Закрыть их по рынку?",
      [["Закрыть все и выключить торговлю", "close", "danger"], ["Выключить торговлю, позиции довести", "keep", "primary"], ["Отмена", null]]);
    if (!a) return; close = a === "close";
  }
  await api("/api/stop", { close }); toast("Торговля выключена"); poll();
};

// ---------------------------------------------------------------- график
const CH = { chart: null, tf: 15, ticker: "SBER", data: null, go: 0, lines: [] };
const TFNAME = { 1: "1м", 5: "5м", 15: "15м", 60: "1ч", 1440: "Д" };

function ensureChart() {
  if (CH.chart) return;
  const el = $("#chart");
  const c = LightweightCharts.createChart(el, {
    width: el.clientWidth, height: el.clientHeight,
    layout: { background: { color: "#131722" }, textColor: "#b2b5be", fontSize: 11 },
    grid: { vertLines: { color: "#1f2433" }, horzLines: { color: "#1f2433" } },
    rightPriceScale: { borderColor: "#283048" },
    timeScale: { borderColor: "#283048", timeVisible: true, secondsVisible: false, rightOffset: 4 },
    crosshair: { mode: 0 },
  });
  CH.chart = c;
  CH.candles = c.addCandlestickSeries({ upColor: "#26a69a", downColor: "#ef5350", borderVisible: false, wickUpColor: "#26a69a", wickDownColor: "#ef5350" });
  CH.vol = c.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
  c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  CH.e10 = c.addLineSeries({ color: "#e8e8e8", lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
  CH.e20 = c.addLineSeries({ color: "#2962ff", lineWidth: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
  c.subscribeCrosshairMove((p) => hover(p));
  new ResizeObserver(() => c.applyOptions({ width: el.clientWidth, height: el.clientHeight })).observe(el);
}

function openChart(tk, tf) { CH.ticker = tk; CH.tf = tf || CH.tf; $("#chTicker").value = tk; showTab("chart"); loadChart(); }

function markTf() { $$("#tfSeg button").forEach((b) => b.classList.toggle("on", +b.dataset.tf === CH.tf)); }
$$("#tfSeg button").forEach((b) => (b.onclick = () => { CH.tf = +b.dataset.tf; loadChart(); }));
$("#chTicker").addEventListener("change", () => { CH.ticker = $("#chTicker").value.trim().toUpperCase(); loadChart(); });
$("#chTicker").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); });
$("#chHist").onchange = () => drawTrades();
$("#chPrev").onclick = () => {
  const T = CH.data && CH.data.trades; if (!T || !T.length) return;
  selectTrade(T.length - 1 - (CH.go++ % T.length));
};

async function loadChart(quiet) {
  ensureChart(); markTf();
  const tk = CH.ticker, tf = CH.tf;
  if (!quiet) $("#chartInfo").textContent = `Загружаю ${tk} ${TFNAME[tf]}…`;
  try {
    const d = await api(`/api/chart?ticker=${encodeURIComponent(tk)}&tf=${tf}`);
    if (tk !== CH.ticker || tf !== CH.tf) return;
    const keepRange = quiet && CH.data && CH.data.bars.length ? CH.chart.timeScale().getVisibleLogicalRange() : null;
    CH.data = d;
    const B = d.bars;
    CH.candles.setData(B.map((b) => ({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c })));
    CH.vol.setData(B.map((b) => ({ time: b.t, value: b.v, color: b.c >= b.o ? "rgba(38,166,154,.45)" : "rgba(239,83,80,.45)" })));
    CH.e10.setData(B.map((b, i) => ({ time: b.t, value: d.ema10[i] })));
    CH.e20.setData(B.map((b, i) => ({ time: b.t, value: d.ema20[i] })));
    const pf = B.length ? B[B.length - 1].c : 1;
    const prec = pf >= 1000 ? 1 : pf >= 10 ? 2 : pf >= 1 ? 3 : 5;
    CH.candles.applyOptions({ priceFormat: { type: "price", precision: prec, minMove: Math.pow(10, -prec) } });
    $("#chTbank").href = d.tbank;
    drawTrades();
    if (keepRange) CH.chart.timeScale().setVisibleLogicalRange(keepRange);
    else CH.chart.timeScale().setVisibleLogicalRange({ from: B.length - 120, to: B.length + 3 });
    summary(d);
    if (!quiet) CH.go = 0;
  } catch (e) { if (e.message !== "401") $("#chartInfo").textContent = "Ошибка: " + e.message; }
}

function summary(d) {
  let s = "";
  if (![5, 15, 60].includes(d.tf)) s = "сделки робота показываются на 15м и 1ч";
  else if (d.tf === 5) s = "на 5 мин робот не торгует — переключите на 15м или 1ч";
  else if (d.tier === "3") s = "3-й эшелон — робот не торгует";
  else if (!d.trades.length) s = `сделок робота за ${d.days} дн. нет`;
  else {
    const done = d.trades.filter((t) => !t.open), w = done.filter((t) => t.result > 0).length;
    const sum = done.reduce((a, t) => a + t.result, 0) * 100;
    s = `${{ index: "индекс", 2: "2-й эшелон" }[d.tier] || ""}: сделок за ${d.days} дн. ${d.trades.length}, в плюсе ${w}, сумма <b class="${cls(sum)}">${pct(sum)}</b>`;
  }
  $("#chartInfo").innerHTML = `${esc(d.ticker)} ${TFNAME[d.tf]} · ${s}`;
  $("#chTrades").innerHTML = d.trades.slice().reverse().map((t, k) => {
    const i = d.trades.length - 1 - k;
    const last = t.exits[t.exits.length - 1];
    return `<div class="item tap ${t.side > 0 ? "l" : "s"}" data-trade="${i}"><div class="hd">
      <span><b>${new Date(t.entry_t * 1000).toISOString().slice(5, 16).replace("T", " ").replace("-", ".")}</b>
      <span class="small ${t.side > 0 ? "up" : "down"}">${t.side > 0 ? "ЛОНГ" : "ШОРТ"}</span> по ${fmt(t.entry)}</span>
      <b class="${cls(t.result)}">${t.open ? "открыта" : pct(t.result * 100)}</b></div>
      <div class="meta">${t.exits.map((x) => esc(x.why)).join(" → ")}</div></div>`;
  }).join("");
}
$("#chTrades").addEventListener("click", (e) => { const it = e.target.closest("[data-trade]"); if (it) { selectTrade(+it.dataset.trade); window.scrollTo({ top: 0, behavior: "smooth" }); } });

const EXIT_COLOR = (w) => w.startsWith("тейк") ? "#ffd54f" : w === "перенос" ? "#b388ff" : ["б/у", "открыта", "конец дня", "18:25"].includes(w) ? "#9e9e9e" : w === "трейлинг" ? "#4fc3f7" : "#ff5252";

function drawTrades() {
  const d = CH.data; if (!d) return;
  CH.lines.forEach((l) => CH.candles.removePriceLine(l)); CH.lines = [];
  const M = [];
  if ($("#chHist").checked) {
    for (const t of d.trades) {
      M.push(t.side > 0 ? { time: t.entry_t, position: "belowBar", color: "#00e676", shape: "arrowUp", text: "вход" }
        : { time: t.entry_t, position: "aboveBar", color: "#ff5252", shape: "arrowDown", text: "вход" });
      t.exits.forEach((x, j) => M.push({ time: x.t, position: t.side > 0 ? "aboveBar" : "belowBar", color: EXIT_COLOR(x.why), shape: "circle",
        text: x.why + (j === t.exits.length - 1 && !t.open ? " " + pct(t.result * 100) : "") }));
    }
  }
  M.sort((a, b) => a.time - b.time);
  CH.candles.setMarkers(M);
  for (const p of d.positions || []) {
    const L = [[p.entry, "#9598a1", "вход"], [p.stop, "#ef5350", "стоп"], [(p.stage || 0) >= 1 ? null : p.take, "#26a69a", "тейк1"], [(p.stage || 0) >= 2 ? null : p.take2, "#26a69a", "тейк2"]];
    for (const [v, c, n] of L) if (v) CH.lines.push(CH.candles.createPriceLine({ price: v, color: c, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: n }));
  }
  if (CH.sel != null && d.trades[CH.sel]) {
    const t = d.trades[CH.sel];
    for (const [v, c, n] of [[t.entry, "#e0e0e0", "вход"], [t.stop, "#ef5350", "стоп"], [t.t1, "#ffd54f", "тейк1"], [t.t2, "#ffd54f", "тейк2"]])
      if (v) CH.lines.push(CH.candles.createPriceLine({ price: v, color: c, lineWidth: 1, lineStyle: 1, axisLabelVisible: false, title: n }));
  }
}

function selectTrade(i) {
  const d = CH.data, t = d.trades[i]; CH.sel = i; drawTrades();
  const idx = d.bars.findIndex((b) => b.t >= t.entry_t);
  CH.chart.timeScale().setVisibleLogicalRange({ from: idx - 50, to: idx + 40 });
  $("#chartHover").innerHTML = tradeText(t);
}

function tradeText(t) {
  const ts = (x) => new Date(x * 1000).toISOString().slice(11, 16);
  return `<b class="${t.side > 0 ? "up" : "down"}">${t.side > 0 ? "ЛОНГ" : "ШОРТ"}</b>: импульс ${esc(t.impulse)}, касание ${esc(t.touch)}, вход ${ts(t.entry_t)} по ${fmt(t.entry)},
    стоп ${fmt(t.stop)}, тейк1 ${fmt(t.t1)}${t.t2 ? ", тейк2 " + fmt(t.t2) : ""} → ${t.exits.map((x) => `${esc(x.why)} ${ts(x.t)} ${fmt(x.px)} (${Math.round(x.frac * 100)}%)`).join(", ")};
    итог <b class="${cls(t.result)}">${pct(t.result * 100)}</b>`;
}

function hover(p) {
  const d = CH.data; if (!d || !p || !p.time) return;
  const i = d.bars.findIndex((b) => b.t === p.time); if (i < 0) return;
  const b = d.bars[i], ch = b.o ? (b.c / b.o - 1) * 100 : 0;
  const tr = d.trades.find((t) => t.entry_t === b.t);
  $("#chartHover").innerHTML = `${new Date(b.t * 1000).toISOString().slice(5, 16).replace("T", " ")} О ${fmt(b.o)} М ${fmt(b.h)} Н ${fmt(b.l)} З ${fmt(b.c)}
    <span class="${cls(ch)}">(${pct(ch)})</span> · EMA10 ${fmt(d.ema10[i])} · <span style="color:#5b8cff">EMA20 ${fmt(d.ema20[i])}</span>` + (tr ? "<br>" + tradeText(tr) : "");
}

// ---------------------------------------------------------------- сделки и журнал
let TRV = "robot";
$$("#trSeg button").forEach((b) => (b.onclick = () => { TRV = b.dataset.v; $$("#trSeg button").forEach((x) => x.classList.toggle("on", x === b)); loadTrades(); }));
async function loadTrades() {
  $("#trJournalFill").hidden = TRV !== "journal";
  try {
    if (TRV === "robot") {
      const d = await api("/api/trades");
      $("#trSum").innerHTML = d.n ? `Сделок: <b>${d.n}</b>, в плюсе ${d.wins} (${Math.round(d.wins / d.n * 100)}%), сумма результатов <b class="${cls(d.sum)}">${pct(d.sum)}</b>`
        : "Закрытых сделок робота пока нет.";
      $("#trList").innerHTML = d.rows.map((r) => {
        const v = +r.result_pct;
        return `<div class="item tap" data-chart="${esc(r.ticker)}" data-tf="15"><div class="hd"><span><b>${esc(r.ticker)}</b> <span class="muted small">${esc(r.strat)} · ${esc(r.side)}</span></span>
          <b class="${cls(v)}">${pct(v)}</b></div><div class="meta">${esc(r.date)} · ${esc(r.lots)} лот. · ${esc(r.entry)} → ${esc(r.exit)} · ${esc(r.reason)}</div></div>`;
      }).join("");
    } else {
      const d = await api("/api/journal");
      $("#trSum").innerHTML = d.days ? `Дней: ${d.days} (${esc(d.period)}). Сделок: ${d.trades}, в плюсе ${d.wins} (${d.trades ? Math.round(d.wins / d.trades * 100) : 0}%).
        Средняя после затрат: ${pct(d.avg, 3)}. Итог <b class="${cls(d.total)}">${pct(d.total)}</b>, просадка ${pct(d.dd)}`
        : "Журнал пуст. Укажите дату и нажмите «Дозаполнить», или ждите 17:20.";
      $("#trList").innerHTML = d.rows.map((r) => {
        const v = +r.net_pct;
        return `<div class="item"><div class="hd"><span><b>${esc(r.ticker)}</b> <span class="muted small">${esc(r.side)} · движение ${pct(+r.move_pct)}</span></span>
          <b class="${cls(v)}">${pct(v)}</b></div><div class="meta">${esc(r.date)} · ${esc(r.entry)} → ${fmt(+r.exit)} · ${r.reason === "stop" ? "стоп" : "17:00"}</div></div>`;
      }).join("");
    }
  } catch (e) { if (e.message !== "401") $("#trSum").textContent = "Ошибка: " + e.message; }
}
$("#fillBtn").onclick = async () => {
  const v = $("#fillFrom").value; if (!v) { toast("Укажите дату"); return; }
  await api("/api/journal_fill", { from: v }); toast("Считаю журнал — ход виден в «Журнале действий» на вкладке «Робот»");
};

// ---------------------------------------------------------------- настройки
const LEV = { "1": "без маржи", "auto": "с маржой — плечо брокера по бумаге", "2": "с маржой ×2", "3": "с маржой ×3", "5": "с маржой ×5" };
let SET = null;
async function loadSettings() {
  try { SET = await api("/api/settings"); renderSettings(SET); } catch (e) { if (e.message !== "401") toast("Ошибка: " + e.message); }
}
function chk(name, label, hint) {
  return `<label class="c"><input type="checkbox" name="${name}" ${SET[name] ? "checked" : ""}><span>${label}</span></label>${hint ? `<div class="hint">${hint}</div>` : ""}`;
}
function sel(name, label, opts) {
  return `<label class="f"><span>${label}</span><select name="${name}">${opts.map(([v, t]) => `<option value="${v}" ${String(SET[name]) === String(v) ? "selected" : ""}>${t}</option>`).join("")}</select></label>`;
}
function inp(name, label, type = "text", extra = "") {
  return `<label class="f"><span>${label}</span><input type="${type}" name="${name}" value="${esc(SET[name] ?? "")}" ${extra}></label>`;
}
function renderSettings(s) {
  const tfs = s.spike_tfs || [];
  const mk = s.spike_markets || [];
  $("#setForm").innerHTML = `
  <div class="grp"><h3>Т-Инвестиции</h3>
    <label class="f"><span>Токен песочницы ${s.token_set ? `(сохранён: ${esc(s.token_hint)}; оставьте пустым, чтобы не менять)` : "(не введён)"}</span>
      <input type="password" name="token" placeholder="${s.token_set ? "••••••••" : "t.xxxxx…"}" autocomplete="off"></label>
    ${chk("sandbox_token", "Это токен песочницы (демо-счёт)", "Робот торгует только на демо-счёте песочницы.")}
    ${inp("capital", "Капитал песочницы, ₽", "number", 'inputmode="numeric"')}
  </div>
  <div class="grp"><h3>Всплеск объёма + откат к EMA20</h3>
    ${chk("spike_on", "Робот торгует по этой формации")}
    <div class="f"><span>Таймфреймы</span>
      <label class="c"><input type="checkbox" name="tf5" ${tfs.includes(5) ? "checked" : ""}><span>5 мин (только уведомления)</span></label>
      <label class="c"><input type="checkbox" name="tf15" ${tfs.includes(15) ? "checked" : ""}><span>15 мин</span></label>
      <label class="c"><input type="checkbox" name="tf60" ${tfs.includes(60) ? "checked" : ""}><span>1 час</span></label></div>
    ${chk("trend_on", "Искать трендовые откаты", "Импульс на объёме был в последние дни, EMA10 над EMA20, касание EMA20 + подтверждение.")}
    ${chk("trend_trade", "…и торговать все трендовые откаты", "На истории в целом в минусе; по умолчанию только по профилю ниже.")}
    ${chk("tier_profile", "Профиль по эшелонам", "Индекс — формация 15 мин и 1 час; 2-й эшелон — формация 1 час и трендовый откат 15 мин; 3-й эшелон не торгуется.")}
    ${chk("all_hours", "Входить по формациям с 07:00 до 23:40", "Выход 23:45, позиция в плюсе переносится (до 2 дней).")}
    <div class="f"><span>Чем торговать</span>
      ${(s.markets_all || []).map(([k, t]) => `<label class="c"><input type="checkbox" name="mk_${k}" ${mk.includes(k) ? "checked" : ""}><span>${esc(t)}</span></label>`).join("")}</div>
    ${sel("two_takes", "Тейки", [["true", "два тейка"], ["false", "один тейк"]])}
    ${sel("spike_half", "Закрыть на тейке1, %", [30, 50, 70, 100].map((v) => [v, v]))}
    ${sel("spike_trail", "Трейлинг, R (расстояние до стопа)", [0.5, 1, 1.5, 2].map((v) => [v, v]))}
    ${sel("spike_max", "Макс. позиций", [1, 2, 3, 5, 8, 10].map((v) => [v, v]))}
    ${sel("spike_share", "Вход в позицию, % от портфеля", [1, 2, 3, 5, 10, 15, 20, 25, 33, 50, 100].map((v) => [v, v + "%"]))}
    ${sel("leverage", "Маржа", Object.entries(LEV))}
    ${sel("direction", "Направление", [["both", "лонг и шорт"], ["long", "только лонг"], ["short", "только шорт"]])}
  </div>
  <div class="grp"><h3>Движение дня</h3>
    ${chk("day_on", "Торговать «движение дня» (вход в заданное время)")}
    ${inp("threshold", "Порог движения, %", "number", 'step="0.1" inputmode="decimal"')}
    ${inp("entry", "Время входа (МСК)", "time")}
    ${chk("rescan", "Добирать новые сигналы до 16:30")}
    ${chk("auto_journal", "Считать журнал «движения дня» в 17:20")}
  </div>
  <div class="grp"><h3>Telegram</h3>
    <label class="f"><span>Токен бота ${s.tg_token_set ? "(сохранён; пусто — не менять)" : ""}</span>
      <input type="password" name="tg_token" placeholder="${s.tg_token_set ? "••••••••" : "123456:ABC…"}" autocomplete="off"></label>
    ${inp("tg_chat", "ID чата")}
    <div class="row"><button type="button" class="btn small" id="tgFind">Найти чат</button><button type="button" class="btn small" id="tgTest">Тест</button></div>
    ${chk("tg_form", "Все найденные формации")}
    ${chk("tg_trend", "Трендовые откаты")}
    ${chk("tg_trades", "Входы и выходы робота")}
    ${chk("tg_imp", "Всплески объёма (импульсы)")}
  </div>
  <div class="grp"><h3>Этот телефон</h3>
    <label class="c"><input type="checkbox" id="phSound" ${store("sound") === "0" ? "" : "checked"}><span>Звук при входе и выходе (пока открыт пульт)</span></label>
    <button type="button" class="btn small ghost" id="forget">Сменить сервер / код подключения</button>
    <p class="muted small">Версия сервера: ${esc(ST ? ST.version : "")}</p>
  </div>
  <button type="submit" class="btn primary wide save">Сохранить настройки</button>`;
  $("#tgFind").onclick = async () => {
    const r = await api("/api/tg_find", { tg_token: $("#setForm").tg_token.value.trim() });
    if (r.ok) { $("#setForm").tg_chat.value = r.chat; toast(`Найден чат ${r.name}. Нажмите «Сохранить».`); }
    else toast("Сообщений боту нет. Напишите боту в Telegram и нажмите ещё раз.");
  };
  $("#tgTest").onclick = async () => { try { await api("/api/tg_test", {}); toast("Отправлено — проверьте Telegram (нужно сначала сохранить)"); } catch (e) { toast("Не отправилось: " + e.message); } };
  $("#phSound").onchange = (e) => store("sound", e.target.checked ? "1" : "0");
  $("#forget").onclick = async () => {
    if (!(await ask("Забыть код подключения на этом телефоне?", [["Забыть", true, "danger"], ["Отмена", false]]))) return;
    store("key", null);
    if (NATIVE && NATIVE.reset) NATIVE.reset(); else showLogin("");
  };
}
$("#setForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, v = (n) => f[n] ? f[n].value : undefined, c = (n) => !!(f[n] && f[n].checked);
  const out = {
    sandbox_token: c("sandbox_token"), capital: parseFloat(String(v("capital")).replace(/\s/g, "")) || 1000000,
    spike_on: c("spike_on"), spike_tfs: [5, 15, 60].filter((t) => c("tf" + t)),
    trend_on: c("trend_on"), trend_trade: c("trend_trade"), tier_profile: c("tier_profile"), all_hours: c("all_hours"),
    spike_markets: (SET.markets_all || []).map(([k]) => k).filter((k) => c("mk_" + k)),
    two_takes: v("two_takes") === "true", spike_half: +v("spike_half"), spike_trail: +v("spike_trail"), spike_max: +v("spike_max"),
    spike_share: +v("spike_share"), leverage: v("leverage"), direction: v("direction"),
    day_on: c("day_on"), threshold: parseFloat(String(v("threshold")).replace(",", ".")) || 2, entry: v("entry") || "15:00",
    rescan: c("rescan"), auto_journal: c("auto_journal"),
    tg_chat: (v("tg_chat") || "").trim(), tg_form: c("tg_form"), tg_trend: c("tg_trend"), tg_trades: c("tg_trades"), tg_imp: c("tg_imp"),
  };
  if (!out.spike_markets.length) out.spike_markets = ["imoex"];
  const tok = (v("token") || "").trim(); if (tok) out.token = tok;
  const tgt = (v("tg_token") || "").trim(); if (tgt) out.tg_token = tgt;
  try { await api("/api/settings", out); toast("Сохранено. Робот перезапускается с новыми настройками."); loadSettings(); }
  catch (err) { toast("Ошибка: " + err.message); }
});

// ---------------------------------------------------------------- вход
function showLogin(msg) {
  if (NATIVE && NATIVE.reset && !KEY) { NATIVE.reset(); return; }
  $("#app").hidden = true; $("#login").hidden = false; $("#loginErr").textContent = msg || "";
}
$("#codeBtn").onclick = () => {
  const c = parseCode($("#codeIn").value);
  if (!c) { $("#loginErr").textContent = "Код не распознан. Он начинается с moexrobot://"; return; }
  KEY = c.key; store("key", KEY); start();
};

let timer = null;
function start() {
  if (!KEY) { showLogin(""); return; }
  $("#login").hidden = true; $("#app").hidden = false;
  poll(); clearInterval(timer); timer = setInterval(() => { if (!document.hidden) poll(); }, 3000);
  setInterval(() => { if (!document.hidden && TAB === "chart" && CH.data && CH.tf <= 60) loadChart(true); }, 30000);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && KEY) poll(); });
start();
