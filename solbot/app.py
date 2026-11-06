"""Arma el bot completo a partir de la configuración y prepara lo que muestra la interfaz.

Nada de lo que sale de aquí contiene la llave privada: la interfaz solo ve la dirección
pública, los saldos y la URL del RPC enmascarada.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from . import market, notify, research, wallet
from . import strategies as st
from .backtest import run
from .bot import Bot
from .config import Config, ConfigError
from .executors import LiveExecutor, PaperExecutor
from .jupiter import USDC_MINT, Jupiter
from .orders import Sleeve
from .rpc import RpcError, SolanaRpc, mask_url, resolve_url
from .state import Store

SYMBOL = "SOLUSDT"
TICKERS = ["SOLUSDT", "BTCUSDT", "ETHUSDT", "JUPUSDT", "BONKUSDT", "WIFUSDT"]
RESEARCH_VERSION = 1


class Cache:
    """Valores con vencimiento, para no pedirle lo mismo a Binance o al RPC en cada refresco."""

    def __init__(self):
        self._data: dict = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float, compute):
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        value = compute()
        with self._lock:
            self._data[key] = (time.monotonic() + ttl, value)
        return value


class App:
    def __init__(self, cfg: Config, *, env=None, candles=None, price=None, rpc=None, jupiter=None, now=None, telegram=None):
        env = os.environ if env is None else env
        self.cfg = cfg
        self.now = now or (lambda: pd.Timestamp.now(tz="UTC"))
        self.cache = Cache()
        self.store = Store(cfg.data_dir / cfg.mode, cfg.mode)
        self.jupiter = jupiter or Jupiter(api_key=env.get("JUPITER_API_KEY") or None)
        self.rpc_url = resolve_url(env, cfg.live.rpc_env_file)
        self.rpc = rpc or SolanaRpc(self.rpc_url)
        self._candles = candles or (lambda: market.load_daily(SYMBOL, cfg.data_dir / "SOLUSDT_1d.csv"))
        self._price = price or (lambda: market.last_price(SYMBOL))
        self._research_lock = threading.Lock()
        try:
            self.address = wallet.read_address(cfg.live.wallet_dir)
        except wallet.WalletError:
            self.address = None

        if cfg.mode == "live":
            if not self.address:
                raise ConfigError(f"Modo real sin wallet: no se encontró wallet.address en {cfg.live.wallet_dir}.")
            self.executor = LiveExecutor(
                jupiter=self.jupiter, rpc=self.rpc, limits=cfg.live, address=self.address,
                load_key=lambda: wallet.load_keypair(cfg.live.wallet_dir), journal=self.store.journal,
                kill=self.kill_switch, trades_today=self.trades_today,
            )
        else:
            self.executor = PaperExecutor(self.jupiter, cfg.paper_cost_bps, cfg.paper_start_usd, kill=self.kill_switch)
        self.bot = Bot(strategy=cfg.strategy, store=self.store, executor=self.executor, candles=self.candles,
                       price=self.price, now=self.now, min_trade_usd=cfg.min_trade_usd,
                       max_trade_usd=cfg.live.max_trade_usd if cfg.mode == "live" else None)
        # Telegram: las mismas variables y el mismo config.env que PolloTraderBot
        self.telegram = telegram or notify.Telegram(env=env, env_file=cfg.live.rpc_env_file)
        self.bot.notify = self._notify

    def _notify(self, event: dict) -> None:
        threading.Thread(target=self.telegram.send, args=(notify.message(event),), daemon=True).start()

    # ---- datos ------------------------------------------------------------------------------

    def candles(self) -> pd.DataFrame:
        return self.cache.get("candles", 60, self._candles)

    def price(self) -> float:
        return self.cache.get("price", 10, self._price)

    def kill_switch(self) -> bool:
        files = [self.cfg.data_dir / "KILL"]
        if self.cfg.mode == "live":
            files.append(wallet.expand(self.cfg.live.wallet_dir) / "KILL")  # junto a la wallet
        return any(f.exists() for f in files)

    def trades_today(self) -> int:
        today = self.now().date().isoformat()
        return sum(1 for t in self.store.load()["trades"] if str(t.get("at", "")).startswith(today))

    # ---- acciones ---------------------------------------------------------------------------

    def start(self, confirm: str | None = None) -> dict:
        if self.cfg.mode == "live" and (not confirm or confirm.strip() != self.address[-4:]):
            raise ValueError("Para operar con dinero real escribe los últimos 4 caracteres de tu wallet.")
        self.bot.set_running(True)
        return self.status()

    def pause(self) -> dict:
        self.bot.set_running(False)
        return self.status()

    def run_now(self) -> dict:
        self.bot.cycle()
        return self.status()

    def resolve(self, executed: bool) -> dict:
        self.bot.resolve_pending(bool(executed))
        return self.status()

    # ---- lo que ve la interfaz --------------------------------------------------------------

    def status(self) -> dict:
        state = self.store.load()
        strategy = self.cfg.strategy
        try:
            price, price_error = float(self.price()), None
        except Exception as exc:  # sin precio la página igual debe cargar
            price, price_error = None, f"No se pudo leer el precio: {exc}"
        sleeve = Sleeve(**state["sleeve"]) if state["sleeve"] else None
        now = self.now()
        out = {
            "mode": self.cfg.mode,
            "running": state["running"],
            "strategy": {"id": strategy.id, "family": strategy.family, "name": strategy.name,
                         "description": strategy.description, "params": strategy.params},
            "price": price,
            "price_error": price_error,
            "sleeve": None if not (sleeve and price) else {
                "sol": sleeve.sol, "usdc": sleeve.usdc, "value": sleeve.value(price), "weight": sleeve.weight(price)},
            "start": state["start"],
            "performance": self._performance(state, sleeve, price),
            "signal": self._signal() if price else None,
            "notice": state["notice"],
            "error": state["error"],
            "pending": state["pending"],
            "outcome": state["outcome"],
            "last_bar": state["last_bar"],
            "last_cycle": state["last_cycle"],
            "next_decision_at": (now.floor("D") + pd.Timedelta(days=1, minutes=1)).isoformat(),
            "kill": self.kill_switch(),
            "marks": state["marks"][-730:],
            "trades": list(reversed(state["trades"][-100:])),
            "wallet": self.wallet_info(),
            "telegram": self.telegram.enabled(),
            "limits": {**asdict(self.cfg.live), "paper_start_usd": self.cfg.paper_start_usd,
                       "paper_cost_bps": self.cfg.paper_cost_bps, "min_trade_usd": self.cfg.min_trade_usd},
        }
        return research._strict(out)

    def _performance(self, state: dict, sleeve: Sleeve | None, price: float | None) -> dict | None:
        start = state["start"]
        if not (start and sleeve and price):
            return None
        value = sleeve.value(price)
        hold = start["value_usd"] / start["price"] * price
        return {
            "value": value,
            "start_value": start["value_usd"],
            "pnl": value - start["value_usd"],
            "pnl_pct": value / start["value_usd"] - 1,
            "hold_value": hold,
            "hold_pct": hold / start["value_usd"] - 1,
            "vs_hold_pct": value / hold - 1,
        }

    def _signal(self) -> dict:
        preview = self.bot.preview()
        try:
            df = self.candles()
            preview["indicators"] = {name: float(series.iloc[-1]) for name, series in self.cfg.strategy.indicators(df).items()}
        except Exception:
            preview["indicators"] = {}
        return preview

    def wallet_info(self) -> dict | None:
        if not self.address:
            return None

        def read():
            info = {
                "address": self.address,
                "explorer": f"https://solscan.io/account/{self.address}",
                "rpc": mask_url(self.rpc_url),
                "reserve": self.cfg.live.min_sol_reserve,
                "budget": self.cfg.live.budget_usd,
            }
            try:
                info["sol"] = self.rpc.sol_balance(self.address) / 1e9
                info["usdc"] = self.rpc.token_balance(self.address, USDC_MINT) / 1e6
            except RpcError as exc:
                info["error"] = str(exc)
            return info

        return self.cache.get("wallet", 30, read)

    def tickers(self) -> list[dict]:
        def read():
            try:
                return market.tickers(TICKERS)
            except Exception:
                return []

        return self.cache.get("tickers", 60, read)

    def backtest(self, refresh: bool = False) -> dict:
        strategies = st.catalog()
        if self.cfg.strategy.id not in {s.id for s in strategies}:
            strategies.append(self.cfg.strategy)
        key = [s.id for s in strategies]
        path = self.cfg.data_dir / "research.json"
        with self._research_lock:
            df = self.candles()
            last = df.index[-1].date().isoformat()
            if not refresh and path.exists():
                report = json.loads(path.read_text())
                if report.get("version") == RESEARCH_VERSION and report.get("key") == key and report["settings"]["end"] == last:
                    return {**report, "active": self.cfg.strategy.id}
            report = research.run_research(df, strategies=strategies)
            report.update(version=RESEARCH_VERSION, key=key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, allow_nan=False))
            return {**report, "active": self.cfg.strategy.id}

    def chart(self, days: int = 365) -> dict:
        df = self.candles()
        days = max(30, min(int(days), len(df)))
        window = df.iloc[-days:]
        strategy = self.cfg.strategy
        lines = {}
        for name, series in strategy.indicators(df).items():
            part = series.loc[window.index].dropna()
            lines[name] = [[d.date().isoformat(), round(float(v), 4)] for d, v in part.items()]
        simulated = run(df, strategy.weights(df), start=window.index[0], fee_rate=0.001, band=strategy.band)
        state = self.store.load()
        return research._strict({
            "strategy": strategy.name,
            "candles": [[d.date().isoformat(), r.open, r.high, r.low, r.close] for d, r in window.iterrows()],
            "lines": lines,
            "signals": [[t.date.date().isoformat(), t.side, round(t.price, 4)] for t in simulated.trades],
            "bot_trades": [[t["bar"], t["side"], t["price"]] for t in state["trades"]],
        })
