import base64
import json
import os

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from solbot import wallet

MEMO = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")


def write_wallet(folder, keypair, address=None, mode=0o600):
    folder.mkdir(parents=True, exist_ok=True)
    secret = folder / "wallet.json"
    secret.write_text(json.dumps(list(bytes(keypair))))
    os.chmod(secret, mode)
    (folder / "wallet.address").write_text(f"{address or keypair.pubkey()}\n")
    return folder


def unsigned_tx(*signers):
    """Transacción v0 sin firmar que exige la firma de `signers` (el primero paga)."""
    accounts = [AccountMeta(s, is_signer=True, is_writable=True) for s in signers]
    ix = Instruction(MEMO, b"solbot", accounts)
    msg = MessageV0.try_compile(signers[0], [ix], [], Hash.new_unique())
    tx = VersionedTransaction.populate(msg, [Signature.default()] * len(signers))
    return base64.b64encode(bytes(tx)).decode()


def test_loads_the_key_that_matches_the_saved_address(tmp_path):
    kp = Keypair()
    folder = write_wallet(tmp_path / "w", kp)
    loaded = wallet.load_keypair(folder)
    assert loaded.pubkey() == kp.pubkey()


def test_refuses_a_key_that_belongs_to_another_address(tmp_path):
    folder = write_wallet(tmp_path / "w", Keypair(), address=Keypair().pubkey())
    with pytest.raises(wallet.WalletError, match="no corresponde"):
        wallet.load_keypair(folder)


def test_refuses_a_corrupted_key_without_echoing_it(tmp_path):
    folder = write_wallet(tmp_path / "w", Keypair())
    (folder / "wallet.json").write_text("[1, 2, 3, 99999]")
    with pytest.raises(wallet.WalletError) as err:
        wallet.load_keypair(folder)
    assert "99999" not in str(err.value)


def test_refuses_a_key_other_users_can_read(tmp_path):
    folder = write_wallet(tmp_path / "w", Keypair(), mode=0o644)
    with pytest.raises(wallet.WalletError, match="permisos"):
        wallet.load_keypair(folder)


def test_reads_the_address_without_opening_the_key(tmp_path):
    kp = Keypair()
    folder = write_wallet(tmp_path / "w", kp)
    (folder / "wallet.json").write_text("esto no es una llave")
    assert wallet.read_address(folder) == str(kp.pubkey())


def test_signs_its_own_slot_and_leaves_the_market_maker_slot_empty():
    me, market_maker = Keypair(), Keypair()
    signed = wallet.sign_partial(unsigned_tx(market_maker.pubkey(), me.pubkey()), me)
    tx = VersionedTransaction.from_bytes(base64.b64decode(signed))
    message = to_bytes_versioned(tx.message)
    assert tx.signatures[1].verify(me.pubkey(), message)
    assert tx.signatures[0] == Signature.default()


def test_refuses_to_sign_a_transaction_that_does_not_need_our_wallet():
    with pytest.raises(wallet.WalletError, match="no pide la firma"):
        wallet.sign_partial(unsigned_tx(Keypair().pubkey()), Keypair())


def test_signature_of_a_fee_paying_wallet_is_the_transaction_id():
    me = Keypair()
    signed = wallet.sign_partial(unsigned_tx(me.pubkey()), me)
    tx = VersionedTransaction.from_bytes(base64.b64decode(signed))
    assert wallet.transaction_id(signed) == str(tx.signatures[0])
    assert tx.signatures[0].verify(me.pubkey(), to_bytes_versioned(tx.message))


def test_an_rfq_transaction_has_no_id_until_the_market_maker_signs():
    me, market_maker = Keypair(), Keypair()
    signed = wallet.sign_partial(unsigned_tx(market_maker.pubkey(), me.pubkey()), me)
    assert wallet.transaction_id(signed) is None
