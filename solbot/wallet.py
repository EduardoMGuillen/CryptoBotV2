"""Wallet dedicada, la misma que usa PolloTraderBot (por defecto ~/.solana-paper-bot).

- wallet.address: dirección pública. La interfaz y el estado solo leen esto.
- wallet.json: llave privada en formato solana-keygen (64 bytes), permisos 600.

La llave solo se abre para firmar una orden real. Nunca se imprime, no se copia a este
proyecto y los mensajes de error nunca repiten su contenido.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction


class WalletError(Exception):
    pass


def expand(folder: str | Path) -> Path:
    return Path(os.path.expanduser(str(folder)))


def read_address(folder: str | Path) -> str:
    path = expand(folder) / "wallet.address"
    if not path.exists():
        raise WalletError(f"No hay wallet en {expand(folder)}: falta wallet.address.")
    text = path.read_text().strip()
    try:
        Pubkey.from_string(text)
    except Exception:
        raise WalletError("wallet.address no contiene una dirección de Solana válida.") from None
    return text


def load_keypair(folder: str | Path) -> Keypair:
    folder = expand(folder)
    address = read_address(folder)
    secret = folder / "wallet.json"
    if not secret.exists():
        raise WalletError(f"Falta {secret}.")
    mode = secret.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise WalletError(f"{secret} tiene permisos demasiado abiertos ({oct(mode & 0o777)}). Corrige con: chmod 600 {secret}")
    try:
        raw = json.loads(secret.read_text())
        if not (isinstance(raw, list) and len(raw) == 64 and all(isinstance(b, int) and 0 <= b <= 255 for b in raw)):
            raise ValueError
        data = bytes(raw)
    except Exception:
        raise WalletError("wallet.json no tiene el formato de solana-keygen (64 números de 0 a 255).") from None
    keypair = Keypair.from_seed(data[:32])
    if bytes(keypair.pubkey()) != data[32:]:
        raise WalletError("wallet.json está dañado: su mitad pública no coincide con la privada.")
    if str(keypair.pubkey()) != address:
        raise WalletError("La llave de wallet.json no corresponde a la dirección de wallet.address.")
    return keypair


def _decode(tx_b64: str) -> VersionedTransaction:
    try:
        return VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    except Exception:
        raise WalletError("La transacción recibida no se puede leer.") from None


def sign_partial(tx_b64: str, keypair: Keypair) -> str:
    """Firma solo el lugar de esta wallet. Las firmas de otros (p. ej. un market maker de
    JupiterZ, que firma en /execute) se dejan como están."""
    tx = _decode(tx_b64)
    message = tx.message
    signers = list(message.account_keys)[: message.header.num_required_signatures]
    me = keypair.pubkey()
    if me not in signers:
        raise WalletError("La transacción no pide la firma de esta wallet: no se firma.")
    signatures = list(tx.signatures)
    signatures[signers.index(me)] = keypair.sign_message(to_bytes_versioned(message))
    return base64.b64encode(bytes(VersionedTransaction.populate(message, signatures))).decode()


def verify_own_signature(tx_b64: str, address: str) -> bool:
    """¿La firma que ocupa el lugar de `address` es válida para el mensaje de la transacción?"""
    tx = _decode(tx_b64)
    message = tx.message
    signers = list(message.account_keys)[: message.header.num_required_signatures]
    me = Pubkey.from_string(address)
    if me not in signers:
        return False
    return tx.signatures[signers.index(me)].verify(me, to_bytes_versioned(message))


def transaction_id(tx_b64: str) -> str | None:
    """En Solana el id de una transacción es su primera firma (la de quien paga la comisión).
    En una orden RFQ la paga el market maker y firma después: hasta entonces no hay id."""
    first = _decode(tx_b64).signatures[0]
    return None if first == Signature.default() else str(first)
