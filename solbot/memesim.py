"""Simulador en PAPEL de un bot de memecoins nuevos en Solana. NUNCA firma ni envía órdenes.

Copia las reglas del post "$100 → $4,812" para medir si funcionan con precios reales:
  - par con SOL/USDC, liquidez > 20 000 USD y menos de 6 h de vida;
  - top 10 holders < 35 % y ningún holder > 5 % (aproximación de "dev < 5 %": no sabemos
    quién es el dev), sin mint authority ni freeze authority;
  - 4 % del libro por operación; stop −25 %; vende 1/3 a 2x, la mitad del resto a 3x y el
    resto con trailing de 30 % desde el máximo; sale si la liquidez cae a la mitad o tras 24 h.
No se simula la regla "dev mueve tokens a un exchange" (requiere rastrear la billetera del dev).

Costos simulados (conservadores, a propósito): 1 % de comisión por lado + impacto de precio
de un pool de producto constante + 2 % de retraso en la entrada. Los stops se ejecutan al
precio que se observa en la siguiente consulta, así que pueden salir mucho peor que −25 %.

Fuentes: GeckoTerminal (pools nuevos y en tendencia), DexScreener (precio y liquidez),
RPC de Solana (holders y autoridades del mint).
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests

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
    fee: float = 0.01
    latency: float = 0.02
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


def fill_price(price: float, usd: float, quote_liq_usd: float, side: str, fee: float) -> float:
    """Precio medio efectivo en un pool x·y=k con `quote_liq_usd` del lado cotizado."""
    if quote_liq_usd <= 0:
        return float("inf") if side == "buy" else 0.0
    impact = 1 + usd / quote_liq_usd
    return price * impact / (1 - fee) if side == "buy" else price * (1 - fee) / impact


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

    def open(self, pair: dict, now: float, rules: Rules) -> Position:
        price = float(pair["priceUsd"])
        liq = float(pair["liquidity"]["usd"])
        usd = min(self.equity() * rules.size_frac, self.cash)
        paid = fill_price(price, usd, liq / 2, "buy", rules.fee) * (1 + rules.latency)
        token = pair["baseToken"]["address"]
        p = Position(token=token, symbol=pair["baseToken"].get("symbol", "?"), pool=pair["pairAddress"],
                     entry=price, qty=usd / paid, cost=usd, opened=now, peak=price, entry_liq=liq, last=price)
        self.cash -= usd
        self.positions[token] = p
        return p

    def sell(self, p: Position, frac: float, price: float, liq: float, now: float, reason: str, rules: Rules) -> float:
        qty = p.qty if frac >= 1.0 else p.qty * frac
        got = qty * fill_price(price, qty * price, liq / 2, "sell", rules.fee)
        self.cash += got
        p.qty -= qty
        p.proceeds += got
        if reason.startswith("tp"):
            p.steps_done += 1
        if frac >= 1.0 or p.qty <= 0:
            del self.positions[p.token]
            self.closed.append({**asdict(p), "closed": now, "reason": reason, "pnl": p.proceeds - p.cost,
                                "mult": p.proceeds / p.cost if p.cost else 0.0})
        return got


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
        supply = float(self._rpc("getTokenSupply", [token])["value"]["uiAmount"] or 0)
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
            p = self.book.open(pair, now, self.rules)
            self.count("compras")
            self.event(kind="buy", token=token, symbol=p.symbol, price=p.entry, usd=p.cost,
                       liq=p.entry_liq, age_min=(now - pair["pairCreatedAt"] / 1000) / 60)
            log.info("COMPRA %s %.2f USD a %.8g (liq %.0f)", p.symbol, p.cost, p.entry, p.entry_liq)

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
            for frac, reason in step_position(p, price, liq, now, self.rules):
                got = self.book.sell(p, frac, price, liq, now, reason, self.rules)
                self.event(kind="sell", token=token, symbol=p.symbol, price=price, frac=frac, usd=got,
                           reason=reason, mult=price / p.entry)
                log.info("VENDE %s %.0f%% (%s) a %.8g → %.2f USD", p.symbol, frac * 100, reason, price, got)
            p.peak = max(p.peak, price)

    def run(self) -> None:
        log.info("memesim en papel: libro %.2f USD", self.book.equity())
        last_discover = 0.0
        while True:
            try:
                if time.time() - last_discover >= 60:
                    self.discover()
                    self.evaluate()
                    last_discover = time.time()
                self.manage()
                self.save()
            except Exception as exc:
                log.exception("vuelta fallida: %s", exc)
            time.sleep(15)


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
