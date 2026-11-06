"""El bot: una decisión por vela diaria, con el mismo código de estrategia que el backtest.

Cada ciclo (la interfaz lo corre cada minuto; también se puede correr desde cron):
1. Si está pausado, hay parada de emergencia o una orden sin confirmar: no hace nada.
2. Si ya decidió con la última vela cerrada: no hace nada (nunca opera dos veces la misma señal).
3. Calcula el peso objetivo con la vela recién cerrada y rebalancea su parte (sleeve) hacia él.
"""

from __future__ import annotations

import math
from dataclasses import asdict
from typing import Callable

import pandas as pd

from . import strategies as st
from .jupiter import SOL_MINT, USDC_MINT
from .orders import Fill, Intent, Refused, RetryLater, Sleeve, Unknown
from .state import Store, now_iso

MAX_TRADES_KEPT = 1000
MAX_MARKS_KEPT = 5000


def plan(strategy: st.Strategy, df: pd.DataFrame, sleeve: Sleeve, price: float, min_trade_usd: float = 1.0,
         max_trade_usd: float | None = None) -> dict:
    """Qué haría el bot ahora mismo. No ejecuta nada."""
    target = strategy.weights(df).iloc[-1]
    current = sleeve.weight(price)
    total = sleeve.value(price)
    base = {
        "bar": df.index[-1].date().isoformat(),
        "close": float(df["close"].iloc[-1]),
        "price": price,
        "target": None if pd.isna(target) else float(target),
        "current": current,
        "value": total,
        "action": "hold",
        "usd": 0.0,
        "qty": 0.0,
    }
    if pd.isna(target):
        return {**base, "action": "wait", "reason": "La estrategia todavía no tiene datos suficientes."}
    target = float(target)
    if abs(target - current) <= strategy.band + 1e-9:
        return {**base, "reason": f"La señal pide {target:.0%} en SOL y ya hay {current:.0%}: no se opera."}

    if target > current:
        usd = sleeve.usdc if target >= 1 else min(sleeve.usdc, target * total - sleeve.sol * price)
        action, qty = "buy", usd
        reason = f"La señal pide {target:.0%} en SOL y hay {current:.0%}: comprar SOL con {usd:,.2f} USDC."
    else:
        qty = sleeve.sol if target <= 0 else min(sleeve.sol, (sleeve.sol * price - target * total) / price)
        usd = qty * price
        action = "sell"
        reason = f"La señal pide {target:.0%} en SOL y hay {current:.0%}: vender {qty:.4f} SOL (≈ {usd:,.2f} USD)."
    if usd < min_trade_usd:
        return {**base, "reason": f"La diferencia ({usd:.2f} USD) es menor al mínimo por operación: no se opera."}
    if max_trade_usd and usd > max_trade_usd:
        qty *= max_trade_usd / usd
        usd = max_trade_usd
        reason += f" Por el tope por operación, hoy solo {usd:,.2f} USD; el resto en los próximos días."
    return {**base, "action": action, "usd": usd, "qty": qty, "reason": reason}


def intent_for(decision: dict) -> Intent:
    bar, price = decision["bar"], decision["price"]
    if decision["action"] == "buy":
        amount = math.floor(decision["qty"] * 1e6)
        return Intent(f"{bar}-buy", bar, "buy", USDC_MINT, SOL_MINT, amount, amount / 1e6, amount / 1e6, price)
    amount = math.floor(decision["qty"] * 1e9)
    return Intent(f"{bar}-sell", bar, "sell", SOL_MINT, USDC_MINT, amount, amount / 1e9, amount / 1e9 * price, price)


def apply_fill(sleeve: Sleeve, fill: Fill) -> Sleeve:
    if fill.side == "buy":
        sol, usdc = sleeve.sol + fill.sol_qty, sleeve.usdc - fill.usdc_qty
    else:
        sol, usdc = sleeve.sol - fill.sol_qty, sleeve.usdc + fill.usdc_qty
    return Sleeve(sol=max(sol, 0.0) if sol > -1e-9 else sol, usdc=max(usdc, 0.0) if usdc > -1e-6 else usdc)


class Bot:
    def __init__(
        self,
        *,
        strategy: st.Strategy,
        store: Store,
        executor,
        candles: Callable[[], pd.DataFrame],
        price: Callable[[], float],
        now: Callable[[], pd.Timestamp] = lambda: pd.Timestamp.now(tz="UTC"),
        min_trade_usd: float = 1.0,
        max_trade_usd: float | None = None,
        retry_minutes: int = 5,
        max_attempts: int = 6,
        notify: Callable[[dict], None] | None = None,
    ):
        self.strategy = strategy
        self.store = store
        self.executor = executor
        self.candles = candles
        self.price = price
        self.now = now
        self.min_trade_usd = min_trade_usd
        self.max_trade_usd = max_trade_usd
        self.retry_minutes = retry_minutes
        self.max_attempts = max_attempts
        self.notify = notify

    # ---- controles (la interfaz los usa) -------------------------------------------------

    def set_running(self, running: bool) -> dict:
        with self.store.lock():
            state = self.store.load()
            state["running"] = running
            state["notice"] = "Bot en marcha." if running else "Bot en pausa: no abre operaciones."
            state["retry_at"] = None
            self.store.save(state)
            self.store.journal({"event": "start" if running else "pause"})
        self._emit({"kind": "started" if running else "paused"})
        return state

    def resolve_pending(self, executed: bool) -> dict:
        """Tras revisar en Solscan una orden de resultado incierto: ¿se ejecutó o no?"""
        with self.store.lock():
            state = self.store.load()
            pending = state.get("pending")
            if not pending:
                return state
            if executed:
                sleeve = Sleeve(**state["sleeve"])
                if pending["side"] == "buy":
                    sol, usdc = pending["quote_out"] / 1e9, pending["amount"] / 1e6
                else:
                    sol, usdc = pending["amount"] / 1e9, pending["quote_out"] / 1e6
                fill = Fill(pending["side"], sol, usdc, usdc / sol, 0.0, pending.get("signature") or pending.get("tx_id"), "confirmada a mano")
                state["sleeve"] = asdict(apply_fill(sleeve, fill))
                state["trades"].append(self._trade_record(pending["bar"], fill))
                state["last_bar"] = pending["bar"]
                state["notice"] = "Orden marcada como ejecutada y registrada con los montos de la cotización."
            else:
                state["notice"] = "Orden marcada como NO ejecutada: el bot volverá a intentar la señal."
            state["pending"] = None
            self.store.save(state)
            self.store.journal({"event": "resolved", "executed": executed, **pending})
            return state

    def preview(self) -> dict:
        """La próxima acción con los datos de ahora, sin operar."""
        state = self.store.load()
        try:
            df = self.candles()
            price = float(self.price())
            sleeve = Sleeve(**state["sleeve"]) if state["sleeve"] else self.executor.fund(price)
            return plan(self.strategy, df, sleeve, price, self.min_trade_usd, self.max_trade_usd)
        except (RetryLater, Refused) as exc:
            return {"action": "error", "reason": str(exc)}

    # ---- ciclo -------------------------------------------------------------------------------

    def cycle(self) -> dict:
        with self.store.lock():
            state = self.store.load()
            now = self.now()
            state["last_cycle"] = now.isoformat()
            state["strategy"] = state["strategy"] or self.strategy.id
            state["outcome"] = self._step(state, now)
            self.store.save(state)
            return state

    def _step(self, state: dict, now: pd.Timestamp) -> str:
        if not state["running"]:
            return "paused"
        if self.executor.kill():
            state["notice"] = "Parada de emergencia activa (archivo KILL): el bot no opera."
            return "killed"
        if state["pending"]:
            return "pending"
        if state["retry_at"] and now < pd.Timestamp(state["retry_at"]):
            return "waiting"

        bar = None
        intent = None
        sent = False
        try:
            df = self.candles()
            bar = df.index[-1].date().isoformat()
            if state["last_bar"] == bar:
                return "up_to_date"
            price = float(self.price())
            if state["sleeve"] is None:
                sleeve = self.executor.fund(price)
                state["sleeve"] = asdict(sleeve)
                state["start"] = {"date": bar, "at": now.isoformat(), "value_usd": sleeve.value(price), "price": price}
            sleeve = Sleeve(**state["sleeve"])

            decision = plan(self.strategy, df, sleeve, price, self.min_trade_usd, self.max_trade_usd)
            if decision["action"] in ("buy", "sell"):
                intent = intent_for(decision)
                sent = True  # desde aquí, un error inesperado puede significar que la orden sí salió
                fill = self.executor.execute(intent, sleeve)
                sleeve = apply_fill(sleeve, fill)
                state["sleeve"] = asdict(sleeve)
                state["trades"] = (state["trades"] + [self._trade_record(bar, fill)])[-MAX_TRADES_KEPT:]
                self.store.journal({"event": "fill", "id": intent.id, **asdict(fill)})
                verb = "Compró" if fill.side == "buy" else "Vendió"
                state["notice"] = f"{verb} {fill.sol_qty:.4f} SOL a {fill.price:,.2f} USDC. {decision['reason']}"
                self._emit({"kind": "fill", "bar": bar, "side": fill.side, "sol": fill.sol_qty, "usdc": fill.usdc_qty,
                            "price": fill.price, "signature": fill.signature, "reason": decision["reason"],
                            **self._value(state, sleeve, price)})
            else:
                state["notice"] = decision["reason"]
                self._emit({"kind": "hold", "bar": bar, "reason": decision["reason"], **self._value(state, sleeve, price)})
            self._close_bar(state, bar, sleeve, price)
            return decision["action"]

        except RetryLater as exc:
            return self._retry(state, now, bar, str(exc))
        except Refused as exc:
            state["notice"] = f"Orden cancelada: {exc}"
            self._emit({"kind": "refused", "message": str(exc)})
            if state["sleeve"] is None:  # no hubo fondos que administrar: requiere acción del usuario
                state["running"] = False
                return "refused"
            if bar:
                self._close_bar(state, bar, Sleeve(**state["sleeve"]), None)
            return "refused"
        except Unknown as exc:
            return self._pause_unknown(state, now, str(exc), exc.pending)
        except Exception as exc:  # error inesperado (red, datos, bug)
            if sent and self.executor.mode == "live":
                pending = {"id": intent.id, "bar": intent.bar, "side": intent.side, "amount": intent.amount,
                           "quote_out": None, "reference_price": intent.reference_price}
                return self._pause_unknown(state, now, f"Error inesperado después de enviar la orden: {exc!r}", pending)
            return self._retry(state, now, bar, f"Error inesperado: {exc!r}")

    def _retry(self, state: dict, now: pd.Timestamp, bar: str | None, message: str) -> str:
        state["attempts"] = state.get("attempts", 0) + 1
        state["error"] = message
        if bar and state["attempts"] >= self.max_attempts:
            state["notice"] = f"Se omitió la señal del {bar} tras {state['attempts']} intentos fallidos."
            self._emit({"kind": "skipped", "message": f"{state['notice']} Último error: {message}"})
            self._close_bar(state, bar, Sleeve(**state["sleeve"]) if state["sleeve"] else None, None)
            return "skipped"
        state["retry_at"] = (now + pd.Timedelta(minutes=self.retry_minutes)).isoformat()
        return "retry"

    def _pause_unknown(self, state: dict, now: pd.Timestamp, message: str, pending: dict) -> str:
        state["pending"] = {**pending, "message": message, "at": now.isoformat()}
        state["running"] = False
        state["notice"] = message
        self.store.journal({"event": "unknown", **state["pending"]})
        self._emit({"kind": "unknown", "message": message})
        return "unknown"

    def _value(self, state: dict, sleeve: Sleeve, price: float) -> dict:
        value = sleeve.value(price)
        start = state.get("start") or {}
        base = start.get("value_usd")
        return {"value": value, "pnl_pct": value / base - 1 if base else None}

    def _emit(self, event: dict) -> None:
        """Avisa (Telegram) sin que un fallo del aviso pueda afectar al ciclo."""
        if not self.notify:
            return
        try:
            self.notify({"mode": self.executor.mode, "strategy": self.strategy.name, **event})
        except Exception:
            pass

    def _close_bar(self, state: dict, bar: str, sleeve: Sleeve | None, price: float | None) -> None:
        state["last_bar"] = bar
        if sleeve is not None and price:
            state["marks"] = (state["marks"] + [[bar, round(sleeve.value(price), 6), price]])[-MAX_MARKS_KEPT:]
        state["error"] = None
        state["retry_at"] = None
        state["attempts"] = 0

    def _trade_record(self, bar: str, fill: Fill) -> dict:
        return {
            "bar": bar, "at": now_iso(), "mode": self.executor.mode, "side": fill.side,
            "sol": fill.sol_qty, "usdc": fill.usdc_qty, "price": fill.price, "fee_usd": fill.fee_usd,
            "signature": fill.signature, "route": fill.route,
        }
