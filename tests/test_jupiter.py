import pytest
import requests

from solbot import jupiter as jup


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self.payload = payload
        self.text = str(payload)

    def json(self):
        return self.payload


class FakeSession:
    """Devuelve las respuestas en orden y registra cada llamada."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)


class FakeTime:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


ORDER = {
    "inputMint": jup.SOL_MINT, "outputMint": jup.USDC_MINT, "inAmount": "1000000000",
    "outAmount": "89410150", "otherAmountThreshold": "89133000", "swapMode": "ExactIn",
    "slippageBps": 31, "priceImpactPct": "0.0001", "feeBps": 2, "requestId": "req-1",
    "transaction": "AAAA", "router": "metis", "mode": "ultra",
}


def client(session, fake_time=None, **kwargs):
    t = fake_time or FakeTime()
    return jup.Jupiter(session=session, clock=t.clock, sleep=t.sleep, **kwargs)


def test_order_asks_for_the_pair_amount_and_wallet():
    session = FakeSession(FakeResponse(200, ORDER))
    out = client(session).order(jup.SOL_MINT, jup.USDC_MINT, 1_000_000_000, taker="Wallet111")
    method, url, kwargs = session.calls[0]
    assert (method, url.rsplit("/", 1)[-1]) == ("GET", "order")
    assert kwargs["params"] == {"inputMint": jup.SOL_MINT, "outputMint": jup.USDC_MINT,
                                "amount": "1000000000", "taker": "Wallet111"}
    assert out["requestId"] == "req-1"


def test_order_waits_and_retries_when_rate_limited():
    t = FakeTime()
    session = FakeSession(FakeResponse(429, {"error": "Too Many Requests"}), FakeResponse(200, ORDER))
    out = client(session, t).order(jup.SOL_MINT, jup.USDC_MINT, 1)
    assert out["requestId"] == "req-1"
    assert len(session.calls) == 2
    assert sum(t.slept) >= 2


def test_consecutive_orders_respect_the_keyless_rate_limit():
    t = FakeTime()
    session = FakeSession(FakeResponse(200, ORDER), FakeResponse(200, ORDER))
    c = client(session, t, min_interval=2.0)
    c.order(jup.SOL_MINT, jup.USDC_MINT, 1)
    c.order(jup.SOL_MINT, jup.USDC_MINT, 1)
    assert t.slept == [2.0]


def test_order_rejected_by_jupiter_is_an_error_not_a_retry():
    session = FakeSession(FakeResponse(400, {"error": "Invalid inputMint"}))
    with pytest.raises(jup.JupiterError, match="400"):
        client(session).order("bad", jup.USDC_MINT, 1)
    assert len(session.calls) == 1


def test_api_key_goes_in_the_header():
    session = FakeSession(FakeResponse(200, ORDER))
    client(session, api_key="k-123").order(jup.SOL_MINT, jup.USDC_MINT, 1)
    assert session.calls[0][2]["headers"]["x-api-key"] == "k-123"


def test_execute_sends_the_signed_transaction_and_request_id():
    result = {"status": "Success", "signature": "sig1", "code": 0, "totalInputAmount": "1000000000",
              "totalOutputAmount": "89400000", "inputAmountResult": "1000000000", "outputAmountResult": "89400000"}
    session = FakeSession(FakeResponse(200, result))
    out = client(session).execute("SIGNED", "req-1")
    method, url, kwargs = session.calls[0]
    assert (method, url.rsplit("/", 1)[-1]) == ("POST", "execute")
    assert kwargs["json"] == {"signedTransaction": "SIGNED", "requestId": "req-1"}
    assert out["status"] == "Success"


def test_execute_resends_the_same_transaction_after_a_network_error():
    ok = {"status": "Success", "signature": "sig1", "code": 0}
    session = FakeSession(requests.ConnectionError("reset"), FakeResponse(200, ok))
    out = client(session).execute("SIGNED", "req-1")
    assert out["status"] == "Success"
    assert [c[2]["json"] for c in session.calls] == [{"signedTransaction": "SIGNED", "requestId": "req-1"}] * 2


def test_execute_without_any_answer_reports_unknown_instead_of_failed():
    session = FakeSession(*[requests.Timeout("slow")] * 3)
    out = client(session).execute("SIGNED", "req-1")
    assert out["status"] == "Unknown"


@pytest.mark.parametrize("order, price", [
    ({"inputMint": jup.SOL_MINT, "outputMint": jup.USDC_MINT, "inAmount": "1000000000", "outAmount": "89410150"}, 89.41015),
    ({"inputMint": jup.USDC_MINT, "outputMint": jup.SOL_MINT, "inAmount": "100000000", "outAmount": "1250000000"}, 80.0),
])
def test_price_of_an_order_is_usdc_per_sol(order, price):
    assert jup.order_price(order) == pytest.approx(price)


def test_a_rejection_after_a_lost_attempt_is_unknown_not_failed():
    # El primer envío pudo haber llegado; que el segundo sea rechazado no prueba que no se ejecutó.
    session = FakeSession(requests.Timeout("slow"), FakeResponse(400, {"error": "Invalid"}))
    assert client(session).execute("SIGNED", "req-1")["status"] == "Unknown"


def test_a_rejection_on_the_first_attempt_is_a_failure():
    session = FakeSession(FakeResponse(400, {"error": "Invalid signed transaction"}))
    assert client(session).execute("SIGNED", "req-1")["status"] == "Failed"


class NotJson(FakeResponse):
    def json(self):
        raise ValueError("no es JSON")


def test_an_answer_that_is_not_json_is_unknown():
    session = FakeSession(NotJson(200, None))
    assert client(session).execute("SIGNED", "req-1")["status"] == "Unknown"
