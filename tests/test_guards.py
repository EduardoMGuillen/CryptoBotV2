import pytest

from solbot import guards
from solbot.config import LiveLimits
from solbot.jupiter import SOL_MINT, USDC_MINT
from solbot.orders import Intent, Sleeve

LIMITS = LiveLimits(budget_usd=50, max_trade_usd=30, min_sol_reserve=0.03, max_slippage_bps=100,
                    max_price_impact_bps=100, max_deviation_bps=150, max_trades_per_day=2)
ME = "54D3DC1GgEuPaFr71g3Qj2Rd9hzhYrntapSWS6BXj4zF"

# Vender 0.25 SOL a 90 USDC/SOL = 22.5 USDC
SELL = Intent(id="2026-11-05-sell", bar="2026-11-05", side="sell", input_mint=SOL_MINT, output_mint=USDC_MINT,
              amount=250_000_000, amount_ui=0.25, usd=22.5, reference_price=90.0)


def good_order(**changes):
    order = {
        "inputMint": SOL_MINT, "outputMint": USDC_MINT, "inAmount": "250000000", "outAmount": "22480000",
        "otherAmountThreshold": "22410000", "swapMode": "ExactIn", "slippageBps": 31,
        "priceImpactPct": "0.0002", "priceImpact": 0.02, "feeBps": 2, "taker": ME,
        "requestId": "req-1", "transaction": "AQID", "errorCode": None,
    }
    order.update(changes)
    return order


def test_a_normal_order_passes():
    assert guards.check_order(good_order(), SELL, ME, LIMITS) == []


@pytest.mark.parametrize("changes, problem", [
    ({"transaction": "", "errorCode": 1, "errorMessage": "Insufficient funds"}, "Insufficient funds"),
    ({"outputMint": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}, "par"),
    ({"inAmount": "260000000"}, "monto"),
    ({"outAmount": "21000000"}, "se aleja"),        # 84 USDC/SOL: 6.7% bajo la referencia
    ({"priceImpactPct": "0.015"}, "impacto"),        # 1.5%
    ({"slippageBps": 300}, "slippage"),
    ({"taker": "Otra111111111111111111111111111111111111111"}, "wallet"),
])
def test_each_unsafe_order_is_refused_with_its_reason(changes, problem):
    problems = guards.check_order(good_order(**changes), SELL, ME, LIMITS)
    assert any(problem in p for p in problems), problems


def test_an_order_above_the_trade_cap_is_refused():
    big = Intent(**{**SELL.__dict__, "amount": 400_000_000, "amount_ui": 0.4, "usd": 36.0})
    problems = guards.check_order(good_order(inAmount="400000000", outAmount="35990000"), big, ME, LIMITS)
    assert any("máximo por operación" in p for p in problems)


def test_selling_cannot_dip_into_the_sol_reserve():
    problems = guards.check_wallet(SELL, wallet_sol=0.27, wallet_usdc=0.0, sleeve=Sleeve(sol=0.25, usdc=0), limits=LIMITS)
    assert any("reserva" in p for p in problems)


def test_selling_with_enough_sol_left_over_is_fine():
    assert guards.check_wallet(SELL, wallet_sol=0.42, wallet_usdc=0.0, sleeve=Sleeve(sol=0.25, usdc=0), limits=LIMITS) == []


def test_missing_bot_funds_in_the_wallet_stop_the_trade():
    # El bot cree tener 0.25 SOL pero la wallet solo tiene 0.1 (otro proceso los usó)
    problems = guards.check_wallet(SELL, wallet_sol=0.1, wallet_usdc=0.0, sleeve=Sleeve(sol=0.25, usdc=0), limits=LIMITS)
    assert any("no cuadra" in p for p in problems)


def test_buying_needs_the_usdc_and_sol_for_fees():
    buy = Intent(id="b", bar="2026-11-05", side="buy", input_mint=USDC_MINT, output_mint=SOL_MINT,
                 amount=20_000_000, amount_ui=20.0, usd=20.0, reference_price=90.0)
    assert guards.check_wallet(buy, wallet_sol=0.05, wallet_usdc=20.0, sleeve=Sleeve(0, 20.0), limits=LIMITS) == []
    no_usdc = guards.check_wallet(buy, wallet_sol=0.05, wallet_usdc=5.0, sleeve=Sleeve(0, 20.0), limits=LIMITS)
    assert any("USDC" in p for p in no_usdc)
    no_fees = guards.check_wallet(buy, wallet_sol=0.01, wallet_usdc=20.0, sleeve=Sleeve(0, 20.0), limits=LIMITS)
    assert any("comisiones" in p for p in no_fees)
