"use strict";
/* Solbot: interfaz local. Todo dato del servidor se inserta con textContent. */
(() => {
  const LC = window.LightweightCharts;
  const TOKEN = document.querySelector('meta[name="solbot-token"]')?.content ?? "";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  // ---------------------------------------------------------------- formato
  const LOCALE = "es-MX";
  const MINUS = "−";
  const f2 = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const f0 = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 0 });
  const f4 = new Intl.NumberFormat(LOCALE, { minimumFractionDigits: 4, maximumFractionDigits: 4 });
  const ok = (v) => v != null && Number.isFinite(v);
  const money = (v, digits = 2) => (ok(v) ? (v < 0 ? MINUS : "") + "$" + (digits === 0 ? f0 : f2).format(Math.abs(v)) : "—");
  const moneyBig = (v) => (ok(v) && Math.abs(v) >= 10000 ? money(v, 0) : money(v));
  const pct = (v, digits = 1) => (ok(v) ? (v > 0 ? "+" : v < 0 ? MINUS : "") + Math.abs(v * 100).toFixed(digits) + "%" : "—");
  const share = (v) => (ok(v) ? Math.round(v * 100) + "%" : "—");
  const solAmt = (v) => (ok(v) ? f4.format(v) + " SOL" : "—");
  const usdcAmt = (v) => (ok(v) ? f2.format(v) + " USDC" : "—");
  const dateF = new Intl.DateTimeFormat(LOCALE, { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
  const dateTimeF = new Intl.DateTimeFormat(LOCALE, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
  const toDate = (iso) => new Date(iso.length === 10 ? iso + "T00:00:00Z" : iso);
  const fmtDate = (iso) => (iso ? dateF.format(toDate(iso)) : "—");
  const fmtDateTime = (iso) => (iso ? dateTimeF.format(new Date(iso)) : "—");
  const timeIso = (t) =>
    typeof t === "string" ? t
      : typeof t === "number" ? new Date(t * 1000).toISOString().slice(0, 10)
      : `${t.year}-${String(t.month).padStart(2, "0")}-${String(t.day).padStart(2, "0")}`;
  const tone = (v) => (!ok(v) || Math.abs(v) < 0.0005 ? "flat" : v > 0 ? "up" : "down");

  function chip(v, digits = 1) {
    const t = tone(v);
    const arrow = t === "up" ? "↑ " : t === "down" ? "↓ " : "";
    return el("span", `chip chip--${t}`, arrow + pct(v, digits).replace(/^[+−]/, ""));
  }

  function setHero(node, value) {
    node.replaceChildren();
    if (!ok(value)) { node.textContent = "—"; return; }
    const [whole, cents] = f2.format(Math.abs(value)).split(".");
    node.append((value < 0 ? MINUS : "") + "$" + whole, el("span", "cents", "." + cents));
  }

  // ---------------------------------------------------------------- API
  async function api(path, body) {
    const opts = body === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Solbot-Token": TOKEN },
      body: JSON.stringify(body),
    };
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Error ${res.status}`);
    return data;
  }

  // ---------------------------------------------------------------- estado de la página
  const S = {
    status: null,
    backtest: null,
    backtestLoading: null,
    chart: null,
    tickers: [],
    page: "panel",
    eq: null, // "bot" | "backtest" (null = automático)
    days: 365,
    bt: { scenario: "lump", period: "full", selected: null, log: true },
  };
  const charts = {};

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* sin almacenamiento */ } },
  };

  // ---------------------------------------------------------------- avisos
  let toastTimer;
  function toast(text, isError = false) {
    const t = $("#toast");
    t.textContent = text;
    t.classList.toggle("is-error", isError);
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 7000 : 4000);
  }

  function confirmDialog({ title, text, code = false, okLabel = "Confirmar", live = false }) {
    const dialog = $("#confirmDialog");
    $("#dialogTitle").textContent = title;
    $("#dialogText").textContent = text;
    $("#dialogField").hidden = !code;
    $("#dialogInput").value = "";
    $("#dialogError").textContent = "";
    const okBtn = $("#dialogOk");
    okBtn.textContent = okLabel;
    okBtn.className = `btn ${live ? "btn--live" : "btn--primary"}`;
    return new Promise((resolve) => {
      const onClose = () => {
        dialog.removeEventListener("close", onClose);
        resolve(dialog.returnValue === "ok" ? $("#dialogInput").value.trim() || true : null);
      };
      dialog.addEventListener("close", onClose);
      dialog.showModal();
      if (code) $("#dialogInput").focus();
    });
  }

  // ---------------------------------------------------------------- colores (desde CSS)
  function palette() {
    const cs = getComputedStyle(document.documentElement);
    const v = (n) => cs.getPropertyValue(n).trim();
    return {
      ink: v("--ink"), ink2: v("--ink-2"), muted: v("--muted"), line: v("--line"), panel: v("--panel"),
      violet: v("--violet"), aqua: v("--aqua"), hold: v("--hold"), up: v("--up"), down: v("--down"),
    };
  }
  const alpha = (hex, a) => {
    const h = hex.replace("#", "");
    const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
    return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
  };

  // ---------------------------------------------------------------- gráficas
  function makeChart(container, { log = false, money: isMoney = true, percent = false, interactive = false } = {}) {
    const p = palette();
    const formatter = percent ? (v) => `${v.toFixed(0)}%` : isMoney ? (v) => moneyBig(v) : (v) => f2.format(v);
    return LC.createChart(container, {
      autoSize: true,
      layout: {
        background: { type: LC.ColorType.Solid, color: "transparent" },
        textColor: p.muted,
        fontFamily: getComputedStyle(document.body).fontFamily,
        fontSize: 12,
        attributionLogo: true,
      },
      grid: { vertLines: { visible: false }, horzLines: { color: p.line } },
      rightPriceScale: {
        borderVisible: false,
        mode: log ? LC.PriceScaleMode.Logarithmic : LC.PriceScaleMode.Normal,
        scaleMargins: { top: 0.12, bottom: 0.06 },
      },
      timeScale: { borderColor: p.line, fixLeftEdge: true, fixRightEdge: true, lockVisibleTimeRangeOnResize: true },
      crosshair: {
        mode: LC.CrosshairMode.Magnet,
        vertLine: { color: p.muted, width: 1, style: LC.LineStyle.Solid, labelVisible: false },
        horzLine: { visible: false, labelVisible: false },
      },
      localization: { locale: LOCALE, priceFormatter: formatter },
      handleScroll: interactive ? { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true } : { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true },
      handleScale: interactive ? true : { mouseWheel: false, pinch: true, axisPressedMouseMove: true },
    });
  }

  function seriesOpts(color, extra = {}) {
    const p = palette();
    return {
      color,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerRadius: 4,
      crosshairMarkerBorderColor: p.panel,
      crosshairMarkerBorderWidth: 2,
      ...extra,
    };
  }

  function areaOpts(color, extra = {}) {
    return { ...seriesOpts(color, extra), lineColor: color, topColor: alpha(color, 0.18), bottomColor: alpha(color, 0.0) };
  }

  /** Tooltip de cruce: una línea por serie, el valor primero. */
  function attachTooltip(chart, wrap, tip, rowsFn, format) {
    chart.subscribeCrosshairMove((param) => {
      if (!param.time || !param.point || param.point.x < 0 || param.point.y < 0) { tip.hidden = true; return; }
      tip.replaceChildren(el("div", "tooltip__date", fmtDate(timeIso(param.time))));
      for (const row of rowsFn()) {
        const data = param.seriesData.get(row.series);
        if (!data) continue;
        const value = data.value ?? data.close;
        const line = el("div", "tooltip__row");
        const name = el("span", "tooltip__name");
        const key = el("span", "key");
        key.style.color = row.color;
        name.append(key, document.createTextNode(row.name));
        line.append(name, el("span", "tooltip__value", (row.format || format)(value, data)));
        tip.append(line);
      }
      tip.hidden = false;
      const w = tip.offsetWidth;
      const max = wrap.clientWidth;
      let x = param.point.x + 16;
      if (x + w > max) x = param.point.x - w - 16;
      tip.style.left = `${Math.max(0, x)}px`;
      tip.style.top = "8px";
    });
  }

  function legend(node, items) {
    node.replaceChildren(...items.map(({ color, name, dot }) => {
      const item = el("span", "legend__item");
      const key = el("span", dot ? "key key--dot" : "key");
      key.style.color = color;
      item.append(key, document.createTextNode(name));
      return item;
    }));
  }

  function dropChart(name) {
    if (charts[name]) { charts[name].chart.remove(); delete charts[name]; }
  }

  // ---------------------------------------------------------------- navegación
  const PAGES = ["panel", "mercado", "backtest", "operaciones", "wallet"];

  function route() {
    const page = PAGES.includes(location.hash.slice(1)) ? location.hash.slice(1) : "panel";
    S.page = page;
    for (const view of $$(".view")) view.hidden = view.dataset.view !== page;
    for (const link of $$(".nav a")) {
      if (link.dataset.page === page) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    $("#shell").classList.remove("nav-open");
    renderPage();
  }

  function renderPage() {
    if (S.page === "panel") { renderPanel(); renderEquity(); }
    if (S.page === "mercado") loadMarket();
    if (S.page === "backtest") loadBacktest().then(renderBacktest);
    if (S.page === "operaciones") renderOps();
    if (S.page === "wallet") renderWallet();
  }

  // ---------------------------------------------------------------- franja superior y marco
  function renderTicker() {
    const track = $("#tickerTrack");
    if (!S.tickers.length) return;
    const p = palette();
    track.replaceChildren(...S.tickers.map((t) => {
      const item = el("div", "tick");
      item.append(el("span", "tick__sym", t.symbol), el("span", "tick__price", t.price >= 1 ? money(t.price) : "$" + t.price.toPrecision(3)));
      if (t.spark?.length > 1) {
        const svgNS = "http://www.w3.org/2000/svg";
        const svg = document.createElementNS(svgNS, "svg");
        svg.setAttribute("viewBox", "0 0 54 18");
        svg.setAttribute("aria-hidden", "true");
        const lo = Math.min(...t.spark), hi = Math.max(...t.spark), span = hi - lo || 1;
        const d = t.spark.map((v, i) => `${i ? "L" : "M"}${(i / (t.spark.length - 1)) * 54},${16 - ((v - lo) / span) * 14}`).join(" ");
        const path = document.createElementNS(svgNS, "path");
        path.setAttribute("d", d);
        path.style.stroke = t.change_pct >= 0 ? p.up : p.down;
        svg.append(path);
        item.append(svg);
      }
      const ch = el("span", t.change_pct >= 0 ? "up" : "down", pct(t.change_pct / 100, 2));
      item.append(ch);
      return item;
    }));
  }

  function renderFrame() {
    const st = S.status;
    if (!st) return;
    const live = st.mode === "live";
    document.body.classList.toggle("is-live", live);

    const pill = $("#botPill");
    pill.classList.toggle("is-live", live);
    pill.classList.toggle("is-on", st.running && !st.kill && !st.pending);
    $(".ticker__status-text", pill).textContent =
      `${live ? "Dinero real" : "Papel"}: ${st.kill ? "parada de emergencia" : st.pending ? "esperando revisión" : st.running ? "bot en marcha" : "bot en pausa"}`;

    const card = $("#modeCard");
    card.classList.toggle("is-live", live);
    const addr = st.wallet?.address;
    $("#modeTitle").textContent = live ? "Dinero real" : "Modo papel";
    $("#modeText").textContent = live
      ? `Opera con tu wallet ${addr ? addr.slice(0, 4) + "…" + addr.slice(-4) : ""} dentro de los límites de solbot.toml.`
      : "Precios reales de Jupiter, dinero ficticio. Tu wallet no se toca.";

    $("#topValue").textContent = st.sleeve ? money(st.sleeve.value) : "Sin fondos aún";

    const btn = $("#toggleRun");
    btn.replaceChildren();
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", st.running ? "#i-pause" : "#i-play");
    icon.append(use);
    btn.append(icon, el("span", null, st.running ? "Pausar" : "Iniciar"));
    btn.className = `btn ${!st.running && live ? "btn--live" : "btn--primary"}`;

    renderBanners();
  }

  function renderBanners() {
    const st = S.status;
    const box = $("#banners");
    box.replaceChildren();
    const banner = (kind, title, text, actions = []) => {
      const b = el("div", `banner banner--${kind}`);
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("aria-hidden", "true");
      const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
      use.setAttribute("href", "#i-alert");
      svg.append(use);
      const body = el("div", "banner__body");
      body.append(el("p", "banner__title", title), el("p", "banner__text", text));
      if (actions.length) {
        const row = el("div", "banner__actions");
        row.append(...actions);
        body.append(row);
      }
      b.append(svg, body);
      box.append(b);
    };
    if (st.pending) {
      const yes = el("button", "btn btn--small btn--primary", "Sí se ejecutó");
      const no = el("button", "btn btn--small", "No se ejecutó");
      const look = el("a", "btn btn--small", "Abrir la wallet en Solscan");
      look.href = st.wallet?.explorer || "https://solscan.io";
      look.target = "_blank";
      look.rel = "noopener noreferrer";
      yes.addEventListener("click", () => resolvePending(true));
      no.addEventListener("click", () => resolvePending(false));
      banner("danger", "Orden sin confirmar: el bot está en pausa", st.pending.message || "No se sabe si la última orden se ejecutó.", [look, yes, no]);
    }
    if (st.kill) {
      banner("danger", "Parada de emergencia activa", "Existe el archivo KILL: el bot no opera. Bórralo para reanudar (ver Wallet y límites).");
    }
    if (st.mode === "live" && !st.pending && !st.kill) {
      const w = st.wallet;
      banner("live", "Dinero real", `El bot opera con tu wallet${w?.address ? " " + w.address.slice(0, 4) + "…" + w.address.slice(-4) : ""}. Máximo ${money(st.limits.max_trade_usd)} por operación y ${st.limits.max_trades_per_day} operaciones por día; la reserva de ${st.limits.min_sol_reserve} SOL nunca se toca.`);
    }
    if (st.price_error) banner("danger", "Sin precio de SOL", st.price_error);
  }

  // ---------------------------------------------------------------- panel
  function renderPanel() {
    const st = S.status;
    if (!st) return;
    const perf = st.performance;
    const sleeve = st.sleeve;

    setHero($("#heroValue"), sleeve ? sleeve.value : null);
    const delta = $("#heroDelta");
    delta.replaceChildren();
    if (perf) {
      delta.append(chip(perf.pnl_pct), el("span", null, `${perf.pnl >= 0 ? "+" : MINUS}${money(Math.abs(perf.pnl))} desde ${fmtDate(st.start?.date)}`));
    } else {
      delta.append(el("span", null, st.mode === "live" ? "Al iniciar, el bot toma de tu wallet hasta " + money(st.limits.budget_usd) + "." : `En papel empieza con ${money(st.limits.paper_start_usd, 0)} ficticios.`));
    }

    // distribución
    const bar = $("#segbar");
    const list = $("#distList");
    bar.replaceChildren();
    list.replaceChildren();
    if (sleeve && sleeve.value > 0) {
      const solValue = sleeve.sol * st.price;
      const parts = [
        { cls: "seg-sol", name: "SOL", color: "var(--violet)", value: solValue, sub: `${solAmt(sleeve.sol)} · ${share(solValue / sleeve.value)}` },
        { cls: "seg-usdc", name: "USDC", color: "var(--aqua)", value: sleeve.usdc, sub: `${usdcAmt(sleeve.usdc)} · ${share(sleeve.usdc / sleeve.value)}` },
      ];
      for (const part of parts) {
        if (part.value / sleeve.value > 0.002) {
          const seg = el("span", part.cls);
          seg.style.flex = String(part.value / sleeve.value);
          bar.append(seg);
        }
        const li = el("li");
        const sw = el("span", "swatch");
        sw.style.background = part.color;
        li.append(sw, el("span", "name", part.name), el("span", "amount", money(part.value)), el("span", "sub", part.sub));
        list.append(li);
      }
      bar.setAttribute("aria-label", `SOL ${share(solValue / sleeve.value)}, USDC ${share(sleeve.usdc / sleeve.value)}`);
    } else {
      bar.append(el("span", "seg-empty"));
      list.append(el("li", "empty", "Todavía no hay fondos asignados."));
    }

    const vs = $("#vsHold");
    vs.replaceChildren();
    if (perf) {
      const diff = perf.vs_hold_pct;
      const verdict = Math.abs(diff) < 0.0005 ? "Por ahora va igual que mantener."
        : `Tu bot va ${diff > 0 ? "por encima" : "por debajo"}: ${pct(diff)}.`;
      vs.append(
        document.createTextNode("Si solo hubieras mantenido SOL: "),
        el("strong", null, money(perf.hold_value)),
        document.createTextNode(` (${pct(perf.hold_pct)}). ${verdict}`),
      );
    } else {
      vs.textContent = "Aquí verás la comparación con solo mantener SOL desde el día en que el bot empezó.";
    }

    renderRecentTrades();
    renderSignal();
  }

  function tradeItem(t) {
    const li = el("li");
    const buy = t.side === "buy";
    li.append(
      el("span", `trade-badge trade-badge--${buy ? "buy" : "sell"}`, buy ? "C" : "V"),
      el("span", "what", `${buy ? "Compró" : "Vendió"} ${solAmt(t.sol)}`),
    );
    const val = el("span", "val", money(t.usdc));
    val.append(el("small", null, `a ${money(t.price)}`));
    li.append(val, el("span", "when", `${fmtDateTime(t.at)}${t.mode === "paper" ? " · simulada" : ""}`));
    return li;
  }

  function renderRecentTrades() {
    const st = S.status;
    const list = $("#recentTrades");
    list.replaceChildren();
    const trades = st.trades.slice(0, 5);
    if (!trades.length) {
      list.append(el("li", "empty", `Todavía no hay operaciones. Con ${st.strategy.name} el bot opera solo cuando cambia la señal, unas pocas veces al mes.`));
      return;
    }
    list.append(...trades.map(tradeItem));
  }

  function untilText(iso) {
    const ms = new Date(iso).getTime() - Date.now();
    if (!Number.isFinite(ms) || ms <= 0) return "ahora";
    const min = Math.round(ms / 60000);
    const h = Math.floor(min / 60);
    const m = min % 60;
    return h ? `en ${h} h ${m} min` : `en ${m} min`;
  }

  function renderSignal() {
    const st = S.status;
    const sig = st.signal;
    $("#countdown").textContent = untilText(st.next_decision_at);
    const stateEl = $("#signalState");
    const why = $("#signalWhy");
    const gauge = $("#gauge");
    gauge.hidden = true;

    if (!sig || sig.action === "error") {
      stateEl.textContent = "Sin señal por ahora";
      why.textContent = sig?.reason || "Esperando datos de precio.";
    } else {
      const target = sig.target;
      stateEl.textContent = target == null ? "Calentando datos"
        : target >= 0.99 ? "Dentro: 100% en SOL"
        : target <= 0.01 ? "Fuera del mercado: en USDC"
        : `${Math.round(target * 100)}% en SOL`;

      const fam = st.strategy.family;
      const ind = sig.indicators || {};
      const close = sig.close;
      if (fam === "trend_sma") {
        const name = Object.keys(ind)[0];
        const avg = ind[name];
        const dist = close / avg - 1;
        why.textContent = `SOL cerró en ${money(close)}, ${Math.abs(dist * 100).toFixed(1)}% ${dist >= 0 ? "por encima" : "por debajo"} de su media de ${st.strategy.params.n} días (${money(avg)}). ` +
          (dist >= 0 ? "Mientras siga encima, el bot se queda en SOL." : "El bot vuelve a comprar cuando cierre por encima.");
        gauge.hidden = false;
        const clamped = Math.max(-0.3, Math.min(0.3, dist));
        $("#gaugeDot").style.left = `${50 + (clamped / 0.3) * 50}%`;
        $("#gaugeLeft").textContent = "Debajo de la media";
        $("#gaugeRight").textContent = "Encima";
      } else if (fam === "sma_cross") {
        const [a, b] = Object.keys(ind);
        const up = ind[a] > ind[b];
        why.textContent = `La media corta (${a}: ${money(ind[a])}) está ${up ? "encima" : "debajo"} de la larga (${b}: ${money(ind[b])}).`;
      } else {
        why.textContent = st.strategy.description;
      }
      if (sig.action === "buy" || sig.action === "sell") {
        why.textContent += ` Pendiente para la próxima ejecución: ${sig.reason}`;
      }
    }

    const notice = $("#signalNotice");
    notice.classList.toggle("is-error", Boolean(st.error));
    notice.textContent = st.error ? `Último error: ${st.error}` : st.notice ? `Último aviso: ${st.notice}` : "";
  }

  // gráfica del panel
  function equityData() {
    const st = S.status;
    if (S.eq === "bot" || (S.eq == null && st && st.marks.length >= 14)) {
      const start = st.start;
      if (!start || st.marks.length < 2) return { mode: "bot", empty: true };
      const bot = st.marks.map(([d, v]) => ({ time: d, value: v }));
      const hold = st.marks.map(([d, , p]) => ({ time: d, value: (start.value_usd / start.price) * p }));
      return { mode: "bot", bot, hold, botName: "Tu bot", caption: `Valor real del bot desde ${fmtDate(start.date)} contra mantener el SOL que tenía al empezar.` };
    }
    const bt = S.backtest;
    if (!bt) return { mode: "backtest", loading: true };
    const sid = bt.active;
    const curve = bt.scenarios.lump.curves[sid];
    const hold = bt.scenarios.lump.curves.buy_hold;
    const name = bt.strategies.find((s) => s.id === sid)?.name || sid;
    return {
      mode: "backtest",
      bot: bt.dates.map((d, i) => ({ time: d, value: curve[i] })),
      hold: bt.dates.map((d, i) => ({ time: d, value: hold[i] })),
      botName: `${name} (tu estrategia)`,
      caption: `Simulación con datos reales desde ${fmtDate(bt.settings.start)}: ${money(bt.settings.lump, 0)} al inicio y ${(bt.settings.fee_rate * 100).toFixed(2)}% de costo por operación. Escala logarítmica.`,
    };
  }

  function renderEquity() {
    if (S.page !== "panel" || !S.status) return;
    const data = equityData();
    for (const b of $$("[data-eq]")) b.setAttribute("aria-pressed", String(b.dataset.eq === data.mode));
    const empty = $("#equityEmpty");
    const p = palette();
    dropChart("equity");
    $("#equityTip").hidden = true;
    if (data.loading || data.empty) {
      empty.hidden = false;
      empty.textContent = data.loading ? "Calculando el backtest con datos reales… (unos segundos)"
        : `Tu bot lleva ${S.status.marks.length} día(s). La línea aparece desde el segundo día; mientras tanto, mira el backtest.`;
      $("#equityCaption").textContent = "";
      legend($("#equityLegend"), []);
      return;
    }
    empty.hidden = true;
    $("#equityCaption").textContent = data.caption;
    const chart = makeChart($("#equityChart"), { log: data.mode === "backtest" });
    const hold = chart.addSeries(LC.LineSeries, seriesOpts(p.hold));
    const bot = chart.addSeries(LC.AreaSeries, areaOpts(p.violet));
    hold.setData(data.hold);
    bot.setData(data.bot);
    chart.timeScale().fitContent();
    legend($("#equityLegend"), [{ color: p.violet, name: data.botName }, { color: p.hold, name: "Mantener SOL" }]);
    attachTooltip(chart, $("#equityChart").parentElement, $("#equityTip"),
      () => [{ series: bot, name: data.mode === "bot" ? "Tu bot" : "Estrategia", color: p.violet }, { series: hold, name: "Mantener SOL", color: p.hold }],
      (v) => money(v));
    charts.equity = { chart };
  }

  // ---------------------------------------------------------------- mercado
  async function loadMarket() {
    const wrap = $("#marketChart");
    wrap.classList.add("is-loading");
    try {
      S.chart = await api(`/api/chart?days=${S.days}`);
      renderMarket();
    } catch (err) {
      toast(`No se pudo cargar el mercado: ${err.message}`, true);
    } finally {
      wrap.classList.remove("is-loading");
    }
  }

  function renderMarket() {
    const data = S.chart;
    if (!data || S.page !== "mercado") return;
    const p = palette();
    const st = S.status;
    $("#marketSub").textContent = `SOL/USDT en velas diarias, con las señales de ${data.strategy}.`;
    for (const b of $$("#rangeButtons button")) b.setAttribute("aria-pressed", String(Number(b.dataset.days) === S.days));

    dropChart("market");
    const chart = makeChart($("#marketChart"), { money: true, interactive: true });
    const candles = chart.addSeries(LC.CandlestickSeries, {
      upColor: p.up, downColor: p.down, wickUpColor: p.up, wickDownColor: p.down, borderVisible: false,
      priceLineVisible: false, lastValueVisible: true,
    });
    candles.setData(data.candles.map(([time, open, high, low, close]) => ({ time, open, high, low, close })));

    const lineColors = [p.violet, p.ink2];
    const lines = Object.entries(data.lines).map(([name, points], i) => {
      const s = chart.addSeries(LC.LineSeries, seriesOpts(lineColors[i % 2], { lastValueVisible: false, crosshairMarkerVisible: false }));
      s.setData(points.map(([time, value]) => ({ time, value })));
      return { name, series: s, color: lineColors[i % 2] };
    });

    const many = data.signals.length > 12; // con muchas señales los textos se enciman: quedan las flechas
    const markers = [
      ...data.signals.map(([time, side, price]) => ({
        time, position: side === "buy" ? "belowBar" : "aboveBar", shape: side === "buy" ? "arrowUp" : "arrowDown",
        color: p.violet, text: many ? "" : side === "buy" ? "Compra" : "Venta",
      })),
      ...data.bot_trades.map(([time, side]) => ({
        time, position: side === "buy" ? "belowBar" : "aboveBar", shape: "circle", color: p.aqua, text: "Bot",
      })),
    ].sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
    LC.createSeriesMarkers(candles, markers);
    chart.timeScale().fitContent();

    legend($("#marketLegend"), [
      { color: p.up, name: "Vela que sube", dot: true },
      { color: p.down, name: "Vela que baja", dot: true },
      ...lines.map((l) => ({ color: l.color, name: l.name })),
      { color: p.violet, name: "Señal de la estrategia", dot: true },
      { color: p.aqua, name: "Operación del bot", dot: true },
    ]);
    attachTooltip(chart, $("#marketChart").parentElement, $("#marketTip"),
      () => [{ series: candles, name: "Cierre", color: p.ink2, format: (v, d) => `${money(d.close)} (${pct(d.close / d.open - 1)})` },
        ...lines.map((l) => ({ series: l.series, name: l.name, color: l.color }))],
      (v) => money(v));
    charts.market = { chart };

    // datos destacados
    const stats = $("#marketStats");
    stats.replaceChildren();
    const last = data.candles[data.candles.length - 1];
    const sol = S.tickers.find((t) => t.symbol === "SOL");
    const tile = (label, value, sub, cls) => {
      const box = el("div", "stat");
      box.append(el("p", "stat__label", label), el("p", "stat__value", value));
      if (sub) box.append(el("p", `stat__sub ${cls || ""}`, sub));
      return box;
    };
    stats.append(tile("Precio ahora", money(st?.price ?? last[4]), sol ? `${pct(sol.change_pct / 100, 2)} en 24 h` : null, sol ? tone(sol.change_pct) : ""));
    const lineNames = Object.keys(data.lines);
    if (lineNames.length) {
      const name = lineNames[lineNames.length - 1];
      const pts = data.lines[name];
      const avg = pts[pts.length - 1][1];
      const dist = (st?.price ?? last[4]) / avg - 1;
      stats.append(tile(name.replace("SMA", "Media"), money(avg), `${pct(dist)} del precio`, tone(dist)));
    }
    const first = data.candles[0];
    stats.append(tile(`Cambio en el rango`, pct(last[4] / first[1] - 1), `desde ${fmtDate(first[0])}`, tone(last[4] / first[1] - 1)));
    stats.append(tile("Señales en el rango", String(data.signals.length), "operaciones que hizo la estrategia"));
  }

  // ---------------------------------------------------------------- backtest
  function loadBacktest(refresh = false) {
    if (S.backtest && !refresh) return Promise.resolve(S.backtest);
    if (S.backtestLoading && !refresh) return S.backtestLoading;
    $("#btChart").classList.add("is-loading");
    S.backtestLoading = (refresh ? api("/api/backtest/refresh", {}) : api("/api/backtest"))
      .then((data) => {
        S.backtest = data;
        if (!S.bt.selected) S.bt.selected = data.active;
        renderEquity();
        return data;
      })
      .catch((err) => { toast(`No se pudo calcular el backtest: ${err.message}`, true); return null; })
      .finally(() => { S.backtestLoading = null; $("#btChart").classList.remove("is-loading"); });
    return S.backtestLoading;
  }

  const nameOf = (id) => S.backtest.strategies.find((s) => s.id === id)?.name || id;

  function renderVerdict() {
    const bt = S.backtest;
    const runs = bt.scenarios.lump.runs;
    const win = bt.selection.winner;
    const w = runs[win].test;
    const h = runs.buy_hold.test;
    const box = $("#verdict");
    box.hidden = false;
    let title;
    if (w.total_return > h.total_return && w.total_return > 0) title = `En datos nuevos, ${nameOf(win)} ganó y superó a mantener SOL.`;
    else if (w.total_return > h.total_return) title = `En datos nuevos, ${nameOf(win)} perdió menos que mantener SOL, pero no ganó.`;
    else title = `En datos nuevos, ${nameOf(win)} no superó a mantener SOL.`;
    const s = bt.settings;
    const splitPrev = new Date(toDate(s.split).getTime() - 86400000).toISOString().slice(0, 10);
    const text = `Se eligió por tener el mejor Sharpe de ${fmtDate(s.start)} a ${fmtDate(splitPrev)}. ` +
      `De ${fmtDate(s.split)} a ${fmtDate(s.end)}: ${nameOf(win)} ${pct(w.total_return)} con caída máxima de ${pct(w.max_drawdown, 0)}; ` +
      `mantener SOL ${pct(h.total_return)} con caída máxima de ${pct(h.max_drawdown, 0)}. ` +
      (bt.active !== win ? `Tu bot usa ${nameOf(bt.active)}. ` : "") +
      "Ninguna estrategia evita las caídas grandes de SOL; las de tendencia las acortan.";
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#i-info");
    svg.append(use);
    box.replaceChildren(svg, el("p", "verdict__title", title), el("p", "verdict__text", text));
  }

  function renderBacktest() {
    const bt = S.backtest;
    if (!bt || S.page !== "backtest") return;
    const s = bt.settings;
    $("#btSub").textContent = `¿Alguna estrategia le gana a solo mantener SOL? Datos reales de ${fmtDate(s.start)} a ${fmtDate(s.end)}, con ${(s.fee_rate * 100).toFixed(2)}% de costo por operación.`;
    for (const b of $$("#btScenario button")) {
      b.setAttribute("aria-pressed", String(b.dataset.scenario === S.bt.scenario));
      b.textContent = b.dataset.scenario === "lump" ? `Capital inicial de ${money(s.lump, 0)}` : `${money(s.weekly, 0)} cada semana`;
    }
    const splitYear = s.split.slice(0, 4);
    for (const b of $$("#btPeriod button")) {
      b.setAttribute("aria-pressed", String(b.dataset.period === S.bt.period));
      if (b.dataset.period === "train") b.textContent = `Entrenamiento (hasta ${Number(splitYear) - 1})`;
      if (b.dataset.period === "test") b.textContent = `Prueba (${splitYear} a hoy)`;
    }
    $("#btLog").checked = S.bt.log;
    renderVerdict();
    renderBtTable();
    renderBtCharts();
    renderYears();
    renderCosts();
  }

  function renderBtTable() {
    const bt = S.backtest;
    const scenario = bt.scenarios[S.bt.scenario];
    const dca = S.bt.scenario === "dca";
    const head = $("#btTable thead tr");
    head.children[2].textContent = dca ? "TIR anual" : "Anual";
    head.children[1].textContent = dca ? "Valor final (aportado)" : "Valor final";
    const body = $("#btTable tbody");
    body.replaceChildren();
    for (const strat of bt.strategies) {
      const m = scenario.runs[strat.id][S.bt.period];
      const tr = el("tr", "is-clickable");
      tr.tabIndex = 0;
      if (strat.id === S.bt.selected) tr.classList.add("is-selected");
      tr.setAttribute("aria-selected", String(strat.id === S.bt.selected));
      const name = el("td");
      name.append(el("span", "strategy-name", strat.name));
      const tags = el("span", "tags");
      if (strat.id === bt.active) tags.append(el("span", "chip chip--violet", "En uso"));
      if (strat.id === bt.selection.winner) tags.append(el("span", "chip chip--flat", "Elegida en entrenamiento"));
      if (tags.childElementCount) name.append(tags);
      const final = el("td", "num", money(m.final, 0));
      if (dca) final.append(el("span", null, ` (${money(m.deposited, 0)})`));
      tr.append(
        name,
        final,
        el("td", `num ${tone(dca ? m.irr : m.cagr)}`, pct(dca ? m.irr : m.cagr)),
        el("td", "num", pct(m.max_drawdown, 0)),
        el("td", "num", f2.format(m.sharpe)),
        el("td", "num", String(m.trades)),
      );
      const pick = () => { S.bt.selected = strat.id; renderBtTable(); renderBtCharts(); };
      tr.addEventListener("click", pick);
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
      body.append(tr);
    }
  }

  function renderBtCharts() {
    const bt = S.backtest;
    const sc = bt.scenarios[S.bt.scenario];
    const sel = S.bt.selected;
    const p = palette();
    const dates = bt.dates;
    const s = bt.settings;
    const dca = S.bt.scenario === "dca";

    $("#btChartTitle").textContent = `${nameOf(sel)} vs. mantener SOL`;
    $("#btChartCaption").textContent = dca
      ? `Aportando ${money(s.weekly, 0)} cada semana desde ${fmtDate(s.start)}. La línea fina es lo aportado.`
      : `Con ${money(s.lump, 0)} invertidos el ${fmtDate(s.start)}.`;

    dropChart("bt");
    dropChart("dd");
    const chart = makeChart($("#btChart"), { log: S.bt.log });
    const hold = chart.addSeries(LC.LineSeries, seriesOpts(p.hold));
    hold.setData(dates.map((d, i) => ({ time: d, value: sc.curves.buy_hold[i] })));
    let deposited = null;
    if (dca) {
      deposited = chart.addSeries(LC.LineSeries, seriesOpts(p.muted, { lineWidth: 1, lastValueVisible: false, crosshairMarkerVisible: false }));
      deposited.setData(dates.map((d, i) => ({ time: d, value: sc.deposited[i] })));
    }
    const main = sel === "buy_hold" ? hold : chart.addSeries(LC.AreaSeries, areaOpts(p.violet));
    if (sel !== "buy_hold") main.setData(dates.map((d, i) => ({ time: d, value: sc.curves[sel][i] })));
    LC.createSeriesMarkers(hold, [{ time: s.split, position: "aboveBar", shape: "arrowDown", color: p.muted, text: "Empieza la prueba" }]);

    const ddChart = makeChart($("#ddChart"), { percent: true });
    const ddHold = ddChart.addSeries(LC.LineSeries, seriesOpts(p.hold));
    ddHold.setData(dates.map((d, i) => ({ time: d, value: sc.drawdowns.buy_hold[i] * 100 })));
    const ddMain = sel === "buy_hold" ? ddHold : ddChart.addSeries(LC.AreaSeries, areaOpts(p.violet, { topColor: alpha(p.violet, 0.0), bottomColor: alpha(p.violet, 0.18), invertFilledArea: false }));
    if (sel !== "buy_hold") ddMain.setData(dates.map((d, i) => ({ time: d, value: sc.drawdowns[sel][i] * 100 })));

    const items = [{ color: p.violet, name: nameOf(sel) }];
    if (sel !== "buy_hold") items.push({ color: p.hold, name: "Comprar y mantener" });
    if (dca) items.push({ color: p.muted, name: "Aportado" });
    legend($("#btLegend"), items);

    const rows = () => [
      { series: main, name: nameOf(sel), color: p.violet },
      ...(sel !== "buy_hold" ? [{ series: hold, name: "Mantener", color: p.hold }] : []),
      ...(deposited ? [{ series: deposited, name: "Aportado", color: p.muted }] : []),
    ];
    attachTooltip(chart, $("#btChart").parentElement, $("#btTip"), rows, (v) => money(v));
    attachTooltip(ddChart, $("#ddChart").parentElement, $("#ddTip"),
      () => [{ series: ddMain, name: nameOf(sel), color: p.violet }, ...(sel !== "buy_hold" ? [{ series: ddHold, name: "Mantener", color: p.hold }] : [])],
      (v) => `${v.toFixed(1)}%`);

    // el periodo elegido acerca la vista; las dos gráficas se mueven juntas
    const splitPrev = new Date(toDate(s.split).getTime() - 86400000).toISOString().slice(0, 10);
    const range = S.bt.period === "train" ? { from: s.start, to: splitPrev } : S.bt.period === "test" ? { from: s.split, to: s.end } : null;
    let syncing = false;
    const sync = (from, to) => (r) => {
      if (syncing || !r) return;
      syncing = true;
      to.timeScale().setVisibleLogicalRange(r);
      syncing = false;
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(sync(chart, ddChart));
    ddChart.timeScale().subscribeVisibleLogicalRangeChange(sync(ddChart, chart));
    if (range) chart.timeScale().setVisibleRange(range);
    else chart.timeScale().fitContent();

    charts.bt = { chart };
    charts.dd = { chart: ddChart };
  }

  function renderYears() {
    const bt = S.backtest;
    const runs = bt.scenarios[S.bt.scenario].runs;
    const years = [...new Set(Object.values(runs).flatMap((r) => Object.keys(r.yearly)))].sort();
    $("#yearsCaption").textContent = "Rendimiento de cada año calendario (sin contar aportes). El año en curso va hasta hoy.";
    const table = $("#yearsTable");
    table.replaceChildren();
    const thead = el("thead");
    const hr = el("tr");
    hr.append(el("th", null, "Estrategia"), ...years.map((y) => el("th", "num", y)));
    thead.append(hr);
    const tbody = el("tbody");
    const p = palette();
    for (const strat of bt.strategies) {
      const tr = el("tr");
      if (strat.id === S.bt.selected) tr.classList.add("is-selected");
      tr.append(el("td", "strategy-name", strat.name));
      for (const y of years) {
        const v = runs[strat.id].yearly[y];
        const td = el("td", "num heat", pct(v, 0));
        if (ok(v)) {
          const strength = Math.min(1, Math.log1p(Math.abs(v) * 3) / Math.log1p(6));
          td.style.background = alpha(v >= 0 ? p.up : p.down, 0.06 + 0.3 * strength);
        }
        tr.append(td);
      }
      tbody.append(tr);
    }
    table.append(thead, tbody);
  }

  function renderCosts() {
    const bt = S.backtest;
    const table = $("#costsTable");
    table.replaceChildren();
    const thead = el("thead");
    const hr = el("tr");
    hr.append(el("th", null, "Estrategia"), ...bt.costs.fees.map((f) => el("th", "num", `${(f * 100).toFixed(2)}%`)));
    thead.append(hr);
    const tbody = el("tbody");
    for (const strat of bt.strategies) {
      const tr = el("tr");
      tr.append(el("td", "strategy-name", strat.name), ...bt.costs.cagr[strat.id].map((v) => el("td", `num ${tone(v)}`, pct(v))));
      tbody.append(tr);
    }
    table.append(thead, tbody);
  }

  // ---------------------------------------------------------------- operaciones y wallet
  function renderOps() {
    const st = S.status;
    if (!st) return;
    const body = $("#opsTable tbody");
    body.replaceChildren();
    $("#opsSub").textContent = st.mode === "live"
      ? "Operaciones reales de tu bot, de la más reciente a la más antigua. Cada una enlaza a su transacción en Solscan."
      : "Operaciones simuladas del modo papel, con precios reales de Jupiter.";
    for (const t of st.trades) {
      const tr = el("tr");
      const buy = t.side === "buy";
      const sideCell = el("td");
      sideCell.append(el("span", `chip chip--${buy ? "up" : "down"}`, buy ? "Compra" : "Venta"));
      const txCell = el("td");
      if (t.signature && t.mode === "live") {
        const a = el("a", "link", `${t.signature.slice(0, 6)}…${t.signature.slice(-4)}`);
        a.href = `https://solscan.io/tx/${encodeURIComponent(t.signature)}`;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        txCell.append(a);
      } else {
        txCell.textContent = "Simulada";
      }
      tr.append(el("td", null, fmtDateTime(t.at)), sideCell, el("td", "num", f4.format(t.sol)), el("td", "num", f2.format(t.usdc)),
        el("td", "num", money(t.price)), el("td", "num", money(t.fee_usd)), el("td", null, t.route || "—"), txCell);
      body.append(tr);
    }
    const empty = $("#opsEmpty");
    empty.hidden = st.trades.length > 0;
    empty.textContent = "Todavía no hay operaciones. El bot opera solo cuando la señal cambia.";
  }

  function kv(node, rows) {
    node.replaceChildren();
    for (const [k, v] of rows) node.append(el("dt", null, k), el("dd", null, v));
  }

  function renderWallet() {
    const st = S.status;
    if (!st) return;
    const w = st.wallet;
    const L = st.limits;
    if (w) {
      $("#walletAddress").textContent = w.address;
      $("#walletExplorer").href = w.explorer;
      kv($("#walletKv"), [
        ["SOL en la wallet", ok(w.sol) ? `${f4.format(w.sol)} SOL` : "—"],
        ["USDC en la wallet", ok(w.usdc) ? `${f2.format(w.usdc)} USDC` : "—"],
        ["Valor aproximado", ok(w.sol) && ok(st.price) ? money(w.sol * st.price + (w.usdc || 0)) : "—"],
        ["Reserva intocable", `${w.reserve} SOL`],
        ["RPC", w.rpc],
        ["Avisos por Telegram", st.telegram ? "Activos" : "Apagados"],
      ]);
      $("#walletFoot").textContent = w.error ? `Aviso: ${w.error}`
        : st.telegram ? "Te llegará un mensaje por cada operación y cada cierre diario."
        : "Para recibir avisos, llena TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en ~/.config/PolloTraderBot/config.env. No hace falta reiniciar.";
    } else {
      $("#walletAddress").textContent = "No se encontró la wallet";
    }
    kv($("#limitsKv"), [
      ["Presupuesto que toma el bot", money(L.budget_usd)],
      ["Máximo por operación", money(L.max_trade_usd)],
      ["Operaciones por día", String(L.max_trades_per_day)],
      ["Reserva de SOL", `${L.min_sol_reserve} SOL`],
      ["Slippage máximo", `${(L.max_slippage_bps / 100).toFixed(2)}%`],
      ["Impacto de precio máximo", `${(L.max_price_impact_bps / 100).toFixed(2)}%`],
      ["Diferencia máxima con Binance", `${(L.max_deviation_bps / 100).toFixed(2)}%`],
    ]);
    const kill = $("#killState");
    kill.replaceChildren(el("span", `chip chip--${st.kill ? "down" : "up"}`, st.kill ? "Activa" : "Inactiva"),
      document.createTextNode(st.kill ? " El bot no opera." : " El bot puede operar."));
  }

  // ---------------------------------------------------------------- acciones
  async function act(path, body, okText) {
    try {
      S.status = await api(path, body);
      renderAll();
      if (okText) toast(okText);
    } catch (err) {
      toast(err.message, true);
    }
  }

  async function toggleRun() {
    const st = S.status;
    if (!st) return;
    if (st.running) return act("/api/bot/pause", {}, "Bot en pausa.");
    if (st.mode !== "live") return act("/api/bot/start", {}, "Bot en marcha.");
    const sig = st.signal;
    const next = sig && (sig.action === "buy" || sig.action === "sell") ? `Al iniciar va a operar de inmediato: ${sig.reason}` : `Al iniciar no opera hasta que cambie la señal (${sig?.reason || "sin señal"}).`;
    const code = await confirmDialog({
      title: "Operar con dinero real",
      text: `${next} Escribe los últimos 4 caracteres de tu wallet (${st.wallet.address.slice(0, 4)}…${st.wallet.address.slice(-4)}) para confirmar.`,
      code: true,
      okLabel: "Iniciar con dinero real",
      live: true,
    });
    if (typeof code === "string") act("/api/bot/start", { confirm: code }, "Bot en marcha con dinero real.");
  }

  async function runNow() {
    const st = S.status;
    if (!st) return;
    if (st.mode === "live") {
      const sig = st.signal;
      const ok2 = await confirmDialog({
        title: "Ejecutar ahora",
        text: sig && (sig.action === "buy" || sig.action === "sell")
          ? `Puede enviar una orden real ahora mismo: ${sig.reason}`
          : "Revisa si cerró una vela nueva. Si la señal no cambió, no opera.",
        okLabel: "Ejecutar",
        live: true,
      });
      if (!ok2) return;
    }
    const btn = $("#runNow");
    btn.disabled = true;
    try {
      S.status = await api("/api/bot/run", {});
      renderAll();
      toast(S.status.notice || "Ciclo ejecutado.");
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  async function resolvePending(executed) {
    const yes = await confirmDialog({
      title: executed ? "La orden SÍ se ejecutó" : "La orden NO se ejecutó",
      text: executed
        ? "Se registrará con los montos de la cotización y el bot no volverá a operar esa señal."
        : "El bot volverá a intentar la misma señal en el próximo ciclo. Hazlo solo si en Solscan no aparece la transacción.",
      okLabel: "Confirmar",
    });
    if (yes) act("/api/bot/resolve", { executed }, "Listo. Pulsa Iniciar para reanudar.");
  }

  // ---------------------------------------------------------------- refresco
  function renderAll() {
    renderFrame();
    if (S.page === "panel") renderPanel();
    if (S.page === "operaciones") renderOps();
    if (S.page === "wallet") renderWallet();
  }

  async function refreshStatus() {
    try {
      const before = S.status?.marks?.length;
      S.status = await api("/api/status");
      renderAll();
      if (S.page === "panel" && before !== S.status.marks.length) renderEquity();
    } catch (err) {
      $(".ticker__status-text").textContent = "Sin conexión con el bot";
    }
  }

  async function refreshTickers() {
    try {
      S.tickers = await api("/api/tickers");
      renderTicker();
    } catch { /* la franja es opcional */ }
  }

  // ---------------------------------------------------------------- arranque
  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    $("#themeToggle").checked = theme === "dark";
    store.set("solbot-theme", theme);
    for (const name of Object.keys(charts)) dropChart(name);
    renderTicker();
    if (S.page === "panel") renderEquity();
    if (S.page === "mercado") renderMarket();
    if (S.page === "backtest") renderBacktest();
  }

  function bind() {
    window.addEventListener("hashchange", route);
    $("#themeToggle").addEventListener("change", (e) => applyTheme(e.target.checked ? "dark" : "light"));
    $("#toggleRun").addEventListener("click", toggleRun);
    $("#runNow").addEventListener("click", runNow);
    $("#openNav").addEventListener("click", () => $("#shell").classList.toggle("nav-open"));
    $("#collapseNav").addEventListener("click", () => {
      const collapsed = $("#shell").classList.toggle("is-collapsed");
      store.set("solbot-nav", collapsed ? "collapsed" : "open");
    });
    for (const b of $$("[data-eq]")) b.addEventListener("click", () => { S.eq = b.dataset.eq; renderEquity(); });
    for (const b of $$("#rangeButtons button")) b.addEventListener("click", () => { S.days = Number(b.dataset.days); loadMarket(); });
    for (const b of $$("#btScenario button")) b.addEventListener("click", () => { S.bt.scenario = b.dataset.scenario; renderBacktest(); });
    for (const b of $$("#btPeriod button")) b.addEventListener("click", () => { S.bt.period = b.dataset.period; renderBacktest(); });
    $("#btLog").addEventListener("change", (e) => { S.bt.log = e.target.checked; renderBtCharts(); });
    $("#btRefresh").addEventListener("click", async () => { await loadBacktest(true); renderBacktest(); toast("Backtest recalculado."); });
    $("#copyAddress").addEventListener("click", async () => {
      try { await navigator.clipboard.writeText($("#walletAddress").textContent); toast("Dirección copiada."); }
      catch { toast("No se pudo copiar; selecciónala a mano.", true); }
    });
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(); });
  }

  async function start() {
    const saved = store.get("solbot-theme");
    if (saved === "light" || saved === "dark") {
      document.documentElement.dataset.theme = saved;
      $("#themeToggle").checked = saved === "dark";
    }
    if (store.get("solbot-nav") === "collapsed") $("#shell").classList.add("is-collapsed");
    if (!LC) toast("No cargó la librería de gráficas.", true);
    bind();
    route(); // la página se dibuja ya; los datos llegan después
    refreshTickers();
    loadBacktest();
    await refreshStatus();
    renderPage();
    setInterval(() => { if (!document.hidden) refreshStatus(); }, 15000);
    setInterval(() => { if (!document.hidden) refreshTickers(); }, 60000);
    setInterval(() => { if (S.status && S.page === "panel") $("#countdown").textContent = untilText(S.status.next_decision_at); }, 30000);
  }

  start();
})();
