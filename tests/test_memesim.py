import pytest

from solbot.memesim import Book, Position, Rules, concentration, fill_price, screen, step_position

NOW = 1_800_000_000.0  # segundos
RULES = Rules()


def pair(**kw):
    base = {
        "pairAddress": "POOL",
        "baseToken": {"address": "MINT", "symbol": "X"},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL"},
        "priceUsd": "0.001",
        "liquidity": {"usd": 30_000, "base": 1_000_000, "quote": 150},
        "pairCreatedAt": (NOW - 3600) * 1000,
    }
    base.update(kw)
    return base


# ---- filtro de mercado ----------------------------------------------------------------------

def test_screen_accepts_young_liquid_sol_pair():
    assert screen(pair(), NOW, RULES) is None


def test_screen_rejects_low_liquidity():
    assert screen(pair(liquidity={"usd": 19_000, "base": 1, "quote": 1}), NOW, RULES) == "liquidez"


def test_screen_rejects_old_pairs():
    assert screen(pair(pairCreatedAt=(NOW - 7 * 3600) * 1000), NOW, RULES) == "edad"


def test_screen_rejects_unknown_quote():
    assert screen(pair(quoteToken={"address": "OTHER", "symbol": "FOO"}), NOW, RULES) == "par"


# ---- concentración de holders ---------------------------------------------------------------

def test_concentration_excludes_pool_vault_by_owner_and_by_amount():
    accounts = [
        {"address": "A1", "amount": 600.0, "owner": "POOL"},  # bóveda del pool (dueño = par)
        {"address": "A2", "amount": 200.0, "owner": "AUTH"},  # bóveda con monto = liquidity.base
        {"address": "A3", "amount": 40.0, "owner": "W1"},
        {"address": "A4", "amount": 30.0, "owner": "W2"},
    ]
    top10, biggest = concentration(accounts, supply=1000.0, pool="POOL", pool_base=200.0)
    assert top10 == pytest.approx(0.07)
    assert biggest == pytest.approx(0.04)


def test_concentration_merges_accounts_of_same_owner():
    accounts = [{"address": "A", "amount": 30.0, "owner": "W"}, {"address": "B", "amount": 30.0, "owner": "W"}]
    _, biggest = concentration(accounts, supply=1000.0, pool="POOL", pool_base=0.0)
    assert biggest == pytest.approx(0.06)


# ---- costo de ejecución ---------------------------------------------------------------------

def test_fill_price_buy_pays_fee_and_impact():
    # 1 000 USD contra 10 000 USD del lado cotizado: impacto 10 %, comisión 1 %
    p = fill_price(1.0, 1000.0, quote_liq_usd=10_000.0, side="buy", fee=0.01)
    assert p == pytest.approx(1.10 / 0.99)


def test_fill_price_sell_receives_less():
    p = fill_price(1.0, 1000.0, quote_liq_usd=10_000.0, side="sell", fee=0.01)
    assert p == pytest.approx(0.99 / 1.10)


def test_fill_price_sell_into_empty_pool_is_zero():
    assert fill_price(1.0, 100.0, quote_liq_usd=0.0, side="sell", fee=0.01) == 0.0


# ---- salidas --------------------------------------------------------------------------------

def pos(**kw):
    base = dict(token="MINT", symbol="X", pool="POOL", entry=1.0, qty=100.0, cost=100.0, opened=NOW,
                peak=1.0, entry_liq=30_000.0)
    base.update(kw)
    return Position(**base)


def test_stop_loss_sells_everything():
    assert step_position(pos(), 0.74, 30_000, NOW + 60, RULES) == [(1.0, "stop")]


def test_no_exit_in_normal_range():
    assert step_position(pos(), 1.3, 30_000, NOW + 60, RULES) == []


def test_take_profit_steps_fire_once():
    p = pos()
    assert step_position(p, 2.1, 30_000, NOW + 60, RULES) == [(1 / 3, "tp1")]
    p.steps_done = 1
    assert step_position(p, 2.2, 30_000, NOW + 60, RULES) == []
    assert step_position(p, 3.1, 30_000, NOW + 60, RULES) == [(1 / 2, "tp2")]


def test_trailing_stop_after_first_target():
    p = pos(peak=4.0, steps_done=2)
    assert step_position(p, 2.7, 30_000, NOW + 60, RULES) == [(1.0, "trailing")]


def test_rug_when_liquidity_collapses():
    assert step_position(pos(), 0.9, 10_000, NOW + 60, RULES) == [(1.0, "liquidez")]


def test_time_limit():
    assert step_position(pos(), 1.1, 30_000, NOW + 25 * 3600, RULES) == [(1.0, "tiempo")]


# ---- libro ----------------------------------------------------------------------------------

def test_book_open_and_close_round_trip():
    book = Book(cash=100.0)
    p = book.open(pair(), NOW, RULES)  # 4 USD de entrada
    assert book.cash == pytest.approx(96.0)
    assert p.cost == pytest.approx(4.0)
    proceeds = book.sell(p, 1.0, float(pair()["priceUsd"]), 30_000.0, NOW + 60, "stop", RULES)
    assert proceeds < 4.0  # paga comisión y penalización de entrada/salida
    assert book.positions == {}
    assert len(book.closed) == 1


def test_book_respects_max_open():
    book = Book(cash=100.0)
    for i in range(RULES.max_open):
        book.open(pair(baseToken={"address": f"M{i}", "symbol": "X"}), NOW, RULES)
    assert not book.can_open(RULES)


# ---- datos del dashboard --------------------------------------------------------------------

def test_snapshot_summarizes_files(tmp_path):
    import json

    from solbot.memesim import snapshot

    assert snapshot(tmp_path) == {"ready": False}
    closed = [
        {"symbol": "A", "token": "a", "reason": "stop", "pnl": -3.0, "mult": 0.25, "cost": 4.0, "opened": 1, "closed": 2},
        {"symbol": "B", "token": "b", "reason": "trailing", "pnl": 5.0, "mult": 2.25, "cost": 4.0, "opened": 1, "closed": 3},
    ]
    position = {"token": "c", "symbol": "C", "pool": "P", "entry": 1.0, "qty": 4.0, "cost": 4.0, "opened": 5.0,
                "peak": 2.0, "entry_liq": 1.0, "steps_done": 0, "proceeds": 0.0, "last": 2.0}
    (tmp_path / "state.json").write_text(json.dumps({
        "cash": 90.0, "start": 0.0, "saved": 10.0, "equity": 98.0, "closed": closed, "positions": {"c": position},
        "funnel": {"vistos": 50, "compras": 3, "rechazo_edad": 20, "descartados_sin_liquidez": 10}, "checked": {}}))
    (tmp_path / "equity.jsonl").write_text('{"t": 1, "equity": 100}\n{"t": 2, "equity": 98}\n{"t": 3, "equ')
    snap = snapshot(tmp_path)
    assert snap["wins"] == 1 and snap["losses"] == 1
    assert snap["rejected"] == 30
    assert snap["worst_stop"] == 0.25
    assert snap["open"][0]["mult"] == 2.0 and snap["open"][0]["pnl"] == 4.0
    assert [c["symbol"] for c in snap["closed"]] == ["B", "A"]
    assert len(snap["curve"]) == 2  # la línea a medio escribir se ignora


def test_trade_detail_rejects_bad_tokens(tmp_path):
    from solbot.memesim import trade_detail

    for bad in ("", "../../etc/passwd", "a" * 50, "0OIl" * 10):
        with pytest.raises(ValueError):
            trade_detail(tmp_path, bad)


def test_trade_detail_reads_closed_trade_and_downsamples(tmp_path):
    import json

    from solbot.memesim import trade_detail

    mint = "So11111111111111111111111111111111111111112"
    closed = {"symbol": "X", "token": mint, "pool": "P", "entry": 1.0, "qty": 0.0, "cost": 4.0, "opened": 0.0,
              "peak": 2.0, "entry_liq": 1.0, "steps_done": 1, "proceeds": 6.0, "last": 1.5, "closed": 9.0,
              "reason": "trailing", "pnl": 2.0, "mult": 1.5}
    (tmp_path / "state.json").write_text(json.dumps({"positions": {}, "closed": [closed]}))
    (tmp_path / "paths").mkdir()
    (tmp_path / "paths" / f"{mint}.jsonl").write_text("".join(f'{{"t": {i}, "p": {i}, "l": 1}}\n' for i in range(1000)))
    (tmp_path / "events.jsonl").write_text(f'{{"t": 1, "kind": "buy", "token": "{mint}"}}\n{{"t": 2, "kind": "buy", "token": "other"}}\n')
    d = trade_detail(tmp_path, mint, max_points=100)
    assert d["status"] == "closed" and d["trade"]["reason"] == "trailing"
    assert len(d["path"]) == 100 and d["path"][-1]["t"] == 999
    assert len(d["events"]) == 1
