"""Quién ejecuta una orden: PaperExecutor (simulado) o LiveExecutor (dinero real).

Los dos reciben la misma orden del mismo motor de decisión; solo cambia cómo se llena.

LiveExecutor, en este orden y sin saltarse pasos:
 1. Parada de emergencia (archivo KILL) y tope de operaciones por día.
 2. Saldos reales de la wallet: que cuadren con lo que el bot cree tener y respeten la reserva.
 3. /order de Jupiter con la wallet como `taker`.
 4. Validación de la cotización (guards.check_order).
 5. Simulación de la transacción en el RPC.
 6. Diario: se anota la orden ANTES de firmar.
 7. Se abre la llave, se firma localmente y se manda a /execute.
 8. Diario: se anota el resultado. Si no se sabe si se ejecutó, se pausa (Unknown).
La llave solo se abre en el paso 7: si algo falla antes, nunca se toca.
"""

from __future__ import annotations

import math
from typing import Callable

from .config import LiveLimits
from .guards import check_order, check_wallet
from .jupiter import USDC_MINT, JupiterError, order_price
from .orders import Fill, Intent, Refused, RetryLater, Sleeve, Unknown
from .rpc import RpcError
from .wallet import WalletError, sign_partial, transaction_id, verify_own_signature

LANDED = ("processed", "confirmed", "finalized")
# Códigos de /execute en los que la transacción no llegó a la cadena y vale la pena reintentar con
# una cotización nueva: no aterrizó, venció el bloque o venció la cotización del market maker.
RETRYABLE_CODES = {-1000, -1004, -1005, -2000, -2003}


def _route(order: dict) -> str:
    labels = [step.get("swapInfo", {}).get("label", "?") for step in order.get("routePlan") or []]
    return " + ".join(dict.fromkeys(labels))


def _fill(intent: Intent, amount_in: float, amount_out: float, fee_usd: float, signature: str | None, route: str) -> Fill:
    if intent.side == "sell":
        sol, usdc = amount_in / 1e9, amount_out / 1e6
    else:
        usdc, sol = amount_in / 1e6, amount_out / 1e9
    return Fill(intent.side, sol, usdc, usdc / sol, fee_usd, signature, route)


class PaperExecutor:
    """Llena al precio de una cotización REAL de Jupiter (sin firmar nada), menos un costo supuesto."""

    mode = "paper"

    def __init__(self, jupiter, cost_bps: float = 10.0, start_usd: float = 1000.0, kill: Callable[[], bool] = lambda: False):
        self.jupiter = jupiter
        self.cost_bps = cost_bps
        self.start_usd = start_usd
        self._kill = kill

    def kill(self) -> bool:
        return self._kill()

    def fund(self, price: float) -> Sleeve:
        return Sleeve(sol=0.0, usdc=self.start_usd)

    def execute(self, intent: Intent, sleeve: Sleeve) -> Fill:
        try:
            order = self.jupiter.order(intent.input_mint, intent.output_mint, intent.amount)
        except JupiterError as exc:
            raise RetryLater(str(exc)) from None
        quoted = int(order.get("outAmount") or 0)
        if quoted <= 0:
            raise RetryLater("Jupiter no devolvió una cotización válida.")
        net = quoted * (1 - self.cost_bps / 10_000)
        lost = quoted - net
        fee_usd = lost / 1e6 if intent.side == "sell" else lost / 1e9 * intent.reference_price
        return _fill(intent, intent.amount, net, fee_usd, None, _route(order))


class LiveExecutor:
    mode = "live"

    def __init__(
        self,
        *,
        jupiter,
        rpc,
        limits: LiveLimits,
        address: str,
        load_key: Callable,
        journal: Callable[[dict], None],
        kill: Callable[[], bool],
        trades_today: Callable[[], int],
    ):
        self.jupiter = jupiter
        self.rpc = rpc
        self.limits = limits
        self.address = address
        self.load_key = load_key
        self.journal = journal
        self._kill = kill
        self.trades_today = trades_today

    def kill(self) -> bool:
        return self._kill()

    def wallet(self) -> tuple[float, float]:
        try:
            sol = self.rpc.sol_balance(self.address) / 1e9
            usdc = self.rpc.token_balance(self.address, USDC_MINT) / 1e6
        except RpcError as exc:
            raise RetryLater(str(exc)) from None
        return sol, usdc

    def fund(self, price: float) -> Sleeve:
        """Toma de la wallet hasta `budget_usd` para administrar: primero USDC, luego SOL libre.
        La reserva de SOL nunca entra."""
        sol, usdc = self.wallet()
        take_usdc = min(usdc, self.limits.budget_usd)
        free_sol = max(0.0, sol - self.limits.min_sol_reserve)
        take_sol = min(free_sol, max(0.0, self.limits.budget_usd - take_usdc) / price)
        sleeve = Sleeve(sol=math.floor(take_sol * 1e9) / 1e9, usdc=math.floor(take_usdc * 1e6) / 1e6)
        if sleeve.value(price) < 1.0:
            raise Refused(
                f"La wallet no tiene fondos libres para el bot: {sol:.4f} SOL (reserva {self.limits.min_sol_reserve} SOL) "
                f"y {usdc:.2f} USDC."
            )
        return sleeve

    def _prepare(self, intent: Intent, sleeve: Sleeve, *, count_today: bool) -> dict:
        """Pasos 1 a 5: devuelve una cotización validada y simulada, lista para firmar."""
        if self._kill():
            raise Refused("Parada de emergencia activa (archivo KILL).")
        if count_today:
            done_today = self.trades_today()
            if done_today >= self.limits.max_trades_per_day:
                raise Refused(f"Ya se hicieron {done_today} operaciones hoy; el máximo por día es {self.limits.max_trades_per_day}.")

        sol, usdc = self.wallet()
        problems = check_wallet(intent, sol, usdc, sleeve, self.limits)
        if problems:
            raise Refused(" ".join(problems))

        try:
            order = self.jupiter.order(intent.input_mint, intent.output_mint, intent.amount, taker=self.address)
        except JupiterError as exc:
            raise RetryLater(str(exc)) from None
        problems = check_order(order, intent, self.address, self.limits)
        if problems:
            raise Refused(" ".join(problems))

        try:
            simulation = self.rpc.simulate(order["transaction"])
        except RpcError as exc:
            raise RetryLater(f"No se pudo simular la transacción: {exc}") from None
        if simulation:
            raise Refused(f"La simulación de la transacción falló: {simulation}")
        return order

    def rehearse(self, intent: Intent, sleeve: Sleeve) -> dict:
        """Todo el camino de una orden real con la wallet de verdad, salvo enviarla."""
        order = self._prepare(intent, sleeve, count_today=False)
        try:
            signed = sign_partial(order["transaction"], self.load_key())
        except WalletError as exc:
            raise Refused(f"No se pudo firmar: {exc}") from None
        return {
            "price": order_price(order),
            "in_amount": int(order["inAmount"]),
            "out_amount": int(order["outAmount"]),
            "route": _route(order),
            "router": order.get("router"),
            "slippage_bps": order.get("slippageBps"),
            "price_impact_pct": float(order.get("priceImpactPct") or 0) * 100,
            "fee_bps": order.get("feeBps"),
            "simulation": "ok",
            "signature_ok": verify_own_signature(signed, self.address),
        }

    def execute(self, intent: Intent, sleeve: Sleeve) -> Fill:
        order = self._prepare(intent, sleeve, count_today=True)

        self.journal({
            "event": "intent", "id": intent.id, "side": intent.side, "amount": intent.amount,
            "usd": round(intent.usd, 2), "reference_price": intent.reference_price,
            "quote_out": order.get("outAmount"), "request_id": order.get("requestId"), "router": order.get("router"),
        })
        try:
            signed = sign_partial(order["transaction"], self.load_key())
        except WalletError as exc:
            raise Refused(f"No se pudo firmar: {exc}") from None
        tx_id = transaction_id(signed)
        self.journal({"event": "signed", "id": intent.id, "tx_id": tx_id})

        result = self.jupiter.execute(signed, order["requestId"])
        self.journal({"event": "result", "id": intent.id,
                      **{k: result.get(k) for k in ("status", "code", "error", "signature", "totalInputAmount", "totalOutputAmount")}})

        pending = {
            "id": intent.id, "bar": intent.bar, "side": intent.side, "amount": intent.amount,
            "quote_out": int(order["outAmount"]), "reference_price": intent.reference_price,
            "request_id": order.get("requestId"), "tx_id": tx_id, "signature": result.get("signature"),
        }
        status = result.get("status")
        if status == "Success":
            amount_in = int(result.get("totalInputAmount") or result.get("inputAmountResult") or intent.amount)
            amount_out = int(result.get("totalOutputAmount") or result.get("outputAmountResult") or order["outAmount"])
            fee_usd = intent.usd * float(order.get("feeBps") or 0) / 10_000
            return _fill(intent, amount_in, amount_out, fee_usd, result.get("signature") or tx_id, _route(order))

        if status == "Failed":
            signature = result.get("signature")
            landed = None
            if signature:
                try:
                    landed = self.rpc.signature_status(signature)
                except RpcError:
                    raise Unknown("Jupiter reportó un fallo y no se pudo confirmar en la cadena. Revisa la wallet en Solscan.", pending) from None
            if landed in LANDED:
                raise Unknown(f"Jupiter reportó un fallo, pero la transacción aparece «{landed}» en la cadena. Revisa en Solscan.", pending)
            if result.get("code") in RETRYABLE_CODES:
                raise RetryLater(f"La orden no llegó a la cadena ({result.get('error')}); se reintenta con una cotización nueva.")
            raise Refused(f"Jupiter no pudo ejecutar la orden (código {result.get('code')}): {result.get('error')}")

        raise Unknown(
            f"No se sabe si la orden se ejecutó ({result.get('error') or 'sin respuesta'}). Revisa la wallet en Solscan antes de reanudar.",
            pending,
        )
