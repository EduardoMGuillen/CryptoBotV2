"""Validaciones antes de firmar una orden real. Cualquier problema cancela la orden.

Son funciones puras: reciben la cotización, la orden, los saldos y los límites, y devuelven
la lista de problemas en español (vacía = se puede firmar).
"""

from __future__ import annotations

from .config import LiveLimits
from .jupiter import JupiterError, order_price
from .orders import Intent, Sleeve

TOLERANCE = 1e-6
# SOL mínimo para pagar comisiones de red. Las comisiones salen de la reserva, así que la
# reserva baja un poco con cada operación: comprar solo exige poder pagar la comisión.
FEE_FLOOR_SOL = 0.005


def check_order(order: dict, intent: Intent, taker: str, limits: LiveLimits) -> list[str]:
    if order.get("errorCode") or not order.get("transaction"):
        detail = order.get("errorMessage") or order.get("error") or "sin detalle"
        return [f"Jupiter no devolvió una transacción: {detail}"]

    problems = []
    if (order.get("inputMint"), order.get("outputMint")) != (intent.input_mint, intent.output_mint):
        problems.append("La cotización es de otro par de tokens.")
    if int(order.get("inAmount", -1)) != intent.amount:
        problems.append("El monto de la cotización no coincide con el de la orden.")
    if order.get("taker") and order["taker"] != taker:
        problems.append("La transacción es para otra wallet.")

    try:
        price = order_price(order)
    except JupiterError as exc:
        problems.append(str(exc))
    else:
        deviation = abs(price / intent.reference_price - 1) * 10_000
        if deviation > limits.max_deviation_bps:
            problems.append(
                f"El precio de Jupiter ({price:.2f}) se aleja {deviation / 100:.2f}% del precio de referencia "
                f"({intent.reference_price:.2f}); el máximo es {limits.max_deviation_bps / 100:.2f}%."
            )

    impact = abs(float(order.get("priceImpactPct") or 0)) * 10_000
    if impact > limits.max_price_impact_bps:
        problems.append(f"El impacto de precio ({impact / 100:.2f}%) supera el máximo ({limits.max_price_impact_bps / 100:.2f}%).")

    slippage = int(order.get("slippageBps") or 0)
    if slippage > limits.max_slippage_bps:
        problems.append(f"El slippage permitido ({slippage / 100:.2f}%) supera el máximo ({limits.max_slippage_bps / 100:.2f}%).")

    if intent.usd > limits.max_trade_usd:
        problems.append(f"La orden ({intent.usd:.2f} USD) supera el máximo por operación ({limits.max_trade_usd:.2f} USD).")
    return problems


def check_wallet(intent: Intent, wallet_sol: float, wallet_usdc: float, sleeve: Sleeve, limits: LiveLimits) -> list[str]:
    reserve = limits.min_sol_reserve
    problems = []
    if intent.side == "sell":
        if wallet_sol + TOLERANCE < sleeve.sol:
            problems.append(
                f"El saldo no cuadra: la wallet tiene {wallet_sol:.4f} SOL y el bot administra {sleeve.sol:.4f} SOL. "
                "¿Otro proceso usó esos fondos?"
            )
        elif wallet_sol - intent.amount_ui < reserve - TOLERANCE:
            problems.append(
                f"Vender {intent.amount_ui:.4f} SOL dejaría la wallet con {wallet_sol - intent.amount_ui:.4f} SOL, "
                f"por debajo de la reserva de {reserve} SOL."
            )
    else:
        if wallet_usdc + TOLERANCE < intent.amount_ui:
            problems.append(f"La wallet tiene {wallet_usdc:.2f} USDC y la orden necesita {intent.amount_ui:.2f} USDC.")
        if wallet_sol + TOLERANCE < FEE_FLOOR_SOL:
            problems.append(f"La wallet tiene {wallet_sol:.4f} SOL; hacen falta al menos {FEE_FLOOR_SOL} SOL para comisiones.")
    return problems
