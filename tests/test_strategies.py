import math

import numpy as np
import pandas as pd
import pytest

from solbot import strategies as st
from util import as_list, frame


def test_trend_sma_holds_sol_only_while_close_is_above_its_average():
    df = frame([1, 2, 3, 2, 1])
    # SMA(2) = [nan, 1.5, 2.5, 2.5, 1.5]
    assert as_list(st.trend_sma(df, n=2)) == [None, 1.0, 1.0, 0.0, 0.0]


def test_sma_cross_holds_sol_while_fast_average_is_above_slow():
    df = frame([1, 2, 3, 4, 3, 2, 1])
    # SMA(2) = [nan, 1.5, 2.5, 3.5, 3.5, 2.5, 1.5]
    # SMA(3) = [nan, nan, 2, 3, 3.33, 3, 2]
    assert as_list(st.sma_cross(df, fast=2, slow=3)) == [None, None, 1.0, 1.0, 1.0, 0.0, 0.0]


def test_vol_target_cuts_exposure_when_volatility_is_above_target():
    df = frame([100, 110, 100, 110, 100])
    weights = st.vol_target(df, target=0.6, lookback=2)
    # Últimos 2 retornos log: [+ln1.1, -ln1.1] → desviación (ddof=1) = ln1.1·√2, anualizada ×√365
    realized = math.log(1.1) * math.sqrt(2) * math.sqrt(365)
    assert weights.iloc[-1] == pytest.approx(0.6 / realized)
    assert as_list(weights)[:2] == [None, None]


def test_vol_target_never_goes_above_a_full_position():
    df = frame([100, 100.01, 100.02, 100.03, 100.04])
    assert st.vol_target(df, target=0.6, lookback=2).iloc[-1] == 1.0


def test_fixed_mix_keeps_the_same_share_every_day():
    assert as_list(st.fixed_mix(frame([1, 2, 3]), sol=0.5)) == [0.5, 0.5, 0.5]


def test_make_rejects_an_unknown_strategy():
    with pytest.raises(ValueError, match="desconocida"):
        st.make("martingala")


def test_make_builds_a_strategy_with_the_given_parameters():
    df = frame([1, 2, 3, 2, 1])
    strategy = st.make("trend_sma", n=2)
    assert as_list(strategy.weights(df)) == [None, 1.0, 1.0, 0.0, 0.0]
    assert strategy.id == "trend_sma_2"


@pytest.mark.parametrize("strategy", st.catalog(), ids=lambda s: s.id)
def test_weights_never_peek_at_future_prices(strategy):
    rng = np.random.default_rng(7)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.04, 400)))
    df = frame(closes)
    cut = 300
    full = strategy.weights(df)
    only_past = strategy.weights(df.iloc[:cut])
    pd.testing.assert_series_equal(full.iloc[:cut], only_past)


def test_trend_indicator_is_the_moving_average_drawn_on_the_chart():
    lines = st.make("trend_sma", n=2).indicators(frame([1, 2, 3, 2, 1]))
    assert list(lines) == ["SMA 2"]
    assert as_list(lines["SMA 2"]) == [None, 1.5, 2.5, 2.5, 1.5]


def test_cross_indicators_are_both_averages():
    lines = st.make("sma_cross", fast=2, slow=3).indicators(frame([1, 2, 3, 4]))
    assert list(lines) == ["SMA 2", "SMA 3"]


def test_buy_and_hold_draws_no_indicator():
    assert st.make("buy_hold").indicators(frame([1, 2, 3])) == {}
