"""Cliente de la Swap API v2 de Jupiter: /order (cotización y transacción) y /execute.

Flujo de una orden real: /order con `taker` → se valida → se firma localmente → /execute.
Jupiter envía la transacción y espera su confirmación.

Sin API key el límite es 0.5 peticiones por segundo en /order: el cliente espacia las
llamadas y reintenta los 429. Reenviar a /execute la MISMA transacción firmada es seguro:
en Solana una misma transacción no puede ejecutarse dos veces.
"""

from __future__ import annotations

import time

import requests

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
DECIMALS = {SOL_MINT: 9, USDC_MINT: 6}
BASE = "https://api.jup.ag/swap/v2"


class JupiterError(Exception):
    pass


def order_price(order: dict) -> float:
    """Precio en USDC por SOL que implica una cotización (en cualquier dirección)."""
    amount_in, amount_out = int(order["inAmount"]), int(order["outAmount"])
    pair = (order["inputMint"], order["outputMint"])
    if pair == (SOL_MINT, USDC_MINT):
        return (amount_out / 1e6) / (amount_in / 1e9)
    if pair == (USDC_MINT, SOL_MINT):
        return (amount_in / 1e6) / (amount_out / 1e9)
    raise JupiterError("Par no soportado: este bot solo opera SOL/USDC.")


class Jupiter:
    def __init__(
        self,
        session=None,
        base: str = BASE,
        api_key: str | None = None,
        min_interval: float = 2.1,
        retries: int = 3,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self.session = session or requests.Session()
        self.base = base.rstrip("/")
        self.headers = {"x-api-key": api_key} if api_key else {}
        self.min_interval = min_interval
        self.retries = retries
        self.clock = clock
        self.sleep = sleep
        self._last_order: float | None = None

    def _wait_turn(self) -> None:
        if self._last_order is not None:
            wait = self.min_interval - (self.clock() - self._last_order)
            if wait > 0:
                self.sleep(wait)
        self._last_order = self.clock()

    def order(self, input_mint: str, output_mint: str, amount: int, taker: str | None = None) -> dict:
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount))}
        if taker:
            params["taker"] = taker
        problem = "sin respuesta"
        for attempt in range(1, self.retries + 1):
            self._wait_turn()
            try:
                resp = self.session.get(f"{self.base}/order", params=params, headers=self.headers, timeout=15)
            except requests.RequestException as exc:
                problem = type(exc).__name__
                self.sleep(2.5 * attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                problem = f"HTTP {resp.status_code}"
                self.sleep(2.5 * attempt)
                continue
            if resp.status_code >= 400:
                raise JupiterError(f"Jupiter rechazó la cotización (HTTP {resp.status_code}): {resp.text[:200]}")
            return resp.json()
        raise JupiterError(f"Jupiter no respondió a la cotización tras {self.retries} intentos ({problem}).")

    def execute(self, signed_tx: str, request_id: str) -> dict:
        body = {"signedTransaction": signed_tx, "requestId": request_id}
        problem = "sin respuesta"
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.post(f"{self.base}/execute", json=body, headers=self.headers, timeout=60)
            except requests.RequestException as exc:
                problem = type(exc).__name__
                self.sleep(2.0 * attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                problem = f"HTTP {resp.status_code}"
                self.sleep(2.0 * attempt)
                continue
            if resp.status_code >= 400:
                return {"status": "Failed", "code": resp.status_code, "error": resp.text[:300]}
            return resp.json()
        return {"status": "Unknown", "error": f"/execute no respondió tras {self.retries} intentos ({problem})."}
