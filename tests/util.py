"""Helpers compartidos por los tests: series de precios diarias hechas a mano."""

import pandas as pd


def frame(closes, opens=None, start="2024-01-01"):
    """Velas diarias en UTC. Sin `opens`, cada vela abre en su propio cierre."""
    closes = [float(c) for c in closes]
    opens = closes if opens is None else [float(o) for o in opens]
    index = pd.date_range(start, periods=len(closes), freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) for o, c in zip(opens, closes)],
            "low": [min(o, c) for o, c in zip(opens, closes)],
            "close": closes,
            "volume": [1.0] * len(closes),
        },
        index=index,
    )


def as_list(series):
    """Serie → lista de floats, con None donde hay NaN (fácil de comparar con literales)."""
    return [None if pd.isna(v) else float(v) for v in series]
