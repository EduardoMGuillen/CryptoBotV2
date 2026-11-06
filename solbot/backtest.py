"""Motor de backtest SOL/USDC con velas diarias.

Cómo simula un día:
1. Llegan los depósitos del día (capital inicial el primer día, aportes periódicos después).
2. En la APERTURA se rebalancea hacia el peso que la estrategia decidió con el CIERRE de ayer.
   Nunca se opera con información que todavía no existía.
3. Se cobra la comisión sobre el valor operado.
4. Se valora el portafolio al CIERRE.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass(frozen=True)
class Trade:
    date: pd.Timestamp
    side: str  # "buy" | "sell"
    price: float
    qty: float  # SOL
    value: float  # USD operados, antes de comisión
    fee: float  # USD


@dataclass
class Result:
    equity: pd.Series  # valor del portafolio al cierre (USD)
    weights: pd.Series  # fracción en SOL al cierre
    deposits: pd.Series  # dinero que entró ese día (USD)
    trades: list[Trade] = field(default_factory=list)

    @property
    def fees(self) -> float:
        return sum(t.fee for t in self.trades)


def run(
    df: pd.DataFrame,
    target: pd.Series,
    *,
    start=None,
    end=None,
    initial_cash: float = 1000.0,
    deposit: float = 0.0,
    deposit_every: int = 7,
    fee_rate: float = 0.001,
    band: float = 0.0,
    min_trade: float = 0.01,
) -> Result:
    # Peso decidido al cierre del día anterior: es el que se ejecuta en la apertura de hoy.
    decided_yesterday = target.reindex(df.index).shift(1)
    days = df.loc[start:end]
    if days.empty:
        raise ValueError("Ventana sin datos: revisa las fechas de inicio y fin.")

    cash = 0.0
    units = 0.0
    equity, weights, deposits, trades = [], [], [], []

    for i, (ts, open_, close) in enumerate(zip(days.index, days["open"], days["close"])):
        arrived = (initial_cash if i == 0 else 0.0) + (deposit if deposit and i % deposit_every == 0 else 0.0)
        cash += arrived

        goal = decided_yesterday.loc[ts]
        sol_value = units * open_
        total = cash + sol_value
        if not pd.isna(goal) and total > 0 and abs(goal - sol_value / total) > band:
            delta = goal * total - sol_value
            if delta >= min_trade:
                spend = cash if goal >= 1.0 else min(delta, cash)
                fee = spend * fee_rate
                qty = (spend - fee) / open_
                units += qty
                cash -= spend
                trades.append(Trade(ts, "buy", float(open_), qty, spend, fee))
            elif -delta >= min_trade:
                qty = units if goal <= 0.0 else min(-delta / open_, units)
                value = qty * open_
                fee = value * fee_rate
                units -= qty
                cash += value - fee
                trades.append(Trade(ts, "sell", float(open_), qty, value, fee))

        value_close = cash + units * close
        equity.append(value_close)
        weights.append(units * close / value_close if value_close > 0 else 0.0)
        deposits.append(arrived)

    index = days.index
    return Result(
        equity=pd.Series(equity, index=index, name="equity"),
        weights=pd.Series(weights, index=index, name="weight"),
        deposits=pd.Series(deposits, index=index, name="deposits"),
        trades=trades,
    )
