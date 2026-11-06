import math

import pandas as pd
import pytest

from solbot import metrics as m
from solbot.backtest import Result, Trade


def days(n, start="2025-01-01"):
    return pd.date_range(start, periods=n, freq="D", tz="UTC")


def test_max_drawdown_is_the_worst_fall_from_a_previous_peak():
    index = pd.Series([100, 120, 60, 90, 130], index=days(5), dtype=float)
    assert m.max_drawdown(index) == pytest.approx(-0.5)


def test_twr_does_not_count_deposits_as_profit():
    equity = pd.Series([100.0, 220.0], index=days(2))
    deposits = pd.Series([100.0, 100.0], index=days(2))
    # Día 2: había 100, entraron 100 → 200 trabajando; cerró en 220 → +10%, no +120%
    assert m.twr_returns(equity, deposits).round(10).tolist() == [0.0, 0.1]


def test_cagr_annualizes_total_growth_over_calendar_days():
    returns = pd.Series([3.0] + [0.0] * 729, index=days(730))  # ×4 en dos años
    assert m.cagr(returns) == pytest.approx(1.0)


def test_sharpe_is_mean_over_deviation_annualized():
    returns = pd.Series([0.02, 0.0], index=days(2))
    assert m.sharpe(returns) == pytest.approx(0.01 / math.sqrt(0.0002) * math.sqrt(365))


def test_sharpe_of_a_flat_series_is_zero_not_infinite():
    assert m.sharpe(pd.Series([0.0] * 5, index=days(5))) == 0.0


def test_irr_of_one_deposit_that_grew_ten_percent_in_a_year():
    t0 = pd.Timestamp("2025-01-01", tz="UTC")
    assert m.irr([(t0, 100.0)], 110.0, t0 + pd.Timedelta(days=365)) == pytest.approx(0.10, abs=1e-6)


def test_irr_weights_each_deposit_by_how_long_it_was_invested():
    t0 = pd.Timestamp("2025-01-01", tz="UTC")
    flows = [(t0, 100.0), (t0 + pd.Timedelta(days=365), 100.0)]
    # 100·1.1² + 100·1.1 = 231
    assert m.irr(flows, 231.0, t0 + pd.Timedelta(days=730)) == pytest.approx(0.10, abs=1e-6)


def test_yearly_returns_split_at_new_year():
    idx = days(4, start="2024-12-30")
    equity = pd.Series([100.0, 110.0, 99.0, 99.0], index=idx)
    deposits = pd.Series([100.0, 0.0, 0.0, 0.0], index=idx)
    yearly = m.yearly_returns(equity, deposits)
    assert yearly == {2024: pytest.approx(0.1), 2025: pytest.approx(-0.1)}


def test_summary_reports_the_numbers_a_person_needs():
    idx = days(4)
    res = Result(
        equity=pd.Series([100.0, 110.0, 99.0, 121.0], index=idx),
        weights=pd.Series([1.0, 1.0, 0.5, 0.0], index=idx),
        deposits=pd.Series([100.0, 0.0, 0.0, 0.0], index=idx),
        trades=[Trade(idx[0], "buy", 10.0, 9.99, 100.0, 0.1)],
    )
    s = m.summarize(res)
    assert s["final"] == 121.0
    assert s["deposited"] == 100.0
    assert s["profit"] == pytest.approx(21.0)
    assert s["total_return"] == pytest.approx(0.21)
    assert s["max_drawdown"] == pytest.approx(-0.1)
    assert s["exposure"] == pytest.approx(0.625)
    assert s["trades"] == 1
    assert s["fees"] == pytest.approx(0.1)
