"""Tipos de una orden: lo que el bot administra (Sleeve), lo que quiere hacer (Intent) y lo que pasó (Fill).

Y los tres desenlaces que no son un fill:
- RetryLater: falla pasajera (red, límite de peticiones). Se reintenta la misma señal más tarde.
- Refused: una validación la canceló. No se movió dinero; la señal de hoy se omite.
- Unknown: la orden salió pero no se sabe si se ejecutó. El bot se pausa hasta que alguien revise.
"""

from __future__ import annotations

from dataclasses import dataclass


class RetryLater(Exception):
    pass


class Refused(Exception):
    pass


class Unknown(Exception):
    def __init__(self, message: str, pending: dict | None = None):
        super().__init__(message)
        self.pending = pending or {}


@dataclass
class Sleeve:
    """Parte de la wallet que administra el bot. En papel es todo el portafolio ficticio."""

    sol: float
    usdc: float

    def value(self, price: float) -> float:
        return self.sol * price + self.usdc

    def weight(self, price: float) -> float:
        total = self.value(price)
        return self.sol * price / total if total > 0 else 0.0


@dataclass(frozen=True)
class Intent:
    id: str  # fecha de la vela + lado: una sola orden por señal
    bar: str
    side: str  # "buy" = USDC→SOL, "sell" = SOL→USDC
    input_mint: str
    output_mint: str
    amount: int  # unidades mínimas del token de entrada (lamports o micro-USDC)
    amount_ui: float  # mismo monto en SOL o USDC
    usd: float  # valor aproximado de la orden
    reference_price: float  # precio con el que decidió el bot (USDC por SOL)


@dataclass(frozen=True)
class Fill:
    side: str
    sol_qty: float  # SOL comprados o vendidos (positivo)
    usdc_qty: float  # USDC gastados o recibidos (positivo)
    price: float  # USDC por SOL efectivo
    fee_usd: float
    signature: str | None = None
    route: str = ""
