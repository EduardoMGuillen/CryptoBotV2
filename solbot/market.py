"""Precios de Binance (API pública de datos de mercado: no requiere cuenta ni API key).

Las señales usan velas diarias de SOL/USDT. USDT y USDC valen ~1 USD, así que la diferencia
con el par SOL/USDC que se opera en Jupiter es despreciable para decidir; el precio real de
ejecución siempre lo da Jupiter.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import requests

BASE = "https://data-api.binance.vision/api/v3"
SOL_LISTING = pd.Timestamp("2020-08-11", tz="UTC")
DAY = pd.Timedelta(days=1)
DAY_MS = 86_400_000
COLUMNS = ["open", "high", "low", "close", "volume"]


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _ms(ts: pd.Timestamp) -> int:
    return int(ts.value // 1_000_000)


def last_closed_day(now: pd.Timestamp) -> pd.Timestamp:
    """Apertura (00:00 UTC) de la vela diaria más reciente que ya cerró."""
    return now.floor("D") - DAY


def parse_klines(rows: list, now: pd.Timestamp) -> pd.DataFrame:
    """Filas de /klines → DataFrame. Descarta la vela que todavía no cierra."""
    closed = [r for r in rows if r[6] < _ms(now)]
    index = pd.DatetimeIndex([pd.Timestamp(r[0], unit="ms", tz="UTC") for r in closed], name="date")
    values = [[float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in closed]
    return pd.DataFrame(values, index=index, columns=COLUMNS)


def fetch_daily(symbol: str, since: pd.Timestamp, session=None, now=None, page_size: int = 1000) -> pd.DataFrame:
    session = session or requests.Session()
    now = now or _now()
    start = _ms(since)
    rows: list = []
    while True:
        resp = session.get(
            f"{BASE}/klines",
            params={"symbol": symbol, "interval": "1d", "startTime": start, "limit": page_size},
            timeout=20,
        )
        resp.raise_for_status()
        page = resp.json()
        rows.extend(page)
        if len(page) < page_size:
            break
        start = page[-1][0] + DAY_MS
    return parse_klines(rows, now)


def _read_cache(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, index_col="date")
    df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = "date"
    return df[COLUMNS].astype(float)


def load_daily(symbol: str, path: Path, session=None, now=None, since: pd.Timestamp = SOL_LISTING) -> pd.DataFrame:
    """Velas diarias cerradas, desde la caché en disco; solo descarga los días que faltan."""
    path = Path(path)
    now = now or _now()
    cached = _read_cache(path) if path.exists() else pd.DataFrame(columns=COLUMNS)
    if not cached.empty and cached.index[-1] >= last_closed_day(now):
        return cached

    start = cached.index[-1] + DAY if not cached.empty else since
    fresh = fetch_daily(symbol, start, session=session, now=now)
    merged = pd.concat([cached, fresh]) if not cached.empty else fresh
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    merged.to_csv(temp)
    temp.replace(path)
    return merged


def last_price(symbol: str, session=None) -> float:
    """Precio de la última operación en Binance (referencia para decidir y para validar a Jupiter)."""
    session = session or requests.Session()
    resp = session.get(f"{BASE}/ticker/price", params={"symbol": symbol}, timeout=10)
    resp.raise_for_status()
    return float(resp.json()["price"])


def tickers(symbols: list[str], session=None) -> list[dict]:
    """Precio, cambio de 24 h y las últimas 24 velas horarias (para la mini-gráfica)."""
    session = session or requests.Session()
    resp = session.get(f"{BASE}/ticker/24hr", params={"symbols": json.dumps(symbols, separators=(",", ":"))}, timeout=10)
    resp.raise_for_status()
    by_symbol = {row["symbol"]: row for row in resp.json()}
    out = []
    for symbol in symbols:
        row = by_symbol.get(symbol)
        if row is None:
            continue
        spark = session.get(f"{BASE}/klines", params={"symbol": symbol, "interval": "1h", "limit": 24}, timeout=10)
        spark.raise_for_status()
        out.append({
            "symbol": symbol.removesuffix("USDT"),
            "price": float(row["lastPrice"]),
            "change_pct": float(row["priceChangePercent"]),
            "spark": [float(k[4]) for k in spark.json()],
        })
    return out
