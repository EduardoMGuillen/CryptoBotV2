import pandas as pd
import pytest

from solbot.backtest import run
from util import frame


def weights(df, values):
    return pd.Series(values, index=df.index, dtype=float)


def test_buy_and_hold_buys_once_at_the_first_open_and_pays_the_fee():
    # La fila 0 es el día anterior al inicio (de ahí sale la primera señal).
    df = frame(closes=[10, 11, 12, 6], opens=[10, 10, 11, 12])
    res = run(df, weights(df, [1, 1, 1, 1]), start=df.index[1], initial_cash=1000, fee_rate=0.001)
    # Compra en la apertura (10): 1000 − 1 de comisión = 999 → 99.9 SOL
    assert res.equity.round(6).tolist() == [1098.9, 1198.8, 599.4]
    assert len(res.trades) == 1
    assert res.fees == pytest.approx(1.0)
    assert res.weights.round(6).tolist() == [1.0, 1.0, 1.0]


def test_a_signal_seen_at_a_close_is_executed_at_the_next_open():
    df = frame(closes=[10, 10, 12, 6], opens=[10, 10, 10, 12])
    # La señal de compra aparece al cierre de la fila 2 (precio 12).
    res = run(df, weights(df, [0, 0, 1, 1]), start=df.index[1], initial_cash=1000, fee_rate=0.001)
    # Se compra en la apertura de la fila 3 (12): 999 / 12 = 83.25 SOL → cierre 6 = 499.5
    assert res.equity.round(6).tolist() == [1000.0, 1000.0, 499.5]
    assert res.trades[0].date == df.index[3]
    assert res.trades[0].price == 12


def test_selling_goes_back_to_cash_minus_the_fee():
    df = frame(closes=[10, 10, 20, 20], opens=[10, 10, 10, 20])
    res = run(df, weights(df, [1, 0, 0, 0]), start=df.index[1], initial_cash=1000, fee_rate=0.001)
    # Fila 1: compra a 10 → 99.9 SOL (999). Fila 2: vende a 10 → 999 − 0.999 = 998.001
    assert res.equity.round(6).tolist() == [999.0, 998.001, 998.001]
    assert [t.side for t in res.trades] == ["buy", "sell"]


def test_band_ignores_small_changes_in_the_target():
    df = frame(closes=[10, 10, 10, 10])
    res = run(df, weights(df, [1.0, 0.95, 0.85, 0.85]), start=df.index[1],
              initial_cash=1000, fee_rate=0.0, band=0.1)
    # 1.0 → 0.95 se ignora (0.05 ≤ banda); 1.0 → 0.85 sí rebalancea: vende 15% de 1000
    assert [t.side for t in res.trades] == ["buy", "sell"]
    assert res.trades[1].value == pytest.approx(150.0)
    assert res.weights.round(6).tolist() == [1.0, 1.0, 0.85]


def test_weekly_deposits_are_invested_the_day_they_arrive():
    df = frame(closes=[10] * 9)
    res = run(df, weights(df, [1] * 9), start=df.index[1], initial_cash=0,
              deposit=100, deposit_every=7, fee_rate=0.0)
    assert res.deposits[res.deposits > 0].index.tolist() == [df.index[1], df.index[8]]
    assert [t.value for t in res.trades] == [100, 100]
    assert res.equity.iloc[-1] == 200


def test_a_missing_signal_keeps_the_current_position():
    nan = float("nan")
    df = frame(closes=[10, 10, 20, 20], opens=[10, 10, 10, 20])
    res = run(df, weights(df, [1, nan, nan, 1]), start=df.index[1], initial_cash=1000, fee_rate=0.0)
    assert len(res.trades) == 1
    assert res.equity.tolist() == [1000.0, 2000.0, 2000.0]


def test_end_date_stops_the_simulation():
    df = frame(closes=[10, 11, 12, 13, 14])
    res = run(df, weights(df, [1] * 5), start=df.index[1], end=df.index[3], initial_cash=100)
    assert res.equity.index.tolist() == [df.index[1], df.index[2], df.index[3]]


def test_an_empty_window_is_an_error():
    df = frame(closes=[10, 11])
    with pytest.raises(ValueError, match="sin datos"):
        run(df, weights(df, [1, 1]), start=pd.Timestamp("2030-01-01", tz="UTC"))
