"""Configuración del bot (solbot.toml). Sin archivo, el bot corre en papel con valores seguros.

Los límites de dinero real viven SOLO en este archivo: la interfaz web los muestra pero no
puede cambiarlos. Una clave mal escrita es un error (no se ignora en silencio).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from . import strategies as st


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class LiveLimits:
    wallet_dir: str = "~/.solana-paper-bot"  # la misma wallet de PolloTraderBot
    rpc_env_file: str = "~/.config/PolloTraderBot/config.env"  # de aquí se lee SOLANA_RPC_URL
    budget_usd: float = 25.0  # valor máximo de la wallet que el bot toma para administrar
    max_trade_usd: float = 25.0  # tope por operación
    min_sol_reserve: float = 0.03  # SOL que nunca se toca (comisiones y rentas)
    max_slippage_bps: int = 100
    max_price_impact_bps: int = 100
    max_deviation_bps: int = 150  # precio de Jupiter vs. precio con el que decidió el bot
    max_trades_per_day: int = 2


@dataclass(frozen=True)
class Config:
    strategy: st.Strategy = field(default_factory=lambda: st.make("trend_sma", n=50))
    mode: str = "paper"
    paper_start_usd: float = 1000.0
    paper_cost_bps: float = 10.0  # costo extra supuesto en papel, sobre la cotización real de Jupiter
    min_trade_usd: float = 1.0
    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = 8787
    live: LiveLimits = field(default_factory=LiveLimits)


POSITIVE = {"budget_usd", "max_trade_usd", "max_slippage_bps", "max_price_impact_bps", "max_deviation_bps",
            "max_trades_per_day", "paper_start_usd", "min_trade_usd", "port"}
NON_NEGATIVE = {"min_sol_reserve", "paper_cost_bps"}


def _check_number(name: str, value) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{name} debe ser un número.")
    if name in POSITIVE and value <= 0:
        raise ConfigError(f"{name} debe ser mayor que 0.")
    if name in NON_NEGATIVE and value < 0:
        raise ConfigError(f"{name} no puede ser negativo.")


def _section(raw: dict, name: str, allowed: set[str]) -> dict:
    values = raw.get(name, {})
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise ConfigError(f"Claves desconocidas en [{name}]: {', '.join(unknown)}")
    return values


def load(path: str | Path = "solbot.toml") -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text()) if path.exists() else {}
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} no es TOML válido: {exc}") from None

    strategy_raw = _section(raw, "strategy", {"family", "params"})
    bot = _section(raw, "bot", {"mode", "paper_start_usd", "paper_cost_bps", "min_trade_usd", "data_dir"})
    server = _section(raw, "server", {"host", "port"})
    live_raw = _section(raw, "live", {f.name for f in fields(LiveLimits)})

    family = strategy_raw.get("family", "trend_sma")
    params = strategy_raw.get("params", {"n": 50} if family == "trend_sma" and "family" not in strategy_raw else {})
    try:
        strategy = st.make(family, **params)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None

    mode = bot.get("mode", "paper")
    if mode not in ("paper", "live"):
        raise ConfigError(f'mode debe ser "paper" o "live", no {mode!r}.')

    for name, value in {**live_raw, **{k: v for k, v in bot.items() if k not in ("mode", "data_dir")}, **server}.items():
        if name not in ("wallet_dir", "rpc_env_file", "host"):
            _check_number(name, value)

    return Config(
        strategy=strategy,
        mode=mode,
        paper_start_usd=float(bot.get("paper_start_usd", 1000.0)),
        paper_cost_bps=float(bot.get("paper_cost_bps", 10.0)),
        min_trade_usd=float(bot.get("min_trade_usd", 1.0)),
        data_dir=Path(bot.get("data_dir", "data")),
        host=server.get("host", "127.0.0.1"),
        port=int(server.get("port", 8787)),
        live=LiveLimits(**live_raw),
    )
