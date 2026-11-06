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
  if (t === "market") loadMarket();
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
  $("#clock").textContent = "МСК " + st.time.slice(11) + " · v" + String(st.version).split(" ")[0];
  const ix = st.index;
  $("#idx").innerHTML = ix ? `IMOEX ${ix.value.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
    <small class="${cls(ix.pct)}">(${ix.chg > 0 ? "+" : ""}${ix.chg.toLocaleString("ru-RU", { maximumFractionDigits: 2 })}; ${pct(ix.pct)})</small>` : "";
  $("#idx").title = ix ? "обновлено " + ix.time + " МСК" : "";
  const b = $("#tradeBadge");
  if (!st.running) { b.textContent = st.token_set ? "робот запускается" : "нужен токен"; b.className = "badge dead"; }
  else if (st.trade) { b.textContent = "● ТОРГОВЛЯ ВКЛ"; b.className = "badge on"; }
  else { b.textContent = "наблюдение"; b.className = "badge off"; }
  $("#status").textContent = st.status || "";
  $("#market").innerHTML = ix ? `<span class="${cls(ix.pct)}">${pct(ix.pct)}</span>` : "—";     // реальное изменение к вчерашнему закрытию
  const mo = st.idx_ref === "imoex" ? null : (st.market_open != null ? st.market_open : st.market);
  $("#marketOpen").textContent = st.idx_ref === "imoex_open" ? `для робота: IMOEX от открытия основной сессии ${st.market != null ? pct(st.market * 100) : "—"}`
    : st.idx_ref === "imoex" ? "направление для робота — по индексу IMOEX к вчерашнему дню"
    : mo == null ? "" : `для робота: акции индекса с открытия дня ${pct(mo * 100)}`;
  const sb = $("#startBtn");
  if (st.trade) { sb.textContent = "Стоп"; sb.className = "btn stop"; }
  else { sb.textContent = "Старт"; sb.className = "btn start"; }
  const pr = st.pair;
  const pairTxt = pr && pr.enabled ? ` Пара серверов: торгует ${pr.active ? "этот" : "второй"} сервер (${pr.active ? "" : pr.peer_host + ", "}этот — ${pr.role === "primary" ? "основной" : "резервный"}); второй ${pr.peer_ok ? "на связи" : "не отвечает с " + pr.peer_last_ok}.` : "";
  $("#robotHint").textContent = (st.trade ? "Робот сам входит и выходит по стратегиям. «Стоп» — выключить новые сделки."
    : st.token_set ? "Робот следит за рынком и шлёт формации, но не торгует. «Старт» — включить торговлю."
      : "Введите токен песочницы Т-Инвестиций во вкладке «Настройки».") + pairTxt +
    (st.broker === "finam" ? " Брокер: Финам." : st.broker === "tinvest_real" ? " Брокер: РЕАЛЬНЫЙ счёт Т-Инвестиций." : " Брокер: песочница Т-Инвестиций.");

  // позиции
  const P = st.positions || [];
  $("#posCount").textContent = P.length ? `(${P.length})` : "";
  $("#closeAllBtn").hidden = P.length < 2;
  $("#closeProfitBtn").hidden = P.length < 1;
  const posCard = (p) => {
    const res = p.last ? p.side * (p.last / p.entry - 1) * 100 : null;
    const take = p.take ? ((p.stage || 0) === 0 ? fmt(p.take) : (p.stage === 1 && p.take2 ? "т2 " + fmt(p.take2) : "трейл")) : "—";
    const tfl = { 5: "5м", 15: "15м", 60: "1ч" }[p.tf] || "";
    const strat = p.strat === "spike" ? "всплеск " + (tfl || "15м") + (p.took ? " (б/у)" : "") : p.strat === "manual" ? "вручную" + (tfl ? " " + tfl : "") : "15:00";
    return `<div class="item ${p.side > 0 ? "l" : "s"}">
      <div class="hd"><span class="tk">${esc(p.ticker)} <span class="small ${p.side > 0 ? "up" : "down"}">${p.side > 0 ? "ЛОНГ" : "ШОРТ"}</span></span>
        <span style="text-align:right"><span class="big ${cls(res)}" style="font-size:18px">${pct(res)}</span>
        ${p.pnl_rub != null ? `<div class="small ${cls(p.pnl_rub)}">${rub(p.pnl_rub, true)}</div>` : ""}</span></div>
      <div class="meta">${strat} · ${p.lots} лот.${p.carried ? " · перенос " + p.carried : ""}</div>
      <div class="grid4"><div><span>вход</span><b>${fmt(p.entry)}</b></div><div><span>сейчас</span><b>${fmt(p.last)}</b></div>
        <div><span>стоп</span><b class="down">${fmt(p.stop)}</b></div><div><span>тейк</span><b class="up">${take}</b></div></div>
      <div class="acts"><button class="btn small" data-chart="${esc(p.ticker)}" data-tf="${p.tf || 15}">График</button>
        <button class="btn small" data-lv="${esc(p.ticker)}" data-stop="${p.stop || ""}" data-take="${p.take || ""}">Стоп/тейк</button>
        <button class="btn small danger" data-close="${esc(p.ticker)}">Закрыть</button></div>
      <div class="row" style="margin-top:8px"><span class="small muted">Во время выхода (${p.exit_min ? String(Math.floor(p.exit_min / 60)).padStart(2, "0") + ":" + String(p.exit_min % 60).padStart(2, "0") : "—"}):</span>
        <div class="seg">${[["", "авто"], ["carry", "перенести"], ["close", "закрыть"]].map(([v, n]) =>
          `<button data-carry="${esc(p.ticker)}" data-mode="${v}" class="${(p.carry_pick || "") === v ? "on" : ""}">${n}</button>`).join("")}</div></div></div>`;
  };
  const PS = st.pos_summary || { by_strat: {}, total: {} };
  const grpHead = (name, d) => `<div class="grp-h"><span>${name} · ${d.n} поз.</span>
    <span>${d.avg_pct != null ? `<span class="${cls(d.avg_pct)}">${pct(d.avg_pct)} ср.</span> · ` : ""}<b class="${cls(d.pnl_rub)}">${d.known ? rub(d.pnl_rub, true) : "—"}</b></span></div>`;
  let html = "";
  for (const [k, kp, name] of [["всплеск", "spike", "Всплеск + EMA20"], ["15:00", "15", "Движение дня"], ["вручную", "manual", "Открытые вручную"]]) {
    const G = P.filter((p) => (p.strat === "spike" ? "spike" : p.strat === "manual" ? "manual" : "15") === kp);
    if (!G.length) continue;
    html += grpHead(name, PS.by_strat[k] || { n: G.length }) + G.map(posCard).join("");
  }
  if (P.length) {
    const T = PS.total || {};
    html += `<div class="grp-h total"><span>Итого открытые · ${P.length} поз.</span><b class="${cls(T.pnl_rub)}">${T.known ? rub(T.pnl_rub, true) : "—"}</b></div>`;
  }
  $("#positions").innerHTML = html || `<div class="empty">Открытых позиций нет</div>`;

  // кандидаты «движения дня»
  const S = st.scan || [];
  $("#scanInfo").textContent = st.last_scan ? `порог ${(st.threshold * 100).toFixed(1)}%` : "";
  $("#scan").innerHTML = S.length ? S.map(([tk, mv, why]) =>
    `<div class="item tap" data-chart="${esc(tk)}" data-tf="5"><div class="hd"><span class="tk" style="font-size:14px">${esc(tk)}</span>
      <span class="${why === "СИГНАЛ" ? cls(mv) : "muted"}">${pct(mv * 100)}</span></div><div class="meta">${why === "СИГНАЛ"
      ? (st.day_on === false ? "проходит порог, но «движение дня» выключено" : st.day_mode !== "signal"
        ? `кандидат — робот войдёт в ${esc(st.entry || "15:00")}, если движение сохранится` : "СИГНАЛ — вход сразу")
      : esc(why)}</div></div>`).join("")
    : `<div class="empty">Данные появятся после 07:01</div>`;

  // формации
  const K = st.spikes || [];
  $("#spikes").innerHTML = K.length ? K.map(([tm, tk, tfn, val, mult, body, why, sigt]) => {
    let w = why || "";
    if (w.startsWith("жду") && st.market != null && st.market !== 0 && (st.market > 0) !== (body > 0)) w += " (индекс против)";
    const tf = { "5м": 5, "15м": 15, "1ч": 60 }[tfn] || 15;
    return `<div class="item tap ${body > 0 ? "l" : "s"}" data-chart="${esc(tk)}" data-tf="${tf}">
      <div class="hd"><span class="tk">${esc(tk)} <span class="muted small">${esc(tfn)} · импульс ${esc(tm)}${sigt ? " · <b style='color:#e6e9f0'>сигнал " + esc(sigt) + "</b>" : ""}</span></span>
        <span class="${body > 0 ? "up" : "down"}" style="font-weight:700">${body > 0 ? "ЛОНГ ⬆" : "ШОРТ ⬇"}</span></div>
      <div class="meta">оборот ${(val / 1e6).toFixed(0)} млн · ×${(+mult).toFixed(1)} · импульс ${pct(body * 100)}</div>
      <div class="meta" style="color:#c5cad6">${esc(w)}</div></div>`;
  }).join("") : `<div class="empty">Всплесков пока нет</div>`;
}

document.addEventListener("click", async (ev) => {
  const c = ev.target.closest("[data-chart]");
  if (c) { openChart(c.dataset.chart, +c.dataset.tf || 15); return; }
  const lv = ev.target.closest("[data-lv]");
  if (lv) {
    $("#modalText").innerHTML = `<b>${esc(lv.dataset.lv)}</b> — новые уровни<div class="col" style="margin-top:10px">
      <label class="col small"><span class="muted">Стоп</span><input id="lvStop" type="number" step="any" inputmode="decimal" value="${lv.dataset.stop}" style="padding:9px;border-radius:10px;border:1px solid var(--line);background:var(--card2);color:var(--fg);font:inherit"></label>
      <label class="col small"><span class="muted">Тейк</span><input id="lvTake" type="number" step="any" inputmode="decimal" value="${lv.dataset.take}" style="padding:9px;border-radius:10px;border:1px solid var(--line);background:var(--card2);color:var(--fg);font:inherit"></label></div>`;
    const box = $("#modalBtns"); box.innerHTML = "";
    const ok = document.createElement("button"); ok.className = "btn primary"; ok.textContent = "Сохранить";
    const no = document.createElement("button"); no.className = "btn"; no.textContent = "Отмена";
    no.onclick = () => { $("#modal").hidden = true; };
    ok.onclick = async () => {
      const st = $("#lvStop").value, tk = $("#lvTake").value; $("#modal").hidden = true;
      const r = await api("/api/levels", { ticker: lv.dataset.lv, stop: st, take: tk });
      toast(r.msg || "Готово"); poll();
    };
    box.append(ok, no); $("#modal").hidden = false; return;
  }
  const cr = ev.target.closest("[data-carry]");
  if (cr) {
    const r = await api("/api/carry", { ticker: cr.dataset.carry, mode: cr.dataset.mode });
    toast(r.ok ? `${cr.dataset.carry}: ` + ({ carry: "перенести на следующий день", close: "закрыть во время выхода" }[cr.dataset.mode] || "по настройкам стратегии") : r.msg);
    poll(); return;
  }
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

$("#closeProfitBtn").onclick = async () => {
  if (await ask("Закрыть по рынку все позиции, которые сейчас в плюсе?", [["Закрыть в плюсе", true, "danger"], ["Отмена", false]])) {
    await api("/api/close", { ticker: "+" }); toast("Закрытие позиций в плюсе запрошено");
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
    timeScale: { borderColor: "#283048", timeVisible: true, secondsVisible: false, rightOffset: 12 },
    crosshair: { mode: 0 },
  });
  CH.chart = c;
  CH.candles = c.addCandlestickSeries({ upColor: "#26a69a", downColor: "#ef5350", borderVisible: false, wickUpColor: "#26a69a", wickDownColor: "#ef5350" });
  CH.vol = c.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
  c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  CH.e10 = c.addLineSeries({ color: "#e8e8e8", lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
  CH.e20 = c.addLineSeries({ color: "#2962ff", lineWidth: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
  CH.e200 = c.addLineSeries({ color: "#ef5350", lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false, autoscaleInfoProvider: () => null });
  CH.s200 = c.addLineSeries({ color: "#b388ff", lineWidth: 1, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false, autoscaleInfoProvider: () => null });
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

let LIVE_BUSY = false;
async function chartLive() {
  if (LIVE_BUSY) return;
  LIVE_BUSY = true;
  const tk = CH.ticker;
  try {
    const d = await api(`/api/last?ticker=${encodeURIComponent(tk)}`);
    const B = CH.data && CH.data.bars;
    if (tk !== CH.ticker || !B || !B.length || !d.price) return;
    const b = B[B.length - 1];
    if (CH.tf < 1440 && d.t >= b.t + CH.tf * 60) return;     // началась новая свеча — её добавит обновление свечей
    if (b.c === d.price) return;
    b.c = d.price; b.h = Math.max(b.h, d.price); b.l = Math.min(b.l, d.price);
    CH.candles.update({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c });
  } catch (e) { /* следующая попытка через секунду */ }
  finally { LIVE_BUSY = false; }
}
let TAIL_BUSY = false;
async function chartTail() {
  if (TAIL_BUSY) return;
  TAIL_BUSY = true;
  const tk = CH.ticker, tf = CH.tf;
  try {
    const d = await api(`/api/chart_tail?ticker=${encodeURIComponent(tk)}&tf=${tf}`);
    if (tk !== CH.ticker || tf !== CH.tf || !CH.data) return;
    const B = CH.data.bars, lastT = B.length ? B[B.length - 1].t : 0;
    for (const b of d.bars) {
      if (b.t < lastT - tf * 60 * 3) continue;
      CH.candles.update({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c });
      CH.vol.update({ time: b.t, value: b.v, color: b.c >= b.o ? "rgba(38,166,154,.45)" : "rgba(239,83,80,.45)" });
      const i = B.findIndex((x) => x.t === b.t);
      if (i >= 0) B[i] = b; else if (b.t > lastT) B.push(b);
    }
  } catch (e) { /* тихо — следующая попытка через 3 с */ }
  finally { TAIL_BUSY = false; }
}
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
    if (!quiet) CH.chart.priceScale("right").applyOptions({ autoScale: true });   // новая бумага/ТФ — цена снова по размеру свечей
    CH.candles.setData(B.map((b) => ({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c })));
    CH.vol.setData(B.map((b) => ({ time: b.t, value: b.v, color: b.c >= b.o ? "rgba(38,166,154,.45)" : "rgba(239,83,80,.45)" })));
    CH.e10.setData(B.map((b, i) => ({ time: b.t, value: d.ema10[i] })));
    CH.e20.setData(B.map((b, i) => ({ time: b.t, value: d.ema20[i] })));
    CH.e200.setData(d.ema200 ? B.map((b, i) => ({ time: b.t, value: d.ema200[i] })).filter((x, i) => i >= 50) : []);
    CH.s200.setData(d.sma200 ? B.map((b, i) => ({ time: b.t, value: d.sma200[i] })).filter((x) => x.value != null) : []);
    const pf = B.length ? B[B.length - 1].c : 1;
    const prec = pf >= 1000 ? 1 : pf >= 10 ? 2 : pf >= 1 ? 3 : 5;
    CH.candles.applyOptions({ priceFormat: { type: "price", precision: prec, minMove: Math.pow(10, -prec) } });
    $("#chTbank").href = d.tbank;
    drawTrades();
    if (keepRange) CH.chart.timeScale().setVisibleLogicalRange(keepRange);
    else CH.chart.timeScale().setVisibleLogicalRange({ from: B.length - 120, to: B.length + 12 });
    summary(d);
    if (!quiet) CH.go = 0;
  } catch (e) { if (e.message !== "401") $("#chartInfo").textContent = "Ошибка: " + e.message; }
}

function summary(d) {
  let s = "";
  if (![5, 15, 60].includes(d.tf)) s = "сделки робота показываются на 15м и 1ч";
  else if (d.tf === 5) s = "проверка по истории — на 15м и 1ч; настоящие сделки робота отмечены и здесь";
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
  // настоящие сделки робота: время «ГГГГ-ММ-ДД ЧЧ:ММ» (МСК) → ближайшая свеча не позже
  const ts = (x) => { const m = /^(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d)/.exec(x || ""); return m ? Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5]) / 1000 : null; };
  const bar = (t) => { if (t == null || !d.bars.length || t < d.bars[0].t) return null; let k = null; for (const b of d.bars) { if (b.t <= t) k = b.t; else break; } return k; };
  for (const r of d.real || []) {
    const te = bar(ts(r.entry_time)), tx = bar(ts(r.exit_time));
    if (te != null) M.push({ time: te, position: r.side > 0 ? "belowBar" : "aboveBar", color: r.side > 0 ? "#00e676" : "#ff5252", shape: r.side > 0 ? "arrowUp" : "arrowDown", text: "вход" });
    if (tx != null) M.push({ time: tx, position: r.side > 0 ? "aboveBar" : "belowBar", color: (r.result_pct || 0) > 0 ? "#26a69a" : "#ef5350", shape: "circle", text: (r.reason || "") + " " + pct(r.result_pct || 0) });
  }
  for (const p of d.positions || []) {
    const te = bar(ts(`${p.date || ""} ${String(p.time || "").slice(0, 5)}`));
    if (te != null) M.push({ time: te, position: p.side > 0 ? "belowBar" : "aboveBar", color: p.side > 0 ? "#00e676" : "#ff5252", shape: p.side > 0 ? "arrowUp" : "arrowDown", text: "вход (открыта)" });
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

// ---------------------------------------------------------------- рынки
const MK = { type: "share", tier: "", sort: "pct_desc", q: "", data: null, formOnly: false };
function bigRub(v) {
  if (!v) return "—";
  return v >= 1e9 ? (v / 1e9).toFixed(1).replace(".", ",") + " млрд" : v >= 1e6 ? (v / 1e6).toFixed(0) + " млн" : (v / 1e3).toFixed(0) + " тыс";
}
$$("#mkType button").forEach((b) => (b.onclick = () => { MK.type = b.dataset.v; $$("#mkType button").forEach((x) => x.classList.toggle("on", x === b)); renderMarket(); }));
$$("#mkTier button").forEach((b) => (b.onclick = () => { MK.tier = b.dataset.v; $$("#mkTier button").forEach((x) => x.classList.toggle("on", x === b)); renderMarket(); }));
$("#mkSearch").addEventListener("input", (e) => { MK.q = e.target.value.trim().toUpperCase(); renderMarket(); });
$("#mkSort").onchange = (e) => { MK.sort = e.target.value; renderMarket(); };
$("#mkForm").onchange = (e) => { MK.formOnly = e.target.checked; renderMarket(); };
const formBadge = (f) => `<span class="fb ${f.status === "вход" ? "in" : f.side > 0 ? "l" : "s"}" title="${esc(f.full)}">EMA20 ${esc(f.tf)} ${f.side > 0 ? "▲" : "▼"} ${esc(f.status)}</span>`;
async function loadMarket() {
  try { MK.data = await api("/api/market"); renderMarket(); }
  catch (e) { if (e.message !== "401") $("#mkSum").textContent = "Ошибка: " + e.message; }
}
function renderMarket() {
  const d = MK.data; if (!d) { $("#mkSum").textContent = "Загружаю…"; return; }
  $("#mkTier").hidden = MK.type !== "share";
  let R = d.rows.filter((r) => r.type === MK.type && r.price != null);
  if (MK.type === "share" && MK.tier) R = R.filter((r) => MK.tier === "blue" ? r.blue : r.tier === MK.tier);
  if (MK.formOnly) R = R.filter((r) => r.forms && r.forms.length);
  if (MK.q) R = R.filter((r) => r.ticker.toUpperCase().includes(MK.q) || String(r.name).toUpperCase().includes(MK.q));
  const k = MK.sort, pv = (r) => r.pct == null ? -1e9 : r.pct;
  R.sort((a, b) => k === "pct_desc" ? pv(b) - pv(a) : k === "pct_asc" ? (a.pct ?? 1e9) - (b.pct ?? 1e9)
    : k === "abs" ? Math.abs(b.pct || 0) - Math.abs(a.pct || 0) : k === "value" ? (b.value || 0) - (a.value || 0) : String(a.name).localeCompare(String(b.name), "ru"));
  const s = d.summary || {};
  $("#mkSum").innerHTML = `Акции: растут <b class="up">${s.up || 0}</b>, падают <b class="down">${s.down || 0}</b>, оборот ${bigRub(s.value)} ₽ ·
    ${s.live ? "цены акций — в реальном времени (Т-Инвестиции)" : "данные Мосбиржи (могут быть с задержкой)"} · обновлено ${esc(d.time.slice(11))} · показано ${R.length} · с формацией EMA20 сегодня: ${d.forms_n || 0}`
    + (d.errors && d.errors.length ? `<br><span class="down">${esc(d.errors.join("; "))}</span>` : "");
  const tierName = { index: "индекс", 2: "2-й эш.", 3: "3-й эш." };
  $("#mkList").innerHTML = R.length ? R.slice(0, 400).map((r) => `<div class="item tap" ${r.type === "share" ? `data-chart="${esc(r.ticker)}" data-tf="15"` : ""}>
    <div class="hd"><span><b>${esc(r.ticker)}</b> <span class="muted small">${esc(r.name)}</span></span>
      <b class="${cls(r.pct)}">${pct(r.pct)}</b></div>
    <div class="meta">${fmt(r.price)}${r.live ? " ●" : ""} · оборот ${bigRub(r.value)} · ${esc(r.time || "—")}
      ${r.type === "share" ? " · " + (r.blue ? "голубая фишка · " : "") + (tierName[r.tier] || "") : ""}</div>
    ${r.forms && r.forms.length ? `<div class="fbs">${r.forms.map(formBadge).join("")}</div>` : ""}</div>`).join("")
    : `<div class="empty">Ничего не найдено</div>`;
}
setInterval(() => { if (!document.hidden && TAB === "market") loadMarket(); }, 30000);

// ---------------------------------------------------------------- сделки и журнал
let TRV = "results", TRF = "";
const rub = (v, sign) => v == null || isNaN(v) ? "—" : (sign && v > 0 ? "+" : "") + Math.round(v).toLocaleString("ru-RU").replace(/,/g, " ") + " ₽";
async function loadCalendar() {
  let c; try { c = await api("/api/calendar"); } catch (e) { return; }
  const today = c.days[0];
  $("#calInfo").textContent = today ? "· сегодня: " + today.name : "";
  $("#cal").innerHTML = c.days.map((d) => {
    const on = d.kind === "trading" || (d.kind === "weekend_session" && c.trade_weekends);
    const col = d.kind === "trading" ? "up" : d.kind === "weekend_session" ? "" : "down";
    return `<div class="item"><div class="hd"><span><b>${d.date.split("-").reverse().slice(0, 2).join(".")}</b> <span class="muted">${d.dow}</span></span>
      <span class="${col}">${esc(d.name)}${d.start ? ` ${d.start}–${d.end}` : ""}</span></div>
      <div class="meta">${on ? "робот работает" : d.kind === "weekend_session" ? "робот не торгует (торговля в выходные выключена)" : "робот отдыхает"}</div></div>`;
  }).join("") + `<p class="muted small">Источник: ${esc(c.source)}.</p>`;
}

// --- открыть позицию вручную
let OPEN_SIDE = "long";
$$("#openSide button").forEach((b) => (b.onclick = () => { OPEN_SIDE = b.dataset.v; $$("#openSide button").forEach((x) => x.classList.toggle("on", x === b)); }));
$("#openBtn").onclick = () => {
  const f = $("#openForm");
  f.hidden = !f.hidden;
  if (!f.hidden) {
    const times = [...new Set([...Array(14).keys()].map((i) => String(10 + i).padStart(2, "0") + ":00").concat(["18:45", "19:25", "23:45"]))].sort();
    f.exit_at.innerHTML = times.map((t) => `<option ${t === ((SET && SET.day_exit) || "18:45") ? "selected" : ""}>${t}</option>`).join("");
    if (!f.share.value) f.share.value = (SET && +SET.spike_share) || 10;
    f.ticker.focus();
  }
};
$("#openCancel").onclick = () => { $("#openForm").hidden = true; };
$("#openForm").onsubmit = async (ev) => {
  ev.preventDefault();
  const f = ev.target, tk = f.ticker.value.trim().toUpperCase();
  if (!tk) { toast("Укажите тикер"); return; }
  const side = OPEN_SIDE === "long" ? "ЛОНГ" : "ШОРТ";
  if (!(await ask(`Открыть ${side} ${tk} по рынку на ${f.share.value}% портфеля, стоп ${f.stop.value}%?`, [["Открыть", true, "primary"], ["Отмена", false]]))) return;
  try {
    const r = await api("/api/open", { ticker: tk, side: OPEN_SIDE, share: f.share.value, stop: f.stop.value, take: f.take.value,
      trail: f.trail.value, exit_at: f.exit_at.value, carry: f.carry.value === "1", by_form: f.by_form.value === "1" });
    toast(r.msg || (r.ok ? "Запрошено" : "Не получилось"), 5000);
    if (r.ok) { f.hidden = true; f.ticker.value = ""; setTimeout(poll, 3000); }
  } catch (e) { toast("Не получилось: " + e.message); }
};

let RES = null, RCH = null;
$("#resetBtn").onclick = async () => {
  const cap = SET && SET.capital ? rub(+SET.capital) : "сумму капитала из настроек";
  if (!(await ask(`Сбросить торговлю? Все открытые позиции закроются, демо-счёт откроется заново на ${cap}, история сделок и баланса очистится.`,
    [["Сбросить", true, "danger"], ["Отмена", false]]))) return;
  if (!(await ask("Точно? Это нельзя отменить.", [["Да, начать сначала", true, "danger"], ["Отмена", false]]))) return;
  try { const r = await api("/api/reset", { confirm: "СБРОС" }); toast(r.msg || "Сброс начат.", 6000); setTimeout(loadResults, 15000); setTimeout(loadResults, 40000); }
  catch (e) { toast("Не получилось: " + e.message); }
};
async function loadResults() {
  try { RES = await api("/api/results"); } catch (e) { return; }
  const d = RES.periods.find((p) => p.kind === "day");
  $("#balance").textContent = rub(RES.balance);
  $("#balDay").innerHTML = d && d.change != null ? `сегодня <b class="${cls(d.change)}">${rub(d.change, true)} (${pct(d.change_pct)})</b>` : "";
  if (TAB !== "trades" || TRV !== "results") return;
  $("#resBal").textContent = rub(RES.balance);
  $("#resTime").textContent = "на " + RES.time + " МСК";
  $("#resTbl").innerHTML = `<tr><th>Период</th><th>Изменение</th><th>%</th><th>Сделок · в +</th><th>итог сделок</th></tr>` +
    RES.periods.map((p) => `<tr><td>${esc(p.name)}<div class="muted" style="font-size:11px">с ${esc((p.from || "").split("-").reverse().join("."))}</div></td>
      <td class="${cls(p.change)}">${rub(p.change, true)}</td><td class="${cls(p.change_pct)}">${pct(p.change_pct)}</td>
      <td>${p.trades}${p.trades ? " · " + Math.round(p.wins / p.trades * 100) + "%" : ""}</td>
      <td class="${cls(p.pnl_rub)}">${rub(p.pnl_rub, true)}</td></tr>` +
      [["всплеск", "Всплеск + EMA20"], ["15:00", "Движение дня"], ["вручную", "Вручную"]].map(([k, n]) => { const x = (p.by_strat || {})[k] || { n: 0 };
        return `<tr class="sub"><td>↳ ${n}</td><td></td><td class="${cls(x.avg_pct)}">${x.n ? pct(x.avg_pct) + " ср." : ""}</td><td>${x.n}${x.n ? " · " + Math.round(x.wins / x.n * 100) + "%" : ""}</td><td class="${cls(x.pnl_rub)}">${rub(x.pnl_rub, true)}</td></tr>`; }).join("")).join("");
  const el = $("#resChart");
  if (!RCH) {
    RCH = LightweightCharts.createChart(el, { width: el.clientWidth, height: 220, layout: { background: { color: "#131722" }, textColor: "#b2b5be", fontSize: 11 },
      grid: { vertLines: { color: "#1f2433" }, horzLines: { color: "#1f2433" } }, rightPriceScale: { borderColor: "#283048" }, timeScale: { borderColor: "#283048" } });
    RCH.s = RCH.addAreaSeries({ lineColor: "#2f7cf6", topColor: "rgba(47,124,246,.35)", bottomColor: "rgba(47,124,246,0)", lineWidth: 2,
      priceFormat: { type: "price", precision: 0, minMove: 1 } });
    new ResizeObserver(() => RCH.applyOptions({ width: el.clientWidth })).observe(el);
  }
  RCH.s.setData(RES.series.map((x) => ({ time: x.date, value: x.value })));
  RCH.timeScale().fitContent();
  el.hidden = RES.series.length < 2;
}
$$("#trSeg button").forEach((b) => (b.onclick = () => { TRV = b.dataset.v; $$("#trSeg button").forEach((x) => x.classList.toggle("on", x === b)); loadTrades(); }));
async function loadTrades() {
  $("#trJournalFill").hidden = TRV !== "journal";
  $("#resBox").hidden = TRV !== "results";
  $("#trSum").hidden = $("#trList").hidden = TRV === "results";
  if (TRV === "results") { loadResults(); return; }
  try {
    if (TRV === "robot") {
      const d = await api("/api/trades");
      const line = (name, x, key) => `<tr class="${TRF === key ? "sel" : ""}" data-trf="${key}"><td>${name}</td><td>${x.n}</td><td>${x.n ? Math.round(x.wins / x.n * 100) + "%" : "—"}</td>
        <td class="${cls(x.avg_pct)}">${pct(x.avg_pct)}</td><td class="${cls(x.pnl_rub)}">${rub(x.pnl_rub, true)}</td></tr>`;
      $("#trSum").innerHTML = d.n ? `<table class="tbl"><tr><th>Стратегия</th><th>Сделок</th><th>в плюсе</th><th>средняя</th><th>итог</th></tr>
        ${line("Всплеск + EMA20", d.by_strat["всплеск"], "всплеск")}${line("Движение дня", d.by_strat["15:00"], "15:00")}${d.by_strat["вручную"] && d.by_strat["вручную"].n ? line("Вручную", d.by_strat["вручную"], "вручную") : ""}
        ${line("<b>Всего</b>", d.total, "")}</table><div class="muted small">Нажмите на строку — в списке останутся сделки этой стратегии.</div>`
        : "Закрытых сделок робота пока нет.";
      $$("#trSum tr[data-trf]").forEach((tr) => (tr.onclick = () => { TRF = tr.dataset.trf; loadTrades(); }));
      $("#trList").innerHTML = d.rows.filter((r) => !TRF || r.strat === TRF).map((r) => {
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
  <div class="grp"><h3>Брокер для торговли</h3>
    ${sel("broker", "Через кого робот торгует", s.brokers || [["tinvest", "Т-Инвестиции — песочница (демо-счёт)"], ["tinvest_real", "Т-Инвестиции — реальный счёт"], ["finam", "Финам — Trade API"]])}
    <div class="hint" style="margin-left:0"><b>Реальный счёт Т-Инвестиций и Финам — сделки на реальные деньги.</b></div>
    <h3>Т-Инвестиции — реальный счёт</h3>
    <label class="f"><span>Токен с полным доступом ${s.real_set ? `(сохранён: ${esc(s.real_hint)}; оставьте пустым, чтобы не менять)` : "(не введён)"}</span>
      <input type="password" name="real_token" placeholder="${s.real_set ? "••••••••" : "Т-Инвестиции → Настройки → Токены → полный доступ"}" autocomplete="off"></label>
    ${inp("real_account", "Номер счёта (пусто — основной брокерский)")}
    <div class="hint" style="margin-left:0">Свечи и цены — этим же токеном; токен песочницы для этого варианта не нужен.</div>
    <h3>Финам</h3>
    <div class="hint" style="margin-left:0">Свечи и цены — из Т-Инвестиций (нужен токен песочницы выше). Через Финам торгуются только акции. Для проверки используйте демо-счёт Финама.</div>
    <label class="f"><span>Секретный токен Финама ${s.finam_set ? `(сохранён: ${esc(s.finam_hint)}; оставьте пустым, чтобы не менять)` : "(не введён)"}</span>
      <input type="password" name="finam_secret" placeholder="${s.finam_set ? "••••••••" : "из личного кабинета Trade API → Токены"}" autocomplete="off"></label>
    ${inp("finam_account", "Номер счёта Финама (пусто — первый доступный токену)")}
  </div>
  <div class="grp"><h3>Всплеск объёма + откат к EMA20</h3>
    ${chk("spike_on", "Робот торгует по этой формации")}
    <div class="f"><span>Таймфреймы</span>
      <label class="c"><input type="checkbox" name="tf5" ${tfs.includes(5) ? "checked" : ""}><span>5 мин</span></label>
      <label class="c"><input type="checkbox" name="tf15" ${tfs.includes(15) ? "checked" : ""}><span>15 мин</span></label>
      <label class="c"><input type="checkbox" name="tf60" ${tfs.includes(60) ? "checked" : ""}><span>1 час</span></label></div>
    ${chk("trend_on", "Искать трендовые откаты", "Импульс на объёме был в последние дни, EMA10 над EMA20, касание EMA20 + подтверждение.")}
    ${chk("trend_trade", "…и торговать все трендовые откаты", "На истории в целом в минусе; по умолчанию только по профилю ниже.")}
    ${chk("tier_profile", "Профиль по эшелонам", "Индекс — формация 15 мин и 1 час; 2-й эшелон — формация 1 час и трендовый откат 15 мин; 3-й эшелон не торгуется.")}
    ${chk("idx_form", "Только по направлению индекса", "Лонг — когда индекс растёт, шорт — когда падает. Если снять — входит в обе стороны.")}
    ${chk("trade_5m", "Торговать и на 5 мин", "Формации 5 мин (акции индекса и 2-го эшелона) — тоже вход, а не только уведомление. На истории: 556 сделок за год, средняя −0,12% — в минусе.")}
    ${chk("ema10_touch", "Формация и на откате к EMA10", "Если откат не дошёл до EMA20, но коснулся EMA10 и отскочил — тоже формация (стоп — под откатом).")}
    ${chk("ema200", "Только по тренду EMA200", "Лонг — только если цена выше EMA200, шорт — только ниже. Если отмечено несколько ТФ — условие на всех.")}
    <div class="row" style="margin:-4px 0 6px 30px">${[["form", "ТФ формации"], ["5", "5 мин"], ["15", "15 мин"], ["60", "1 час"], ["1440", "1 день"]].map(([k, n]) =>
      `<label class="chk"><input type="checkbox" name="e2_${k}" ${(SET.ema200_tfs || ["form"]).includes(k) ? "checked" : ""}> ${n}</label>`).join("")}</div>
    ${sel("entry_window", "Время входов", [["9-19", "09:00–19:00"], ["10-18", "10:00–18:00"]])}
    <label class="f"><span>Время выхода</span>
      <select name="exit_late" id="exitSel"></select></label>
    <label class="f"><span>Во время выхода</span><select name="carry_all">
      <option value="all" ${SET.carry_all === true || SET.carry_all === undefined ? "selected" : ""}>переносить все позиции (до стопа, тейка или трейлинга)</option>
      <option value="loss" ${SET.carry_all === false ? "selected" : ""}>закрыть только в минусе (в плюсе — перенести, до 2 дней)</option>
      <option value="none" ${SET.carry_all === "none" ? "selected" : ""}>закрыть все позиции</option></select></label>
    <div class="f"><span>Чем торговать</span>
      ${(s.markets_all || []).map(([k, t]) => `<label class="c"><input type="checkbox" name="mk_${k}" ${mk.includes(k) ? "checked" : ""}><span>${esc(t)}</span></label>`).join("")}</div>
    ${sel("two_takes", "Тейки", [["true", "два тейка"], ["false", "один тейк"]])}
    ${sel("spike_half", "Закрыть на тейке1, %", [30, 50, 70, 100].map((v) => [v, v]))}
    ${sel("spike_trail", "Трейлинг, R (расстояние до стопа)", [0.5, 1, 1.5, 2].map((v) => [v, v]))}
    ${sel("spike_max", "Макс. позиций", [[0, "без ограничения"]].concat([1, 2, 3, 5, 8, 10, 15, 20].map((v) => [v, v])))}
    ${sel("spike_share", "Вход в позицию, % от портфеля", [1, 2, 3, 5, 10, 15, 20, 25, 33, 50, 100].map((v) => [v, v + "%"]))}
    ${sel("leverage", "Маржа", Object.entries(LEV))}
    ${sel("direction", "Направление", [["both", "лонг и шорт"], ["long", "только лонг"], ["short", "только шорт"]])}
  </div>
  <div class="grp"><h3>Календарь</h3>
    ${chk("trade_weekends", "Торговать в выходные и праздничные дни (сессии выходного дня Мосбиржи)", "Робот смотрит календарь Мосбиржи: в праздники и выходные без торгов он не работает. Если галочка снята — в сессии выходного дня тоже не торгует (оборот там маленький, на истории не проверено).")}
  </div>
  <div class="grp"><h3>Время входов (обе стратегии)</h3>
    ${sel("entry_from", "С какого времени робот может входить в позиции", [["", "как в окне формации / времени входа «Движения дня»"], ["7", "с открытия рынка — 07:00"], ["10", "с начала основной сессии — 10:00"]])}
    <div class="hint" style="margin-left:0">Формация: начало окна входов (конец — по выбранному окну). «Движение дня»: с этого времени входит режим «сразу при сигнале». На истории проверены только окна 09:00–19:00 и 10:00–18:00.</div>
  </div>
  <div class="grp"><h3>Направление рынка (обе стратегии)</h3>
    ${sel("idx_ref", "Как робот определяет, растёт рынок или падает", [["imoex", "IMOEX — реальное изменение к вчерашнему дню"], ["imoex_open", "IMOEX — от открытия основной сессии (10:00)"], ["open", "акции индекса с открытия дня (как в проверке на истории)"]])}
    <div class="hint" style="margin-left:0">Влияет на фильтр «только по направлению индекса/рынка» и на закрытие при развороте. По умолчанию — реальный индекс IMOEX. Проверка на истории делалась по второму варианту (акции с открытия дня).</div>
  </div>
  <div class="grp"><h3>Разворот индекса (обе стратегии)</h3>
    <label class="c"><input type="checkbox" name="flip_all" ${SET.idx_flip === "all" ? "checked" : ""}><span>При смене направления индекса закрыть все сделки</span></label>
    <label class="c"><input type="checkbox" name="flip_profit" ${SET.idx_flip === "profit" ? "checked" : ""}><span>При смене направления индекса закрыть все сделки в плюсе</span></label>
    <div class="hint">Направление — рост или падение индекса от открытия дня. Разворот засчитывается, когда индекс ушёл больше чем на 0,1% в другую сторону. Можно включить только один вариант.</div>
  </div>
  <div class="grp"><h3>Движение дня</h3>
    ${chk("day_on", "Торговать «движение дня» (вход в заданное время)")}
    ${chk("idx_day", "Только по направлению рынка", "Акция идёт туда же, куда рынок. Если снять — в обе стороны.")}
    ${sel("day_mode", "Вход", [["fixed", "в заданное время (ниже) — проверено на истории"], ["signal", "сразу при сигнале 09:00–19:00 — на истории в минусе"]])}
    ${sel("day_tp", "Тейк, % (лучше по истории — 8%)", [[0, "без тейка"], [3, "3%"], [5, "5%"], [8, "8%"], [10, "10%"]])}
    ${sel("day_tp_half", "Закрыть на тейке", [[50, "50% (остаток — б/у и трейлинг)"], [100, "всю позицию"]])}
    ${sel("day_trail", "Трейлинг-стоп, %", [[0, "нет"], [1, "1%"], [2, "2%"], [3, "3%"]])}
    ${sel("day_act", "Трейлинг включается", [[0, "после тейка"], [1, "с +1%"], [2, "с +2%"], [3, "с +3%"]])}
    ${sel("day_ref", "Движение бумаги считать", [["main", "от начала основной сессии (10:00)"], ["market", "от открытия рынка (07:00)"]])}
    <div class="hint" style="margin-left:0">До 10:00 движение всегда считается от 07:00. На истории проверено «от 10:00».</div>
    <div class="f"><span>Чем торговать (на истории проверены только акции индекса)</span>
      ${(s.markets_all || []).map(([k, t]) => `<label class="c"><input type="checkbox" name="dmk_${k}" ${(s.day_markets || ["imoex"]).includes(k) ? "checked" : ""}><span>${esc(t)}</span></label>`).join("")}</div>
    ${sel("day_exit", "Время выхода (на истории 17:00 лучше)", [...new Set([...Array(14).keys()].map((i) => String(10 + i).padStart(2, "0") + ":00").concat(["18:45", "19:25", "23:45", SET.day_exit || "18:45"]))].sort().map((t) => [t, t]))}
    ${sel("day_carry", "Переносить позиции (выход в выбранное время последнего дня, стоп 3% действует всё время)", [[0, "нет"], [1, "на 1 день"], [2, "на 2 дня"], [3, "на 3 дня"]])}
    ${inp("threshold", "Порог движения, %", "number", 'step="0.1" inputmode="decimal"')}
    ${sel("entry", "Время входа (МСК)", [...new Set([...Array(10).keys()].map((i) => String(9 + i).padStart(2, "0") + ":00").concat(["14:30", SET.entry || "15:00"]))].sort().map((t) => [t, t]))}
    <div class="hint" style="margin-left:0">При входе в 09:00 и 10:00 движение считается от открытия утренней сессии (09:00); в 09:00 бумаги ещё почти не сдвинулись — сигналов будет мало.</div>
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
    <p class="muted small">Программа на сервере: ${esc(ST ? ST.version : "")}${NATIVE && NATIVE.bundleVersion ? "<br>В этом приложении: " + esc(NATIVE.bundleVersion()) : ""}</p>
    ${NATIVE && NATIVE.updateServer ? '<button type="button" class="btn small" id="srvUpd">Обновить программу на сервере</button>' : ""}
  </div>
  <button type="submit" class="btn primary wide save">Сохранить настройки</button>`;
  const fillExit = () => {
    const f = $("#setForm"), early = f.entry_window.value === "10-18" ? "18:45" : "19:25";
    const times = [...new Set([...Array(14).keys()].map((i) => String(10 + i).padStart(2, "0") + ":00").concat(["18:45", "19:25", "23:45", early]))].sort();
    let cur = f.exit_late.value || (typeof SET.exit_late === "string" ? SET.exit_late : SET.exit_late === false ? early : "23:45");
    if (cur === "19:25" || cur === "18:45") cur = times.includes(cur) ? cur : early;
    f.exit_late.innerHTML = times.map((t) => `<option value="${t}">${t}</option>`).join("");
    f.exit_late.value = times.includes(cur) ? cur : early;
    f.exit_late.dataset.early = early;
  };
  fillExit(); $("#setForm").entry_window.onchange = fillExit;
  const fA = $("#setForm").flip_all, fP = $("#setForm").flip_profit;     // только один из двух вариантов
  fA.onchange = () => { if (fA.checked) fP.checked = false; };
  fP.onchange = () => { if (fP.checked) fA.checked = false; };
  $("#tgFind").onclick = async () => {
    const r = await api("/api/tg_find", { tg_token: $("#setForm").tg_token.value.trim() });
    if (r.ok) { $("#setForm").tg_chat.value = r.chat; toast(`Найден чат ${r.name}. Нажмите «Сохранить».`); }
    else toast("Сообщений боту нет. Напишите боту в Telegram и нажмите ещё раз.");
  };
  $("#tgTest").onclick = async () => { try { await api("/api/tg_test", {}); toast("Отправлено — проверьте Telegram (нужно сначала сохранить)"); } catch (e) { toast("Не отправилось: " + e.message); } };
  $("#phSound").onchange = (e) => store("sound", e.target.checked ? "1" : "0");
  if ($("#srvUpd")) $("#srvUpd").onclick = () => NATIVE.updateServer();
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
    sandbox_token: c("sandbox_token"), broker: v("broker") || "tinvest", finam_account: (v("finam_account") || "").trim(),
    real_account: (v("real_account") || "").trim(), real_token: (v("real_token") || "").trim(),
    finam_secret: (v("finam_secret") || "").trim(), capital: parseFloat(String(v("capital")).replace(/\s/g, "")) || 1000000,
    spike_on: c("spike_on"), spike_tfs: [5, 15, 60].filter((t) => c("tf" + t)),
    trend_on: c("trend_on"), trend_trade: c("trend_trade"), tier_profile: c("tier_profile"), entry_window: v("entry_window"), exit_late: v("exit_late") === "23:45" ? true : v("exit_late") === f.exit_late.dataset.early ? false : v("exit_late"), carry_all: v("carry_all") === "none" ? "none" : v("carry_all") !== "loss",
    spike_markets: (SET.markets_all || []).map(([k]) => k).filter((k) => c("mk_" + k)),
    day_markets: (SET.markets_all || []).map(([k]) => k).filter((k) => c("dmk_" + k)),
    two_takes: v("two_takes") === "true", spike_half: +v("spike_half"), spike_trail: +v("spike_trail"), spike_max: +v("spike_max"),
    spike_share: +v("spike_share"), leverage: v("leverage"), direction: v("direction"),
    day_on: c("day_on"), day_carry: +v("day_carry") || 0, day_exit: v("day_exit") || "18:45", day_mode: v("day_mode") || "fixed", idx_form: c("idx_form"), ema10_touch: c("ema10_touch"), trade_5m: c("trade_5m"), ema200: c("ema200"), ema200_tfs: ["form", "5", "15", "60", "1440"].filter((k) => c("e2_" + k)), idx_day: c("idx_day"), idx_flip: c("flip_all") ? "all" : c("flip_profit") ? "profit" : "off", trade_weekends: c("trade_weekends"), entry_from: v("entry_from") || "", day_ref: v("day_ref") || "main", idx_ref: v("idx_ref") || "imoex", day_tp: +v("day_tp") || 0, day_tp_half: +v("day_tp_half") || 100,
    day_trail: +v("day_trail") || 0, day_act: +v("day_act") || 0, threshold: parseFloat(String(v("threshold")).replace(",", ".")) || 2, entry: v("entry") || "15:00",
    rescan: c("rescan"), auto_journal: c("auto_journal"),
    tg_chat: (v("tg_chat") || "").trim(), tg_form: c("tg_form"), tg_trend: c("tg_trend"), tg_trades: c("tg_trades"), tg_imp: c("tg_imp"),
  };
  if (!out.spike_markets.length) out.spike_markets = ["imoex"];
  if (!out.day_markets.length) out.day_markets = ["imoex"];
  const tok = (v("token") || "").trim(); if (tok) out.token = tok;
  const tgt = (v("tg_token") || "").trim(); if (tgt) out.tg_token = tgt;
  if (out.broker !== "tinvest" && out.broker !== (SET.broker || "tinvest") &&
      !(await ask(out.broker === "finam" ? "Робот будет торговать через Финам. Если указан реальный счёт — сделки пойдут на реальные деньги. Продолжить?"
        : "Робот будет торговать на РЕАЛЬНОМ счёте Т-Инвестиций — на реальные деньги. Продолжить?",
        [["Да, торговать", true, "danger"], ["Отмена", false]]))) return;
  if (!out.real_token) delete out.real_token;
  if (!out.finam_secret) delete out.finam_secret;
  try {
    const r = await api("/api/settings", out);
    if (r.ok === false) { toast(r.msg, 6000); return; }
    toast("Сохранено. Робот перезапускается с новыми настройками."); loadSettings();
  }
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
  loadResults(); setInterval(() => { if (!document.hidden) loadResults(); }, 60000);
  loadCalendar(); setInterval(() => { if (!document.hidden) loadCalendar(); }, 30 * 60000);
  setInterval(() => { if (!document.hidden && TAB === "chart" && CH.data) loadChart(true); }, 60000);
  setInterval(() => { if (!document.hidden && TAB === "chart" && CH.data) chartTail(); }, 5000);   // последние свечи — каждые 5 с
  setInterval(() => { if (!document.hidden && TAB === "chart" && CH.data) chartLive(); }, 1000);   // живая цена — каждую секунду
}
document.addEventListener("visibilitychange", () => { if (!document.hidden && KEY) poll(); });
start();
