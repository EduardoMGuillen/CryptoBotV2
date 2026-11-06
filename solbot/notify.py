"""Avisos por Telegram, configurados igual que en PolloTraderBot.

Lee TELEGRAM_BOT_TOKEN (de @BotFather) y TELEGRAM_CHAT_ID del entorno o, si no están, del
mismo ~/.config/PolloTraderBot/config.env. Se vuelven a leer en cada aviso: al llenarlos,
los avisos empiezan sin reiniciar el bot. Sin los dos valores, los avisos quedan apagados.
El token nunca aparece en los registros.
"""

from __future__ import annotations

import html
import logging
import os

import requests

from .rpc import read_env_file

log = logging.getLogger("solbot")
DEFAULT_ENV_FILE = "~/.config/PolloTraderBot/config.env"


def settings(env: dict, env_file) -> tuple[str, str] | None:
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (env.get("TELEGRAM_CHAT_ID") or "").strip()
    if not (token and chat) and env_file:
        values = read_env_file(env_file)
        token = token or values.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat = chat or values.get("TELEGRAM_CHAT_ID", "").strip()
    return (token, chat) if token and chat else None


class Telegram:
    def __init__(self, env: dict | None = None, env_file=DEFAULT_ENV_FILE, session=None):
        self.env = os.environ if env is None else env
        self.env_file = env_file
        self.session = session or requests.Session()

    def enabled(self) -> bool:
        return settings(self.env, self.env_file) is not None

    def send(self, text: str) -> bool:
        conf = settings(self.env, self.env_file)
        if not conf:
            return False
        token, chat = conf
        try:
            resp = self.session.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=10,
            )
        except requests.RequestException as exc:
            log.warning("Telegram no respondió (%s).", type(exc).__name__)  # el mensaje de la excepción lleva la URL con el token
            return False
        if resp.status_code != 200:
            log.warning("Telegram rechazó el aviso (HTTP %s).", resp.status_code)
            return False
        return True


def _money(value: float) -> str:
    return f"{'-' if value < 0 else ''}${abs(value):,.2f}"


def _pct(value: float) -> str:
    return f"{'+' if value > 0 else ''}{value * 100:.1f}%"


def message(event: dict) -> str:
    esc = html.escape
    live = event.get("mode") == "live"
    tag = "" if live else "[papel] "
    kind = event.get("kind")

    def value_line() -> list[str]:
        if event.get("value") is None:
            return []
        line = f"Valor del bot: {_money(event['value'])}"
        if event.get("pnl_pct") is not None:
            line += f" ({_pct(event['pnl_pct'])} desde el inicio)"
        return [line]

    if kind == "fill":
        buy = event["side"] == "buy"
        lines = [
            f"{'🟢' if buy else '🔴'} <b>{tag}{'Compró' if buy else 'Vendió'} {event['sol']:.4f} SOL</b> "
            f"a {_money(event['price'])} ({event['usdc']:,.2f} USDC)",
            f"Estrategia: {esc(event.get('strategy', ''))}",
            *value_line(),
        ]
        if event.get("reason"):
            lines.append(esc(event["reason"]))
        if live and event.get("signature"):
            lines.append(f'<a href="https://solscan.io/tx/{esc(event["signature"])}">Ver en Solscan</a>')
        if not live:
            lines.append("Simulado: no se movió dinero real.")
        return "\n".join(lines)
    if kind == "hold":
        return "\n".join([f"⚪ {tag}Sin operar hoy ({esc(event.get('bar', ''))}).", esc(event.get("reason", "")), *value_line()])
    if kind == "refused":
        return f"⚠️ {tag}Orden cancelada. {esc(event.get('message', ''))}"
    if kind == "unknown":
        return (f"🚨 <b>{tag}Orden sin confirmar</b>\n{esc(event.get('message', ''))}\n"
                "El bot quedó pausado. Revisa la wallet en Solscan y marca en el panel si se ejecutó o no.")
    if kind == "skipped":
        return f"⚠️ {tag}{esc(event.get('message', ''))}"
    if kind == "started":
        return f"▶️ {tag}Bot en marcha con {esc(event.get('strategy', ''))}."
    if kind == "paused":
        return f"⏸ {tag}Bot en pausa: no abre operaciones."
    return f"{tag}{esc(str(event.get('message', '')))}"
