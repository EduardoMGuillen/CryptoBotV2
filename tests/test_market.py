import pandas as pd
import pytest

from solbot import market

DAY = 86_400_000
T0 = 1_735_689_600_000  # 2025-01-01 00:00 UTC en ms


def at(ms):
    return pd.Timestamp(ms, unit="ms", tz="UTC")


def kline(i, close):
    """Fila con la forma exacta de /api/v3/klines de Binance."""
    open_time = T0 + i * DAY
    return [open_time, str(close), str(close + 1), str(close - 1), str(close), "10.0",
            open_time + DAY - 1, "100.0", 5, "1.0", "1.0", "0"]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        pass


class FakeBinance:
    """Imita la paginación real: startTime inclusivo y como máximo `limit` filas."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        page = [r for r in self.rows if r[0] >= params["startTime"]][: params["limit"]]
        return FakeResponse(page)


def test_parse_drops_the_candle_that_has_not_closed_yet():
    rows = [kline(0, 10), kline(1, 11), kline(2, 12)]
    df = market.parse_klines(rows, now=at(T0 + 2 * DAY + 3_600_000))  # 01:00 del día 2
    assert df["close"].tolist() == [10.0, 11.0]
    assert df.index[0] == pd.Timestamp("2025-01-01", tz="UTC")


def test_fetch_follows_pages_until_the_last_closed_day():
    session = FakeBinance([kline(i, 100 + i) for i in range(5)])
    df = market.fetch_daily("SOLUSDT", since=at(T0), session=session, now=at(T0 + 5 * DAY), page_size=2)
    assert df["close"].tolist() == [100, 101, 102, 103, 104]
    assert [c["startTime"] for c in session.calls] == [T0, T0 + 2 * DAY, T0 + 4 * DAY]
    assert session.calls[0]["symbol"] == "SOLUSDT" and session.calls[0]["interval"] == "1d"


def test_cache_only_downloads_the_days_it_does_not_have(tmp_path):
    rows = [kline(i, 100 + i) for i in range(6)]
    path = tmp_path / "SOLUSDT_1d.csv"
    market.load_daily("SOLUSDT", path, session=FakeBinance(rows[:4]), now=at(T0 + 4 * DAY))

    second = FakeBinance(rows)
    df = market.load_daily("SOLUSDT", path, session=second, now=at(T0 + 6 * DAY))
    assert second.calls[0]["startTime"] == T0 + 4 * DAY
    assert df["close"].tolist() == [100, 101, 102, 103, 104, 105]


def test_an_up_to_date_cache_makes_no_request(tmp_path):
    rows = [kline(i, 100 + i) for i in range(3)]
    path = tmp_path / "SOLUSDT_1d.csv"
    market.load_daily("SOLUSDT", path, session=FakeBinance(rows), now=at(T0 + 3 * DAY))

    again = FakeBinance(rows)
    df = market.load_daily("SOLUSDT", path, session=again, now=at(T0 + 3 * DAY + 3_600_000))
    assert again.calls == []
    assert len(df) == 3


class FakeTickers:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if url.endswith("/ticker/24hr"):
            return FakeResponse([
                {"symbol": "SOLUSDT", "priceChange": "-1.20", "priceChangePercent": "-1.320",
                 "lastPrice": "89.50", "openPrice": "90.70", "highPrice": "92.0", "lowPrice": "88.1",
                 "volume": "1000", "quoteVolume": "89500"},
                {"symbol": "BTCUSDT", "priceChange": "500", "priceChangePercent": "0.500",
                 "lastPrice": "100500", "openPrice": "100000", "highPrice": "101000", "lowPrice": "99000",
                 "volume": "10", "quoteVolume": "1005000"},
            ])
        closes = [1, 2, 3] if params["symbol"] == "SOLUSDT" else [7, 8, 9]
        return FakeResponse([[T0 + i * 3_600_000, "0", "0", "0", str(c), "0",
                              T0 + (i + 1) * 3_600_000 - 1, "0", 0, "0", "0", "0"]
                             for i, c in enumerate(closes)])


def test_tickers_report_price_daily_change_and_a_sparkline():
    rows = market.tickers(["SOLUSDT", "BTCUSDT"], session=FakeTickers())
    assert rows == [
        {"symbol": "SOL", "price": 89.5, "change_pct": -1.32, "spark": [1.0, 2.0, 3.0]},
        {"symbol": "BTC", "price": 100500.0, "change_pct": 0.5, "spark": [7.0, 8.0, 9.0]},
    ]


class FakePrice:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return FakeResponse({"symbol": "SOLUSDT", "price": "89.33000000"})


def test_last_price_reads_the_latest_trade():
    session = FakePrice()
    assert market.last_price("SOLUSDT", session=session) == 89.33
    assert session.calls[0][0].endswith("/ticker/price") and session.calls[0][1] == {"symbol": "SOLUSDT"}
