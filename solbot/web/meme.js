"use strict";
// Meme Desk. Solo lee /api/meme y /api/meme/trade.
// Los nombres de tokens los pone cualquiera en internet: siempre textContent, nunca innerHTML.
(() => {
  const $ = (s) => document.querySelector(s);
  const LOCALE = "es-HN";
  const f2 = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const f1 = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const f0 = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 0 });
  const hm = new Intl.DateTimeFormat(LOCALE, { hour: "numeric", minute: "2-digit" });
  const dhm = new Intl.DateTimeFormat(LOCALE, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
  const MINUS = "−";
  const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Si falta un dato se muestra "—" (nunca "NaN").
  const money = (v) => (Number.isFinite(v) ? (v < 0 ? MINUS : "") + "$" + f2.format(Math.abs(v)) : "—");
  const signed = (v) => (Number.isFinite(v) ? (v >= 0 ? "+" : MINUS) + "$" + f2.format(Math.abs(v)) : "—");
  const pct = (v) => (v >= 0 ? "+" : MINUS) + f1.format(Math.abs(v * 100)) + " %";
  const mult = (m) => "x" + (m >= 10 ? f1.format(m) : f2.format(m));
  const tone = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
  const price = (p) => (p >= 1 ? "$" + f2.format(p) : "$" + Number(p).toPrecision(4));
  const ago = (sec) => (sec < 3600 ? `${f0.format(sec / 60)} min` : sec < 172800 ? `${f1.format(sec / 3600)} h` : `${f0.format(sec / 86400)} días`);
  const at = (t) => (Date.now() / 1000 - t < 86400 ? hm : dhm).format(new Date(t * 1000));

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }
  const svg = (tag, attrs = {}) => {
    const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const REASON = {
    stop: "stop de −25 %", trailing: "trailing desde el máximo", liquidez: "se fue la liquidez (rug)",
    tiempo: "límite de 24 h", tp1: "tomó ganancia a 2x", tp2: "tomó ganancia a 3x",
    sin_ruta: "nadie compra: se perdió todo",
  };
  const FAIL = { fallo: "la transacción falló", sin_ruta: "Jupiter no encontró ruta", error: "Jupiter no respondió" };
  const isGoodExit = (r) => r === "trailing" || (r || "").startsWith("tp");

  let snap = null;
  const VARIANT_NAMES = {
    principal: "Principal", sin_copias: "Sin copias", sin_copias_30m: "Sin copias, 30 min",
    todo_1_2x: "Vende todo a 1.2x", todo_1_5x: "Vende todo a 1.5x", mitad_1_3x: "Mitad a 1.3x",
    alerta_rug: "Alerta de rug", momentum: "Momentum", pocas_grandes: "Pocas y grandes", loteria_3x: "Lotería 3x",
  };
  const VARIANT_HELP = {
    principal: "Sin copias, espera 5 min, vende la mitad a 1.5x y el resto con trailing de 25 %. Stop −20 %, sale si la liquidez cae 20 %.",
    sin_copias: "Reglas del post, pero no compra monedas que copian el nombre de otra.",
    sin_copias_30m: "Sin copias y solo compra monedas con al menos 30 minutos de vida.",
    todo_1_2x: "Vende todo al subir 20 %. Stop −15 %.",
    todo_1_5x: "Vende todo a 1.5x. Stop −20 %.",
    mitad_1_3x: "Vende la mitad a 1.3x y el resto con trailing de 20 %.",
    alerta_rug: "Sale apenas la liquidez cae 10 %.",
    momentum: "Solo compra si en 5 min hubo al menos 1.5 compras por cada venta.",
    pocas_grandes: "Máximo 4 posiciones de 15 % cada una; vende todo a 1.5x.",
    loteria_3x: "Espera 3x para vender la mitad; stop −50 %, trailing 40 %.",
  };
  const ROOT = "principal";
  let variant = (location.hash.match(/v=([a-z0-9_]+)/) || [])[1] || ROOT;
  const vq = () => (variant === ROOT ? "" : "v=" + encodeURIComponent(variant));

  // ======================================================================= colador
  // Orden en que una moneda enfrenta los filtros; cada puerta agrupa motivos del embudo.
  const GATES = [
    { key: "dead", label: "Murieron sin liquidez", color: "--muted", keys: ["descartados_sin_liquidez"] },
    { key: "market", label: "Viejas o mal par", color: "--warn", keys: ["rechazo_edad", "rechazo_par"] },
    { key: "contract", label: "Contrato peligroso", color: "--loss", keys: ["rechazo_mint_authority", "rechazo_freeze_authority", "rechazo_mint_ilegible"] },
    { key: "holders", label: "Pocas manos", color: "--sol", keys: ["rechazo_top10", "rechazo_holder_grande"] },
    { key: "full", label: "Sin cupo", color: "--ink-2", keys: ["sin_cupo"] },
    { key: "bought", label: "Compradas", color: "--gain", keys: ["compras"] },
  ];
  const gateCounts = (funnel) => GATES.map((g) => g.keys.reduce((a, k) => a + (funnel[k] || 0), 0));

  const sieve = {
    canvas: $("#sieve"), ctx: null, w: 0, h: 0, dots: [], queue: [], last: null, prevCounts: null, colors: [],
    init() {
      this.ctx = this.canvas.getContext("2d");
      this.colors = GATES.map((g) => cssVar(g.color));
      const resize = () => {
        const r = this.canvas.getBoundingClientRect();
        const dpr = window.devicePixelRatio || 1;
        this.w = r.width; this.h = r.height;
        this.canvas.width = r.width * dpr; this.canvas.height = r.height * dpr;
        this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        if (REDUCED) this.draw();
      };
      new ResizeObserver(resize).observe(this.canvas);
      resize();
      if (!REDUCED) requestAnimationFrame((t) => this.frame(t));
    },
    gateX(i) { return this.w * (0.12 + i * 0.165); },
    // Programa puntos para los cambios del embudo, repartidos en los próximos segundos.
    feed(funnel, buys) {
      const counts = gateCounts(funnel);
      if (!this.prevCounts) {
        // Primera carga: una muestra con la mezcla real de lo que ha pasado hasta ahora.
        const total = counts.reduce((a, b) => a + b, 0);
        if (total) counts.forEach((c, i) => { for (let k = 0; k < Math.round((c / total) * 36); k++) this.schedule(i, Math.random() * 9000); });
      } else {
        counts.forEach((c, i) => {
          if (i === 5) return; // las compras salen con su nombre, abajo
          // El simulador busca una vez por minuto: se reparten en el minuto para que fluya.
          const n = Math.min(60, Math.max(0, c - this.prevCounts[i]));
          for (let k = 0; k < n; k++) this.schedule(i, Math.random() * 55000);
        });
        const watching = Math.min(60, Math.max(0, (funnel.vistos || 0) - (this.prevVistos || 0) - counts.slice(0, 5).reduce((a, c, i) => a + Math.max(0, c - this.prevCounts[i]), 0)));
        for (let k = 0; k < watching; k++) this.schedule(-1, Math.random() * 55000);
      }
      buys.forEach((b, k) => this.schedule(5, 800 + k * 1500, b.symbol));
      this.prevCounts = counts;
      this.prevVistos = funnel.vistos || 0;
      if (REDUCED) this.draw();
    },
    schedule(gate, delay, label) { this.queue.push({ gate, at: performance.now() + delay, label }); },
    spawn(q) {
      const lane = this.h * 0.18 + Math.random() * this.h * 0.5;
      this.dots.push({ x: -6, y: lane, vx: 0.9 + Math.random() * 0.7, vy: 0, gate: q.gate, label: q.label, state: "fly", life: 1, r: q.gate === 5 ? 5.5 : 2.6 });
    },
    frame(t) {
      const now = performance.now();
      this.queue = this.queue.filter((q) => (q.at <= now ? (this.spawn(q), false) : true));
      for (const d of this.dots) {
        if (d.state === "fly") {
          d.x += d.vx;
          d.y += Math.sin((d.x + d.y) / 40) * 0.25;
          if (d.gate === -1 && d.x > this.gateX(0) - 14) d.state = "wait"; // sigue en observación
          else if (d.gate >= 0 && d.gate < 5 && d.x >= this.gateX(d.gate) - 4) { d.state = "fall"; d.vx *= 0.25; }
          else if (d.gate === 5 && d.x >= this.w - 34) { d.state = "land"; d.vx = 0; }
        } else if (d.state === "fall") {
          d.vy += 0.12; d.y += d.vy; d.x += d.vx;
          if (d.y > this.h - 6) { d.y = this.h - 6; d.life -= 0.02; }
        } else if (d.state === "wait") {
          d.life -= 0.006; d.x += 0.05;
        } else if (d.state === "land") {
          d.life -= 0.0035;
        }
      }
      this.dots = this.dots.filter((d) => d.life > 0 && d.x < this.w + 20);
      this.draw();
      requestAnimationFrame((tt) => this.frame(tt));
    },
    draw() {
      const { ctx, w, h } = this;
      if (!ctx || !w) return;
      ctx.clearRect(0, 0, w, h);
      // puertas: mallas verticales
      for (let i = 0; i < 5; i++) {
        const x = this.gateX(i);
        ctx.strokeStyle = this.colors[i];
        ctx.globalAlpha = 0.55;
        ctx.lineWidth = 2;
        ctx.setLineDash([3, 5]);
        ctx.beginPath(); ctx.moveTo(x, h * 0.1); ctx.lineTo(x, h - 4); ctx.stroke();
        ctx.setLineDash([]);
        ctx.globalAlpha = 0.08;
        ctx.fillStyle = this.colors[i];
        ctx.fillRect(x - 10, h - 22, 20, 22);
      }
      // salida: compradas
      ctx.globalAlpha = 0.12;
      ctx.fillStyle = this.colors[5];
      ctx.beginPath(); ctx.roundRect(w - 46, h * 0.12, 40, h * 0.76, 14); ctx.fill();
      ctx.globalAlpha = 1;
      for (const d of this.dots) {
        const color = d.gate === -1 ? cssVar("--ink-2") : d.state === "fly" && d.gate !== 5 ? cssVar("--ink-2") : this.colors[d.gate];
        ctx.globalAlpha = Math.max(0, Math.min(1, d.life)) * (d.state === "fly" ? 0.85 : 1);
        ctx.fillStyle = color;
        if (d.gate === 5) { ctx.shadowColor = color; ctx.shadowBlur = 14; }
        ctx.beginPath(); ctx.arc(d.x, d.y, d.r, 0, Math.PI * 2); ctx.fill();
        ctx.shadowBlur = 0;
        if (d.label && d.state === "land") {
          ctx.font = "600 12px Geist, system-ui";
          ctx.textAlign = "right";
          ctx.fillStyle = cssVar("--gain");
          ctx.fillText(d.label.slice(0, 14), w - 52, d.y + 4);
          ctx.textAlign = "left";
        }
      }
      ctx.globalAlpha = 1;
    },
  };

  function renderGates(s) {
    const counts = gateCounts(s.funnel);
    const list = $("#gates");
    list.replaceChildren();
    GATES.forEach((g, i) => {
      const li = el("li");
      li.style.setProperty("--gate", cssVar(g.color));
      li.append(el("b", null, f0.format(counts[i])), el("span", null, g.label));
      list.append(li);
    });
    $("#sieveTitle").textContent = s.seen
      ? `Dijo que no a ${f0.format(s.rejected)} de ${f0.format(s.seen)} monedas`
      : "Buscando monedas nuevas…";
  }

  // ======================================================================= héroe
  let shownValue = null;
  function tweenValue(node, to) {
    const from = shownValue ?? to;
    shownValue = to;
    if (REDUCED || from === to) { node.textContent = money(to); return; }
    const t0 = performance.now();
    const step = (t) => {
      const k = Math.min(1, (t - t0) / 900);
      const e = 1 - Math.pow(1 - k, 3);
      node.textContent = money(from + (to - from) * e);
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function renderHero(s) {
    const change = s.equity - s.start_cash;
    $("#startCash").textContent = money(s.start_cash);
    const value = $("#value");
    value.className = "hero__value " + (change > 0.005 ? "is-up" : change < -0.005 ? "is-down" : "");
    tweenValue(value, s.equity);
    const delta = $("#delta");
    delta.replaceChildren(
      el("span", tone(change), `${signed(change)} (${pct(change / s.start_cash)})`),
      document.createTextNode(` en ${ago(s.updated - s.started)}`),
    );
    const total = s.wins + s.losses;
    const inPlay = s.open.reduce((a, p) => a + p.value, 0);
    const facts = [
      ["Ganadas", String(s.wins), s.wins ? "up" : ""],
      ["Perdidas", String(s.losses), s.losses ? "down" : ""],
      ["Ganado en cerradas", signed(s.realized), tone(s.realized)],
      ["En juego", money(inPlay), ""],
    ];
    const dl = $("#facts");
    dl.replaceChildren();
    for (const [k, v, cls] of facts) {
      const box = el("div");
      box.append(el("dd", cls, v), el("dt", null, k));
      dl.append(box);
    }
    if (total) dl.title = `${f0.format((s.wins / total) * 100)} % de aciertos`;
  }

  // ======================================================================= radar
  const radarTip = $("#radarTip");
  function showTip(tip, title, rows, x, y) {
    tip.replaceChildren(el("b", null, title));
    for (const [k, v, cls] of rows) {
      const r = el("div", "row");
      r.append(el("span", null, k), el("span", cls || "", v));
      tip.append(r);
    }
    tip.hidden = false;
    const pad = 14;
    const bw = tip.offsetWidth, bh = tip.offsetHeight;
    tip.style.left = Math.min(window.innerWidth - bw - 8, x + pad) + "px";
    tip.style.top = Math.max(8, y - bh - pad) + "px";
  }

  function renderRadar(s) {
    const box = $("#radar");
    const W = box.clientWidth, H = box.clientHeight;
    const m = { l: 44, r: 70, t: 14, b: 28 };
    const yMin = Math.log(0.2), yMax = Math.log(Math.max(4, ...s.open.map((p) => p.mult * 1.25)));
    const maxAge = Math.max(0.25, ...s.open.map((p) => (s.updated - p.opened) / 3600)) * 1.12;
    const X = (h) => m.l + (Math.min(h, maxAge) / maxAge) * (W - m.l - m.r);
    const Y = (mu) => m.t + (1 - (Math.log(Math.max(0.2, mu)) - yMin) / (yMax - yMin)) * (H - m.t - m.b);

    let root = box.querySelector("svg");
    if (!root) {
      root = svg("svg", { role: "img", "aria-label": "Mapa de posiciones abiertas" });
      root.append(svg("g", { class: "lanes" }), svg("g", { class: "bubbles" }));
      box.append(root);
    }
    root.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const lanes = root.querySelector(".lanes");
    lanes.replaceChildren();
    const lane = (mu, cls, label, lcls) => {
      const y = Y(mu);
      lanes.append(svg("line", { x1: m.l, x2: W - m.r, y1: y, y2: y, class: "lane " + cls }));
      const t = svg("text", { x: W - m.r + 8, y: y + 4, class: "lane-label " + (lcls || "") });
      t.textContent = label;
      lanes.append(t);
    };
    lane(0.75, "lane--stop", "stop −25 %", "lane-label--stop");
    lane(1, "lane--entry", "entrada");
    lane(2, "lane--tp", "toma 2x", "lane-label--tp");
    lane(3, "lane--tp", "toma 3x", "lane-label--tp");
    const ticks = [0, maxAge / 2, maxAge].map((h) => {
      const t = svg("text", { x: X(h), y: H - 6, class: "axis", "text-anchor": "middle" });
      t.textContent = h === 0 ? "recién comprada" : h < 1 ? `${f0.format(h * 60)} min` : `${f1.format(h)} h`;
      return t;
    });
    lanes.append(...ticks);
    const yl = svg("text", { x: 0, y: m.t + 10, class: "axis" });
    yl.textContent = "múltiplo";
    lanes.append(yl);

    const layer = root.querySelector(".bubbles");
    const maxVal = Math.max(1, ...s.open.map((p) => p.value));
    const keep = new Set();
    const placed = s.open.map((p) => ({ p, x: X((s.updated - p.opened) / 3600), y: Y(p.mult), r: 7 + 17 * Math.sqrt(p.value / maxVal) }));
    const labelDy = new Map();
    const byY = [...placed].sort((a, b) => a.y - b.y);
    const taken = [];
    for (const it of byY) {
      let ly = it.y;
      for (const o of taken) if (Math.abs(o.x - it.x) < 110 && Math.abs(o.ly - ly) < 15) ly = o.ly + 15;
      taken.push({ x: it.x, ly });
      labelDy.set(it.p.token, ly - it.y);
    }
    for (const p of s.open) {
      keep.add(p.token);
      let g = layer.querySelector(`[data-token="${CSS.escape(p.token)}"]`);
      if (!g) {
        g = svg("g", { class: "bubble", tabindex: "0", role: "button", "data-token": p.token });
        g.append(svg("circle", { cx: 0, cy: 0, r: 0 }), svg("text", { x: 0, y: 4 }));
        g.style.transform = `translate(${X(0)}px, ${Y(1)}px)`;
        g.addEventListener("mouseenter", (e) => g._tip && showTip(radarTip, ...g._tip(), e.clientX, e.clientY));
        g.addEventListener("mousemove", (e) => g._tip && showTip(radarTip, ...g._tip(), e.clientX, e.clientY));
        g.addEventListener("mouseleave", () => (radarTip.hidden = true));
        g.addEventListener("click", () => openTrade(p.token));
        g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openTrade(p.token); } });
        layer.append(g);
      }
      const r = 7 + 17 * Math.sqrt(p.value / maxVal);
      g.setAttribute("class", "bubble " + (p.pnl >= 0 ? "is-up" : "is-down"));
      g.setAttribute("aria-label", `${p.symbol}: ${mult(p.mult)}, ${signed(p.pnl)}`);
      const age = (s.updated - p.opened) / 3600;
      requestAnimationFrame(() => (g.style.transform = `translate(${X(age)}px, ${Y(p.mult)}px)`));
      g.querySelector("circle").setAttribute("r", r);
      const label = g.querySelector("text");
      label.setAttribute("x", r + 6);
      label.setAttribute("y", 4 + (labelDy.get(p.token) || 0));
      label.textContent = p.symbol.slice(0, 12);
      g._tip = () => [p.symbol, [
        ["Múltiplo", mult(p.mult), tone(p.mult - 1)],
        ["Máximo visto", mult(p.peak_mult)],
        ["Costo", money(p.cost)],
        ["Vale ahora", money(p.value)],
        ["Resultado", signed(p.pnl), tone(p.pnl)],
        ["Abierta hace", ago(s.updated - p.opened)],
      ]];
    }
    layer.querySelectorAll(".bubble").forEach((g) => { if (!keep.has(g.dataset.token)) g.remove(); });
    let empty = box.querySelector(".radar__empty");
    if (!s.open.length) {
      if (!empty) { empty = el("div", "radar__empty", "No hay posiciones abiertas. Cuando una moneda pase todos los filtros aparecerá aquí."); box.append(empty); }
    } else if (empty) empty.remove();
    $("#radarSub").textContent = `${s.open.length} de 10 cupos usados, 4 % del libro en cada una. Altura: cuánto se ha multiplicado.`;
  }

  // ======================================================================= cinta
  const seenEvents = new Set();
  let firstTape = true;
  function renderTape(s) {
    const list = $("#tape");
    list.replaceChildren();
    if (!s.events.length) { list.append(el("li", "empty", "Todavía no hay movimientos.")); return; }
    for (const e of s.events) {
      const id = `${e.t}-${e.token}-${e.kind}`;
      const li = el("li");
      if (!firstTape && !seenEvents.has(id)) li.classList.add("is-new");
      seenEvents.add(id);
      const isBuy = e.kind === "buy";
      const what = el("span", "what");
      const time = el("time", null, hm.format(new Date(e.t * 1000)));
      if (e.kind === "fail") {
        what.append(el("i", "warn", e.side === "buy" ? "Compra fallida" : "Venta fallida"), document.createTextNode(e.symbol || "?"));
        li.append(time, what, el("span", "amt down", e.fee ? `${MINUS}${money(e.fee)} red` : ""));
        li.append(el("span", "why", `${FAIL[e.note] || e.note}${e.side === "sell" ? ", se reintenta" : ""}${e.reason ? ` (${REASON[e.reason] || e.reason})` : ""}`));
      } else {
        const cls = isBuy ? "buy" : isGoodExit(e.reason) ? "up" : "down";
        what.append(el("i", cls, isBuy ? "Compró" : "Vendió"), document.createTextNode(e.symbol || "?"));
        li.append(time, what, el("span", "amt " + (isBuy ? "" : cls), isBuy ? money(e.usd) : `recibió ${money(e.usd)}`));
        const real = e.real_mult !== undefined ? `, ejecutado ${mult(e.real_mult)}` : "";
        li.append(el("span", "why", isBuy ? `liquidez $${f0.format(e.liq)}, ${f0.format(e.age_min)} min de vida` : `${REASON[e.reason] || e.reason}: pantalla ${mult(e.mult)}${real}`));
      }
      li.addEventListener("click", () => openTrade(e.token));
      list.append(li);
    }
    firstTape = false;
  }

  // ======================================================================= curva
  let curveChart, curveSeries, rangeHours = 0;
  const TZ = -new Date().getTimezoneOffset() * 60; // el gráfico pinta en UTC: se desplaza a hora local
  function renderCurve(s) {
    const LC = window.LightweightCharts;
    if (!LC) return;
    if (!curveChart) {
      const box = $("#curve");
      curveChart = LC.createChart(box, {
        autoSize: true,
        layout: { background: { type: LC.ColorType.Solid, color: "transparent" }, textColor: cssVar("--muted"), fontFamily: "Geist, system-ui", fontSize: 12 },
        grid: { vertLines: { visible: false }, horzLines: { color: cssVar("--line") } },
        rightPriceScale: { borderVisible: false },
        timeScale: { borderColor: cssVar("--line"), timeVisible: true, secondsVisible: false },
        crosshair: { mode: LC.CrosshairMode.Magnet, horzLine: { visible: false, labelVisible: false }, vertLine: { color: cssVar("--line-2"), labelVisible: false } },
        localization: { locale: LOCALE, priceFormatter: (v) => money(v) },
        handleScale: { mouseWheel: false },
      });
      curveSeries = curveChart.addSeries(LC.AreaSeries, {
        lineColor: cssVar("--sol"), lineWidth: 2.5, topColor: "rgba(153,69,255,0.35)", bottomColor: "rgba(153,69,255,0)",
        priceLineVisible: false, crosshairMarkerBorderColor: cssVar("--bg"), crosshairMarkerRadius: 5,
      });
      curveSeries.createPriceLine({ price: s.start_cash, color: cssVar("--muted"), lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: true, title: "inicio" });
      const tip = $("#curveTip");
      curveChart.subscribeCrosshairMove((param) => {
        const d = param.time && param.seriesData.get(curveSeries);
        if (!d || !param.point) { tip.hidden = true; return; }
        const change = d.value - s.start_cash;
        tip.replaceChildren(el("b", null, money(d.value)));
        const r = el("div", "row");
        r.append(el("span", null, dhm.format(new Date((param.time - TZ) * 1000))), el("span", tone(change), pct(change / s.start_cash)));
        tip.append(r);
        tip.hidden = false;
        tip.style.left = Math.min(box.clientWidth - tip.offsetWidth - 70, param.point.x + 14) + "px";
        tip.style.top = "8px";
      });
    }
    const seen = new Set();
    const data = [];
    for (const p of s.curve) {
      const t = Math.floor(p.t) + TZ;
      if (seen.has(t)) continue;
      seen.add(t);
      data.push({ time: t, value: p.equity });
    }
    // Último punto: el valor de ahora, para que la curva termine donde dice el titular.
    const nowT = Math.floor(s.updated) + TZ;
    if (!data.length || nowT > data[data.length - 1].time) data.push({ time: nowT, value: s.equity });
    curveSeries.setData(data);
    applyRange();
  }
  function applyRange() {
    if (!curveChart || !snap) return;
    if (!rangeHours) { curveChart.timeScale().fitContent(); return; }
    const to = Math.floor(snap.updated) + TZ;
    curveChart.timeScale().setVisibleRange({ from: to - rangeHours * 3600, to });
  }
  function segmented(id, onPick) {
    const root = $(id);
    root.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      root.querySelectorAll("button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
      onPick(b);
    });
  }
  segmented("#ranges", (b) => { rangeHours = Number(b.dataset.h); applyRange(); });

  // ======================================================================= tabla
  let filter = "all";
  let sort = { key: "time", dir: -1 };
  const COLUMNS = [
    { key: "symbol", label: "Moneda" },
    { key: "state", label: "Estado" },
    { key: "time", label: "Abrió" },
    { key: "dur", label: "Duró", num: true },
    { key: "mult", label: "Múltiplo", num: true },
    { key: "pnl", label: "Resultado", num: true },
    { key: "reason", label: "Cómo salió" },
  ];
  segmented("#filters", (b) => { filter = b.dataset.f; renderTable(snap); });

  function rows(s) {
    const open = s.open.map((p) => ({ token: p.token, symbol: p.symbol, state: "open", time: p.opened, dur: s.updated - p.opened, mult: p.mult, pnl: p.pnl, reason: "" }));
    const closed = s.closed.map((c) => ({ token: c.token, symbol: c.symbol, state: c.pnl > 0 ? "win" : "loss", time: c.opened, dur: c.closed - c.opened, mult: c.mult, pnl: c.pnl, reason: REASON[c.reason] || c.reason }));
    return open.concat(closed).filter((r) => filter === "all" || r.state === filter);
  }
  function renderTable(s) {
    if (!s) return;
    const table = $("#trades");
    table.replaceChildren();
    const thead = el("thead"), tr = el("tr");
    for (const c of COLUMNS) {
      const th = el("th", c.num ? "num" : "");
      if (sort.key === c.key) th.setAttribute("aria-sort", sort.dir > 0 ? "ascending" : "descending");
      const b = el("button", null, c.label);
      b.addEventListener("click", () => { sort = { key: c.key, dir: sort.key === c.key ? -sort.dir : -1 }; renderTable(snap); });
      th.append(b);
      tr.append(th);
    }
    thead.append(tr);
    const tbody = el("tbody");
    const data = rows(s).sort((a, b) => {
      const va = a[sort.key], vb = b[sort.key];
      return (typeof va === "string" ? va.localeCompare(vb) : va - vb) * sort.dir;
    });
    if (!data.length) {
      const r = el("tr"), td = el("td", "empty", filter === "all" ? "Todavía no hay operaciones." : "Ninguna operación en este filtro.");
      td.colSpan = COLUMNS.length;
      r.append(td);
      tbody.append(r);
    }
    const TAG = { open: ["Abierta", "tag--open"], win: ["Ganó", "tag--win"], loss: ["Perdió", "tag--loss"] };
    for (const d of data.slice(0, 200)) {
      const r = el("tr");
      r.tabIndex = 0;
      r.addEventListener("click", () => openTrade(d.token));
      r.addEventListener("keydown", (e) => { if (e.key === "Enter") openTrade(d.token); });
      const tag = el("span", "tag " + TAG[d.state][1], TAG[d.state][0]);
      const cells = [
        [el("span", null, d.symbol), "sym"], [tag], [at(d.time)], [ago(d.dur), "num"],
        [mult(d.mult), "num " + tone(d.mult - 1)], [signed(d.pnl), "num " + tone(d.pnl)], [d.reason || "sigue abierta"],
      ];
      for (const [content, cls] of cells) {
        const td = el("td", cls || "");
        if (content instanceof Node) td.append(content); else td.textContent = content;
        r.append(td);
      }
      tbody.append(r);
    }
    table.append(thead, tbody);
  }

  // ======================================================================= verdad
  function renderTruth(s) {
    const list = $("#truth");
    list.replaceChildren();
    const item = (strong, rest) => { const li = el("li"); li.append(el("b", null, strong), document.createTextNode(" " + rest)); list.append(li); };
    if (s.worst_stop !== null) item(`El peor "stop de −25 %" vendió a ${mult(s.worst_stop)}.`, "Si la moneda se desploma entre dos consultas, el stop vende a lo que quede.");
    const rugs = s.exit_reasons.liquidez || 0;
    if (rugs) item(`${rugs} ${rugs === 1 ? "salida" : "salidas"} porque sacaron la liquidez.`, "Eso es un rug pull: el precio se ve bien, pero ya no hay quién te compre.");
    const tx = s.tx || {};
    const failed = (tx.fallidas || 0) + (tx.sin_ruta || 0);
    if (tx.intentos) item(`${failed} de ${tx.intentos} transacciones fallaron.`, `Cada intento pagó la red igual: ${money(s.network_fees)} en comisiones hasta ahora. Una venta fallida se reintenta con el precio que haya después.`);
    item("Cada operación usa la cotización real de Jupiter tras 1.5 s de retraso, más 1 % del bot.", "Los posts no muestran comisiones, deslizamiento ni transacciones fallidas.");
    item("Ningún post enseña su billetera.", "Cualquier ganancia real en Solana se puede comprobar en la blockchain. Si no te la enseñan, no existe.");
  }

  // ======================================================================= cajón de detalle
  const drawer = $("#drawer");
  let lastFocus = null, drawerChart = null;
  drawer.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeDrawer(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !drawer.hidden) closeDrawer(); });
  function closeDrawer() {
    drawer.hidden = true;
    if (drawerChart) { drawerChart.remove(); drawerChart = null; }
    lastFocus?.focus?.();
  }
  async function openTrade(token) {
    radarTip.hidden = true;
    lastFocus = document.activeElement;
    let d;
    try {
      const res = await fetch("/api/meme/trade?token=" + encodeURIComponent(token) + (vq() ? "&" + vq() : ""), { cache: "no-store" });
      d = await res.json();
      if (!res.ok) throw new Error(d.error || res.status);
    } catch (err) {
      return;
    }
    const t = d.trade;
    const isOpen = d.status === "open";
    const last = t.last || t.entry;
    const value = isOpen ? t.qty * last : 0;
    const pnl = isOpen ? value + t.proceeds - t.cost : t.pnl;
    $("#dTitle").textContent = t.symbol;
    $("#dSub").textContent = isOpen ? `Abierta desde ${at(t.opened)}` : `Cerró ${at(t.closed)}: ${REASON[t.reason] || t.reason}`;
    const stats = [
      ["Puso", money(t.cost)],
      [isOpen ? "Vale ahora" : "Recibió", money(isOpen ? value + t.proceeds : t.proceeds)],
      ["Resultado", signed(pnl), tone(pnl)],
      ["Precio de entrada", price(t.entry)],
      [isOpen ? "Precio ahora" : "Último precio", price(last)],
      ["Máximo visto", mult(t.peak / t.entry)],
    ];
    if (t.fees) stats.push(["Comisiones de red", money(t.fees)]);
    const dl = $("#dStats");
    dl.replaceChildren();
    for (const [k, v, cls] of stats) { const b = el("div"); b.append(el("dt", null, k), el("dd", cls || "", v)); dl.append(b); }
    const ev = $("#dEvents");
    ev.replaceChildren();
    for (const e of d.events) {
      const li = el("li");
      let text, cls;
      if (e.kind === "buy") { text = `Compró ${money(e.usd)} a ${price(e.price)}`; cls = "buy"; }
      else if (e.kind === "fail") { text = `${e.side === "buy" ? "Compra" : "Venta"} fallida: ${FAIL[e.note] || e.note}${e.fee ? `, pagó ${money(e.fee)} de red` : ""}`; cls = "warn"; }
      else {
        const real = e.real_mult !== undefined ? `, en pantalla ${mult(e.mult)} pero ejecutado ${mult(e.real_mult)}` : "";
        text = `Vendió ${f0.format(e.frac * 100)} %: recibió ${money(e.usd)} (${REASON[e.reason] || e.reason}${real})`;
        cls = isGoodExit(e.reason) ? "up" : "down";
      }
      li.append(el("span", cls, text), el("time", null, at(e.t)));
      ev.append(li);
    }
    const links = $("#dLinks");
    links.replaceChildren();
    for (const [label, href] of [["Ver en DexScreener", "https://dexscreener.com/solana/" + encodeURIComponent(t.token)], ["Ver en Solscan", "https://solscan.io/token/" + encodeURIComponent(t.token)]]) {
      const a = el("a", null, label);
      a.href = href; a.target = "_blank"; a.rel = "noopener noreferrer";
      links.append(a);
    }
    drawer.hidden = false;
    drawer.querySelector(".drawer__panel").focus();
    drawTradeChart(d, t);
  }
  function drawTradeChart(d, t) {
    const LC = window.LightweightCharts;
    const box = $("#dChart");
    if (drawerChart) { drawerChart.remove(); drawerChart = null; }
    box.replaceChildren();
    if (!LC || d.path.length < 2) { box.append(el("p", "empty", "Todavía no hay suficientes precios para graficar.")); return; }
    drawerChart = LC.createChart(box, {
      autoSize: true,
      layout: { background: { type: LC.ColorType.Solid, color: "transparent" }, textColor: cssVar("--muted"), fontFamily: "Geist, system-ui", fontSize: 11 },
      grid: { vertLines: { visible: false }, horzLines: { color: cssVar("--line") } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderColor: cssVar("--line"), timeVisible: true, secondsVisible: false },
      handleScale: { mouseWheel: false },
    });
    const up = (t.last || t.entry) >= t.entry;
    const series = drawerChart.addSeries(LC.LineSeries, {
      color: up ? cssVar("--gain") : cssVar("--loss"), lineWidth: 2, priceLineVisible: false,
      priceFormat: { type: "custom", minMove: 1e-12, formatter: (v) => price(v) },
      // Que siempre se vean la entrada y el stop, aunque el precio esté lejos.
      autoscaleInfoProvider: (orig) => {
        const r = orig();
        if (!r) return r;
        r.priceRange.minValue = Math.min(r.priceRange.minValue, t.entry * 0.7);
        r.priceRange.maxValue = Math.max(r.priceRange.maxValue, t.entry * 1.08);
        return r;
      },
    });
    const seen = new Set();
    const pts = [];
    for (const p of d.path) {
      const time = Math.floor(p.t) + TZ;
      if (seen.has(time)) continue;
      seen.add(time);
      pts.push({ time, value: p.p });
    }
    series.setData(pts);
    const line = (mu, color, title, style) => series.createPriceLine({ price: t.entry * mu, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title });
    line(1, cssVar("--muted"), "entrada", LC.LineStyle.Solid);
    line(0.75, cssVar("--loss"), "stop", LC.LineStyle.Dashed);
    line(2, cssVar("--gain"), "2x", LC.LineStyle.Dashed);
    line(3, cssVar("--gain"), "3x", LC.LineStyle.Dashed);
    drawerChart.timeScale().fitContent();
  }

  // ======================================================================= ciclo
  const seenBuys = new Set();
  let firstLoad = true;
  async function tick() {
    let s;
    try {
      const [res, vres] = await Promise.all([
        fetch("/api/meme" + (vq() ? "?" + vq() : ""), { cache: "no-store" }),
        fetch("/api/meme/variants", { cache: "no-store" }),
      ]);
      s = await res.json();
      renderVariants(await vres.json());
    } catch (err) {
      $("#updated").textContent = "No se pudo leer el simulador. Revisa que Solbot siga corriendo.";
      $("#pulse").className = "pulse is-stale";
      return;
    }
    if (!s.ready) { $("#sieveTitle").textContent = "El simulador todavía no ha guardado datos."; return; }
    snap = s;
    const newBuys = s.events.filter((e) => e.kind === "buy" && !seenBuys.has(`${e.t}-${e.token}`));
    newBuys.forEach((e) => seenBuys.add(`${e.t}-${e.token}`));
    sieve.feed(s.funnel, firstLoad ? newBuys.slice(0, 3) : newBuys);
    firstLoad = false;
    renderHero(s);
    renderGates(s);
    renderRadar(s);
    renderTape(s);
    renderCurve(s);
    renderTable(s);
    renderTruth(s);
    const age = Date.now() / 1000 - s.updated;
    $("#pulse").className = "pulse " + (age < 120 ? "is-live" : "is-stale");
    $("#updated").textContent = age < 120 ? `En vivo, ${hm.format(new Date(s.updated * 1000))}` : `Sin datos nuevos desde ${dhm.format(new Date(s.updated * 1000))}. ¿El simulador está corriendo?`;
  }

  function renderVariants(list) {
    const nav = $("#variants");
    if (!Array.isArray(list) || list.length < 2) { nav.hidden = true; return; }
    nav.hidden = false;
    nav.replaceChildren();
    for (const v of list) {
      const b = el("button", "variant");
      b.type = "button";
      b.setAttribute("aria-pressed", String(v.name === variant));
      b.title = VARIANT_HELP[v.name] || "";
      const change = v.equity - 100;
      const meta = el("span", "variant__meta");
      meta.append(el("b", tone(change), money(v.equity)), document.createTextNode(`, ${v.closed} cerradas`));
      b.append(el("span", "variant__name", VARIANT_NAMES[v.name] || v.name), meta);
      b.addEventListener("click", () => switchVariant(v.name));
      nav.append(b);
    }
  }

  function switchVariant(name) {
    if (name === variant) return;
    variant = name;
    history.replaceState(null, "", name === ROOT ? location.pathname : "#v=" + name);
    // Cada variante es otro libro: se reinicia lo que la página recuerda del anterior.
    shownValue = null;
    seenEvents.clear();
    seenBuys.clear();
    firstTape = true;
    firstLoad = true;
    sieve.prevCounts = null;
    sieve.dots = [];
    sieve.queue = [];
    const svgRoot = $("#radar svg");
    if (svgRoot) svgRoot.remove();
    if (curveChart) { curveChart.remove(); curveChart = null; curveSeries = null; }
    tick();
  }

  window.addEventListener("resize", () => snap && renderRadar(snap));
  window.addEventListener("load", () => {
    sieve.init();
    tick();
    setInterval(tick, 15000);
  });
})();
