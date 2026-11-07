"""Simulador en PAPEL de estrategias con acciones tokenizadas (xStocks) en Solana. NUNCA opera dinero.

Las señales salen de las velas diarias reales de la bolsa (Yahoo Finance, solo días ya cerrados).
La ejecución se simula con cotizaciones reales de Jupiter USDC ↔ xStock (mismo `TxSim` que el
simulador de memecoins: retraso, transacciones fallidas que pagan red, comisión de red). Los tokens
se negocian 24/7; fuera del horario de la bolsa su precio puede desviarse ~1 % del real.

Cada estrategia es una mesa con $100 en data/meme/variants/acc_<nombre>/ y aparece en /meme.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .jupiter import USDC_MINT, Jupiter, JupiterError
from .market import last_price
from .memesim import Book, Desk, Fill, Position, Rules, TxSim
from .rpc import resolve_url  # noqa: F401  (misma convención de configuración)

log = logging.getLogger("stocksim")
NY = ZoneInfo("America/New_York")
JUP_PRICE = "https://lite-api.jup.ag/price/v3"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}"

# Tokens xStocks con liquidez real en Jupiter (revisado el 2026-11-07). 8 decimales.
XSTOCKS = {
    "SPY": "XsoCS1TfEyfFhfvj8EtZ528L3CaKBDBRqRapnBbDF2W",
    "QQQ": "Xs8S1uUs1zvS2p7iwtsG3b6fkhpvmwz4GYU3gWAmWHZ",
    "NVDA": "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh",
    "TSLA": "XsDoVfqeBukxuZHWhdvWHBhgEHjGNst4MLodqsJHzoB",
    "MSTR": "XsP7xzNPvEHS1m6qfanPUGjNmdnmsLKEoNAnHjdxxyZ",
    "COIN": "Xs7ZdzSHLU9ftNJsii5fCeJhoRWSC32SQGzGQtePxNu",
    "PLTR": "XsoBhf2ufR8fTyNSjqfU71DYGaE6Z3SUGAidpzriAA4",
    "HOOD": "XsvNBAYkrDRNhA7wPHQfX3ZUXZyZLdnCQDfHZ56bzpg",
    "AAPL": "XsbEhLAtcf6HdfpFZ5xEMdqW8nfAvcsP5bdudRLJzJp",
    "META": "Xsa62P5mvPszXL1krVUnU5ar38bBSVcWAB6fmPCo5Zu",
    "GOOGL": "XsCPL9dNWBMvFtTmwcCA5v3xWPSMEBCszbQdiLLq6aN",
    "AMZN": "Xs3eBt7uRfJX8QUs4suhyU8p2M6DoUDrJyWBa8LLZsg",
    "MSFT": "XspzcW1PRtgf6Wj92HCiZdjzKCyFekVD8P5Ueh3dRMX",
    "CRCL": "XsueG8BtpquVJX9LVLLEGuViXUungE6WmK5YZ3p3bd1",
}
DECIMALS = 8
VOLATILE = ["NVDA", "TSLA", "MSTR", "COIN", "PLTR", "HOOD", "CRCL"]
LARGE = ["NVDA", "TSLA", "MSTR", "COIN", "PLTR", "HOOD", "AAPL", "META", "GOOGL", "AMZN", "MSFT", "QQQ", "CRCL"]

# Para acciones no hay plataforma de bot que cobre 1 %: la comisión real ya viene en la cotización.
STOCK_RULES = Rules(fee=0.0, fail_rate=0.05, stop=-1.0, targets=(), max_hold_h=1e9, rug_liq_drop=0.0, max_open=20)


# ---- indicadores y estrategias (puras: velas diarias → pesos objetivo) -------------------------

def rsi(c: pd.Series, n: int) -> pd.Series:
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + g / loss)


def _state_machine(enter: pd.Series, leave: pd.Series) -> bool:
    """¿Está dentro al final? Entra cuando `enter`, sale cuando `leave` (evaluado día a día)."""
    inside = False
    for e, x in zip(enter.fillna(False), leave.fillna(False)):
        if not inside and e:
            inside = True
        elif inside and x:
            inside = False
    return inside


def hold_spy(closes: dict[str, pd.Series]) -> dict[str, float]:
    return {"SPY": 1.0}


def trend_sma50(closes: dict[str, pd.Series], names=VOLATILE) -> dict[str, float]:
    """Cada acción volátil tiene su porción fija; está dentro si cerró sobre su media de 50 días."""
    w = 1 / len(names)
    return {t: w for t in names if t in closes and len(closes[t]) > 50 and closes[t].iloc[-1] > closes[t].rolling(50).mean().iloc[-1]}


def breakout(closes: dict[str, pd.Series], names=VOLATILE, slots: int = 4) -> dict[str, float]:
    """Entra al romper el máximo de 20 días; sale al perder el mínimo de 10. Hasta `slots` a la vez."""
    inside = []
    for t in names:
        c = closes.get(t)
        if c is None or len(c) < 30:
            continue
        if _state_machine(c > c.rolling(20).max().shift(1), c < c.rolling(10).min().shift(1)):
            inside.append((c.iloc[-1] / c.iloc[-21] - 1, t))
    return {t: 1 / slots for _, t in sorted(inside, reverse=True)[:slots]}


def rsi2(closes: dict[str, pd.Series], names=LARGE, slots: int = 4) -> dict[str, float]:
    """Compra tras 2 días de caída fuerte (RSI-2 < 10) en tendencia alcista; vende al cerrar sobre la
    media de 5 días. Hasta `slots` a la vez, prioridad a la más sobrevendida."""
    inside = []
    for t in names:
        c = closes.get(t)
        if c is None or len(c) < 210:
            continue
        r = rsi(c, 2)
        if _state_machine((r < 10) & (c > c.rolling(200).mean()), c > c.rolling(5).mean()):
            inside.append((r.iloc[-1], t))
    return {t: 1 / slots for _, t in sorted(inside)[:slots]}


def weekly_rotation(closes: dict[str, pd.Series], names=VOLATILE, top: int = 2) -> dict[str, float]:
    """Las `top` más fuertes en 20 días (sobre su media de 50), elegidas con el cierre del último
    viernes; se mantienen toda la semana."""
    ranked = []
    for t in names:
        c = closes.get(t)
        if c is None or len(c) < 60:
            continue
        fridays = c[c.index.day_of_week == 4]
        if fridays.empty:
            continue
        upto = c.loc[: fridays.index[-1]]
        if upto.iloc[-1] > upto.rolling(50).mean().iloc[-1]:
            ranked.append((upto.iloc[-1] / upto.iloc[-21] - 1, t))
    return {t: 1 / top for _, t in sorted(ranked, reverse=True)[:top]}


STRATEGIES = {
    "acc_spy": hold_spy,
    "acc_tendencia": trend_sma50,
    "acc_ruptura": breakout,
    "acc_rsi2": rsi2,
    "acc_rotacion": weekly_rotation,
}


# ---- datos ------------------------------------------------------------------------------------

def completed_closes(rows: list[tuple[int, float]], now: datetime.datetime) -> pd.Series:
    """Cierres ajustados de días ya cerrados: se quita la vela de hoy si la bolsa no ha cerrado."""
    idx = pd.to_datetime([ts for ts, _ in rows], unit="s", utc=True).tz_convert(NY).normalize().tz_localize(None)
    s = pd.Series([v for _, v in rows], index=idx).dropna()
    s = s[~s.index.duplicated(keep="last")]
    ny = now.astimezone(NY)
    if len(s) and s.index[-1].date() == ny.date() and (ny.hour, ny.minute) < (16, 15):
        s = s.iloc[:-1]
    return s


# ---- mesa de acciones ---------------------------------------------------------------------------

class StockDesk(Desk):
    """Mesa con pesos objetivo: compra lo que entra a la cartera y vende lo que sale."""

    def rebalance(self, target: dict[str, float], prices: dict[str, float], now: float) -> None:
        held = {p.symbol.removesuffix("x"): p for p in self.book.positions.values()}
        for sym, p in held.items():
            if sym not in target and prices.get(sym):
                self.count("senal_venta")
                self.execute_sell(p, 1.0, "senal", prices[sym], now, lambda t: DECIMALS)
        capital = self.book.capital()
        for sym, weight in target.items():
            if sym in held or not prices.get(sym) or sym not in XSTOCKS:
                continue
            usd = min(capital * weight, self.book.cash)
            if usd < 1.0:
                continue
            self.count("senal_compra")
            self.buy(sym, usd, prices[sym], now)

    def buy(self, sym: str, usd: float, price: float, now: float) -> None:
        mint = XSTOCKS[sym]
        fill = self.tx.buy(mint, usd)
        self.count("tx_intentos")
        if not fill.ok:
            self.book.pay_fee(None, fill.fee_usd)
            self._tx_failed(fill)
            self.count("compra_fallida")
            self.event(kind="fail", side="buy", token=mint, symbol=sym + "x", note=fill.note, fee=fill.fee_usd)
            log.info("[%s] COMPRA FALLIDA %s (%s): se reintenta en la próxima revisión", self.name, sym, fill.note)
            return
        self.count("tx_ok")
        pair = {"pairAddress": mint, "baseToken": {"address": mint, "symbol": sym + "x"}, "priceUsd": str(price),
                "liquidity": {"usd": 0.0}, "pairCreatedAt": 0}
        p = self.book.open(pair, now, usd, fill, DECIMALS)
        self.record_price(mint, now, price, 0)
        self.count("compras")
        self.event(kind="buy", token=mint, symbol=sym + "x", price=price, usd=usd, got_usd=fill.usd, fee=fill.fee_usd,
                   liq=0, age_min=0)
        log.info("[%s] COMPRA %s %.2f USD (recibe %.2f) a %.2f", self.name, sym, usd, fill.usd, price)

    def mark(self, prices: dict[str, float], now: float) -> None:
        for p in self.book.positions.values():
            price = prices.get(p.symbol.removesuffix("x"))
            if price:
                p.last = price
                p.peak = max(p.peak, price)
                self.record_price(p.token, now, price, 0)


class StockSim:
    def __init__(self, folder: Path, strategies=STRATEGIES, start_cash: float = 100.0):
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "Mozilla/5.0"
        self.jupiter = Jupiter(session=self.http)
        self._sol = (0.0, 0.0)
        self.strategies = strategies
        self.desks = {name: StockDesk(name, folder / "variants" / name, STOCK_RULES,
                                      TxSim(self.jupiter.order, self.sol_price, STOCK_RULES, quote_mint=USDC_MINT),
                                      start_cash)
                      for name in strategies}
        self._closes: dict[str, pd.Series] = {}
        self._closes_at = 0.0

    def sol_price(self) -> float:
        price, at = self._sol
        if time.time() - at > 60:
            price = last_price("SOLUSDT", session=self.http)
            self._sol = (price, time.time())
        return price

    def closes(self) -> dict[str, pd.Series]:
        if time.time() - self._closes_at < 3600 and self._closes:
            return self._closes
        out = {}
        for sym in XSTOCKS:
            try:
                r = self.http.get(YAHOO.format(sym), params={"period1": int(time.time()) - 400 * 86400,
                                                              "period2": int(time.time()), "interval": "1d"}, timeout=20)
                r.raise_for_status()
                res = r.json()["chart"]["result"][0]
                adj = res["indicators"]["adjclose"][0]["adjclose"]
                out[sym] = completed_closes(list(zip(res["timestamp"], adj)), datetime.datetime.now(datetime.timezone.utc))
            except Exception as exc:
                log.warning("Yahoo %s: %s", sym, exc)
            time.sleep(0.3)
        if out:
            self._closes, self._closes_at = out, time.time()
        return self._closes

    def prices(self) -> dict[str, float]:
        """Precio de cada xStock en Jupiter (lo que valen los tokens, no la acción en bolsa)."""
        r = self.http.get(JUP_PRICE, params={"ids": ",".join(XSTOCKS.values())}, timeout=15)
        r.raise_for_status()
        data = r.json()
        by_mint = {m: s for s, m in XSTOCKS.items()}
        return {by_mint[m]: float(v["usdPrice"]) for m, v in data.items() if v and v.get("usdPrice") and m in by_mint}

    def step(self) -> None:
        now = time.time()
        prices = self.prices()
        closes = self.closes()
        for name, fn in self.strategies.items():
            desk = self.desks[name]
            desk.mark(prices, now)
            try:
                target = fn(closes)
            except Exception as exc:
                log.warning("[%s] señal: %s", name, exc)
                continue
            desk.rebalance(target, prices, now)
            desk.save()

    def run(self) -> None:
        log.info("stocksim en papel: %s", ", ".join(f"{n} {d.book.equity():.2f}" for n, d in self.desks.items()))
        last_equity = 0.0
        while True:
            try:
                self.step()
                if time.time() - last_equity >= 300:
                    last_equity = time.time()
                    for d in self.desks.values():
                        d.record_equity()
            except Exception as exc:
                log.exception("vuelta fallida: %s", exc)
            time.sleep(60)


def main() -> None:
    ap = argparse.ArgumentParser(description="Simulador en papel de acciones tokenizadas (no opera dinero real).")
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument("--dir", default="data/meme")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    StockSim(Path(args.dir)).run()


if __name__ == "__main__":
    main()
