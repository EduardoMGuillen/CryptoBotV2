from solbot import notify


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.payload = payload or {"ok": True, "result": {"message_id": 1}}

    def json(self):
        return self.payload


class FakeTelegram:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        if self.error:
            raise self.error
        return self.response


TOKEN = "1234567890:AAEfakefakefakefakefakefakefakefake"


def test_settings_come_from_the_environment_before_the_shared_file(tmp_path):
    env_file = tmp_path / "config.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=desde-archivo\nTELEGRAM_CHAT_ID=111\n")
    assert notify.settings({"TELEGRAM_BOT_TOKEN": "env", "TELEGRAM_CHAT_ID": "222"}, env_file) == ("env", "222")
    assert notify.settings({}, env_file) == ("desde-archivo", "111")


def test_empty_settings_turn_alerts_off(tmp_path):
    env_file = tmp_path / "config.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n")
    assert notify.settings({}, env_file) is None
    session = FakeTelegram()
    assert notify.Telegram(env={}, env_file=env_file, session=session).send("hola") is False
    assert session.calls == []


def test_send_posts_html_to_the_configured_chat(tmp_path):
    session = FakeTelegram()
    sent = notify.Telegram(env={"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "987"},
                           env_file=tmp_path / "none", session=session).send("<b>hola</b>")
    url, body = session.calls[0]
    assert sent is True
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert body["chat_id"] == "987" and body["parse_mode"] == "HTML" and body["text"] == "<b>hola</b>"


def test_a_failed_send_never_leaks_the_token(tmp_path, caplog):
    import requests
    session = FakeTelegram(error=requests.ConnectionError(f"fallo en https://api.telegram.org/bot{TOKEN}/sendMessage"))
    sent = notify.Telegram(env={"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "987"},
                           env_file=tmp_path / "none", session=session).send("hola")
    assert sent is False
    assert TOKEN not in caplog.text


def test_live_fill_message_says_what_happened_and_links_the_transaction():
    text = notify.message({
        "kind": "fill", "mode": "live", "side": "sell", "sol": 0.3889, "usdc": 34.66, "price": 89.12,
        "signature": "5Sig", "strategy": "Tendencia SMA 50", "value": 34.66, "pnl_pct": -0.002,
        "reason": "La señal pide 0% en SOL <y> hay 100%",
    })
    assert "Vendió 0.3889 SOL" in text
    assert "$89.12" in text
    assert "https://solscan.io/tx/5Sig" in text
    assert "&lt;y&gt;" in text  # el texto variable va escapado para HTML


def test_paper_messages_are_marked_as_simulated():
    text = notify.message({"kind": "fill", "mode": "paper", "side": "buy", "sol": 1.0, "usdc": 89.0, "price": 89.0,
                           "signature": None, "strategy": "X", "value": 1000.0, "pnl_pct": 0.0, "reason": ""})
    assert "papel" in text.lower()
    assert "solscan" not in text


def test_an_order_with_unknown_outcome_is_an_urgent_alert():
    text = notify.message({"kind": "unknown", "mode": "live", "message": "No se sabe si la orden se ejecutó", "strategy": "X"})
    assert "pausado" in text.lower()
