import json
import math

import numpy as np
import pandas as pd
import pytest

from solbot import research
from util import frame


@pytest.fixture(scope="module")
def prices():
    rng = np.random.default_rng(11)
    closes = 50 * np.exp(np.cumsum(rng.normal(0.0005, 0.04, 600)))
    opens = np.concatenate([[50.0], closes[:-1]])  # mercado 24/7: abre donde cerró ayer
    return frame(closes, opens=opens, start="2022-01-01")


@pytest.fixture(scope="module")
def report(prices):
    return research.run_research(prices, split="2023-03-01", fee_rate=0.001, lump=1000, weekly=50)


def test_evaluation_starts_once_every_strategy_has_a_signal(prices, report):
    # SMA 200 necesita 200 cierres: primera señal en la fila 199 → primera apertura operable, fila 200
    assert report["settings"]["start"] == prices.index[200].date().isoformat()


def test_training_and_test_periods_do_not_overlap(report):
    run = report["scenarios"]["lump"]["runs"]["buy_hold"]
    assert run["train"]["end"] == "2023-02-28"
    assert run["test"]["start"] == "2023-03-01"


def test_buy_and_hold_in_the_test_period_matches_the_price_move(prices, report):
    test = report["scenarios"]["lump"]["runs"]["buy_hold"]["test"]
    entry = prices.loc[pd.Timestamp("2023-03-01", tz="UTC"), "open"]
    exit_ = prices["close"].iloc[-1]
    assert test["total_return"] == pytest.approx(exit_ / entry * (1 - 0.001) - 1)


def test_dca_scenario_adds_the_same_amount_every_week(report):
    full = report["scenarios"]["dca"]["runs"]["buy_hold"]["full"]
    assert full["deposited"] == 50 * math.ceil(full["days"] / 7)


def test_pick_best_looks_only_at_the_training_period():
    runs = {
        "a": {"train": {"sharpe": 0.4}, "test": {"sharpe": 2.0}},
        "b": {"train": {"sharpe": 0.9}, "test": {"sharpe": -1.0}},
    }
    assert research.pick_best(runs, ["a", "b"]) == "b"


def test_the_winner_is_the_best_training_sharpe_of_all(report):
    runs = report["scenarios"]["lump"]["runs"]
    best = max(runs, key=lambda k: runs[k]["train"]["sharpe"])
    assert report["selection"]["winner"] == best


def test_report_is_strict_json_for_the_browser(report):
    json.dumps(report, allow_nan=False)
