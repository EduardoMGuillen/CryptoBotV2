import pytest

from solbot.jupiter import JupiterError
from solbot.memesim import Book, Fill, Position, Rules, TxSim, concentration, screen, step_position

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


# ---- transacción simulada ------------------------------------------------------------------

def quote_ok(out_amount=1_000_000, out_usd=4.0):
    calls = []

    def quote(i, o, amount):
        calls.append((i, o, amount))
        return {"outAmount": str(out_amount), "outUsdValue": out_usd}

    return quote, calls


def txsim(quote, rng=0.99, sol=100.0, **rules):
    return TxSim(quote, lambda: sol, Rules(**rules), rng=lambda: rng, sleep=lambda s: None)


def test_buy_quotes_exact_lamports_and_charges_platform_fee():
    quote, calls = quote_ok(out_amount=5_000_000, out_usd=3.96)
    fill = txsim(quote).buy("MINT", 4.0)
    assert calls == [("So11111111111111111111111111111111111111112", "MINT", 40_000_000)]
    assert fill.ok and fill.raw == 5_000_000
    assert fill.usd == pytest.approx(3.96 * 0.99)
    assert fill.fee_usd == pytest.approx(0.0006 * 100)


def test_failed_tx_still_pays_network_fee_and_does_not_quote():
    quote, calls = quote_ok()
    fill = txsim(quote, rng=0.01).sell("MINT", 123)
    assert not fill.ok and fill.note == "fallo"
    assert fill.fee_usd == pytest.approx(0.06)
    assert calls == []


def test_sell_net_of_network_fee():
    quote, _ = quote_ok(out_usd=2.0)
    fill = txsim(quote).sell("MINT", 10)
    assert fill.usd == pytest.approx(2.0 * 0.99 - 0.06)


def test_no_route_when_jupiter_rejects():
    def quote(i, o, a):
        raise JupiterError("Jupiter rechazó la cotización (HTTP 400): no route")

    fill = txsim(quote).sell("MINT", 10)
    assert not fill.ok and fill.note == "sin_ruta"


def test_api_error_is_not_no_route():
    def quote(i, o, a):
        raise JupiterError("Jupiter no respondió a la cotización tras 3 intentos (HTTP 429).")

    assert txsim(quote).sell("MINT", 10).note == "error"


def test_empty_output_is_no_route():
    quote, _ = quote_ok(out_amount=0)
    assert txsim(quote).sell("MINT", 10).note == "sin_ruta"


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

def test_book_open_partial_and_close():
    book = Book(cash=100.0)
    p = book.open(pair(), NOW, 4.0, Fill(ok=True, usd=3.9, raw=4_000_000, fee_usd=0.05), decimals=6)
    assert book.cash == pytest.approx(95.95)
    assert p.qty == pytest.approx(4.0) and p.cost == pytest.approx(4.05)
    book.sell(p, 4.0 / 3, 3.0, 0.05, NOW + 60, "tp1")
    assert p.steps_done == 1 and "MINT" in book.positions
    book.pay_fee(p, 0.05)  # venta fallida
    book.sell(p, p.qty, 2.0, 0.05, NOW + 120, "trailing")
    assert book.positions == {}
    closed = book.closed[0]
    assert closed["pnl"] == pytest.approx(3.0 + 2.0 - 0.05 - 4.05)
    assert book.cash == pytest.approx(100 - 4.05 + 3.0 - 0.05 + 2.0)


def test_stop_sell_closes_even_if_quantity_remains():
    book = Book(cash=100.0)
    p = book.open(pair(), NOW, 4.0, Fill(ok=True, usd=3.9, raw=4_000_000, fee_usd=0.0), decimals=6)
    book.sell(p, p.qty, 1.0, 0.0, NOW + 60, "stop")
    assert book.positions == {} and book.closed[0]["reason"] == "stop"


def test_book_respects_max_open():
    book = Book(cash=100.0)
    for i in range(RULES.max_open):
        book.open(pair(baseToken={"address": f"M{i}", "symbol": "X"}), NOW, 4.0, Fill(ok=True, raw=1), decimals=0)
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


def test_sim_sell_retries_then_gives_up_without_route(tmp_path):
    from solbot.memesim import Sim

    sim = Sim(tmp_path, RULES, rpc_url="http://unused")
    p = sim.book.open(pair(), NOW, 4.0, Fill(ok=True, usd=3.9, raw=4_000_000, fee_usd=0.0), decimals=6)
    outcomes = iter([Fill(ok=False, fee_usd=0.06, note="fallo")] + [Fill(ok=False, note="sin_ruta")] * 3)
    sim.tx.sell = lambda mint, raw: next(outcomes)
    sim.execute_sell(p, 1.0, "stop", 0.0007, NOW + 5)  # falla: paga red, sigue abierta
    assert "MINT" in sim.book.positions and p.fees == pytest.approx(0.06)
    for i in range(3):
        sim.execute_sell(p, 1.0, "liquidez", 0.0001, NOW + 10 + i)
    assert sim.book.positions == {}
    assert sim.book.closed[0]["reason"] == "sin_ruta"
    assert sim.book.closed[0]["pnl"] == pytest.approx(-4.06)
    assert sim.funnel["tx_fallidas"] == 1 and sim.funnel["tx_sin_ruta"] == 3


def test_sim_sell_records_real_multiple(tmp_path):
    import json

    from solbot.memesim import Sim

    sim = Sim(tmp_path, RULES, rpc_url="http://unused")
    # 4 USD a 0.001 USD por token = 4 000 tokens
    p = sim.book.open(pair(), NOW, 4.0, Fill(ok=True, usd=3.9, raw=4_000_000_000, fee_usd=0.0), decimals=6)
    sim.tx.sell = lambda mint, raw: Fill(ok=True, usd=1.0, fee_usd=0.06)
    sim.execute_sell(p, 1.0, "stop", 0.00074, NOW + 5)  # pantalla x0.74, pero solo llega 1 USD
    event = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert event["mult"] == pytest.approx(0.74)
    assert event["real_mult"] == pytest.approx(0.25)


def test_copycat_detection_uses_oldest_mint_with_same_name(tmp_path):
    from solbot.memesim import Sim

    sim = Sim(tmp_path, RULES, rpc_url="http://unused")
    original = pair(baseToken={"address": "ORIG", "symbol": "Karl"}, pairCreatedAt=1000)
    copy = pair(baseToken={"address": "COPY", "symbol": "KARL "}, pairCreatedAt=2000)
    sim.note_symbol(copy)
    sim.note_symbol(original)
    assert not sim.is_copy(original)
    assert sim.is_copy(copy)


def test_variants_decide_independently_on_same_candidate(tmp_path):
    from solbot.memesim import Sim

    sim = Sim(tmp_path, RULES, rpc_url="http://unused",
              variants={"post": Rules(), "sin_copias": Rules(skip_copycats=True)})
    for d in sim.desks:
        d.tx.buy = lambda mint, usd: Fill(ok=True, usd=usd, raw=4_000_000_000, fee_usd=0.0)
    for d in sim.desks:
        d.try_buy(pair(), NOW, lambda t: 6, is_copy=True)
    post, clean = sim.desks
    assert "MINT" in post.book.positions
    assert clean.book.positions == {} and clean.funnel["rechazo_copia"] == 1
    assert clean.folder == tmp_path / "variants" / "sin_copias"
    sim.save()
    assert (tmp_path / "state.json").exists() and (tmp_path / "variants" / "sin_copias" / "state.json").exists()


def test_variant_folder_validates_names(tmp_path):
    from solbot.memesim import variant_folder

    assert variant_folder(tmp_path, "") == tmp_path
    assert variant_folder(tmp_path, "principal") == tmp_path
    assert variant_folder(tmp_path, "post") == tmp_path / "variants" / "post"
    assert variant_folder(tmp_path, "sin_copias") == tmp_path / "variants" / "sin_copias"
    for bad in ("../x", "A", "a/b", "x" * 40):
        with pytest.raises(ValueError):
            variant_folder(tmp_path, bad)


def test_with_jup_price_overrides_dexscreener_price(tmp_path):
    from solbot.memesim import Sim

    sim = Sim(tmp_path, RULES, rpc_url="http://unused")
    sim._get = lambda url, **params: {"MINT": {"usdPrice": 0.002, "decimals": 6}}
    out = sim.with_jup_price({"MINT": pair(), "GONE": pair(baseToken={"address": "GONE", "symbol": "G"})})
    assert out["MINT"]["priceUsd"] == "0.002" and out["MINT"]["dexPriceUsd"] == "0.001"
    assert out["MINT"]["liquidity"]["usd"] == 30_000  # la liquidez sigue siendo la de DexScreener
    assert out["GONE"]["priceUsd"] == "0.001"  # sin precio de Jupiter se usa el de DexScreener
    assert sim.decimals["MINT"] == 6


def test_min_age_waits_then_buys_when_old_enough(tmp_path):
    from solbot.memesim import Sim

    sim = Sim(tmp_path, RULES, rpc_url="http://unused",
              variants={"post": Rules(), "viejas": Rules(min_age_min=30.0)})
    for d in sim.desks:
        d.tx.buy = lambda mint, usd: Fill(ok=True, usd=usd, raw=4_000_000_000, fee_usd=0.0)
    sim.holders_ok = lambda pair: None
    sim.with_jup_price = lambda pairs: pairs
    sim.decimals["MINT"] = 6
    young = pair(pairCreatedAt=(NOW - 10 * 60) * 1000)
    sim.watch = {"MINT": NOW}
    sim.best_pairs = lambda tokens, prefer=None: {"MINT": young} if "MINT" in tokens else {}
    import solbot.memesim as m
    real_time = m.time.time
    m.time.time = lambda: NOW
    try:
        sim.evaluate()
        post, old = sim.desks
        assert "MINT" in post.book.positions and old.book.positions == {}
        assert sim.waiting == {"MINT": {"viejas"}}
        sim.evaluate()  # sigue muy nueva: espera, y post no compra dos veces
        assert len(post.funnel) and post.funnel["compras"] == 1 and old.book.positions == {}
        m.time.time = lambda: NOW + 25 * 60  # ya tiene 35 min
        sim.evaluate()
        assert "MINT" in old.book.positions and sim.waiting == {}
        assert post.funnel["compras"] == 1
    finally:
        m.time.time = real_time


def test_size_uses_capital_not_inflated_marks():
    book = Book(cash=96.0)
    p = book.open(pair(), NOW, 4.0, Fill(ok=True, raw=4_000_000_000), decimals=6)
    p.last = 10_000.0  # precio falso
    assert book.equity() > 1_000_000
    assert book.capital() == pytest.approx(96.0)  # 92 de efectivo + 4 de costo


def test_best_pairs_prefers_bought_pool_and_ignores_foreign_quotes(tmp_path):
    from solbot.memesim import Sim

    sim = Sim(tmp_path, RULES, rpc_url="http://unused")
    real = pair(pairAddress="REAL", liquidity={"usd": 100.0, "base": 1, "quote": 1})
    fake = pair(pairAddress="FAKE", liquidity={"usd": 380e6, "base": 1, "quote": 1}, priceUsd="0.45")
    foreign = pair(pairAddress="FOREIGN", quoteToken={"address": "OTHER", "symbol": "X"},
                   liquidity={"usd": 1e9, "base": 1, "quote": 1})
    sim._get = lambda url, **kw: [fake, real, foreign]
    assert sim.best_pairs(["MINT"], prefer={"MINT": "REAL"})["MINT"]["pairAddress"] == "REAL"
    assert sim.best_pairs(["MINT"])["MINT"]["pairAddress"] == "FAKE"  # sin compra previa: el más líquido
    sim._get = lambda url, **kw: [foreign]
    assert sim.best_pairs(["MINT"]) == {}


def test_price_spike_needs_confirmation(tmp_path):
    from solbot.memesim import Sim

    sim = Sim(tmp_path, RULES, rpc_url="http://unused")
    desk = sim.desks[0]
    p = desk.book.open(pair(), NOW, 4.0, Fill(ok=True, raw=4_000_000_000), decimals=6)
    sold = []
    desk.execute_sell = lambda *a, **k: sold.append(a)
    spiked = {"MINT": pair(priceUsd="0.45")}
    desk.manage(spiked, NOW + 5, lambda t: 6)
    assert p.last == 0.001 and sold == [] and desk.funnel["precio_sospechoso"] == 1
    desk.manage(spiked, NOW + 10, lambda t: 6)  # se repite: se acepta
    assert p.last == 0.45 and sold


def test_backup_rotates(tmp_path):
    import datetime
    import tarfile

    from solbot.memesim import backup

    folder = tmp_path / "meme"
    folder.mkdir()
    (folder / "state.json").write_text("{}")
    (folder / "state.tmp").write_text("x")
    dest = tmp_path / "backups"
    for h in range(4):
        path = backup(folder, dest, keep=2, now=datetime.datetime(2026, 11, 7, h, 0))
    assert sorted(p.name for p in dest.glob("*.tar.gz")) == ["meme-20261107-0200.tar.gz", "meme-20261107-0300.tar.gz"]
    names = tarfile.open(path).getnames()
    assert "meme/state.json" in names and "meme/state.tmp" not in names


def test_shared_quote_is_scaled_and_expires(tmp_path):
    from solbot.memesim import Sim

    sim = Sim(tmp_path, RULES, rpc_url="http://unused")
    calls = []

    def order(i, o, amount):
        calls.append(amount)
        return {"outAmount": "1000", "outUsdValue": 4.0}

    sim.jupiter.order = order
    a = sim.quote("SOL", "MINT", 100)
    b = sim.quote("SOL", "MINT", 50)  # otra mesa, mismo instante: se reutiliza a la mitad
    assert calls == [100]
    assert a["outAmount"] == "1000" and b["outAmount"] == "500" and b["outUsdValue"] == pytest.approx(2.0)
    c = sim.quote("SOL", "MINT", 100, max_age=0.0)  # vencida: cotiza de nuevo
    assert calls == [100, 100] and c["outAmount"] == "1000"


def test_shared_quote_reraises_no_route(tmp_path):
    from solbot.memesim import Sim

    sim = Sim(tmp_path, RULES, rpc_url="http://unused")
    sim.jupiter.order = lambda i, o, a: (_ for _ in ()).throw(JupiterError("Jupiter rechazó la cotización (HTTP 400)"))
    for _ in range(2):
        with pytest.raises(JupiterError):
            sim.quote("MINT", "SOL", 10)


def test_momentum_filter(tmp_path):
    from solbot.memesim import Desk

    rules = Rules(min_buy_ratio=1.5)
    desk = Desk("m", tmp_path, rules, TxSim(lambda *a: {"outAmount": "1", "outUsdValue": 1.0}, lambda: 100.0, rules,
                                             rng=lambda: 0.99, sleep=lambda s: None))
    weak = pair(txns={"m5": {"buys": 10, "sells": 9}})
    assert desk.try_buy(weak, NOW, lambda t: 6, is_copy=False) is None
    assert desk.funnel["rechazo_momentum"] == 1 and desk.book.positions == {}
    strong = pair(txns={"m5": {"buys": 30, "sells": 10}})
    desk.try_buy(strong, NOW, lambda t: 6, is_copy=False)
    assert "MINT" in desk.book.positions


def test_full_exit_at_single_target():
    rules = Rules(targets=((1.5, 1.0),))
    book = Book(cash=100.0)
    p = book.open(pair(), NOW, 4.0, Fill(ok=True, raw=4_000_000_000), decimals=6)
    assert step_position(p, 0.0016, 30_000, NOW + 60, rules) == [(1.0, "tp1")]
    book.sell(p, p.qty, 6.0, 0.05, NOW + 60, "tp1")
    assert book.positions == {} and book.closed[0]["reason"] == "tp1"


def test_only_pump_filter(tmp_path):
    from solbot.memesim import Desk

    rules = Rules(only_pump=True)
    desk = Desk("p", tmp_path, rules, TxSim(lambda *a: {"outAmount": "1", "outUsdValue": 1.0}, lambda: 100.0, rules,
                                             rng=lambda: 0.99, sleep=lambda s: None))
    desk.try_buy(pair(), NOW, lambda t: 6, is_copy=False)
    assert desk.funnel["rechazo_no_pump"] == 1 and desk.book.positions == {}
    desk.try_buy(pair(baseToken={"address": "AbCpump", "symbol": "P"}), NOW, lambda t: 6, is_copy=False)
    assert "AbCpump" in desk.book.positions
