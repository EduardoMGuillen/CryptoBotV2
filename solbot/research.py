"""Comparación honesta de estrategias sobre datos históricos.

- Todas las estrategias empiezan el mismo día: cuando la más lenta ya tiene señal.
- Los parámetros se eligen SOLO con el periodo de entrenamiento (mejor Sharpe) y se juzgan
  en el periodo de prueba, que la elección nunca vio.
- Dos escenarios: todo el capital al inicio, o un aporte fijo cada semana (DCA).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pandas as pd

from . import metrics
from . import strategies as st
from .backtest import run

DAY = pd.Timedelta(days=1)
FEE_LEVELS = [0.0005, 0.001, 0.003, 0.01]


def pick_best(runs: dict, ids: list[str]) -> str:
    """La de mejor Sharpe en ENTRENAMIENTO. El periodo de prueba no participa."""
    return max(ids, key=lambda i: runs[i]["train"]["sharpe"])


def _strict(value):
    """El navegador no acepta NaN ni infinito en JSON."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _strict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_strict(v) for v in value]
    return value


def run_research(
    df: pd.DataFrame,
    *,
    split: str = "2024-01-01",
    fee_rate: float = 0.001,
    lump: float = 1000.0,
    weekly: float = 50.0,
    strategies: list[st.Strategy] | None = None,
) -> dict:
    strategies = strategies or st.catalog()
    split_at = pd.Timestamp(split, tz="UTC")
    weights = {s.id: s.weights(df) for s in strategies}
    start = max(w.first_valid_index() for w in weights.values()) + DAY
    end = df.index[-1]
    if not start < split_at <= end:
        raise ValueError(f"El corte {split} debe caer entre {start.date()} y {end.date()}.")

    money = {
        "lump": {"initial_cash": lump, "deposit": 0.0},
        "dca": {"initial_cash": 0.0, "deposit": weekly, "deposit_every": 7},
    }
    window = df.loc[start:end]
    scenarios = {}
    for key, cash in money.items():
        runs, curves, drawdowns, trades = {}, {}, {}, {}
        deposited = None
        for s in strategies:
            w = weights[s.id]
            opts = {"fee_rate": fee_rate, "band": s.band, **cash}
            full = run(df, w, start=start, **opts)
            runs[s.id] = {
                "full": metrics.summarize(full),
                "train": metrics.summarize(run(df, w, start=start, end=split_at - DAY, **opts)),
                "test": metrics.summarize(run(df, w, start=split_at, **opts)),
                "yearly": metrics.yearly_returns(full.equity, full.deposits),
            }
            curves[s.id] = [round(v, 2) for v in full.equity]
            index = metrics.growth_index(metrics.twr_returns(full.equity, full.deposits))
            drawdowns[s.id] = [round(v, 4) for v in metrics.drawdown(index)]
            trades[s.id] = [[t.date.date().isoformat(), t.side, round(t.price, 4)] for t in full.trades]
            if deposited is None:
                deposited = [round(v, 2) for v in full.deposits.cumsum()]
        scenarios[key] = {"runs": runs, "curves": curves, "drawdowns": drawdowns, "trades": trades, "deposited": deposited}

    lump_runs = scenarios["lump"]["runs"]
    families = list(dict.fromkeys(s.family for s in strategies))
    by_family = {f: pick_best(lump_runs, [s.id for s in strategies if s.family == f]) for f in families}

    costs = {}
    for s in strategies:
        costs[s.id] = [
            metrics.summarize(run(df, weights[s.id], start=start, fee_rate=fee, band=s.band, **money["lump"]))["cagr"]
            for fee in FEE_LEVELS
        ]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data": {"first": df.index[0].date().isoformat(), "last": end.date().isoformat(), "days": len(df)},
        "settings": {
            "start": start.date().isoformat(),
            "split": split_at.date().isoformat(),
            "end": end.date().isoformat(),
            "fee_rate": fee_rate,
            "lump": lump,
            "weekly": weekly,
        },
        "strategies": [
            {"id": s.id, "family": s.family, "name": s.name, "description": s.description, "params": s.params}
            for s in strategies
        ],
        "dates": [d.date().isoformat() for d in window.index],
        "price": [round(v, 4) for v in window["close"]],
        "scenarios": scenarios,
        "selection": {"by_family": by_family, "winner": pick_best(lump_runs, [s.id for s in strategies])},
        "costs": {"fees": FEE_LEVELS, "cagr": costs},
    }
    return _strict(report)
