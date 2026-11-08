"""Servidor local de la interfaz.

Seguridad (el bot puede mover dinero real):
- Escucha solo en 127.0.0.1.
- Rechaza cualquier Host que no sea localhost/127.0.0.1 (evita ataques de DNS rebinding).
- Toda acción (POST) exige el token que viene incrustado en la página. Otra web abierta en
  tu navegador no puede leerlo, así que no puede ordenarle nada al bot.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import memesim

WEB = Path(__file__).parent / "web"
log = logging.getLogger("solbot")

CSP = (
    "default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)

GET_ROUTES = {
    "/api/status": lambda app, q: app.status(),
    "/api/backtest": lambda app, q: app.backtest(),
    "/api/chart": lambda app, q: app.chart(int(q.get("days", ["365"])[0])),
    "/api/tickers": lambda app, q: app.tickers(),
    "/api/meme": lambda app, q: memesim.snapshot(memesim.variant_folder(app.cfg.data_dir / "meme", q.get("v", [""])[0])),
    "/api/meme/feed": lambda app, q: memesim.feed(app.cfg.data_dir / "meme"),
    "/api/meme/variants": lambda app, q: memesim.variants_summary(app.cfg.data_dir / "meme"),
    "/api/meme/trade": lambda app, q: memesim.trade_detail(
        memesim.variant_folder(app.cfg.data_dir / "meme", q.get("v", [""])[0]), q.get("token", [""])[0]),
}
POST_ROUTES = {
    "/api/bot/start": lambda app, body: app.start(confirm=body.get("confirm")),
    "/api/bot/pause": lambda app, body: app.pause(),
    "/api/bot/run": lambda app, body: app.run_now(),
    "/api/bot/resolve": lambda app, body: app.resolve(executed=body.get("executed") is True),
    "/api/backtest/refresh": lambda app, body: app.backtest(refresh=True),
}


def make_handler(app, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "solbot"
        sys_version = ""

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload) -> None:
            self._send(status, json.dumps(payload, allow_nan=False, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _run(self, fn) -> None:
            try:
                result = fn()
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            except Exception as exc:
                log.exception("Error en %s", self.path)
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            self._json(200, result)

        def _static(self, relative: str) -> None:
            root = WEB.resolve()
            target = (root / relative).resolve()
            if root not in target.parents or not target.is_file():
                return self._json(404, {"error": "No existe."})
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in ("application/javascript", "image/svg+xml"):
                content_type += "; charset=utf-8"
            self._send(200, target.read_bytes(), content_type)

        def do_GET(self):
            if not self._host_ok():
                return self._json(403, {"error": "Host no permitido."})
            url = urlsplit(self.path)
            if url.path in ("/", "/index.html"):
                html = (WEB / "index.html").read_text(encoding="utf-8").replace("{{TOKEN}}", token)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if url.path in ("/meme", "/meme/"):
                return self._send(200, (WEB / "live.html").read_bytes(), "text/html; charset=utf-8")
            if url.path in ("/meme/clasico", "/meme/clasico/"):
                return self._send(200, (WEB / "meme.html").read_bytes(), "text/html; charset=utf-8")
            if url.path.startswith("/static/"):
                return self._static(url.path[len("/static/"):])
            route = GET_ROUTES.get(url.path)
            if route is None:
                return self._json(404, {"error": "No existe."})
            self._run(lambda: route(app, parse_qs(url.query)))

        def do_POST(self):
            if not self._host_ok():
                return self._json(403, {"error": "Host no permitido."})
            if not secrets.compare_digest(self.headers.get("X-Solbot-Token", ""), token):
                return self._json(403, {"error": "Falta el token de la página. Recarga la interfaz."})
            route = POST_ROUTES.get(urlsplit(self.path).path)
            if route is None:
                return self._json(404, {"error": "No existe."})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except json.JSONDecodeError:
                return self._json(400, {"error": "Cuerpo JSON inválido."})
            self._run(lambda: route(app, body if isinstance(body, dict) else {}))

    return Handler


def _scheduler(app, interval: float, stop: threading.Event) -> None:
    """Corre un ciclo del bot al arrancar y luego cada `interval` segundos."""
    while not stop.is_set():
        try:
            app.bot.cycle()
        except Exception:
            log.exception("Falló un ciclo del bot")
        stop.wait(interval)


def make_server(app, host: str = "127.0.0.1", port: int = 8787, token: str | None = None,
                scheduler: bool = True, interval: float = 60.0) -> ThreadingHTTPServer:
    token = token or secrets.token_urlsafe(24)
    httpd = ThreadingHTTPServer((host, port), make_handler(app, token))
    httpd.daemon_threads = True
    if scheduler:
        stop = threading.Event()
        threading.Thread(target=_scheduler, args=(app, interval, stop), daemon=True, name="solbot-ciclo").start()
        httpd.stop_scheduler = stop
    return httpd
