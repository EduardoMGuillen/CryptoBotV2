"""Simulador en PAPEL de un bot de memecoins nuevos en Solana. NUNCA firma ni envía órdenes.

Copia las reglas del post "$100 → $4,812" para medir si funcionan con precios reales:
  - par con SOL/USDC, liquidez > 20 000 USD y menos de 6 h de vida;
  - top 10 holders < 35 % y ningún holder > 5 % (aproximación de "dev < 5 %": no sabemos
    quién es el dev), sin mint authority ni freeze authority;
  - 4 % del libro por operación; stop −25 %; vende 1/3 a 2x, la mitad del resto a 3x y el
    resto con trailing de 30 % desde el máximo; sale si la liquidez cae a la mitad o tras 24 h.
No se simula la regla "dev mueve tokens a un exchange" (requiere rastrear la billetera del dev).

Cada compra y venta se simula como una transacción real (`TxSim`):
  - se espera un retraso (lo que tarda en firmarse y llegar al bloque) y se cotiza en ese
    momento con Jupiter, por el monto exacto: precio, impacto y comisión del pool son reales;
  - 15 % de las transacciones fallan (congestión, slippage); igual se paga la comisión de red
    y la venta se reintenta en la siguiente revisión, con el precio que haya entonces;
  - comisión de red + prioridad por intento y 1 % de la plataforma del bot;
  - si Jupiter no encuentra ruta (sacaron la liquidez), tras 3 intentos se da por perdida.
Los precios se revisan cada 5 s con la API de precios de Jupiter (la misma fuente con la que se
ejecuta: combina todos los pools). DexScreener solo da la liquidez para detectar rugs; su precio
es el de UN pool y a veces difiere mucho del real. Un stop de −25 % puede ejecutarse mucho peor: eso es real.

Fuentes: GeckoTerminal (pools nuevos y en tendencia), DexScreener (precio y liquidez),
RPC de Solana (holders y autoridades del mint).
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import random
import re
import tarfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

from .jupiter import SOL_MINT, USDC_MINT, Jupiter, JupiterError
from .market import last_price
from .rpc import resolve_url

log = logging.getLogger("memesim")

QUOTES = {
    "So11111111111111111111111111111111111111112": "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
}
# Autoridades de pools conocidas (dueñas de las bóvedas de tokens): no son holders reales.
POOL_AUTHORITIES = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM
    "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC",  # Meteora DAMM v2
}
GECKO = "https://api.geckoterminal.com/api/v2/networks/solana"
DEX = "https://api.dexscreener.com"
JUP_PRICE = "https://lite-api.jup.ag/price/v3"
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")  # base58: también evita rutas raras en disco


@dataclass(frozen=True)
class Rules:
    min_liq: float = 20_000.0
    max_age_h: float = 6.0
    max_top10: float = 0.35
    max_holder: float = 0.05
    size_frac: float = 0.04
    stop: float = -0.25
    targets: tuple = ((2.0, 1 / 3), (3.0, 1 / 2))  # (múltiplo, fracción de lo que queda)
    trail: float = 0.30
    max_hold_h: float = 24.0
    rug_liq_drop: float = 0.5
    fee: float = 0.01  # plataforma del bot (fomo, axiom, photon… cobran ~1 %)
    network_fee_sol: float = 0.0006  # base + prioridad/propina por transacción, se cobre o falle
    fail_rate: float = 0.15
    latency_s: float = 1.5
    max_no_route: int = 3
    max_open: int = 10
    skip_copycats: bool = False  # no comprar si otra moneda más antigua ya usa el mismo nombre
    min_age_min: float = 0.0  # esperar a que la moneda tenga esta edad (minutos) antes de comprar
    min_buy_ratio: float = 0.0  # momentum: compras/ventas de los últimos 5 min (0 = sin filtro)
    only_pump: bool = False  # solo monedas lanzadas en pump.fun (mint termina en "pump")


# ---- reglas puras ---------------------------------------------------------------------------

def screen(pair: dict, now: float, rules: Rules) -> str | None:
    """None si el par pasa el filtro de mercado; si no, el motivo del rechazo."""
    if pair.get("quoteToken", {}).get("address") not in QUOTES:
        return "par"
    created = pair.get("pairCreatedAt")
    if not created or now - created / 1000 > rules.max_age_h * 3600:
        return "edad"
    if float((pair.get("liquidity") or {}).get("usd") or 0) < rules.min_liq:
        return "liquidez"
    if not float(pair.get("priceUsd") or 0):
        return "precio"
    return None


def concentration(accounts: list[dict], supply: float, pool: str, pool_base: float) -> tuple[float, float]:
    """(fracción del top 10, fracción del mayor holder), sin contar bóvedas de pools.
    `accounts`: [{address, amount, owner}] con montos en unidades del token."""
    by_owner: dict[str, float] = {}
    for acc in accounts:
        owner, amount = acc.get("owner"), float(acc["amount"])
        if owner == pool or owner in POOL_AUTHORITIES:
            continue
        if pool_base and abs(amount - pool_base) <= 0.03 * pool_base:
            continue
        by_owner[owner or acc["address"]] = by_owner.get(owner or acc["address"], 0.0) + amount
    top = sorted(by_owner.values(), reverse=True)[:10]
    if supply <= 0 or not top:
        return 0.0, 0.0
    return sum(top) / supply, top[0] / supply


@dataclass
class Fill:
    ok: bool
    usd: float = 0.0  # compra: USD de tokens recibidos; venta: USD recibidos (netos)
    raw: int = 0  # unidades mínimas del token recibidas (compra)
    fee_usd: float = 0.0  # comisión de red pagada (también si falla)
    note: str = ""  # "fallo" | "sin_ruta" | "error"


class TxSim:
    """Transacción en papel: cotización real de Jupiter tras un retraso, con fallos y comisiones."""

    def __init__(self, quote, sol_price, rules: Rules, rng=random.random, sleep=time.sleep, quote_mint: str = SOL_MINT):
        self.quote, self.sol_price, self.rules, self.rng, self.sleep = quote, sol_price, rules, rng, sleep
        self.quote_mint = quote_mint  # con qué se paga y en qué se cobra (SOL para memecoins, USDC para acciones)

    def _net_fee(self) -> float:
        return self.rules.network_fee_sol * self.sol_price()

    def _swap(self, input_mint: str, output_mint: str, amount: int) -> Fill:
        self.sleep(self.rules.latency_s)  # lo que tarda en llegar al bloque: el precio sigue moviéndose
        fee = self._net_fee()
        if self.rng() < self.rules.fail_rate:
            return Fill(ok=False, fee_usd=fee, note="fallo")
        try:
            order = self.quote(input_mint, output_mint, amount)
        except JupiterError as exc:
            # 4xx: no hay ruta (pool vacío o token muerto). Otro error: no se sabe, se reintenta.
            return Fill(ok=False, fee_usd=0.0, note="sin_ruta" if "rechazó" in str(exc) else "error")
        out = int(order.get("outAmount") or 0)
        if out <= 0 or order.get("outUsdValue") is None:
            return Fill(ok=False, fee_usd=0.0, note="sin_ruta")
        usd = float(order["outUsdValue"]) * (1 - self.rules.fee)
        return Fill(ok=True, usd=usd, raw=out, fee_usd=fee)

    def buy(self, mint: str, usd: float) -> Fill:
        if self.quote_mint == USDC_MINT:
            return self._swap(USDC_MINT, mint, int(usd * 1e6))
        lamports = int(usd / self.sol_price() * 1e9)
        return self._swap(SOL_MINT, mint, lamports)

    def sell(self, mint: str, raw: int) -> Fill:
        fill = self._swap(mint, self.quote_mint, raw)
        if fill.ok:
            fill.usd = max(0.0, fill.usd - fill.fee_usd)
        return fill


@dataclass
class Position:
    token: str
    symbol: str
    pool: str
    entry: float  # precio de mercado al entrar (sin costos): base de los múltiplos
    qty: float
    cost: float
    opened: float
    peak: float
    entry_liq: float
    steps_done: int = 0
    proceeds: float = 0.0
    last: float = 0.0
    decimals: int = -1  # -1: desconocido (se consulta al RPC)
    spike: float = 0.0  # salto >10x sin confirmar (posible precio falso)
    no_route: int = 0
    fees: float = 0.0


def step_position(p: Position, price: float, liq: float, now: float, rules: Rules) -> list[tuple[float, str]]:
    """Ventas a hacer ahora: [(fracción de la cantidad restante, motivo)]."""
    if liq < p.entry_liq * rules.rug_liq_drop:
        return [(1.0, "liquidez")]
    if now - p.opened >= rules.max_hold_h * 3600:
        return [(1.0, "tiempo")]
    mult = price / p.entry
    if p.steps_done == 0 and mult <= 1 + rules.stop:
        return [(1.0, "stop")]
    if p.steps_done > 0 and price <= max(p.peak, price) * (1 - rules.trail):
        return [(1.0, "trailing")]
    if p.steps_done < len(rules.targets):
        target, frac = rules.targets[p.steps_done]
        if mult >= target:
            return [(frac, f"tp{p.steps_done + 1}")]
    return []


@dataclass
class Book:
    cash: float
    positions: dict = field(default_factory=dict)
    closed: list = field(default_factory=list)
    start: float = 0.0

    def equity(self) -> float:
        return self.cash + sum(p.qty * (p.last or p.entry) for p in self.positions.values())

    def capital(self) -> float:
        """Efectivo + lo que costaron las posiciones abiertas. Se usa para el tamaño de cada compra:
        el valor de mercado puede inflarse con precios falsos (pools manipulados)."""
        return self.cash + sum(p.cost for p in self.positions.values())

    def can_open(self, rules: Rules) -> bool:
        return len(self.positions) < rules.max_open

    def open(self, pair: dict, now: float, usd: float, fill: Fill, decimals: int) -> Position:
        """Abre con lo que de verdad llegó: `fill.raw` tokens por `usd` + comisión de red."""
        price = float(pair["priceUsd"])
        token = pair["baseToken"]["address"]
        p = Position(token=token, symbol=pair["baseToken"].get("symbol", "?"), pool=pair["pairAddress"],
                     entry=price, qty=fill.raw / 10 ** decimals, cost=usd + fill.fee_usd, opened=now, peak=price,
                     entry_liq=float(pair["liquidity"]["usd"]), last=price, decimals=decimals, fees=fill.fee_usd)
        self.cash -= usd + fill.fee_usd
        self.positions[token] = p
        return p

    def pay_fee(self, p: Position | None, fee: float) -> None:
        """Transacción fallida: se paga la red igual."""
        self.cash -= fee
        if p is not None:
            p.proceeds -= fee
            p.fees += fee

    def sell(self, p: Position, qty: float, got: float, fee: float, now: float, reason: str) -> None:
        self.cash += got
        p.qty -= qty
        p.proceeds += got
        p.fees += fee
        if reason.startswith("tp"):
            p.steps_done += 1
        if p.qty <= 0 or not reason.startswith("tp"):
            self.close(p, now, reason)

    def close(self, p: Position, now: float, reason: str) -> None:
        del self.positions[p.token]
        self.closed.append({**asdict(p), "closed": now, "reason": reason, "pnl": p.proceeds - p.cost,
                            "mult": p.proceeds / p.cost if p.cost else 0.0})


# ---- simulador en vivo ----------------------------------------------------------------------

WATCH_H = 6.0
VARIANT_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def symbol_key(symbol: str) -> str:
    return "".join(ch for ch in (symbol or "").casefold() if ch.isalnum())


class Desk:
    """Un libro en papel con sus reglas. Varias mesas comparten el mismo escáner y los mismos
    precios, así se comparan variantes de estrategia sobre exactamente las mismas monedas."""

    def __init__(self, name: str, folder: Path, rules: Rules, tx: TxSim, start_cash: float = 100.0):
        self.name, self.folder, self.rules, self.tx = name, folder, rules, tx
        self.folder.mkdir(parents=True, exist_ok=True)
        self.state_path = folder / "state.json"
        self.funnel: dict[str, int] = {"vistos": 0}
        self.book = Book(cash=start_cash, start=time.time())
        self.extra: dict = {}
        if self.state_path.exists():
            st = json.loads(self.state_path.read_text())
            self.book = Book(cash=st["cash"], start=st["start"], closed=st["closed"],
                             positions={k: Position(**v) for k, v in st["positions"].items()})
            self.funnel = st["funnel"]
            self.extra = {k: st[k] for k in ("checked",) if k in st}

    def save(self, **extra) -> None:
        st = {"cash": self.book.cash, "start": self.book.start, "funnel": self.funnel, "closed": self.book.closed,
              "positions": {k: asdict(v) for k, v in self.book.positions.items()}, "variant": self.name,
              "rules": asdict(self.rules), "equity": self.book.equity(), "saved": time.time(), **extra}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(self.state_path)

    def event(self, **kw) -> None:
        with open(self.folder / "events.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), **kw}) + "\n")

    def record_equity(self) -> None:
        with open(self.folder / "equity.jsonl", "a") as f:
            f.write(json.dumps({"t": time.time(), "equity": self.book.equity(), "cash": self.book.cash,
                                "open": len(self.book.positions)}) + "\n")

    def record_price(self, token: str, now: float, price: float, liq: float) -> None:
        """Historial de precio de cada operación (para la gráfica del dashboard)."""
        if not MINT_RE.match(token):
            return
        folder = self.folder / "paths"
        folder.mkdir(exist_ok=True)
        with open(folder / f"{token}.jsonl", "a") as f:
            f.write(json.dumps({"t": round(now, 1), "p": price, "l": round(liq)}) + "\n")

    def count(self, key: str) -> None:
        self.funnel[key] = self.funnel.get(key, 0) + 1

    def _tx_failed(self, fill: Fill) -> None:
        self.count("tx_fallidas" if fill.note == "fallo" else f"tx_{fill.note}")

    def try_buy(self, pair: dict, now: float, decimals, is_copy: bool) -> str | None:
        """None si ya decidió (compró o rechazó); "wait" si la moneda es muy nueva para esta mesa."""
        token = pair["baseToken"]["address"]
        symbol = pair["baseToken"].get("symbol", "?")
        if token in self.book.positions:
            return None
        if self.rules.skip_copycats and is_copy:
            self.count("rechazo_copia")
            return None
        if self.rules.only_pump and not token.endswith("pump"):
            self.count("rechazo_no_pump")
            return None
        age_min = (now - pair["pairCreatedAt"] / 1000) / 60
        if age_min > self.rules.max_age_h * 60:  # el escáner deja pasar hasta la mesa más permisiva
            self.count("rechazo_edad")
            return None
        if age_min < self.rules.min_age_min:
            return "wait"
        if float((pair.get("liquidity") or {}).get("usd") or 0) < self.rules.min_liq:  # se mide cuando ya tiene la edad pedida
            self.count("rechazo_liquidez")
            return None
        if self.rules.min_buy_ratio:
            m5 = (pair.get("txns") or {}).get("m5") or {}
            if (m5.get("buys") or 0) < self.rules.min_buy_ratio * max(1, m5.get("sells") or 0):
                self.count("rechazo_momentum")
                return None
        if not self.book.can_open(self.rules):
            self.count("sin_cupo")
            return None
        usd = min(self.book.capital() * self.rules.size_frac, self.book.cash)
        fill = self.tx.buy(token, usd)
        self.count("tx_intentos")
        if not fill.ok:
            self.book.pay_fee(None, fill.fee_usd)
            self._tx_failed(fill)
            self.count("compra_fallida")
            self.event(kind="fail", side="buy", token=token, symbol=symbol, note=fill.note, fee=fill.fee_usd)
            log.info("[%s] COMPRA FALLIDA %s (%s)", self.name, symbol, fill.note)
            return None
        self.count("tx_ok")
        p = self.book.open(pair, now, usd, fill, decimals(token))
        self.record_price(token, now, p.entry, p.entry_liq)
        self.count("compras")
        self.event(kind="buy", token=token, symbol=p.symbol, price=p.entry, usd=usd, got_usd=fill.usd,
                   fee=fill.fee_usd, liq=p.entry_liq, age_min=(now - pair["pairCreatedAt"] / 1000) / 60)
        log.info("[%s] COMPRA %s %.2f USD (recibe %.2f en tokens) a %.8g (liq %.0f)",
                 self.name, p.symbol, usd, fill.usd, p.entry, p.entry_liq)
        return None

    def manage(self, pairs: dict, now: float, decimals) -> None:
        for token, p in list(self.book.positions.items()):
            pair = pairs.get(token)
            if pair is None:
                continue
            price = float(pair.get("priceUsd") or 0)
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
            if p.last and price > p.last * 10 and not p.spike:
                # Subida de más de 10x en una revisión: suele ser un pool falso. Se espera a que la
                # siguiente revisión la confirme antes de valuar o vender con ese precio.
                p.spike = price
                self.count("precio_sospechoso")
                continue
            p.spike = 0.0
            p.last = price
            self.record_price(token, now, price, liq)
            for frac, reason in step_position(p, price, liq, now, self.rules):
                self.execute_sell(p, frac, reason, price, now, decimals)
            p.peak = max(p.peak, price)

    def execute_sell(self, p: Position, frac: float, reason: str, price: float, now: float, decimals) -> None:
        if p.decimals < 0:
            p.decimals = decimals(p.token)
        qty = p.qty if frac >= 1.0 else p.qty * frac
        raw = int(qty * 10 ** p.decimals)
        fill = self.tx.sell(p.token, raw)
        self.count("tx_intentos")
        if not fill.ok:
            self.book.pay_fee(p, fill.fee_usd)
            self._tx_failed(fill)
            self.event(kind="fail", side="sell", token=p.token, symbol=p.symbol, note=fill.note, reason=reason,
                       fee=fill.fee_usd, mult=price / p.entry)
            log.info("[%s] VENTA FALLIDA %s (%s, %s): se reintenta", self.name, p.symbol, reason, fill.note)
            if fill.note == "sin_ruta":
                p.no_route += 1
                if p.no_route >= self.rules.max_no_route:  # nadie te compra: se pierde lo que queda
                    self.book.close(p, now, "sin_ruta")
                    self.event(kind="sell", token=p.token, symbol=p.symbol, price=price, frac=1.0, usd=0.0,
                               reason="sin_ruta", mult=0.0)
            return
        self.count("tx_ok")
        self.book.sell(p, qty, fill.usd, fill.fee_usd, now, reason)
        # real_mult: lo que de verdad se obtuvo por token frente a la entrada (impacto y comisiones)
        real_mult = (fill.usd / qty) / p.entry if qty else 0.0
        self.event(kind="sell", token=p.token, symbol=p.symbol, price=price, frac=frac, usd=fill.usd,
                   fee=fill.fee_usd, reason=reason, mult=price / p.entry, real_mult=real_mult)
        log.info("[%s] VENDE %s %.0f%% (%s): pantalla x%.2f, ejecutado x%.2f → %.2f USD",
                 self.name, p.symbol, frac * 100, reason, price / p.entry, real_mult, fill.usd)


# Variantes que corren en paralelo sobre las mismas monedas. La primera vive en la carpeta raíz.
# Cada cambio se registra en data/meme/journal.md. "post" (las reglas del post de X) quedó
# archivada el 2026-11-07 a las 09:55 tras −77 % en 68 operaciones (data/meme/archive/).
ROOT_VARIANT = "principal"
_QUICK = dict(skip_copycats=True)
VARIANTS = {
    # v2 (2026-11-07 16:45, reinicio a $100 por pedido del usuario; la v1 quedó en
    # data/meme/archive/principal_v1_fallida con −82 % en 53 operaciones).
    # "Sobrevivientes": de 442 operaciones, las monedas con <15 min de vida sufrieron rug el 65 %
    # de las veces; las de ≥2 h, 1 de 50 (muestra chica: ~8 monedas distintas). Solo compra monedas
    # con 2–24 h de vida y liquidez ≥ 30 000, toma ganancias pronto y corta pérdidas cortas.
    "principal": Rules(skip_copycats=True, min_age_min=120.0, max_age_h=24.0, min_liq=30_000.0,
                       targets=((1.3, 0.5),), trail=0.20, stop=-0.15, rug_liq_drop=0.8, max_hold_h=6.0,
                       size_frac=0.05, max_open=8),
    "sin_copias_30m": Rules(skip_copycats=True, min_age_min=30.0),
    # 11:55: retiradas por fallidas con ≥30 cerradas (datos en data/meme/archive/): sin_copias (−55 %),
    # todo_1_2x (−31 %), todo_1_5x (−30 %), alerta_rug (−51 %), mitad_1_3x (−53 %), loteria_3x (−63 %),
    # pocas_grandes (−84 %, 29 cerradas). 12:05: momentum (−48 %, 31 cerradas).
    # 2026-11-07 10:15: de 98 operaciones cerradas, las monedas de pump.fun (mint "...pump") sufrieron
    # rug 1 de 16 veces; las demás 59 de 82.
    "solo_pump": Rules(**_QUICK, only_pump=True, min_age_min=5.0, targets=((1.5, 0.5),), trail=0.25, stop=-0.20,
                       rug_liq_drop=0.8, max_hold_h=6.0, size_frac=0.06, max_open=8),
    "solo_pump_1_5x": Rules(**_QUICK, only_pump=True, targets=((1.5, 1.0),), stop=-0.20, rug_liq_drop=0.8, max_hold_h=3.0),
    # 11:55: combinan las dos señales que menos pierden (pump.fun y esperar edad / momentum).
    "solo_pump_30m": Rules(**_QUICK, only_pump=True, min_age_min=30.0, targets=((1.5, 0.5),), trail=0.25, stop=-0.20,
                           rug_liq_drop=0.8, max_hold_h=6.0, size_frac=0.06, max_open=8),
    "solo_pump_momentum": Rules(**_QUICK, only_pump=True, min_buy_ratio=1.5, targets=((1.5, 1.0),), stop=-0.20,
                                rug_liq_drop=0.8, max_hold_h=3.0),
    # 16:45: sobrevivientes con más liquidez (≥ 60 000) y compradores dominando los últimos 5 min.
    "sobrevivientes_momentum": Rules(skip_copycats=True, min_age_min=120.0, max_age_h=24.0, min_liq=60_000.0,
                                     min_buy_ratio=1.2, targets=((1.5, 1.0),), stop=-0.15, rug_liq_drop=0.8,
                                     max_hold_h=6.0, size_frac=0.05, max_open=8),
}
SCAN_RULES = Rules(max_age_h=max(r.max_age_h for r in VARIANTS.values()),
                   min_liq=min(r.min_liq for r in VARIANTS.values()))


class Sim:
    def __init__(self, folder: Path, rules: Rules, rpc_url: str, start_cash: float = 100.0,
                 variants: dict[str, Rules] | None = None):
        self.folder, self.rules, self.rpc_url = folder, rules, rpc_url
        self.http = requests.Session()
        self.http.headers["accept"] = "application/json"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.watch: dict[str, float] = {}  # token -> primera vez visto
        self.decimals: dict[str, int] = {}
        self.symbols: dict[str, tuple[str, float]] = {}  # símbolo -> (mint más antiguo, creado ms)
        self._sol = (0.0, 0.0)  # (precio, cuándo)
        self.jupiter = Jupiter(session=self.http)
        self._quotes: dict[tuple[str, str], tuple[float, int, object]] = {}
        variants = variants or {ROOT_VARIANT: rules}
        self.desks: list[Desk] = []
        for name, vrules in variants.items():
            vfolder = folder if not self.desks else folder / "variants" / name
            self.desks.append(Desk(name, vfolder, vrules, TxSim(self.quote, self.sol_price, vrules), start_cash))
        self.checked: dict[str, float] = self.desks[0].extra.get("checked", {})
        self.waiting: dict[str, set[str]] = {}  # token -> mesas que esperan a que tenga más edad
        self._last_manage = 0.0

    # compatibilidad: la mesa principal
    @property
    def book(self) -> Book:
        return self.desks[0].book

    @property
    def funnel(self) -> dict:
        return self.desks[0].funnel

    @property
    def tx(self) -> TxSim:
        return self.desks[0].tx

    def execute_sell(self, p, frac, reason, price, now) -> None:
        self.desks[0].execute_sell(p, frac, reason, price, now, self.token_decimals)

    def count(self, key: str) -> None:
        for d in self.desks:  # lo que decide el escáner cuenta igual para todas las mesas
            d.count(key)

    def save(self) -> None:
        for i, d in enumerate(self.desks):
            d.save(**({"checked": self.checked} if i == 0 else {}))

    def record_equity(self) -> None:
        for d in self.desks:
            d.record_equity()

    # -- datos
    def _get(self, url: str, **params):
        for attempt in range(3):
            r = self.http.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def quote(self, input_mint: str, output_mint: str, amount: int, max_age: float = 3.0) -> dict:
        """Cotización de Jupiter compartida entre mesas: si otra mesa cotizó el mismo par hace menos de
        `max_age` s, se reutiliza escalada al monto (con $2–15 en pools de >$20k el impacto es casi
        lineal). Evita pasar el límite de Jupiter con muchas variantes."""
        key = (input_mint, output_mint)
        cached = self._quotes.get(key)
        if cached and time.time() - cached[0] < max_age:
            _, base, result = cached
        else:
            try:
                result = self.jupiter.order(input_mint, output_mint, amount)
            except JupiterError as exc:
                result = exc
            base = amount
            self._quotes[key] = (time.time(), base, result)
        if isinstance(result, Exception):
            raise result
        k = amount / base if base else 0.0
        out = dict(result)
        out["outAmount"] = str(int(int(result.get("outAmount") or 0) * k))
        if result.get("outUsdValue") is not None:
            out["outUsdValue"] = float(result["outUsdValue"]) * k
        return out

    def sol_price(self) -> float:
        price, at = self._sol
        if time.time() - at > 60:
            try:
                price = last_price("SOLUSDT", session=self.http)
                self._sol = (price, time.time())
            except Exception as exc:
                log.warning("precio de SOL: %s", exc)
                if not price:
                    raise
        return price

    def token_decimals(self, token: str) -> int:
        if token not in self.decimals:
            self.decimals[token] = int(self._rpc("getTokenSupply", [token])["value"]["decimals"])
        return self.decimals[token]

    def _rpc(self, method: str, params: list):
        r = self.http.post(self.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
        r.raise_for_status()
        body = r.json()
        if "error" in body:
            raise RuntimeError(f"{method}: {body['error'].get('message')}")
        return body["result"]

    def held(self) -> set[str]:
        return {t for d in self.desks for t in d.book.positions}

    def discover(self) -> None:
        now = time.time()
        sources = [(f"{GECKO}/new_pools", {"page": p, "include": "base_token"}) for p in (1, 2)]
        sources += [(f"{GECKO}/trending_pools", {"duration": d, "include": "base_token"}) for d in ("5m", "1h")]
        held = self.held()
        for url, params in sources:
            try:
                data = self._get(url, **params)
            except Exception as exc:
                log.warning("GeckoTerminal: %s", exc)
                continue
            for pool in data.get("data", []):
                token = pool["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
                if token in QUOTES or token in self.watch or token in held:
                    continue
                self.watch[token] = now
                self.count("vistos")
            time.sleep(2.5)  # GeckoTerminal: 30 llamadas/min
        # Se vigila cada moneda hasta WATCH_H horas después de descubrirla (las de tendencia
        # pueden llegar ya viejas: su edad la filtra cada mesa).
        limit = now - WATCH_H * 3600
        self.watch = {t: s for t, s in self.watch.items() if s > limit}
        self.checked = {t: s for t, s in self.checked.items() if s > limit}
        self.waiting = {t: names for t, names in self.waiting.items() if t in self.watch}

    def best_pairs(self, tokens: list[str], prefer: dict[str, str] | None = None) -> dict[str, dict]:
        """Par de cada token (DexScreener, 30 tokens por llamada): el pool indicado en `prefer` (donde
        se compró) si existe; si no, el más líquido contra SOL/USDC. Los pares contra otros tokens se
        ignoran: así se crean pools falsos con liquidez y precio inventados."""
        prefer = prefer or {}
        out: dict[str, dict] = {}
        for i in range(0, len(tokens), 30):
            try:
                pairs = self._get(f"{DEX}/tokens/v1/solana/" + ",".join(tokens[i:i + 30]))
            except Exception as exc:
                log.warning("DexScreener: %s", exc)
                continue
            for pair in pairs or []:
                tok = pair["baseToken"]["address"]
                if (pair.get("quoteToken") or {}).get("address") not in QUOTES:
                    continue
                if prefer.get(tok) and out.get(tok, {}).get("pairAddress") == prefer[tok]:
                    continue  # ya tenemos el pool de la compra
                if prefer.get(tok) == pair.get("pairAddress"):
                    out[tok] = pair
                    continue
                liq = float((pair.get("liquidity") or {}).get("usd") or 0)
                if tok not in out or liq > float((out[tok].get("liquidity") or {}).get("usd") or 0):
                    out[tok] = pair
        for pair in out.values():
            self.note_symbol(pair)
        return out

    def jup_prices(self, tokens: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        for i in range(0, len(tokens), 50):
            try:
                data = self._get(JUP_PRICE, ids=",".join(tokens[i:i + 50]))
            except Exception as exc:
                log.warning("Precios de Jupiter: %s", exc)
                continue
            for tok, info in (data or {}).items():
                price = (info or {}).get("usdPrice")
                if price:
                    out[tok] = float(price)
                if (info or {}).get("decimals") is not None:
                    self.decimals.setdefault(tok, int(info["decimals"]))
        return out

    def with_jup_price(self, pairs: dict[str, dict]) -> dict[str, dict]:
        """Mismo par de DexScreener (liquidez), pero con el precio de Jupiter (el ejecutable)."""
        prices = self.jup_prices(sorted(pairs))
        return {t: {**pair, "priceUsd": str(prices[t]), "dexPriceUsd": pair.get("priceUsd")} if t in prices else pair
                for t, pair in pairs.items()}

    def note_symbol(self, pair: dict) -> None:
        key = symbol_key(pair["baseToken"].get("symbol", ""))
        created = pair.get("pairCreatedAt") or float("inf")
        if key and (key not in self.symbols or created < self.symbols[key][1]):
            self.symbols[key] = (pair["baseToken"]["address"], created)

    def is_copy(self, pair: dict) -> bool:
        """Otra moneda más antigua ya usa este nombre: típico de copias que terminan en rug."""
        first = self.symbols.get(symbol_key(pair["baseToken"].get("symbol", "")))
        return bool(first) and first[0] != pair["baseToken"]["address"]

    def holders_ok(self, pair: dict) -> str | None:
        token = pair["baseToken"]["address"]
        mint = self._rpc("getAccountInfo", [token, {"encoding": "jsonParsed"}])
        info = ((mint or {}).get("value") or {}).get("data", {}).get("parsed", {}).get("info", {})
        if not info:
            return "mint_ilegible"
        if info.get("mintAuthority"):
            return "mint_authority"
        if info.get("freezeAuthority"):
            return "freeze_authority"
        supply_info = self._rpc("getTokenSupply", [token])["value"]
        supply = float(supply_info["uiAmount"] or 0)
        self.decimals[token] = int(supply_info["decimals"])
        largest = self._rpc("getTokenLargestAccounts", [token])["value"][:20]
        owners = self._rpc("getMultipleAccounts", [[a["address"] for a in largest], {"encoding": "jsonParsed"}])["value"]
        accounts = []
        for a, acc in zip(largest, owners):
            owner = (((acc or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("owner")
            accounts.append({"address": a["address"], "amount": float(a["uiAmount"] or 0), "owner": owner})
        pool_base = float((pair.get("liquidity") or {}).get("base") or 0)
        top10, biggest = concentration(accounts, supply, pair["pairAddress"], pool_base)
        if top10 > self.rules.max_top10:
            return "top10"
        if biggest > self.rules.max_holder:
            return "holder_grande"
        return None

    def evaluate(self) -> None:
        now = time.time()
        held = self.held()
        tokens = [t for t in self.watch if t not in self.checked and t not in held]
        due = [t for t in self.waiting if t not in tokens]
        pairs = self.best_pairs(tokens + due)
        for token in tokens:  # los que siguen muertos tras 30 min no se vuelven a consultar
            pair = pairs.get(token)
            dead = pair is None or float((pair.get("liquidity") or {}).get("usd") or 0) < self.rules.min_liq / 4
            if dead and now - self.watch[token] > 1800:
                del self.watch[token]
                self.count("descartados_sin_liquidez")
        for token, pair in pairs.items():
            # Revisar holders y cotizar compras tarda; un bot real sigue vigilando sus posiciones.
            if time.time() - self._last_manage >= 5:
                self.manage()
            retry = token in self.waiting  # ya pasó los filtros antes; una mesa espera su edad mínima
            desks = [d for d in self.desks if d.name in self.waiting.get(token, ())] if retry else self.desks
            count = (lambda key: [d.count(key) for d in desks]) if retry else self.count
            reason = screen(pair, now, self.rules)
            if reason == "liquidez" or reason == "precio":
                if retry:  # mientras esperaba se fue la liquidez: se descarta
                    del self.waiting[token]
                    count(f"rechazo_{reason}")
                continue  # si no, puede crecer: se vuelve a mirar en la siguiente vuelta
            if reason:
                self.checked[token] = now
                self.waiting.pop(token, None)
                count(f"rechazo_{reason}")
                continue
            try:
                reason = self.holders_ok(pair)  # al reintentar se revisa de nuevo: los holders cambian
            except Exception as exc:
                log.warning("RPC %s: %s", token, exc)
                continue
            self.checked[token] = now
            if reason:
                self.waiting.pop(token, None)
                count(f"rechazo_{reason}")
                continue
            copy = self.is_copy(pair)
            pair = self.with_jup_price({token: pair})[token]
            still = set()
            for desk in desks:
                if desk.try_buy(pair, now, self.token_decimals, copy) == "wait":
                    still.add(desk.name)
            if still:
                self.waiting[token] = still
            else:
                self.waiting.pop(token, None)

    def manage(self) -> None:
        self._last_manage = time.time()
        held = self.held()
        if not held:
            return
        now = time.time()
        prefer = {t: p.pool for d in self.desks for t, p in d.book.positions.items()}
        pairs = self.with_jup_price(self.best_pairs(sorted(held), prefer))
        for desk in self.desks:
            desk.manage(pairs, now, self.token_decimals)

    def run(self) -> None:
        log.info("memesim en papel: %s", ", ".join(f"{d.name} {d.book.equity():.2f} USD" for d in self.desks))
        last_discover = 0.0
        # Desde el último respaldo en disco: reiniciar el simulador no debe saltarse respaldos.
        previous = sorted((self.folder.parent / "backups").glob("meme-*.tar.gz"))
        last_backup = previous[-1].stat().st_mtime if previous else 0.0
        while True:
            try:
                if time.time() - last_backup >= 3600:
                    last_backup = time.time()
                    log.info("respaldo: %s", backup(self.folder, self.folder.parent / "backups"))
                discovered = time.time() - last_discover >= 60
                if discovered:
                    self.discover()
                    self.evaluate()
                    last_discover = time.time()
                self.manage()
                if discovered:
                    self.record_equity()
                self.save()
            except Exception as exc:
                log.exception("vuelta fallida: %s", exc)
            time.sleep(5)


def backup(folder: Path, dest: Path, keep: int = 48, now: datetime.datetime | None = None) -> Path:
    """Copia comprimida de toda la carpeta del simulador; conserva las `keep` más recientes."""
    dest.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M")
    target = dest / f"meme-{stamp}.tar.gz"
    tmp = target.with_suffix(".tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(folder, arcname=folder.name, filter=lambda ti: None if ti.name.endswith(".tmp") else ti)
    tmp.replace(target)
    for old in sorted(dest.glob("meme-*.tar.gz"))[:-keep]:
        old.unlink()
    return target


def _tail_jsonl(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # línea a medio escribir
    return out


def snapshot(folder: Path, start_cash: float = 100.0) -> dict:
    """Todo lo que necesita el dashboard, desde los archivos del simulador (solo lectura)."""
    path = folder / "state.json"
    if not path.exists():
        return {"ready": False}
    s = json.loads(path.read_text())
    closed = s["closed"]
    wins = [c for c in closed if c["pnl"] > 0]
    funnel = s["funnel"]
    rejected = sum(v for k, v in funnel.items() if k.startswith(("rechazo_", "descartados_")))
    open_positions = []
    for p in s["positions"].values():
        last = p.get("last") or p["entry"]
        value = p["qty"] * last
        open_positions.append({"symbol": p["symbol"], "token": p["token"], "cost": p["cost"], "value": value,
                               "mult": last / p["entry"], "pnl": value + p["proceeds"] - p["cost"],
                               "opened": p["opened"], "steps": p["steps_done"], "peak_mult": p["peak"] / p["entry"],
                               "liq": p["entry_liq"], "proceeds": p["proceeds"]})
    reasons: dict[str, int] = {}
    for c in closed:
        reasons[c["reason"]] = reasons.get(c["reason"], 0) + 1
    worst_stop = min((c["mult"] for c in closed if c["reason"] == "stop"), default=None)
    curve = [{"t": e["t"], "equity": e["equity"]} for e in _tail_jsonl(folder / "equity.jsonl", 20_000)]
    return {
        "ready": True,
        "start_cash": start_cash,
        "equity": s["equity"],
        "cash": s["cash"],
        "started": s["start"],
        "updated": s["saved"],
        "funnel": funnel,
        "seen": funnel.get("vistos", 0),
        "bought": funnel.get("compras", 0),
        "rejected": rejected,
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "realized": sum(c["pnl"] for c in closed),
        "best": max(closed, key=lambda c: c["pnl"], default=None),
        "worst": min(closed, key=lambda c: c["pnl"], default=None),
        "worst_stop": worst_stop,
        "exit_reasons": reasons,
        "open": sorted(open_positions, key=lambda p: -p["opened"]),
        "closed": [{k: c[k] for k in ("symbol", "token", "reason", "pnl", "mult", "cost", "opened", "closed")}
                   for c in reversed(closed[-100:])],
        "events": list(reversed(_tail_jsonl(folder / "events.jsonl", 60))),
        "tx": {k[3:]: v for k, v in funnel.items() if k.startswith("tx_")},
        "network_fees": sum(c.get("fees", 0.0) for c in closed) + sum(p.get("fees", 0.0) for p in s["positions"].values()),
        "curve": curve,
    }


def variant_folder(root: Path, name: str | None) -> Path:
    """Carpeta de una variante (la principal o vacío: la raíz). Valida el nombre: viene de la URL."""
    if not name or name == ROOT_VARIANT:
        return root
    if not VARIANT_RE.match(name):
        raise ValueError("Variante inválida.")
    return root / "variants" / name


def variants_summary(root: Path) -> list[dict]:
    """Resumen de cada variante, desde su propio inicio (cada una arrancó en su momento)."""
    names = [ROOT_VARIANT] + sorted(p.name for p in (root / "variants").glob("*") if p.is_dir() and VARIANT_RE.match(p.name))
    out = []
    for name in names:
        path = variant_folder(root, name) / "state.json"
        if not path.exists():
            continue
        st = json.loads(path.read_text())
        closed = st["closed"]
        out.append({"name": name, "equity": st["equity"], "start": st["start"], "closed": len(closed),
                    "wins": sum(1 for c in closed if c["pnl"] > 0), "open": len(st["positions"]),
                    "realized": sum(c["pnl"] for c in closed),
                    "rules": st.get("rules") or {}})
    return out


RUG_REASONS = ("liquidez", "sin_ruta")


def _desk_names(root: Path) -> list[str]:
    return [ROOT_VARIANT] + sorted(p.name for p in (root / "variants").glob("*") if p.is_dir() and VARIANT_RE.match(p.name))


def feed(root: Path, limit: int = 80, since: float | None = None) -> dict:
    """Lo que pasa en todas las mesas a la vez (pantalla "En vivo"): últimos movimientos, posiciones
    abiertas y el resumen de hoy (desde `since`, por defecto la medianoche local)."""
    if since is None:
        since = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    events, opened, today = [], [], {"wins": 0, "losses": 0, "rugs": 0, "pnl": 0.0}
    for name in _desk_names(root):
        folder = variant_folder(root, name)
        path = folder / "state.json"
        if not path.exists():
            continue
        st = json.loads(path.read_text())
        closed = st.get("closed") or []
        for c in closed:
            if c.get("closed", 0) >= since:
                today["pnl"] += c["pnl"]
                today["wins" if c["pnl"] > 0 else "losses"] += 1
                today["rugs"] += c["reason"] in RUG_REASONS
        for e in _tail_jsonl(folder / "events.jsonl", limit):
            e = {**e, "desk": name}
            if e.get("kind") == "fail":
                e["tone"] = "fail"
            elif e.get("kind") == "buy":
                e["tone"] = "buy"
            else:
                # El cierre se anota al empezar la vuelta y el evento tras la transacción (latencia,
                # reintentos): se toma el cierre más cercano del mismo token, hasta 2 min.
                near = [c for c in closed if c["token"] == e.get("token") and abs(c.get("closed", 0) - e["t"]) < 120]
                match = min(near, key=lambda c: abs(c["closed"] - e["t"])) if near else None
                if match is not None:
                    e["pnl"] = match["pnl"]
                reason = e.get("reason", "")
                if reason in RUG_REASONS:
                    e["tone"] = "rug"
                elif reason.startswith("tp") or (match is not None and match["pnl"] > 0):
                    e["tone"] = "win"
                else:
                    e["tone"] = "loss"
            events.append(e)
        rules = st.get("rules") or {}
        targets = rules.get("targets") or []
        if name.startswith("acc_"):  # acciones: salen por señal diaria, no por stop ni meta
            rules, targets = {}, []
        for pos in (st.get("positions") or {}).values():
            last = pos.get("last") or pos["entry"]
            step = pos.get("steps_done", 0)
            opened.append({"desk": name, "symbol": pos["symbol"], "token": pos["token"], "opened": pos["opened"],
                           "cost": pos["cost"], "value": pos["qty"] * last, "mult": last / pos["entry"],
                           "pnl": pos["qty"] * last + pos.get("proceeds", 0.0) - pos["cost"],
                           "stop": 1 + rules["stop"] if "stop" in rules and step == 0 else None,
                           "target": targets[step][0] if step < len(targets) else None})
    events.sort(key=lambda e: -e["t"])
    opened.sort(key=lambda o: -o["opened"])
    return {"events": events[:limit], "open": opened, "today": today}


def trade_detail(folder: Path, token: str, max_points: int = 600) -> dict:
    """Historial de precio y registro de una operación (abierta o cerrada)."""
    if not MINT_RE.match(token or ""):
        raise ValueError("Token inválido.")
    state = json.loads((folder / "state.json").read_text()) if (folder / "state.json").exists() else {}
    record = (state.get("positions") or {}).get(token)
    status_ = "open" if record else None
    if record is None:
        record = next((c for c in reversed(state.get("closed") or []) if c["token"] == token), None)
        status_ = "closed" if record else None
    if record is None:
        raise ValueError("No hay operación con ese token.")
    path = _tail_jsonl(folder / "paths" / f"{token}.jsonl", 100_000)
    if len(path) > max_points:  # se reduce para la gráfica, conservando el último punto
        step = len(path) / max_points
        path = [path[int(i * step)] for i in range(max_points - 1)] + [path[-1]]
    events = [e for e in _tail_jsonl(folder / "events.jsonl", 20_000) if e.get("token") == token]
    keep = ("symbol", "token", "pool", "entry", "qty", "cost", "opened", "peak", "entry_liq", "steps_done",
            "proceeds", "last", "closed", "reason", "pnl", "mult", "fees")
    return {"status": status_, "trade": {k: record[k] for k in keep if k in record}, "path": path, "events": events}


def status(folder: Path) -> str:
    path = folder / "state.json"
    if not path.exists():
        return "memesim todavía no tiene estado."
    s = json.loads(path.read_text())
    closed = s["closed"]
    wins = sum(1 for c in closed if c["pnl"] > 0)
    days = (s["saved"] - s["start"]) / 86400
    lines = [f"Libro: {s['equity']:.2f} USD (inicio 100.00) — {days:.1f} días",
             f"Cerradas: {len(closed)} ({wins} ganadas, {len(closed) - wins} perdidas); abiertas: {len(s['positions'])}",
             "Embudo: " + ", ".join(f"{k} {v}" for k, v in sorted(s["funnel"].items()))]
    for c in closed[-10:]:
        lines.append(f"  {c['symbol']:<10} {c['reason']:<9} x{c['mult']:.2f}  pnl {c['pnl']:+.2f}")
    return "\n".join(lines)


def main() -> None:
    from .rpc import read_env_file  # noqa: F401  (resolve_url lo usa)
    import os

    ap = argparse.ArgumentParser(description="Simulador en papel de memecoins (no opera dinero real).")
    ap.add_argument("cmd", choices=["run", "status"])
    ap.add_argument("--dir", default="data/meme")
    ap.add_argument("--env-file", default="~/.config/PolloTraderBot/config.env")
    args = ap.parse_args()
    folder = Path(args.dir)
    if args.cmd == "status":
        print(status(folder))
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    Sim(folder, SCAN_RULES, resolve_url(dict(os.environ), args.env_file), variants=VARIANTS).run()


if __name__ == "__main__":
    main()
