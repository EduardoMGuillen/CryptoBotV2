"use strict";
// Meme Desk: solo lee /api/meme. Los nombres de tokens vienen de internet: siempre textContent.
(() => {
  const $ = (s) => document.querySelector(s);
  const f2 = new Intl.NumberFormat("es-HN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const f0 = new Intl.NumberFormat("es-HN", { maximumFractionDigits: 0 });
  const timeF = new Intl.DateTimeFormat("es-HN", { hour: "2-digit", minute: "2-digit" });
  const dayTimeF = new Intl.DateTimeFormat("es-HN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const MINUS = "−";
  const money = (v) => (v < 0 ? MINUS : "") + "$" + f2.format(Math.abs(v));
  const signed = (v) => (v >= 0 ? "+" : MINUS) + "$" + f2.format(Math.abs(v));
  const pct = (v) => (v >= 0 ? "+" : MINUS) + f0.format(Math.abs(v * 100)) + "%";
  const mult = (m) => "x" + f2.format(m);
  const tone = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  const FUNNEL = {
    vistos: "Monedas vistas",
    descartados_sin_liquidez: "Murieron sin liquidez (30 min)",
    rechazo_edad: "Más de 6 h de vida",
    rechazo_par: "No cotiza contra SOL/USDC",
    rechazo_mint_authority: "Pueden crear más tokens",
    rechazo_freeze_authority: "Pueden congelar tu saldo",
    rechazo_mint_ilegible: "Mint ilegible",
    rechazo_top10: "Top 10 holders > 35 %",
    rechazo_holder_grande: "Un holder > 5 %",
    sin_cupo: "Pasaron, pero sin cupo (10 abiertas)",
    compras: "Compradas",
  };
  const REASON = { stop: "stop −25 %", trailing: "trailing", liquidez: "liquidez se fue", tiempo: "24 h", tp1: "toma 2x", tp2: "toma 3x" };

  let chart, series;

  function renderCurve(s) {
    if (!window.LightweightCharts) return;
    const LC = window.LightweightCharts;
    const css = getComputedStyle(document.documentElement);
    const c = (name) => css.getPropertyValue(name).trim();
    if (!chart) {
      chart = LC.createChart($("#curve"), {
        autoSize: true,
        layout: { background: { type: LC.ColorType.Solid, color: "transparent" }, textColor: c("--muted"), fontFamily: "Manrope, system-ui", fontSize: 12 },
        grid: { vertLines: { visible: false }, horzLines: { color: c("--line") } },
        rightPriceScale: { borderVisible: false },
        timeScale: { borderColor: c("--line"), timeVisible: true, secondsVisible: false },
        localization: { locale: "es-HN", priceFormatter: (v) => money(v) },
      });
      series = chart.addSeries(LC.AreaSeries, {
        lineColor: c("--violet"), topColor: "rgba(139,108,255,0.28)", bottomColor: "rgba(139,108,255,0)",
        lineWidth: 2, priceLineVisible: false,
      });
      series.createPriceLine({ price: s.start_cash, color: c("--muted"), lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: true, title: "inicio" });
    }
    // Hora local de Honduras en el eje: lightweight-charts muestra UTC, así que se desplaza.
    const offset = -new Date().getTimezoneOffset() * 60;
    const seen = new Set();
    const data = [];
    for (const p of s.curve) {
      const t = Math.floor(p.t) + offset;
      if (seen.has(t)) continue;
      seen.add(t);
      data.push({ time: t, value: p.equity });
    }
    series.setData(data);
  }

  function stat(label, value, sub, cls) {
    const box = el("div", "stat");
    box.append(el("div", "stat__label", label));
    const v = el("div", "stat__value " + (cls || ""), value);
    box.append(v);
    if (sub) box.append(el("div", "stat__sub", sub));
    return box;
  }

  function render(s) {
    const change = s.equity - s.start_cash;
    $("#hStart").textContent = money(s.start_cash);
    const now = $("#hNow");
    now.textContent = money(s.equity);
    now.className = tone(change);
    const hours = (s.updated - s.started) / 3600;
    const span = hours < 48 ? `${f0.format(hours)} h` : `${f0.format(hours / 24)} días`;
    $("#hSub").textContent = s.seen
      ? `Rechazó ${f0.format(s.rejected)} de ${f0.format(s.seen)} monedas · ${f0.format(s.bought)} compras · ${span}`
      : "Buscando monedas…";

    const total = s.wins + s.losses;
    const stats = $("#stats");
    stats.replaceChildren(
      stat("Resultado", pct(change / s.start_cash), signed(change), tone(change)),
      stat("Ganadas / perdidas", `${s.wins} / ${s.losses}`, total ? `${f0.format((s.wins / total) * 100)} % aciertos` : "sin cierres aún"),
      stat("Realizado", signed(s.realized), "ganancia de cerradas", tone(s.realized)),
      stat("Abiertas", String(s.open.length), money(s.open.reduce((a, p) => a + p.value, 0)) + " en juego"),
      stat("Mejor cerrada", s.best ? signed(s.best.pnl) : "—", s.best ? `${s.best.symbol} ${mult(s.best.mult)}` : "", s.best ? tone(s.best.pnl) : ""),
      stat("Peor cerrada", s.worst ? signed(s.worst.pnl) : "—", s.worst ? `${s.worst.symbol} ${mult(s.worst.mult)}` : "", s.worst ? tone(s.worst.pnl) : ""),
    );

    // embudo
    const funnel = $("#funnel");
    funnel.replaceChildren();
    const keys = Object.keys(s.funnel).sort((a, b) => (a === "vistos" ? -1 : b === "vistos" ? 1 : a === "compras" ? 1 : b === "compras" ? -1 : s.funnel[b] - s.funnel[a]));
    for (const k of keys) {
      const li = el("li");
      li.append(el("span", null, FUNNEL[k] || k), el("span", "n", f0.format(s.funnel[k])));
      const bar = el("div", "bar");
      const fill = el("span", k === "compras" ? "is-buy" : "");
      fill.style.width = `${Math.max(1, (s.funnel[k] / Math.max(1, s.seen)) * 100)}%`;
      bar.append(fill);
      li.append(bar);
      funnel.append(li);
    }

    // feed
    const feed = $("#feed");
    feed.replaceChildren();
    if (!s.events.length) feed.append(el("li", "empty", "Sin movimientos aún."));
    for (const e of s.events) {
      const li = el("li");
      const what = el("span", "feed__what");
      // En ventas se muestra lo que de verdad se recibió: en un rug el precio puede verse alto
      // pero el pool ya no tiene con qué pagarte.
      const good = e.kind === "sell" && (e.reason.startsWith("tp") || e.reason === "trailing");
      const cls = e.kind === "buy" ? "" : good ? "up" : "down";
      const side = el("b", cls, e.kind === "buy" ? "COMPRA " : "VENDE ");
      what.append(side, document.createTextNode(e.symbol || "?"));
      const right = e.kind === "buy" ? money(e.usd) : `recibió ${money(e.usd)} · ${REASON[e.reason] || e.reason}`;
      li.append(el("span", "feed__time", timeF.format(new Date(e.t * 1000))), what, el("span", cls, right));
      feed.append(li);
    }

    // tablas
    const dexLink = (token, text) => {
      const a = el("a", null, text);
      a.href = "https://dexscreener.com/solana/" + encodeURIComponent(token);
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      return a;
    };
    const table = (node, head, rows) => {
      node.replaceChildren();
      const thead = el("thead");
      const tr = el("tr");
      head.forEach(([h, num]) => tr.append(el("th", num ? "num" : "", h)));
      thead.append(tr);
      const tbody = el("tbody");
      if (!rows.length) {
        const r = el("tr");
        const td = el("td", "empty", "Nada todavía.");
        td.colSpan = head.length;
        r.append(td);
        tbody.append(r);
      }
      rows.forEach((cells) => {
        const r = el("tr");
        cells.forEach(([content, cls]) => {
          const td = el("td", cls || "");
          if (content instanceof Node) td.append(content); else td.textContent = content;
          r.append(td);
        });
        tbody.append(r);
      });
      node.append(thead, tbody);
    };

    $("#openCaption").textContent = `${s.open.length} de 10 cupos · 4 % del libro cada una`;
    table($("#openTable"), [["Token"], ["Desde"], ["Costo", 1], ["Vale", 1], ["Múltiplo", 1], ["Resultado", 1]],
      s.open.map((p) => [
        [dexLink(p.token, p.symbol), "sym"], [timeF.format(new Date(p.opened * 1000))],
        [money(p.cost), "num"], [money(p.value), "num"], [mult(p.mult), "num " + tone(p.mult - 1)], [signed(p.pnl), "num " + tone(p.pnl)],
      ]));
    table($("#closedTable"), [["Token"], ["Cerró"], ["Motivo"], ["Múltiplo", 1], ["Resultado", 1]],
      s.closed.map((c) => [
        [dexLink(c.token, c.symbol), "sym"], [dayTimeF.format(new Date(c.closed * 1000))], [REASON[c.reason] || c.reason],
        [mult(c.mult), "num " + tone(c.mult - 1)], [signed(c.pnl), "num " + tone(c.pnl)],
      ]));

    // honestidad
    const truth = $("#truth");
    truth.replaceChildren();
    const item = (strong, rest) => { const li = el("li"); li.append(el("b", null, strong), document.createTextNode(" " + rest)); truth.append(li); };
    if (s.worst_stop !== null) item(`El peor "stop −25 %" salió en ${mult(s.worst_stop)}.`, "Cuando una moneda se desploma entre dos consultas, el stop vende a lo que quede.");
    const rugs = s.exit_reasons.liquidez || 0;
    if (rugs) item(`${rugs} salidas porque se fue la liquidez.`, "Es la señal típica de un rug pull.");
    item("Se cobra 1 % por lado + impacto de precio + 2 % de retraso al entrar.", "Los posts no cuentan comisiones ni deslizamiento.");
    item("No muestra una billetera porque no hay una: es papel.", "Cualquier resultado real se puede verificar en la blockchain; pídelo siempre.");

    const age = Date.now() / 1000 - s.updated;
    const dot = $("#liveDot");
    dot.className = "desk__dot " + (age < 120 ? "is-live" : "is-stale");
    $("#updated").textContent = age < 120 ? `Actualizado ${timeF.format(new Date(s.updated * 1000))}` : `Sin datos nuevos desde ${dayTimeF.format(new Date(s.updated * 1000))} — ¿el simulador está corriendo?`;
    renderCurve(s);
  }

  async function tick() {
    try {
      const res = await fetch("/api/meme", { cache: "no-store" });
      const s = await res.json();
      if (!s.ready) { $("#hSub").textContent = "El simulador todavía no ha guardado datos."; return; }
      render(s);
    } catch (err) {
      $("#updated").textContent = "No se pudo leer el simulador.";
    }
  }

  tick();
  setInterval(tick, 15000);
})();
