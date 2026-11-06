"""Métricas de rendimiento y riesgo de un backtest.

Los aportes NO cuentan como ganancia: el rendimiento se mide día a día sobre el dinero
que ya estaba trabajando (rendimiento ponderado en el tiempo, TWR). Para saber cuánto
rindió cada dólar aportado se usa la TIR (IRR).
"""

from __future__ import annotations

import math

import pandas as pd

from .backtest import Result

DAYS_PER_YEAR = 365


def twr_returns(equity: pd.Series, deposits: pd.Series) -> pd.Series:
    """r_t = E_t / (E_{t-1} + D_t) − 1: el depósito del día trabaja desde la apertura."""
    base = equity.shift(1).fillna(0.0) + deposits
    return (equity / base - 1).where(base > 0, 0.0)


def growth_index(returns: pd.Series) -> pd.Series:
    return (1 + returns).cumprod()


def drawdown(index: pd.Series) -> pd.Series:
    return index / index.cummax() - 1


def max_drawdown(index: pd.Series) -> float:
    return float(drawdown(index).min())


def cagr(returns: pd.Series) -> float:
    if len(returns) == 0:
        return 0.0
    total = float((1 + returns).prod())
    if total <= 0:
        return -1.0
    return total ** (DAYS_PER_YEAR / len(returns)) - 1


def volatility(returns: pd.Series) -> float:
    if len(returns) < 2:
        return 0.0
    return float(returns.std() * math.sqrt(DAYS_PER_YEAR))


def sharpe(returns: pd.Series) -> float:
    if len(returns) < 2:
        return 0.0
    deviation = returns.std()
    if not deviation or math.isnan(deviation):
        return 0.0
    return float(returns.mean() / deviation * math.sqrt(DAYS_PER_YEAR))


def irr(flows: list[tuple[pd.Timestamp, float]], final_value: float, final_date: pd.Timestamp) -> float:
    """Tasa anual r tal que los aportes, capitalizados a r, valen `final_value` en `final_date`."""
    if not flows:
        return 0.0
    if final_value <= 0:
        return -1.0

    def future_value(rate: float) -> float:
        return sum(amount * (1 + rate) ** ((final_date - date).days / DAYS_PER_YEAR) for date, amount in flows)

    low, high = -0.9999, 1.0
    while future_value(high) < final_value and high < 1e6:
        high *= 2
    for _ in range(200):
        mid = (low + high) / 2
        if future_value(mid) < final_value:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def yearly_returns(equity: pd.Series, deposits: pd.Series) -> dict[int, float]:
    returns = twr_returns(equity, deposits)
    return {int(year): float((1 + r).prod() - 1) for year, r in returns.groupby(returns.index.year)}


def summarize(res: Result) -> dict:
    returns = twr_returns(res.equity, res.deposits)
    index = growth_index(returns)
    final = float(res.equity.iloc[-1])
    deposited = float(res.deposits.sum())
    worst = max_drawdown(index)
    annual = cagr(returns)
    flows = [(ts, float(d)) for ts, d in res.deposits.items() if d > 0]
    closes_at = res.equity.index[-1] + pd.Timedelta(days=1)  # el valor final es al cierre del último día
    return {
        "start": res.equity.index[0].date().isoformat(),
        "end": res.equity.index[-1].date().isoformat(),
        "days": len(returns),
        "final": final,
        "deposited": deposited,
        "profit": final - deposited,
        "total_return": float(index.iloc[-1] - 1),
        "cagr": annual,
        "irr": irr(flows, final, closes_at),
        "max_drawdown": worst,
        "volatility": volatility(returns),
        "sharpe": sharpe(returns),
        "calmar": annual / abs(worst) if worst < 0 else 0.0,
        "exposure": float(res.weights.mean()),
        "trades": len(res.trades),
        "fees": float(res.fees),
    }
