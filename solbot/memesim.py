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
Los precios se revisan cada 5 s. Un stop de −25 % puede ejecutarse mucho peor: eso es real.

Fuentes: GeckoTerminal (pools nuevos y en tendencia), DexScreener (precio y liquidez),
RPC de Solana (holders y autoridades del mint).
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

from .jupiter import SOL_MINT, Jupiter, JupiterError
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

    def __init__(self, quote, sol_price, rules: Rules, rng=random.random, sleep=time.sleep):
        self.quote, self.sol_price, self.rules, self.rng, self.sleep = quote, sol_price, rules, rng, sleep

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
        lamports = int(usd / self.sol_price() * 1e9)
        return self._swap(SOL_MINT, mint, lamports)

    def sell(self, mint: str, raw: int) -> Fill:
        fill = self._swap(mint, SOL_MINT, raw)
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

class Sim:
    def __init__(self, folder: Path, rules: Rules, rpc_url: str, start_cash: float = 100.0):
        self.folder, self.rules, self.rpc_url = folder, rules, rpc_url
        self.http = requests.Session()
        self.http.headers["accept"] = "application/json"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.state_path = folder / "state.json"
        self.watch: dict[str, float] = {}  # token -> primera vez visto
        self.checked: dict[str, float] = {}  # token -> cuándo se revisaron holders (rechazado)
        self.funnel: dict[str, int] = {"vistos": 0}
        self.book = Book(cash=start_cash, start=time.time())
        self.decimals: dict[str, int] = {}
        self._sol = (0.0, 0.0)  # (precio, cuándo)
        self.jupiter = Jupiter(session=self.http)
        self.tx = TxSim(self.jupiter.order, self.sol_price, rules)
        self._load()

    # -- persistencia
    def _load(self) -> None:
        if not self.state_path.exists():
            return
        s = json.loads(self.state_path.read_text())
        self.book = Book(cash=s["cash"], start=s["start"], closed=s["closed"],
                         positions={k: Position(**v) for k, v in s["positions"].items()})
        self.funnel = s["funnel"]
        self.checked = s.get("checked", {})

    def save(self) -> None:
        s = {"cash": self.book.cash, "start": self.book.start, "funnel": self.funnel, "closed": self.book.closed,
             "positions": {k: asdict(v) for k, v in self.book.positions.items()},
             "equity": self.book.equity(), "saved": time.time(), "checked": self.checked}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s))
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

    def discover(self) -> None:
        now = time.time()
        sources = [(f"{GECKO}/new_pools", {"page": p, "include": "base_token"}) for p in (1, 2)]
        sources += [(f"{GECKO}/trending_pools", {"duration": d, "include": "base_token"}) for d in ("5m", "1h")]
        for url, params in sources:
            try:
                data = self._get(url, **params)
            except Exception as exc:
                log.warning("GeckoTerminal: %s", exc)
                continue
            for pool in data.get("data", []):
                created = pool["attributes"].get("pool_created_at") or ""
                token = pool["relationships"]["base_token"]["data"]["id"].split("_", 1)[1]
                if token in QUOTES or token in self.watch or token in self.book.positions:
                    continue
                self.watch[token] = now
                self.count("vistos")
            time.sleep(2.5)  # GeckoTerminal: 30 llamadas/min
        # Fuera de la ventana de 6 h ya no pueden pasar el filtro de edad.
        limit = now - self.rules.max_age_h * 3600
        self.watch = {t: s for t, s in self.watch.items() if s > limit}
        self.checked = {t: s for t, s in self.checked.items() if s > limit}

    def best_pairs(self, tokens: list[str]) -> dict[str, dict]:
        """Par más líquido de cada token (DexScreener, 30 tokens por llamada)."""
        out: dict[str, dict] = {}
        for i in range(0, len(tokens), 30):
            try:
                pairs = self._get(f"{DEX}/tokens/v1/solana/" + ",".join(tokens[i:i + 30]))
            except Exception as exc:
                log.warning("DexScreener: %s", exc)
                continue
            for pair in pairs or []:
                tok = pair["baseToken"]["address"]
                liq = float((pair.get("liquidity") or {}).get("usd") or 0)
                if tok not in out or liq > float((out[tok].get("liquidity") or {}).get("usd") or 0):
                    out[tok] = pair
        return out

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
        tokens = [t for t in self.watch if t not in self.checked and t not in self.book.positions]
        pairs = self.best_pairs(tokens)
        for token in tokens:  # los que siguen muertos tras 30 min no se vuelven a consultar
            pair = pairs.get(token)
            dead = pair is None or float((pair.get("liquidity") or {}).get("usd") or 0) < self.rules.min_liq / 4
            if dead and now - self.watch[token] > 1800:
                del self.watch[token]
                self.count("descartados_sin_liquidez")
        for token, pair in pairs.items():
            reason = screen(pair, now, self.rules)
            if reason == "liquidez" or reason == "precio":
                continue  # puede crecer: se vuelve a mirar en la siguiente vuelta
            if reason:
                self.checked[token] = now
                self.count(f"rechazo_{reason}")
                continue
            try:
                reason = self.holders_ok(pair)
            except Exception as exc:
                log.warning("RPC %s: %s", token, exc)
                continue
            self.checked[token] = now
            if reason:
                self.count(f"rechazo_{reason}")
                continue
            if not self.book.can_open(self.rules):
                self.count("sin_cupo")
                continue
            usd = min(self.book.equity() * self.rules.size_frac, self.book.cash)
            symbol = pair["baseToken"].get("symbol", "?")
            fill = self.tx.buy(token, usd)
            self.count("tx_intentos")
            if not fill.ok:
                self.book.pay_fee(None, fill.fee_usd)
                self.count("tx_fallidas" if fill.note == "fallo" else f"tx_{fill.note}")
                self.count("compra_fallida")
                self.event(kind="fail", side="buy", token=token, symbol=symbol, note=fill.note, fee=fill.fee_usd)
                log.info("COMPRA FALLIDA %s (%s)", symbol, fill.note)
                continue
            self.count("tx_ok")
            p = self.book.open(pair, now, usd, fill, self.token_decimals(token))
            self.record_price(token, now, p.entry, p.entry_liq)
            self.count("compras")
            self.event(kind="buy", token=token, symbol=p.symbol, price=p.entry, usd=usd, got_usd=fill.usd,
                       fee=fill.fee_usd, liq=p.entry_liq, age_min=(now - pair["pairCreatedAt"] / 1000) / 60)
            log.info("COMPRA %s %.2f USD (recibe %.2f en tokens) a %.8g (liq %.0f)", p.symbol, usd, fill.usd, p.entry, p.entry_liq)

    def manage(self) -> None:
        if not self.book.positions:
            return
        now = time.time()
        pairs = self.best_pairs(list(self.book.positions))
        for token, p in list(self.book.positions.items()):
            pair = pairs.get(token)
            if pair is None:
                continue
            price = float(pair.get("priceUsd") or 0)
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
            p.last = price
            self.record_price(token, now, price, liq)
            for frac, reason in step_position(p, price, liq, now, self.rules):
                self.execute_sell(p, frac, reason, price, now)
            p.peak = max(p.peak, price)

    def execute_sell(self, p: Position, frac: float, reason: str, price: float, now: float) -> None:
        if p.decimals < 0:
            p.decimals = self.token_decimals(p.token)
        qty = p.qty if frac >= 1.0 else p.qty * frac
        raw = int(qty * 10 ** p.decimals)
        fill = self.tx.sell(p.token, raw)
        self.count("tx_intentos")
        if not fill.ok:
            self.book.pay_fee(p, fill.fee_usd)
            self.count("tx_fallidas" if fill.note == "fallo" else f"tx_{fill.note}")
            self.event(kind="fail", side="sell", token=p.token, symbol=p.symbol, note=fill.note, reason=reason,
                       fee=fill.fee_usd, mult=price / p.entry)
            log.info("VENTA FALLIDA %s (%s, %s): se reintenta", p.symbol, reason, fill.note)
            if fill.note == "sin_ruta":
                p.no_route += 1
                if p.no_route >= self.rules.max_no_route:  # nadie te compra: se pierde lo que queda
                    self.book.close(p, now, "sin_ruta")
                    self.event(kind="sell", token=p.token, symbol=p.symbol, price=price, frac=1.0, usd=0.0,
                               reason="sin_ruta", mult=0.0)
            return
        self.count("tx_ok")
        self.book.sell(p, qty, fill.usd, fill.fee_usd, now, reason)
        # mult: lo que de verdad se obtuvo por token frente a la entrada (incluye impacto y comisiones)
        real_mult = (fill.usd / qty) / p.entry if qty else 0.0
        self.event(kind="sell", token=p.token, symbol=p.symbol, price=price, frac=frac, usd=fill.usd,
                   fee=fill.fee_usd, reason=reason, mult=price / p.entry, real_mult=real_mult)
        log.info("VENDE %s %.0f%% (%s): pantalla x%.2f, ejecutado x%.2f → %.2f USD",
                 p.symbol, frac * 100, reason, price / p.entry, real_mult, fill.usd)

    def run(self) -> None:
        log.info("memesim en papel: libro %.2f USD", self.book.equity())
        last_discover = 0.0
        while True:
            try:
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
    Sim(folder, Rules(), resolve_url(dict(os.environ), args.env_file)).run()


if __name__ == "__main__":
    main()
