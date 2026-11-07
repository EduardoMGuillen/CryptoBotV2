import datetime

import pandas as pd
import pytest

from solbot.jupiter import USDC_MINT
from solbot.memesim import Fill, TxSim
from solbot.stocksim import (STOCK_RULES, XSTOCKS, StockDesk, breakout, completed_closes, hold_spy, rsi2,
                             trend_sma50, weekly_rotation)


def series(values, start="2025-01-01"):
    return pd.Series([float(v) for v in values], index=pd.bdate_range(start, periods=len(values)))


def test_trend_holds_only_names_above_sma50():
    up = series(range(1, 80))
    down = series(range(80, 1, -1))
    w = trend_sma50({"NVDA": up, "TSLA": down}, names=["NVDA", "TSLA"])
    assert w == {"NVDA": 0.5}


def test_breakout_enters_on_new_high_and_exits_on_10_day_low():
    flat_then_up = series([10] * 40 + [11, 12, 13])
    assert breakout({"NVDA": flat_then_up}, names=["NVDA"]) == {"NVDA": 0.25}
    up_then_crash = series([10] * 40 + [11, 12, 13] + [5])
    assert breakout({"NVDA": up_then_crash}, names=["NVDA"]) == {}


def test_rsi2_buys_dip_in_uptrend_and_sells_on_rebound():
    base = list(range(100, 320))
    dip = series(base + [300, 280])  # dos días de caída fuerte, aún sobre la media de 200
    assert rsi2({"QQQ": dip}, names=["QQQ"]) == {"QQQ": 0.25}
    rebound = series(base + [300, 280, 330])
    assert rsi2({"QQQ": rebound}, names=["QQQ"]) == {}


def test_weekly_rotation_uses_last_friday_close():
    strong = series([100 + i for i in range(80)])
    weak = series([100 + i * 0.1 for i in range(80)])
    w = weekly_rotation({"NVDA": strong, "TSLA": weak, "MSTR": series([200 - i for i in range(80)])},
                        names=["NVDA", "TSLA", "MSTR"])
    assert w == {"NVDA": 0.5, "TSLA": 0.5}


def test_completed_closes_drops_todays_bar_before_close():
    ny = datetime.timezone(datetime.timedelta(hours=-5))
    today = datetime.datetime(2026, 11, 6, 11, 0, tzinfo=ny)
    rows = [(int(datetime.datetime(2026, 11, d, 9, 30, tzinfo=ny).timestamp()), 100.0 + d) for d in (4, 5, 6)]
    assert list(completed_closes(rows, today)) == [104.0, 105.0]
    after_close = datetime.datetime(2026, 11, 6, 17, 0, tzinfo=ny)
    assert list(completed_closes(rows, after_close)) == [104.0, 105.0, 106.0]


def test_usdc_quote_mint_for_stocks():
    calls = []

    def quote(i, o, amount):
        calls.append((i, o, amount))
        return {"outAmount": "100000000", "outUsdValue": 24.9}

    tx = TxSim(quote, lambda: 100.0, STOCK_RULES, rng=lambda: 0.99, sleep=lambda s: None, quote_mint=USDC_MINT)
    fill = tx.buy("MINT", 25.0)
    assert calls[0] == (USDC_MINT, "MINT", 25_000_000)
    assert fill.usd == pytest.approx(24.9)  # sin comisión de plataforma
    tx.sell("MINT", 100)
    assert calls[1][1] == USDC_MINT


def test_rebalance_buys_and_sells(tmp_path):
    tx = TxSim(lambda i, o, a: {"outAmount": str(int(a * 4)), "outUsdValue": a / 1e6 * 0.998}, lambda: 100.0,
               STOCK_RULES, rng=lambda: 0.99, sleep=lambda s: None, quote_mint=USDC_MINT)
    desk = StockDesk("acc_x", tmp_path, STOCK_RULES, tx)
    desk.rebalance({"NVDA": 0.5, "TSLA": 0.5}, {"NVDA": 190.0, "TSLA": 360.0}, 1000.0)
    assert sorted(p.symbol for p in desk.book.positions.values()) == ["NVDAx", "TSLAx"]
    assert desk.book.cash == pytest.approx(0.0, abs=0.2)
    desk.rebalance({"NVDA": 0.5}, {"NVDA": 190.0, "TSLA": 360.0}, 2000.0)  # TSLA sale
    assert [p.symbol for p in desk.book.positions.values()] == ["NVDAx"]
    assert desk.book.closed[0]["symbol"] == "TSLAx" and desk.book.closed[0]["reason"] == "senal"
    assert XSTOCKS["NVDA"] in desk.book.positions


def test_hold_spy():
    assert hold_spy({}) == {"SPY": 1.0}


def test_prices_retries_once_after_rate_limit(tmp_path, monkeypatch):
    from solbot import stocksim

    class Resp:
        def __init__(self, code, data):
            self.status_code, self._data = code, data

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests
                raise requests.HTTPError(f"{self.status_code}")

        def json(self):
            return self._data

    mint = XSTOCKS["SPY"]
    answers = [Resp(429, {}), Resp(200, {mint: {"usdPrice": 700.0}})]
    sim = stocksim.StockSim(tmp_path)
    monkeypatch.setattr(sim.http, "get", lambda *a, **k: answers.pop(0))
    monkeypatch.setattr(stocksim.time, "sleep", lambda s: None)
    assert sim.prices() == {"SPY": 700.0}
