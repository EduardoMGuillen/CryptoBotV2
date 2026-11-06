import http.client
import json
import threading

import pytest

from solbot import server


class FakeApp:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"mode": "paper", "running": True}

    def start(self, confirm=None):
        self.calls.append(("start", confirm))
        if confirm == "malo":
            raise ValueError("Escribe los últimos 4 caracteres de la wallet.")
        return {"running": True}

    def pause(self):
        self.calls.append(("pause",))
        return {"running": False}


@pytest.fixture()
def running():
    app = FakeApp()
    httpd = server.make_server(app, host="127.0.0.1", port=0, token="tok-123", scheduler=False)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield app, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def request(port, method, path, body=None, headers=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    hdrs = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body)
        hdrs["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    payload = resp.read().decode()
    conn.close()
    return resp.status, payload


def test_status_is_served_as_json(running):
    _, port = running
    status, body = request(port, "GET", "/api/status")
    assert status == 200 and json.loads(body) == {"mode": "paper", "running": True}


def test_actions_without_the_page_token_are_rejected(running):
    app, port = running
    status, _ = request(port, "POST", "/api/bot/pause", body={})
    assert status == 403
    assert app.calls == []


def test_actions_with_the_page_token_run(running):
    app, port = running
    status, body = request(port, "POST", "/api/bot/pause", body={}, headers={"X-Solbot-Token": "tok-123"})
    assert status == 200 and json.loads(body) == {"running": False}
    assert app.calls == [("pause",)]


def test_a_foreign_host_header_is_rejected(running):
    _, port = running
    status, _ = request(port, "GET", "/api/status", host="evil.example.com")
    assert status == 403


def test_the_page_carries_the_token_for_its_own_scripts(running):
    _, port = running
    status, body = request(port, "GET", "/")
    assert status == 200 and 'content="tok-123"' in body


def test_static_files_cannot_escape_the_web_folder(running):
    _, port = running
    status, _ = request(port, "GET", "/static/../../pyproject.toml")
    assert status == 404


def test_app_errors_come_back_as_readable_messages(running):
    _, port = running
    status, body = request(port, "POST", "/api/bot/start", body={"confirm": "malo"}, headers={"X-Solbot-Token": "tok-123"})
    assert status == 400 and "4 caracteres" in json.loads(body)["error"]
