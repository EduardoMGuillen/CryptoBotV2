import json

import pytest

from solbot.state import StateError, Store


def test_a_new_paper_store_starts_running_with_nothing_funded(tmp_path):
    state = Store(tmp_path / "paper", mode="paper").load()
    assert state["running"] is True
    assert state["sleeve"] is None and state["last_bar"] is None


def test_a_new_live_store_starts_paused(tmp_path):
    assert Store(tmp_path / "live", mode="live").load()["running"] is False


def test_saved_state_reads_back(tmp_path):
    store = Store(tmp_path / "paper", mode="paper")
    state = store.load()
    state["sleeve"] = {"sol": 1.5, "usdc": 20.0}
    store.save(state)
    assert Store(tmp_path / "paper", mode="paper").load()["sleeve"] == {"sol": 1.5, "usdc": 20.0}


def test_a_corrupted_state_is_an_error_not_a_fresh_start(tmp_path):
    store = Store(tmp_path / "live", mode="live")
    (tmp_path / "live").mkdir()
    store.path.write_text("{ roto")
    with pytest.raises(StateError):
        store.load()


def test_journal_keeps_events_in_order(tmp_path):
    store = Store(tmp_path / "live", mode="live")
    store.journal({"event": "intent", "id": "a"})
    store.journal({"event": "result", "id": "a"})
    lines = [json.loads(line) for line in store.journal_path.read_text().splitlines()]
    assert [line["event"] for line in lines] == ["intent", "result"]
    assert all("at" in line for line in lines)
