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

from solbot.config import LiveLimits
from solbot.executors import LiveExecutor, PaperExecutor
from solbot.jupiter import SOL_MINT, USDC_MINT
from solbot.orders import Intent, Refused, RetryLater, Sleeve, Unknown

LIMITS = LiveLimits(budget_usd=25, max_trade_usd=30, min_sol_reserve=0.03, max_slippage_bps=100,
                    max_price_impact_bps=100, max_deviation_bps=150, max_trades_per_day=2)
MEMO = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
KEY = Keypair()
ME = str(KEY.pubkey())

SELL = Intent(id="2026-11-05-sell", bar="2026-11-05", side="sell", input_mint=SOL_MINT, output_mint=USDC_MINT,
              amount=250_000_000, amount_ui=0.25, usd=22.5, reference_price=90.0)


def unsigned_tx_for(signer):
    ix = Instruction(MEMO, b"swap", [AccountMeta(signer, is_signer=True, is_writable=True)])
    msg = MessageV0.try_compile(signer, [ix], [], Hash.new_unique())
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, [Signature.default()]))).decode()


def order(**changes):
    o = {"inputMint": SOL_MINT, "outputMint": USDC_MINT, "inAmount": "250000000", "outAmount": "22480000",
         "otherAmountThreshold": "22410000", "swapMode": "ExactIn", "slippageBps": 31, "priceImpactPct": "0.0002",
         "feeBps": 2, "taker": ME, "requestId": "req-1", "transaction": unsigned_tx_for(KEY.pubkey()),
         "errorCode": None, "router": "metis",
         "routePlan": [{"percent": 100, "bps": 10000, "swapInfo": {"label": "TesseraV", "ammKey": "x",
                                                                   "inputMint": SOL_MINT, "outputMint": USDC_MINT,
                                                                   "inAmount": "250000000", "outAmount": "22480000"}}]}
    o.update(changes)
    return o


class FakeJupiter:
    def __init__(self, quote, result=None):
        self.quote = quote
        self.result = result or {"status": "Success", "code": 0, "signature": "SIG",
                                 "totalInputAmount": "250000000", "totalOutputAmount": "22470000",
                                 "inputAmountResult": "250000000", "outputAmountResult": "22474000"}
        self.orders, self.executed = [], []

    def order(self, input_mint, output_mint, amount, taker=None):
        self.orders.append((input_mint, output_mint, amount, taker))
        return self.quote

    def execute(self, signed_tx, request_id):
        self.executed.append((signed_tx, request_id))
        return self.result


class FakeRpc:
    def __init__(self, sol=0.42, usdc=0.0, simulation=None, status=None):
        self.sol, self.usdc, self.simulation, self.status = sol, usdc, simulation, status

    def sol_balance(self, address):
        return int(self.sol * 1e9)

    def token_balance(self, owner, mint):
        return int(self.usdc * 1e6)

    def simulate(self, tx):
        return self.simulation

    def signature_status(self, sig):
        return self.status


class Journal:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)


def live(jupiter, rpc=None, kill=False, trades_today=0, key_loader=None):
    journal = Journal()
    executor = LiveExecutor(jupiter=jupiter, rpc=rpc or FakeRpc(), limits=LIMITS, address=ME,
                            load_key=key_loader or (lambda: KEY), journal=journal,
                            kill=lambda: kill, trades_today=lambda: trades_today)
    return executor, journal


def never_open_the_key():
    raise AssertionError("La llave no debía abrirse")


def test_paper_fill_uses_the_real_quote_minus_the_assumed_cost():
    quote = {"inputMint": SOL_MINT, "outputMint": USDC_MINT, "inAmount": "1000000000", "outAmount": "89410150",
             "routePlan": [{"swapInfo": {"label": "Kipseli"}}]}
    intent = Intent(**{**SELL.__dict__, "amount": 1_000_000_000, "amount_ui": 1.0, "usd": 89.4})
    fill = PaperExecutor(FakeJupiter(quote), cost_bps=10, start_usd=1000).execute(intent, Sleeve(1.0, 0))
    assert fill.sol_qty == pytest.approx(1.0)
    assert fill.usdc_qty == pytest.approx(89.41015 * 0.999)
    assert fill.route == "Kipseli"


def test_live_order_is_validated_simulated_signed_and_executed():
    jupiter = FakeJupiter(order())
    executor, journal = live(jupiter)
    fill = executor.execute(SELL, Sleeve(sol=0.25, usdc=0))

    assert jupiter.orders == [(SOL_MINT, USDC_MINT, 250_000_000, ME)]
    signed, request_id = jupiter.executed[0]
    tx = VersionedTransaction.from_bytes(base64.b64decode(signed))
    assert tx.signatures[0].verify(KEY.pubkey(), to_bytes_versioned(tx.message))
    assert request_id == "req-1"
    assert (fill.sol_qty, fill.usdc_qty, fill.signature) == (pytest.approx(0.25), pytest.approx(22.47), "SIG")
    assert [e["event"] for e in journal.events] == ["intent", "signed", "result"]


def test_unsafe_quote_is_refused_before_opening_the_key():
    jupiter = FakeJupiter(order(priceImpactPct="0.05"))
    executor, _ = live(jupiter, key_loader=never_open_the_key)
    with pytest.raises(Refused, match="impacto"):
        executor.execute(SELL, Sleeve(sol=0.25, usdc=0))
    assert jupiter.executed == []


def test_failed_simulation_is_refused():
    jupiter = FakeJupiter(order())
    executor, _ = live(jupiter, rpc=FakeRpc(simulation='{"InstructionError":[2,{"Custom":1}]}'),
                       key_loader=never_open_the_key)
    with pytest.raises(Refused, match="simulación"):
        executor.execute(SELL, Sleeve(sol=0.25, usdc=0))
    assert jupiter.executed == []


def test_kill_file_stops_before_asking_for_a_quote():
    jupiter = FakeJupiter(order())
    executor, _ = live(jupiter, kill=True)
    with pytest.raises(Refused, match="KILL"):
        executor.execute(SELL, Sleeve(sol=0.25, usdc=0))
    assert jupiter.orders == []


def test_daily_trade_limit_stops_new_orders():
    jupiter = FakeJupiter(order())
    executor, _ = live(jupiter, trades_today=2)
    with pytest.raises(Refused, match="día"):
        executor.execute(SELL, Sleeve(sol=0.25, usdc=0))
    assert jupiter.orders == []


def test_a_sale_shrinks_so_the_sol_reserve_stays_untouched():
    # La wallet tiene 0.26 SOL: vender 0.25 dejaría 0.01, menos que la reserva de 0.03 → se venden 0.23
    jupiter = FakeJupiter(order(inAmount="230000000", outAmount="20680000"))
    executor, _ = live(jupiter, rpc=FakeRpc(sol=0.26))
    fill = executor.execute(SELL, Sleeve(sol=0.25, usdc=0))
    assert jupiter.orders[0][2] == 230_000_000
    assert fill.sol_qty == pytest.approx(0.25)  # el fill viene de lo que reporta /execute (FakeJupiter)


def test_nothing_free_to_sell_above_the_reserve_is_refused_before_the_quote():
    jupiter = FakeJupiter(order())
    executor, _ = live(jupiter, rpc=FakeRpc(sol=0.035))
    with pytest.raises(Refused, match="reserva"):
        executor.execute(SELL, Sleeve(sol=0.03, usdc=0))
    assert jupiter.orders == []


def test_no_answer_from_execute_leaves_the_order_pending():
    jupiter = FakeJupiter(order(), result={"status": "Unknown", "error": "sin respuesta"})
    executor, _ = live(jupiter)
    with pytest.raises(Unknown) as err:
        executor.execute(SELL, Sleeve(sol=0.25, usdc=0))
    assert err.value.pending["request_id"] == "req-1"


def test_reported_failure_that_landed_on_chain_is_treated_as_unknown():
    failed = {"status": "Failed", "code": -1000, "error": "Failed to land", "signature": "SIG"}
    executor, _ = live(FakeJupiter(order(), result=failed), rpc=FakeRpc(status="confirmed"))
    with pytest.raises(Unknown):
        executor.execute(SELL, Sleeve(sol=0.25, usdc=0))


def test_a_swap_rejected_by_the_market_maker_is_a_refusal():
    failed = {"status": "Failed", "code": -2004, "error": "Swap rejected", "signature": "SIG"}
    executor, _ = live(FakeJupiter(order(), result=failed), rpc=FakeRpc(status=None))
    with pytest.raises(Refused, match="Swap rejected"):
        executor.execute(SELL, Sleeve(sol=0.25, usdc=0))


def test_funding_takes_at_most_the_budget_and_never_the_reserve():
    executor, _ = live(FakeJupiter(order()), rpc=FakeRpc(sol=0.4189, usdc=0.0001))
    sleeve = executor.fund(price=90.0)
    assert sleeve.value(90.0) == pytest.approx(25.0)
    rich = LiveExecutor(jupiter=FakeJupiter(order()), rpc=FakeRpc(sol=0.4189, usdc=0), address=ME,
                        limits=LiveLimits(budget_usd=1000, min_sol_reserve=0.03), load_key=lambda: KEY,
                        journal=Journal(), kill=lambda: False, trades_today=lambda: 0)
    assert rich.fund(price=90.0).sol == pytest.approx(0.4189 - 0.03 - 0.005)  # reserva + colchón para comisiones


def test_funding_an_empty_wallet_is_refused():
    executor, _ = live(FakeJupiter(order()), rpc=FakeRpc(sol=0.031, usdc=0))
    with pytest.raises(Refused, match="fondos"):
        executor.fund(price=90.0)


def test_rehearsal_goes_all_the_way_to_a_valid_signature_but_never_sends():
    jupiter = FakeJupiter(order())
    executor, journal = live(jupiter)
    report = executor.rehearse(SELL, Sleeve(sol=0.25, usdc=0))
    assert jupiter.executed == []
    assert report["signature_ok"] is True
    assert report["price"] == pytest.approx(89.92)
    assert journal.events == []  # el ensayo no deja rastro en el diario de órdenes


def test_rehearsal_stops_on_the_same_checks_as_a_real_order():
    executor, _ = live(FakeJupiter(order(slippageBps=500)), key_loader=never_open_the_key)
    with pytest.raises(Refused, match="slippage"):
        executor.rehearse(SELL, Sleeve(sol=0.25, usdc=0))


def test_a_quote_that_expired_before_landing_is_retried_with_a_new_one():
    expired = {"status": "Failed", "code": -2003, "error": "Quote expired", "signature": "SIG"}
    executor, _ = live(FakeJupiter(order(), result=expired), rpc=FakeRpc(status=None))
    with pytest.raises(RetryLater):
        executor.execute(SELL, Sleeve(sol=0.25, usdc=0))


def test_an_order_that_cannot_be_recorded_is_never_sent():
    jupiter = FakeJupiter(order())
    executor, _ = live(jupiter)

    def broken_save(pending):
        raise OSError("disco lleno")

    with pytest.raises(OSError):
        executor.execute(SELL, Sleeve(sol=0.25, usdc=0), on_send=broken_save)
    assert jupiter.executed == []


def test_anything_that_breaks_after_sending_is_unknown_with_the_full_order():
    garbage = {"status": "Success", "code": 0, "signature": "SIG", "totalInputAmount": "no-es-un-número"}
    executor, _ = live(FakeJupiter(order(), result=garbage))
    with pytest.raises(Unknown) as err:
        executor.execute(SELL, Sleeve(sol=0.25, usdc=0))
    assert err.value.pending["quote_out"] == 22480000
    assert err.value.pending["request_id"] == "req-1"
