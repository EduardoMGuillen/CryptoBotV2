"use strict";
// Meme Desk en vivo. Lee /api/meme/feed, /api/meme/variants, /api/meme, /api/meme/trade y /api/status.
// Los nombres de tokens los pone cualquiera en internet: siempre textContent, nunca innerHTML.
(() => {
  const $ = (s) => document.querySelector(s);
  const LOCALE = "es-HN";
  const f2 = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const f1 = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const f0 = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 0 });
  const hm = new Intl.DateTimeFormat(LOCALE, { hour: "numeric", minute: "2-digit" });
  const MINUS = "−";
  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const START = 100;
  const ROOT = "principal";

  const money = (v) => (Number.isFinite(v) ? (v < 0 ? MINUS : "") + "$" + f2.format(Math.abs(v)) : "—");
  const signed = (v) => (Number.isFinite(v) ? (v >= 0 ? "+" : MINUS) + "$" + f2.format(Math.abs(v)) : "—");
  const pct = (v) => (Number.isFinite(v) ? (v >= 0 ? "+" : MINUS) + f1.format(Math.abs(v * 100)) + " %" : "—");
  const mult = (m) => "x" + (m >= 10 ? f1.format(m) : f2.format(m));
  const tone = (v) => (v > 0.0001 ? "up" : v < -0.0001 ? "down" : "");
  const price = (p) => (p >= 1 ? "$" + f2.format(p) : "$" + Number(p).toPrecision(4));
  const ago = (t) => {
    const s = Math.max(0, Date.now() / 1000 - t);
    if (s < 60) return "ahora";
    if (s < 3600) return `hace ${f0.format(Math.floor(s / 60))} min`;
    if (s < 86400) return `hace ${f1.format(s / 3600)} h`;
    return hm.format(new Date(t * 1000));
  };
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const NAMES = {
    principal: "Principal: sobrevivientes", sobrevivientes_momentum: "Sobrevivientes + momentum",
    sin_copias_30m: "Sin copias, 30 min", solo_pump: "Solo pump.fun", solo_pump_1_5x: "Pump.fun, todo a 1.5x",
    solo_pump_30m: "Pump.fun, 30 min", solo_pump_momentum: "Pump.fun + momentum",
    acc_spy: "Acciones: S&P 500", acc_tendencia: "Acciones: tendencia", acc_ruptura: "Acciones: ruptura",
    acc_rsi2: "Acciones: rebote RSI-2", acc_rotacion: "Acciones: rotación",
  };
  const name = (d) => NAMES[d] || d.replace(/_/g, " ");
  const REASON = {
    stop: "stop", trailing: "trailing", liquidez: "se fue la liquidez", sin_ruta: "nadie compra",
    tiempo: "tiempo límite", tp1: "toma ganancia", tp2: "toma ganancia", cierre_manual: "cierre manual",
    senal: "señal", "señal": "señal",
  };

  // ---- sonido y vibración (apagados hasta que el usuario los activa) ----
  let soundOn = false;
  try { soundOn = localStorage.getItem("live.sound") === "1"; } catch (_) { /* sin almacenamiento */ }
  let audio = null;
  function beep(notes) {
    if (!soundOn) return;
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      let t = audio.currentTime;
      for (const [freq, dur, type] of notes) {
        const o = audio.createOscillator();
        const g = audio.createGain();
        o.type = type || "sine";
        o.frequency.value = freq;
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.18, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
        o.connect(g).connect(audio.destination);
        o.start(t);
        o.stop(t + dur + 0.02);
        t += dur * 0.8;
      }
    } catch (_) { /* sin audio */ }
  }
  const buzz = (p) => { if (soundOn && navigator.vibrate) navigator.vibrate(p); };
  function renderSound() { $("#sound").setAttribute("aria-pressed", soundOn ? "true" : "false"); }
  $("#sound").addEventListener("click", () => {
    soundOn = !soundOn;
    try { localStorage.setItem("live.sound", soundOn ? "1" : "0"); } catch (_) { /* nada */ }
    renderSound();
    beep([[660, 0.12], [880, 0.16]]);
  });
  renderSound();

  // ---- confeti ----
  const fx = $("#fx");
  const ctx = fx.getContext("2d");
  let parts = [];
  let raf = 0;
  function confetti(x, y) {
    if (REDUCED) return;
    const colors = [cssVar("--mint"), cssVar("--violet"), cssVar("--amber"), "#ffffff"];
    for (let i = 0; i < 70; i++) {
      const a = -Math.PI / 2 + (Math.random() - 0.5) * 2.2;
      const v = 4 + Math.random() * 7;
      parts.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, r: 2 + Math.random() * 3.5, c: colors[i % 4],
                   rot: Math.random() * 6, life: 1 });
    }
    if (!raf) raf = requestAnimationFrame(tick);
  }
  function tick() {
    const dpr = window.devicePixelRatio || 1;
    if (fx.width !== innerWidth * dpr) { fx.width = innerWidth * dpr; fx.height = innerHeight * dpr; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, innerWidth, innerHeight);
    parts = parts.filter((p) => p.life > 0);
    for (const p of parts) {
      p.x += p.vx; p.y += p.vy; p.vy += 0.22; p.vx *= 0.985; p.rot += 0.2; p.life -= 0.012;
      ctx.globalAlpha = Math.max(0, p.life);
      ctx.fillStyle = p.c;
      ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.rot); ctx.fillRect(-p.r, -p.r / 2, p.r * 2, p.r); ctx.restore();
    }
    raf = parts.length ? requestAnimationFrame(tick) : 0;
    if (!raf) ctx.clearRect(0, 0, innerWidth, innerHeight);
  }

  // ---- total con números que cuentan ----
  let shownTotal = null;
  function countTo(node, from, to) {
    if (REDUCED || from === null || Math.abs(to - from) < 0.005) { node.textContent = money(to); return; }
    node.classList.remove("flash-up", "flash-down");
    void node.offsetWidth;
    node.classList.add(to > from ? "flash-up" : "flash-down");
    const t0 = performance.now();
    const step = (now) => {
      const k = Math.min(1, (now - t0) / 900);
      const e = 1 - Math.pow(1 - k, 3);
      node.textContent = money(from + (to - from) * e);
      if (k < 1) requestAnimationFrame(step);
      else setTimeout(() => node.classList.remove("flash-up", "flash-down"), 300);
    };
    requestAnimationFrame(step);
  }

  function renderTotal(variants) {
    const total = variants.reduce((a, v) => a + v.equity, 0);
    const base = variants.length * START;
    countTo($("#total"), shownTotal, total);
    shownTotal = total;
    const sub = $("#totalSub");
    const diff = total - base;
    sub.replaceChildren(el("b", tone(diff), `${signed(diff)} (${pct(diff / base)})`),
      document.createTextNode(` desde el inicio, en ${variants.length} mesas de $${START}`));
  }

  function renderToday(t) {
    const box = $("#today");
    const cell = (label, value, cls) => {
      const d = el("div", cls);
      d.append(el("dt", null, label), el("dd", null, value));
      return d;
    };
    box.replaceChildren(
      cell("Ganadas hoy", f0.format(t.wins)),
      cell("Perdidas hoy", f0.format(t.losses)),
      cell("Rugs hoy", f0.format(t.rugs), t.rugs ? "is-rug" : ""),
      cell("Resultado hoy", signed(t.pnl)),
    );
    box.lastChild.querySelector("dd").className = tone(t.pnl);
  }

  function renderReal(s) {
    if (!s || !s.sleeve) return;
    const a = $("#real");
    a.hidden = false;
    $("#realValue").textContent = money(s.sleeve.value);
    const meta = $("#realMeta");
    const where = s.sleeve.weight > 0.5 ? "En SOL" : "En USDC";
    const next = s.next_decision_at ? new Date(s.next_decision_at) : null;
    let left = "";
    if (next) {
      const m = Math.max(0, (next - Date.now()) / 60000);
      left = m >= 60 ? `decide en ${f0.format(Math.floor(m / 60))} h ${f0.format(Math.floor(m % 60))} min` : `decide en ${f0.format(Math.ceil(m))} min`;
    }
    const perf = s.performance ? pct(s.performance.pnl_pct) : "";
    meta.replaceChildren(el("span", null, `${where}, ${perf}`), el("br"), el("span", null, left));
    if (s.pending || s.error || s.kill) meta.replaceChildren(el("span", "down", "Revisar Solbot"));
  }

  // ---- la carrera (reordenamiento animado) ----
  let selected = (location.hash.match(/v=([a-z0-9_]+)/) || [])[1] || ROOT;
  const lanes = new Map();
  function renderRace(variants) {
    const list = $("#race");
    const sorted = [...variants].sort((a, b) => b.equity - a.equity);
    const span = Math.max(0.1, ...sorted.map((v) => Math.abs(v.equity / START - 1)));
    const before = new Map([...lanes].map(([k, n]) => [k, n.getBoundingClientRect().top]));
    sorted.forEach((v, i) => {
      let li = lanes.get(v.name);
      if (!li) {
        li = el("li", "lane");
        li.tabIndex = 0;
        li.setAttribute("role", "button");
        li.append(el("span", "lane__rank"), el("span", "lane__name"), el("span", "lane__pct"));
        const track = el("span", "lane__track");
        track.append(el("span", "lane__bar"));
        li.append(track);
        const pick = () => { selected = v.name; location.hash = "v=" + v.name; refreshCurve(true); paintSelection(); };
        li.addEventListener("click", pick);
        li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
        lanes.set(v.name, li);
      }
      const r = v.equity / START - 1;
      li.querySelector(".lane__rank").textContent = i + 1;
      const nm = li.querySelector(".lane__name");
      nm.replaceChildren(document.createTextNode(name(v.name)), el("small", null, `${v.closed} cerradas${v.open ? `, ${v.open} abiertas` : ""}`));
      const p = li.querySelector(".lane__pct");
      p.textContent = pct(r);
      p.className = "lane__pct " + tone(r);
      const bar = li.querySelector(".lane__bar");
      const w = (Math.min(1, Math.abs(r) / span) * 50).toFixed(2);
      bar.className = "lane__bar " + (r >= 0 ? "up" : "down");
      bar.style.left = r >= 0 ? "50%" : `${50 - w}%`;
      bar.style.width = `${w}%`;
      list.append(li);
    });
    for (const [k, n] of lanes) if (!sorted.some((v) => v.name === k)) { n.remove(); lanes.delete(k); }
    if (!REDUCED) {
      for (const [k, n] of lanes) {
        const old = before.get(k);
        if (old === undefined) continue;
        const dy = old - n.getBoundingClientRect().top;
        if (Math.abs(dy) > 2) n.animate([{ transform: `translateY(${dy}px)` }, { transform: "none" }], { duration: 650, easing: "cubic-bezier(.2,.8,.2,1)" });
      }
    }
    paintSelection();
  }
  function paintSelection() {
    for (const [k, n] of lanes) n.setAttribute("aria-pressed", k === selected ? "true" : "false");
    $("#curveTitle").textContent = "Curva de " + name(selected);
  }

  // ---- abiertas ----
  function renderOpens(open) {
    const box = $("#opens");
    $("#opensTitle").textContent = open.length ? `Abiertas ahora (${open.length})` : "Abiertas ahora";
    if (!open.length) { box.replaceChildren(el("p", "empty", "Ninguna mesa tiene posiciones abiertas.")); return; }
    box.replaceChildren(...open.map((o) => {
      const b = el("button", "pos");
      const top = el("div", "pos__top");
      top.append(el("span", "pos__sym", o.symbol), el("span", "pos__mult " + tone(o.mult - 1), mult(o.mult)));
      b.append(top, el("div", "pos__desk", `${name(o.desk)}, ${ago(o.opened)}`));
      if (o.stop && o.target) {
        const lo = o.stop, hi = o.target;
        const at = (m) => `${Math.max(0, Math.min(100, ((m - lo) / (hi - lo)) * 100))}%`;
        const range = el("div", "pos__range");
        const entry = el("span", "pos__entry");
        entry.style.left = at(1);
        const now = el("span", "pos__now");
        now.style.left = at(o.mult);
        now.style.background = o.mult >= 1 ? cssVar("--mint") : cssVar("--coral");
        range.append(entry, now);
        const ends = el("div", "pos__ends");
        ends.append(el("span", null, `stop ${mult(lo)}`), el("span", null, `meta ${mult(hi)}`));
        b.append(range, ends);
      }
      const pnl = el("div", "pos__pnl");
      pnl.append(document.createTextNode(`Invertido ${money(o.cost)}, ahora `), el("b", tone(o.pnl), signed(o.pnl)));
      b.append(pnl);
      b.addEventListener("click", () => openSheet(o.desk, o.token));
      return b;
    }));
  }

  // ---- feed ----
  const rows = new Map();
  let primed = false;
  const keyOf = (e) => `${e.desk}|${e.t}|${e.kind}|${e.token}`;
  function describe(e) {
    const isStock = e.desk.startsWith("acc_");
    if (e.kind === "buy") return { what: "compra", amount: money(e.usd), small: "invertido" };
    if (e.kind === "fail") return { what: e.side === "sell" ? "venta fallida" : "compra fallida", amount: signed(-(e.fee || 0)), small: "comisión de red" };
    const reason = REASON[e.reason] || (isStock ? "vende" : e.reason || "vende");
    if (Number.isFinite(e.pnl)) return { what: reason, amount: signed(e.pnl), small: Number.isFinite(e.real_mult) ? mult(e.real_mult) : "" };
    return { what: reason, amount: money(e.usd), small: "cobrado" };
  }
  function renderFeed(events) {
    const list = $("#feed");
    const fresh = [];
    const keys = new Set();
    events.forEach((e) => {
      const k = keyOf(e);
      keys.add(k);
      let li = rows.get(k);
      if (!li) {
        const d = describe(e);
        li = el("li", `ev ev--${e.tone}`);
        li.tabIndex = 0;
        const main = el("div", "ev__main");
        const what = el("div", "ev__what", e.symbol || "?");
        what.append(document.createTextNode(" "), el("span", null, d.what));
        main.append(what, el("div", "ev__meta"));
        const amount = el("div", "ev__amount", d.amount);
        if (d.small) amount.append(el("small", null, d.small));
        li.append(el("span", "ev__bar"), main, amount);
        li.addEventListener("click", () => openSheet(e.desk, e.token));
        li.addEventListener("keydown", (ev) => { if (ev.key === "Enter") openSheet(e.desk, e.token); });
        rows.set(k, li);
        if (primed) fresh.push([li, e]);
      }
      li.querySelector(".ev__meta").textContent = `${name(e.desk)}, ${ago(e.t)}`;
    });
    for (const [k, n] of rows) if (!keys.has(k)) { n.remove(); rows.delete(k); }
    // Solo se insertan los nuevos arriba: mover nodos existentes reiniciaría sus animaciones.
    const ordered = events.map((e) => rows.get(keyOf(e)));
    ordered.forEach((n, i) => { if (list.children[i] !== n) list.insertBefore(n, list.children[i] || null); });
    if (!events.length) list.replaceChildren(el("p", "empty", "Todavía no hay movimientos."));
    applyFold();
    primed = true;
    fresh.slice(0, 6).forEach(([li, e], i) => setTimeout(() => celebrate(li, e), i * 350));
  }
  // En el teléfono se muestran los últimos 12; "Ver más" despliega el resto.
  let expanded = false;
  const PHONE = window.matchMedia("(max-width: 979px)");
  function applyFold() {
    const fold = PHONE.matches && !expanded;
    [...$("#feed").children].forEach((n, i) => { n.hidden = fold && i >= 12; });
    const more = $("#more");
    more.hidden = !PHONE.matches || $("#feed").children.length <= 12;
    more.textContent = expanded ? "Ver menos" : "Ver más movimientos";
    more.setAttribute("aria-expanded", expanded ? "true" : "false");
  }
  $("#more").addEventListener("click", () => { expanded = !expanded; applyFold(); });
  PHONE.addEventListener("change", applyFold);
  function celebrate(li, e) {
    li.classList.add("is-new");
    li.addEventListener("animationend", () => li.classList.remove("is-new"), { once: true });
    const r = li.getBoundingClientRect();
    if (e.tone === "win") {
      if (r.bottom > 0 && r.top < innerHeight) confetti(r.left + r.width * 0.75, r.top + r.height / 2);
      beep([[523, 0.1], [659, 0.1], [784, 0.18]]);
      buzz(30);
    } else if (e.tone === "rug") {
      beep([[220, 0.18, "sawtooth"], [150, 0.3, "sawtooth"]]);
      buzz([80, 40, 80]);
    } else if (e.tone === "loss") {
      beep([[300, 0.15, "triangle"]]);
      buzz(20);
    } else if (e.tone === "buy") {
      beep([[440, 0.07]]);
    }
  }

  // ---- curva de la mesa elegida ----
  let chart, series, rangeHours = 0, curveData = [];
  const TZ = -new Date().getTimezoneOffset() * 60;
  function chartOptions(LC, size) {
    return {
      autoSize: true,
      layout: { background: { type: LC.ColorType.Solid, color: "transparent" }, textColor: cssVar("--muted"), fontFamily: "Figtree, system-ui", fontSize: size },
      grid: { vertLines: { visible: false }, horzLines: { color: cssVar("--line") } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderColor: cssVar("--line"), timeVisible: true, secondsVisible: false },
      handleScale: { mouseWheel: false }, handleScroll: { mouseWheel: false, vertTouchDrag: false },
    };
  }
  function drawCurve() {
    const LC = window.LightweightCharts;
    if (!LC) return;
    if (!chart) {
      chart = LC.createChart($("#curve"), { ...chartOptions(LC, 12), localization: { locale: LOCALE, priceFormatter: (v) => money(v) } });
      series = chart.addSeries(LC.AreaSeries, {
        lineColor: cssVar("--violet"), lineWidth: 2.5, topColor: "rgba(167,123,255,0.35)", bottomColor: "rgba(167,123,255,0)",
        priceLineVisible: false,
      });
      series.createPriceLine({ price: START, color: cssVar("--muted"), lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: true, title: "inicio" });
    }
    const seen = new Set();
    const pts = [];
    const from = rangeHours ? Date.now() / 1000 - rangeHours * 3600 : 0;
    for (const p of curveData) {
      if (p.t < from) continue;
      const t = Math.floor(p.t) + TZ;
      if (seen.has(t)) continue;
      seen.add(t);
      pts.push({ time: t, value: p.equity });
    }
    series.setData(pts);
    chart.timeScale().fitContent();
  }
  async function refreshCurve(force) {
    const q = selected === ROOT ? "" : "?v=" + encodeURIComponent(selected);
    try {
      const s = await getJSON("/api/meme" + q);
      if (!s.ready) return;
      curveData = s.curve || [];
      drawCurve();
    } catch (_) { if (force) curveData = []; }
  }
  $("#ranges").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    rangeHours = Number(b.dataset.h);
    for (const x of $("#ranges").children) x.setAttribute("aria-pressed", x === b ? "true" : "false");
    drawCurve();
  });

  // ---- hoja de detalle de una operación ----
  let sheetChart = null;
  async function openSheet(desk, token) {
    const sheet = $("#sheet");
    sheet.hidden = false;
    $("#sTitle").textContent = "Cargando…";
    $("#sSub").textContent = name(desk);
    $("#sStats").replaceChildren();
    $("#sLinks").replaceChildren();
    $(".sheet__panel").focus();
    const q = "token=" + encodeURIComponent(token) + (desk === ROOT ? "" : "&v=" + encodeURIComponent(desk));
    let d;
    try { d = await getJSON("/api/meme/trade?" + q); } catch (_) { $("#sTitle").textContent = "No se pudo cargar la operación."; return; }
    const t = d.trade || {};
    $("#sTitle").textContent = t.symbol || "?";
    const closed = d.status === "closed";
    const cur = closed ? t.mult : (t.last || t.entry) / t.entry;
    const pnl = closed ? t.pnl : (t.qty * (t.last || t.entry) + (t.proceeds || 0) - t.cost);
    $("#sSub").textContent = `${name(desk)}, ${closed ? "cerrada" : "abierta"} ${ago(t.closed || t.opened)}`;
    const stat = (k, v, cls) => { const x = el("div"); x.append(el("dt", null, k), el("dd", cls, v)); return x; };
    $("#sStats").replaceChildren(
      stat("Invertido", money(t.cost)),
      stat(closed ? "Resultado" : "Ahora", signed(pnl), tone(pnl)),
      stat(closed ? "Múltiplo real" : "Múltiplo", Number.isFinite(cur) ? mult(cur) : "—", tone(cur - 1)),
      stat("Entrada", t.entry ? price(t.entry) : "—"),
      stat("Máximo", t.peak && t.entry ? mult(t.peak / t.entry) : "—"),
      stat("Salida", closed ? (REASON[t.reason] || t.reason) : "sigue abierta", t.reason && ["liquidez", "sin_ruta"].includes(t.reason) ? "down" : ""),
    );
    if (/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(token) && !desk.startsWith("acc_")) {
      const a = el("a", null, "Ver en DexScreener");
      a.href = "https://dexscreener.com/solana/" + token;
      a.target = "_blank";
      a.rel = "noopener";
      $("#sLinks").append(a);
    }
    const LC = window.LightweightCharts;
    const box = $("#sChart");
    if (sheetChart) { sheetChart.remove(); sheetChart = null; }
    box.replaceChildren();
    if (!LC || !d.path || d.path.length < 2) { box.append(el("p", "empty", "Todavía no hay suficientes precios para graficar.")); return; }
    sheetChart = LC.createChart(box, chartOptions(LC, 11));
    const s = sheetChart.addSeries(LC.LineSeries, {
      color: cur >= 1 ? cssVar("--mint") : cssVar("--coral"), lineWidth: 2, priceLineVisible: false,
      priceFormat: { type: "custom", minMove: 1e-12, formatter: (v) => price(v) },
    });
    const seen = new Set();
    const pts = [];
    for (const p of d.path) {
      const time = Math.floor(p.t) + TZ;
      if (seen.has(time)) continue;
      seen.add(time);
      pts.push({ time, value: p.p });
    }
    s.setData(pts);
    s.createPriceLine({ price: t.entry, color: cssVar("--muted"), lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: true, title: "entrada" });
    sheetChart.timeScale().fitContent();
  }
  function closeSheet() { $("#sheet").hidden = true; if (sheetChart) { sheetChart.remove(); sheetChart = null; } }
  $("#sheet").addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeSheet(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#sheet").hidden) closeSheet(); });

  // ---- ciclo de actualización ----
  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }
  function setLive(ok) {
    const n = $("#live");
    n.className = "live " + (ok ? "is-on" : "is-off");
    $("#liveText").textContent = ok ? "En vivo" : "Sin conexión";
  }
  async function fast() {
    try {
      const [f, v] = await Promise.all([getJSON("/api/meme/feed"), getJSON("/api/meme/variants")]);
      renderTotal(v);
      renderToday(f.today);
      renderRace(v);
      renderOpens(f.open);
      renderFeed(f.events);
      setLive(true);
    } catch (_) {
      setLive(false);
    }
  }
  async function slow() {
    try { renderReal(await getJSON("/api/status")); } catch (_) { /* Solbot puede no responder */ }
    refreshCurve(false);
  }
  fast();
  slow();
  setInterval(() => { if (!document.hidden) fast(); }, 3000);
  setInterval(() => { if (!document.hidden) slow(); }, 15000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { fast(); slow(); } });
})();
