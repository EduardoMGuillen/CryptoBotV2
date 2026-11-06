import json
import os

import pandas as pd
import pytest
from solders.keypair import Keypair

from solbot import config
from solbot.app import App
from util import frame

KEY = Keypair()
ADDRESS = str(KEY.pubkey())


class NoNetworkRpc:
    def sol_balance(self, address):
        return int(0.42 * 1e9)

    def token_balance(self, owner, mint):
        return 0

    def simulate(self, tx):
        return None

    def signature_status(self, sig):
        return None


class NoNetworkJupiter:
    def order(self, *args, **kwargs):
        raise AssertionError("no debía cotizar")

    def execute(self, *args, **kwargs):
        raise AssertionError("no debía ejecutar")


def make_wallet(folder):
    folder.mkdir(parents=True)
    secret = folder / "wallet.json"
    secret.write_text(json.dumps(list(bytes(KEY))))
    os.chmod(secret, 0o600)
    (folder / "wallet.address").write_text(ADDRESS + "\n")
    return folder


def make_app(tmp_path, mode="paper", price=12.0):
    wallet_dir = make_wallet(tmp_path / "wallet")
    cfg = config.Config(mode=mode, data_dir=tmp_path / "data",
                        live=config.LiveLimits(wallet_dir=str(wallet_dir), rpc_env_file=str(tmp_path / "none.env")))
    return App(cfg, candles=lambda: frame([10, 11, 12]), price=lambda: price,
               rpc=NoNetworkRpc(), jupiter=NoNetworkJupiter(), now=lambda: pd.Timestamp("2024-01-04 08:00", tz="UTC"))


def test_live_mode_only_starts_with_the_last_four_characters_of_the_wallet(tmp_path):
    app = make_app(tmp_path, mode="live")
    with pytest.raises(ValueError, match="4"):
        app.start(confirm="")
    with pytest.raises(ValueError):
        app.start(confirm="XXXX")
    assert app.start(confirm=ADDRESS[-4:])["running"] is True


def test_paper_mode_starts_without_confirmation(tmp_path):
    app = make_app(tmp_path, mode="paper")
    app.pause()
    assert app.start(confirm=None)["running"] is True


def test_kill_file_in_the_shared_wallet_folder_stops_live_mode(tmp_path):
    app = make_app(tmp_path, mode="live")
    assert app.kill_switch() is False
    (tmp_path / "wallet" / "KILL").touch()
    assert app.kill_switch() is True


def test_kill_file_in_the_data_folder_stops_paper_mode(tmp_path):
    app = make_app(tmp_path, mode="paper")
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "KILL").touch()
    assert app.kill_switch() is True


def test_status_compares_the_bot_with_just_holding_sol(tmp_path):
    app = make_app(tmp_path, mode="paper", price=15.0)
    state = app.store.load()
    state.update(sleeve={"sol": 0.0, "usdc": 1100.0},
                 start={"date": "2024-01-01", "at": "x", "value_usd": 1000.0, "price": 10.0})
    app.store.save(state)
    perf = app.status()["performance"]
    assert perf["value"] == pytest.approx(1100.0)
    assert perf["pnl_pct"] == pytest.approx(0.10)
    assert perf["hold_value"] == pytest.approx(1500.0)  # 1000 USD en SOL a 10 → 100 SOL × 15
    assert perf["vs_hold_pct"] == pytest.approx(1100 / 1500 - 1)


def test_status_never_contains_the_private_key(tmp_path):
    app = make_app(tmp_path, mode="live")
    text = json.dumps(app.status())
    assert ADDRESS in text
    assert str(list(bytes(KEY))[:8])[1:-1] not in text


def test_only_todays_trades_count_toward_the_daily_limit(tmp_path):
    app = make_app(tmp_path, mode="live")
    state = app.store.load()
    state["trades"] = [{"at": "2024-01-03T23:59:00+00:00"}, {"at": "2024-01-04T01:00:00+00:00"},
                       {"at": "2024-01-04T07:30:00+00:00"}]
    app.store.save(state)
    assert app.trades_today() == 2
