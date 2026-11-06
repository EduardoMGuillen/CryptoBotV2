import pytest

from solbot import rpc
from solbot.jupiter import USDC_MINT


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload


class FakeRpc:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        return FakeResponse(self.payloads.pop(0))


URL = "https://mainnet.helius-rpc.com/?api-key=secreto-muy-largo-123"


def token_account(mint, amount):
    return {"pubkey": "Acc111", "account": {"data": {"parsed": {"info": {
        "mint": mint, "owner": "Owner111", "tokenAmount": {"amount": str(amount), "decimals": 6,
                                                           "uiAmount": amount / 1e6, "uiAmountString": str(amount / 1e6)}},
        "type": "account"}, "program": "spl-token", "space": 165}, "executable": False, "lamports": 2039280,
        "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "rentEpoch": 0}}


def test_sol_balance_reads_lamports():
    fake = FakeRpc({"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 418914737}})
    assert rpc.SolanaRpc(URL, session=fake).sol_balance("Owner111") == 418914737
    assert fake.calls[0]["method"] == "getBalance" and fake.calls[0]["params"][0] == "Owner111"


def test_token_balance_adds_every_account_of_that_mint():
    fake = FakeRpc({"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": [
        token_account(USDC_MINT, 1_500_000), token_account(USDC_MINT, 250_000)]}})
    assert rpc.SolanaRpc(URL, session=fake).token_balance("Owner111", USDC_MINT) == 1_750_000
    assert fake.calls[0]["params"][1] == {"mint": USDC_MINT}


def test_signature_status_distinguishes_failed_from_confirmed():
    fake = FakeRpc(
        {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": [
            {"slot": 5, "confirmations": None, "err": None, "confirmationStatus": "finalized"}]}},
        {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": [
            {"slot": 5, "confirmations": 3, "err": {"InstructionError": [2, {"Custom": 6001}]}, "confirmationStatus": "confirmed"}]}},
        {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": [None]}},
    )
    client = rpc.SolanaRpc(URL, session=fake)
    assert client.signature_status("s1") == "finalized"
    assert client.signature_status("s2") == "failed"
    assert client.signature_status("s3") is None


def test_simulation_returns_the_error_or_none():
    fake = FakeRpc(
        {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": {"err": None, "logs": [], "unitsConsumed": 1}}},
        {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": {
            "err": {"InstructionError": [3, {"Custom": 1}]}, "logs": ["Program log: insufficient funds"]}}},
    )
    client = rpc.SolanaRpc(URL, session=fake)
    assert client.simulate("TX") is None
    assert "InstructionError" in client.simulate("TX")
    assert fake.calls[0]["params"][1]["sigVerify"] is False


def test_rpc_errors_never_show_the_url_with_its_api_key():
    fake = FakeRpc({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "Invalid param"}})
    with pytest.raises(rpc.RpcError) as err:
        rpc.SolanaRpc(URL, session=fake).sol_balance("x")
    assert "secreto" not in str(err.value)
    assert "Invalid param" in str(err.value)


def test_rpc_url_comes_from_the_environment_first(tmp_path):
    env_file = tmp_path / "config.env"
    env_file.write_text("# comentario\nSOLANA_RPC_URL=https://desde-archivo.example/rpc\n")
    assert rpc.resolve_url({"SOLANA_RPC_URL": "https://env.example"}, env_file) == "https://env.example"
    assert rpc.resolve_url({}, env_file) == "https://desde-archivo.example/rpc"
    assert rpc.resolve_url({}, tmp_path / "no-existe.env") == rpc.PUBLIC_RPC


def test_masked_url_hides_api_keys():
    masked = rpc.mask_url(URL)
    assert "secreto-muy-largo-123" not in masked
    assert masked.startswith("mainnet.helius-rpc.com")
