"""Estrategias de asignación SOL/USDC.

Cada estrategia devuelve, para cada día, qué fracción del dinero debe estar en SOL
(0 = todo en USDC, 1 = todo en SOL), calculada con el cierre de ese día.

Reglas que todas cumplen:
- Solo usan precios hasta el cierre del día (nunca el futuro). Hay un test que lo verifica.
- NaN significa "todavía no hay datos suficientes" (calentamiento). El motor no opera con NaN.
"""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

DAYS_PER_YEAR = 365  # el mercado cripto opera todos los días


def buy_hold(df: pd.DataFrame) -> pd.Series:
    return pd.Series(1.0, index=df.index)


def fixed_mix(df: pd.DataFrame, sol: float = 0.5) -> pd.Series:
    return pd.Series(float(sol), index=df.index)


def trend_sma(df: pd.DataFrame, n: int) -> pd.Series:
    close = df["close"]
    sma = close.rolling(n).mean()
    return (close > sma).astype(float).where(sma.notna())


def sma_cross(df: pd.DataFrame, fast: int, slow: int) -> pd.Series:
    close = df["close"]
    fast_sma = close.rolling(fast).mean()
    slow_sma = close.rolling(slow).mean()
    return (fast_sma > slow_sma).astype(float).where(fast_sma.notna() & slow_sma.notna())


def vol_target(df: pd.DataFrame, target: float, lookback: int = 30) -> pd.Series:
    returns = np.log(df["close"]).diff()
    vol = returns.rolling(lookback).std() * math.sqrt(DAYS_PER_YEAR)
    return (target / vol).clip(upper=1.0).where(vol.notna())


@dataclass(frozen=True)
class Family:
    fn: Callable[..., pd.Series]
    title: Callable[[dict], str]
    description: str
    # Líneas que se dibujan sobre el precio (nombre → serie)
    lines: Callable[[pd.DataFrame, dict], dict] | None = None
    # Solo se rebalancea si el peso actual se aleja más que esto del objetivo.
    band: float = 0.0


FAMILIES: dict[str, Family] = {
    "buy_hold": Family(
        buy_hold,
        lambda p: "Comprar y mantener",
        "Todo en SOL desde el día uno y no se toca. Es la referencia a vencer.",
    ),
    "fixed_mix": Family(
        fixed_mix,
        lambda p: f"Rebalanceo {round(p['sol'] * 100)}/{round((1 - p['sol']) * 100)}",
        "Mantiene una mezcla fija SOL/USDC: vende SOL cuando sube y compra cuando baja.",
        band=0.05,
    ),
    "trend_sma": Family(
        trend_sma,
        lambda p: f"Tendencia SMA {p['n']}",
        "100% SOL mientras el precio cierra sobre su media de N días; si no, 100% USDC.",
        lines=lambda df, p: {f"SMA {p['n']}": df["close"].rolling(p["n"]).mean()},
    ),
    "sma_cross": Family(
        sma_cross,
        lambda p: f"Cruce SMA {p['fast']}/{p['slow']}",
        "100% SOL mientras la media rápida está sobre la lenta; si no, 100% USDC.",
        lines=lambda df, p: {
            f"SMA {p['fast']}": df["close"].rolling(p["fast"]).mean(),
            f"SMA {p['slow']}": df["close"].rolling(p["slow"]).mean(),
        },
    ),
    "vol_target": Family(
        vol_target,
        lambda p: f"Volatilidad objetivo {round(p['target'] * 100)}%",
        "Menos SOL cuando el mercado está más volátil, para que el riesgo se mantenga parejo.",
        band=0.10,
    ),
}


def _fmt(value) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


@dataclass(frozen=True)
class Strategy:
    family: str
    params: dict
    band: float

    @property
    def id(self) -> str:
        return "_".join([self.family, *(_fmt(v) for v in self.params.values())])

    @property
    def name(self) -> str:
        return FAMILIES[self.family].title(self.params)

    @property
    def description(self) -> str:
        return FAMILIES[self.family].description

    def weights(self, df: pd.DataFrame) -> pd.Series:
        return FAMILIES[self.family].fn(df, **self.params)

    def indicators(self, df: pd.DataFrame) -> dict[str, pd.Series]:
        lines = FAMILIES[self.family].lines
        return lines(df, self.params) if lines else {}


def make(family: str, **params) -> Strategy:
    """Crea una estrategia. Los parámetros quedan en el orden de la función, así el id es
    siempre el mismo (p. ej. vol_target_0.4_30) sin importar cómo se escribieron."""
    if family not in FAMILIES:
        raise ValueError(f"Estrategia desconocida: {family!r}. Opciones: {', '.join(FAMILIES)}")
    spec = FAMILIES[family]
    signature = inspect.signature(spec.fn)
    names = [n for n in signature.parameters if n != "df"]
    unknown = sorted(set(params) - set(names))
    if unknown:
        raise ValueError(f"Parámetros desconocidos para {family}: {', '.join(unknown)}")
    ordered = {}
    for n in names:
        if n in params:
            ordered[n] = params[n]
        elif signature.parameters[n].default is not inspect.Parameter.empty:
            ordered[n] = signature.parameters[n].default
        else:
            raise ValueError(f"Falta el parámetro {n!r} de la estrategia {family}.")
    return Strategy(family=family, params=ordered, band=spec.band)


def catalog() -> list[Strategy]:
    """Estrategias que compara el backtest. Pocos parámetros y valores estándar, para no sobreajustar."""
    return [
        make("buy_hold"),
        make("fixed_mix", sol=0.5),
        *(make("trend_sma", n=n) for n in (20, 50, 100, 200)),
        *(make("sma_cross", fast=f, slow=s) for f, s in ((10, 30), (20, 50), (50, 200))),
        *(make("vol_target", target=t, lookback=30) for t in (0.4, 0.6, 0.8)),
    ]
