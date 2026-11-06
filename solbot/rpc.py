"""Cliente mínimo de JSON-RPC de Solana: saldos, estado de transacciones y simulación.

La URL del RPC suele llevar una API key (Helius, QuickNode...): nunca aparece en mensajes
de error ni en la interfaz; para mostrarla se usa `mask_url`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import requests

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


class RpcError(Exception):
    pass


def read_env_file(path: str | Path) -> dict[str, str]:
    """Líneas KEY=VALOR (como .env). Ignora comentarios y quita comillas."""
    path = Path(os.path.expanduser(str(path)))
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def resolve_url(env: dict, env_file: str | Path | None) -> str:
    """SOLANA_RPC_URL del entorno; si no, la del archivo de PolloTraderBot; si no, el RPC público."""
    if env.get("SOLANA_RPC_URL", "").strip():
        return env["SOLANA_RPC_URL"].strip()
    if env_file:
        from_file = read_env_file(env_file).get("SOLANA_RPC_URL", "").strip()
        if from_file:
            return from_file
    return PUBLIC_RPC


def _mask(token: str) -> str:
    return f"{token[:4]}…{token[-3:]}" if len(token) > 12 else token


def mask_url(url: str) -> str:
    parts = urlsplit(url)
    path = "/".join(_mask(p) for p in parts.path.split("/"))
    query = "&".join(f"{k}={_mask(v)}" for k, v in parse_qsl(parts.query))
    return f"{parts.hostname}{path}" + (f"?{query}" if query else "")


class SolanaRpc:
    def __init__(self, url: str, session=None, timeout: float = 15):
        self.url = url
        self.session = session or requests.Session()
        self.timeout = timeout

    def _call(self, method: str, params: list):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            resp = self.session.post(self.url, json=payload, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RpcError(f"El RPC no respondió a {method} ({type(exc).__name__}).") from None
        if resp.status_code != 200:
            raise RpcError(f"El RPC respondió HTTP {resp.status_code} a {method}.")
        body = resp.json()
        if "error" in body:
            raise RpcError(f"El RPC rechazó {method}: {body['error'].get('message', 'error desconocido')}")
        return body["result"]

    def sol_balance(self, address: str) -> int:
        return int(self._call("getBalance", [address, {"commitment": "confirmed"}])["value"])

    def token_balance(self, owner: str, mint: str) -> int:
        result = self._call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
        return sum(int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for a in result["value"])

    def signature_status(self, signature: str) -> str | None:
        result = self._call("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
        status = result["value"][0]
        if status is None:
            return None
        if status.get("err"):
            return "failed"
        return status.get("confirmationStatus") or "processed"

    def simulate(self, tx_b64: str) -> str | None:
        """None si la simulación sale bien; si no, una descripción corta del error."""
        result = self._call(
            "simulateTransaction",
            [tx_b64, {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": False, "commitment": "confirmed"}],
        )
        err = result["value"].get("err")
        if err is None:
            return None
        logs = result["value"].get("logs") or []
        return json.dumps(err) + (f" · {logs[-1]}" if logs else "")
