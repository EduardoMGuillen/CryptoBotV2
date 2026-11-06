import pandas as pd
import pytest

from solbot import strategies as st
from solbot.bot import Bot
from solbot.orders import Fill, Refused, RetryLater, Sleeve, Unknown
from solbot.state import Store
from util import frame

UP = frame([10, 11, 12])  # cierre 12 > SMA(2) 11.5 → 100% SOL
DOWN = frame([10, 11, 12, 11])  # cierre 11 < SMA(2) 11.5 → 100% USDC


class Clock:
    def __init__(self):
        self.now = pd.Timestamp("2024-01-04 00:05", tz="UTC")

    def __call__(self):
        return self.now


class FakeExecutor:
    """Llena al precio de referencia sin comisión; se le puede programar un error."""

    def __init__(self, mode="paper", kill=False):
        self.mode = mode
        self.killed = kill
        self.intents = []
        self.fail_with = None

    def kill(self):
        return self.killed

    def fund(self, price):
        return Sleeve(sol=0.0, usdc=1000.0)

    def execute(self, intent, sleeve, on_send=None):
        self.intents.append(intent)
        if self.fail_with:
            error, self.fail_with = self.fail_with, None
            raise error
        price = intent.reference_price
        if intent.side == "buy":
            return Fill("buy", intent.amount_ui / price, intent.amount_ui, price, 0.0, None, "test")
        return Fill("sell", intent.amount_ui, intent.amount_ui * price, price, 0.0, None, "test")


def make_bot(tmp_path, executor=None, candles=UP, price=12.0, clock=None):
    executor = executor or FakeExecutor()
    store = Store(tmp_path / executor.mode, mode=executor.mode)
    feed = {"df": candles, "price": price}
    bot = Bot(strategy=st.make("trend_sma", n=2), store=store, executor=executor,
              candles=lambda: feed["df"], price=lambda: feed["price"], now=clock or Clock())
    return bot, store, executor, feed


def test_first_paper_cycle_buys_with_the_whole_paper_balance(tmp_path):
    bot, store, executor, _ = make_bot(tmp_path)
    bot.cycle()
    state = store.load()
    assert state["sleeve"] == {"sol": pytest.approx(1000 / 12), "usdc": pytest.approx(0.0)}
    assert state["last_bar"] == "2024-01-03"
    assert state["start"]["value_usd"] == 1000.0 and state["start"]["price"] == 12.0
    assert [t["side"] for t in state["trades"]] == ["buy"]


def test_the_same_daily_candle_is_never_traded_twice(tmp_path):
    bot, _, executor, _ = make_bot(tmp_path)
    bot.cycle()
    bot.cycle()
    assert len(executor.intents) == 1


def test_a_new_candle_with_the_trend_down_sells_everything(tmp_path):
    bot, store, executor, feed = make_bot(tmp_path)
    bot.cycle()
    feed.update(df=DOWN, price=11.0)
    bot.cycle()
    state = store.load()
    assert executor.intents[1].side == "sell"
    assert executor.intents[1].amount == int(1000 / 12 * 1e9)
    assert state["sleeve"]["sol"] == pytest.approx(0.0, abs=1e-6)
    assert state["sleeve"]["usdc"] == pytest.approx(1000 / 12 * 11, rel=1e-6)


def test_holding_when_the_signal_does_not_change(tmp_path):
    bot, store, executor, feed = make_bot(tmp_path)
    bot.cycle()
    feed.update(df=frame([10, 11, 12, 13]), price=13.0)
    bot.cycle()
    assert len(executor.intents) == 1
    assert [m[0] for m in store.load()["marks"]] == ["2024-01-03", "2024-01-04"]


def test_a_paused_bot_does_nothing(tmp_path):
    bot, store, executor, _ = make_bot(tmp_path)
    bot.set_running(False)
    bot.cycle()
    assert executor.intents == [] and store.load()["last_bar"] is None


def test_the_kill_file_blocks_every_cycle(tmp_path):
    bot, store, executor, _ = make_bot(tmp_path, executor=FakeExecutor(kill=True))
    bot.cycle()
    assert executor.intents == []
    assert "KILL" in store.load()["notice"]


def test_a_temporary_failure_retries_the_same_candle_later(tmp_path):
    clock = Clock()
    executor = FakeExecutor()
    executor.fail_with = RetryLater("Jupiter no respondió")
    bot, store, _, _ = make_bot(tmp_path, executor=executor, clock=clock)
    bot.cycle()
    assert store.load()["last_bar"] is None and "Jupiter" in store.load()["error"]

    bot.cycle()  # todavía dentro de la espera
    assert len(executor.intents) == 1

    clock.now += pd.Timedelta(minutes=6)
    bot.cycle()
    assert len(executor.intents) == 2
    assert store.load()["last_bar"] == "2024-01-03" and store.load()["error"] is None


def test_a_refused_order_skips_the_candle_and_says_why(tmp_path):
    executor = FakeExecutor()
    executor.fail_with = Refused("El impacto de precio supera el máximo")
    bot, store, _, _ = make_bot(tmp_path, executor=executor)
    bot.cycle()
    state = store.load()
    assert state["last_bar"] == "2024-01-03"
    assert state["trades"] == []
    assert "impacto" in state["notice"]


def test_an_order_with_unknown_outcome_pauses_until_someone_checks(tmp_path):
    executor = FakeExecutor()
    executor.fail_with = Unknown("sin respuesta de /execute", {"request_id": "req-1"})
    bot, store, _, _ = make_bot(tmp_path, executor=executor)
    bot.cycle()
    state = store.load()
    assert state["running"] is False and state["pending"]["request_id"] == "req-1"

    bot.set_running(True)
    bot.cycle()
    assert len(executor.intents) == 1  # sigue bloqueado hasta que alguien revise

    bot.resolve_pending(executed=False)
    bot.cycle()
    assert len(executor.intents) == 2  # no salió: reintenta la misma señal


def test_confirming_that_an_unknown_order_did_execute_books_it_once(tmp_path):
    executor = FakeExecutor()
    executor.fail_with = Unknown("sin respuesta", {
        "request_id": "req-1", "bar": "2024-01-03", "side": "buy",
        "amount": 1_000_000_000, "quote_out": 80_000_000_000, "reference_price": 12.0})
    bot, store, _, _ = make_bot(tmp_path, executor=executor)
    bot.cycle()
    bot.resolve_pending(executed=True)
    state = store.load()
    assert state["sleeve"] == {"sol": pytest.approx(80.0), "usdc": pytest.approx(0.0)}
    assert state["last_bar"] == "2024-01-03" and state["pending"] is None

    bot.set_running(True)
    bot.cycle()
    assert len(executor.intents) == 1  # no vuelve a comprar la misma señal


class SendThenBreak(FakeExecutor):
    def execute(self, intent, sleeve, on_send=None):
        self.intents.append(intent)
        on_send({"id": intent.id, "bar": intent.bar, "side": intent.side, "amount": intent.amount,
                 "quote_out": 1, "reference_price": intent.reference_price, "request_id": "r"})
        raise KeyError("signature")


def test_an_unexpected_error_after_sending_a_live_order_pauses_instead_of_retrying(tmp_path):
    executor = SendThenBreak(mode="live")
    bot, store, _, _ = make_bot(tmp_path, executor=executor)
    bot.set_running(True)
    bot.cycle()
    state = store.load()
    assert state["running"] is False and state["pending"] is not None


def test_preview_explains_the_next_action_without_trading(tmp_path):
    bot, _, executor, _ = make_bot(tmp_path)
    preview = bot.preview()
    assert preview["action"] == "buy"
    assert preview["usd"] == pytest.approx(1000.0)
    assert executor.intents == []


def test_a_trade_larger_than_the_cap_is_split_across_days():
    from solbot.bot import plan
    decision = plan(st.make("trend_sma", n=2), UP, Sleeve(sol=0.0, usdc=100.0), price=12.0, max_trade_usd=40.0)
    assert decision["action"] == "buy"
    assert decision["usd"] == pytest.approx(40.0)
    assert "tope" in decision["reason"]


def test_each_closed_candle_reports_what_the_bot_did(tmp_path):
    events = []
    bot, _, _, feed = make_bot(tmp_path)
    bot.notify = events.append
    bot.cycle()
    feed.update(df=frame([10, 11, 12, 13]), price=13.0)
    bot.cycle()
    assert [e["kind"] for e in events] == ["fill", "hold"]
    assert events[0]["side"] == "buy" and events[0]["sol"] == pytest.approx(1000 / 12)


def test_an_unknown_outcome_is_reported_right_away(tmp_path):
    events = []
    executor = FakeExecutor()
    executor.fail_with = Unknown("sin respuesta", {"request_id": "r"})
    bot, _, _, _ = make_bot(tmp_path, executor=executor)
    bot.notify = events.append
    bot.cycle()
    assert [e["kind"] for e in events] == ["unknown"]


class SendThenCrash(FakeExecutor):
    """Imita un proceso que muere a mitad de /execute: anota la orden y no vuelve."""

    def execute(self, intent, sleeve, on_send=None):
        self.intents.append(intent)
        on_send({"id": intent.id, "bar": intent.bar, "side": intent.side, "amount": intent.amount,
                 "quote_out": 80_000_000_000, "reference_price": intent.reference_price, "request_id": "r"})
        raise SystemExit("proceso detenido")


def test_a_crash_while_an_order_is_in_flight_never_trades_that_signal_again(tmp_path):
    executor = SendThenCrash(mode="live")
    bot, store, _, _ = make_bot(tmp_path, executor=executor)
    bot.set_running(True)
    with pytest.raises(SystemExit):
        bot.cycle()
    bot.cycle()  # el bot vuelve a arrancar
    assert len(executor.intents) == 1
    assert store.load()["pending"]["request_id"] == "r"


def test_a_failure_before_sending_retries_instead_of_pausing(tmp_path):
    executor = FakeExecutor(mode="live")
    executor.fail_with = ValueError("respuesta que no es JSON")
    bot, store, _, _ = make_bot(tmp_path, executor=executor)
    bot.set_running(True)
    bot.cycle()
    state = store.load()
    assert state["running"] is True and state["pending"] is None
    assert state["outcome"] == "retry"


def test_an_older_candle_than_the_last_decision_is_ignored(tmp_path):
    bot, store, executor, feed = make_bot(tmp_path)
    bot.cycle()  # decide con 2024-01-03
    feed.update(df=frame([10, 11]))  # llega una lectura vieja que termina el 2024-01-02
    bot.cycle()
    assert len(executor.intents) == 1
    assert store.load()["last_bar"] == "2024-01-03"


def test_failures_before_a_new_candle_do_not_use_up_its_retries(tmp_path):
    clock = Clock()
    bot, store, executor, feed = make_bot(tmp_path, clock=clock)
    good = feed["df"]

    def broken():
        raise ConnectionError("Binance caído")

    bot.candles = broken
    for _ in range(6):
        bot.cycle()
        clock.now += pd.Timedelta(minutes=6)
    bot.candles = lambda: good
    executor.fail_with = RetryLater("Jupiter no respondió")
    bot.cycle()
    assert store.load()["outcome"] == "retry"


def test_confirming_an_order_without_quote_amounts_uses_the_reference_price(tmp_path):
    executor = FakeExecutor()
    executor.fail_with = Unknown("sin respuesta", {"request_id": "r", "bar": "2024-01-03", "side": "buy",
                                                    "amount": 120_000_000, "quote_out": None, "reference_price": 12.0})
    bot, store, _, _ = make_bot(tmp_path, executor=executor)
    bot.cycle()
    bot.resolve_pending(executed=True)
    assert store.load()["sleeve"]["sol"] == pytest.approx(10.0)  # 120 USDC / 12
