"""Línea de comandos.

  solbot serve        interfaz web + bot (cada minuto revisa si cerró una vela nueva)
  solbot backtest     compara las estrategias con datos reales y muestra la tabla
  solbot ensayo       recorre una orden real con tu wallet (cotiza, valida, simula y firma) SIN enviarla
  solbot wallet       dirección y saldos de la wallet (nunca muestra la llave)
  solbot run-once     un solo ciclo del bot (para cron)
  solbot status       resumen del bot en la terminal
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import math
import sys
import webbrowser

from . import config as configuration
from . import market
from .app import App
from .jupiter import SOL_MINT, USDC_MINT
from .orders import Intent, Refused, RetryLater, Sleeve
from .server import make_server


def _pct(value) -> str:
    return "   n/d" if value is None else f"{value * 100:7.1f}%"


def _load(args) -> configuration.Config:
    try:
        return configuration.load(args.config)
    except configuration.ConfigError as exc:
        sys.exit(f"Error en la configuración: {exc}")


def cmd_serve(args) -> None:
    cfg = _load(args)
    app = App(cfg)
    port = args.port or cfg.port
    httpd = make_server(app, host=cfg.host, port=port)
    url = f"http://127.0.0.1:{port}/"
    mode = "DINERO REAL" if cfg.mode == "live" else "papel (simulado)"
    print(f"Solbot en {url}  ·  modo {mode}  ·  estrategia {cfg.strategy.name}")
    print("Ctrl+C para detener.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.")


def cmd_backtest(args) -> None:
    cfg = _load(args)
    report = App(dataclasses.replace(cfg, mode="paper")).backtest(refresh=True)
    s = report["settings"]
    names = {x["id"]: x["name"] for x in report["strategies"]}
    print(f"Evaluado {s['start']} → {s['end']} · entrenamiento hasta {s['split']} · costo {s['fee_rate'] * 100:.2f}% por operación\n")
    for key, title in (("lump", f"Capital inicial de ${s['lump']:,.0f}"), ("dca", f"Aporte semanal de ${s['weekly']:,.0f}")):
        print(f"== {title} ==")
        print(f"{'Estrategia':28s} {'Valor final':>12s} {'Anual':>8s} {'Caída máx':>9s} {'Sharpe':>6s}   {'PRUEBA':>8s} {'vs mantener':>11s}")
        hold_test = report["scenarios"][key]["runs"]["buy_hold"]["test"]["total_return"]
        for sid, r in report["scenarios"][key]["runs"].items():
            f, t = r["full"], r["test"]
            print(f"{names[sid]:28s} {f['final']:12,.0f} {_pct(f['cagr'])} {_pct(f['max_drawdown'])} {f['sharpe']:6.2f}   "
                  f"{_pct(t['total_return'])} {_pct(t['total_return'] - hold_test)}")
        print()
    winner = report["selection"]["winner"]
    print(f"Elegida con datos de entrenamiento: {names[winner]}. "
          f"Tu bot usa: {cfg.strategy.name}. Detalle completo en la interfaz (solbot serve).")


def cmd_wallet(args) -> None:
    cfg = _load(args)
    info = App(dataclasses.replace(cfg, mode="paper")).wallet_info()
    if not info:
        sys.exit(f"No hay wallet en {cfg.live.wallet_dir}.")
    print(f"Dirección:  {info['address']}")
    print(f"SOL:        {info.get('sol', 'n/d')}")
    print(f"USDC:       {info.get('usdc', 'n/d')}")
    print(f"RPC:        {info['rpc']}")
    if info.get("error"):
        print(f"Aviso: {info['error']}")


def cmd_rehearse(args) -> None:
    cfg = _load(args)
    app = App(dataclasses.replace(cfg, mode="live"))
    price = market.last_price("SOLUSDT")
    if args.side == "sell":
        amount = math.floor(args.usd / price * 1e9)
        intent = Intent("ensayo-sell", "ensayo", "sell", SOL_MINT, USDC_MINT, amount, amount / 1e9, amount / 1e9 * price, price)
        sleeve = Sleeve(sol=amount / 1e9, usdc=0.0)
        what = f"vender {amount / 1e9:.6f} SOL (≈ {args.usd:.2f} USD)"
    else:
        amount = math.floor(args.usd * 1e6)
        intent = Intent("ensayo-buy", "ensayo", "buy", USDC_MINT, SOL_MINT, amount, amount / 1e6, amount / 1e6, price)
        sleeve = Sleeve(sol=0.0, usdc=amount / 1e6)
        what = f"comprar SOL con {args.usd:.2f} USDC"
    print(f"Ensayo con la wallet {app.address}: {what}. NO se envía nada.\n")
    try:
        report = app.executor.rehearse(intent, sleeve)
    except (Refused, RetryLater) as exc:
        sys.exit(f"El ensayo se detuvo: {exc}")
    print(f"  Precio de Jupiter:     {report['price']:,.4f} USDC/SOL (Binance: {price:,.4f})")
    print(f"  Ruta:                  {report['route']} ({report['router']})")
    print(f"  Slippage permitido:    {report['slippage_bps']} bps · impacto {report['price_impact_pct']:.4f}% · comisión {report['fee_bps']} bps")
    print(f"  Simulación en el RPC:  {report['simulation']}")
    print(f"  Firma local válida:    {'sí' if report['signature_ok'] else 'NO'}")
    print("\nTodo el camino funciona. La transacción firmada se descartó: no se envió.")


def cmd_run_once(args) -> None:
    cfg = _load(args)
    state = App(cfg).bot.cycle()
    print(f"[{cfg.mode}] {state['outcome']}: {state.get('notice') or ''} {state.get('error') or ''}".strip())


def cmd_status(args) -> None:
    cfg = _load(args)
    st = App(cfg).status()
    perf = st["performance"]
    print(f"Modo {st['mode']} · {'en marcha' if st['running'] else 'pausado'} · {st['strategy']['name']}")
    print(f"SOL: {st['price']}  ·  última vela decidida: {st['last_bar']}")
    if perf:
        print(f"Valor: {perf['value']:,.2f} USD ({_pct(perf['pnl_pct']).strip()}) · mantener SOL: {perf['hold_value']:,.2f} USD")
    if st["signal"]:
        print(f"Señal: {st['signal']['reason']}")
    if st["notice"]:
        print(f"Aviso: {st['notice']}")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="solbot", description="Backtest y bot SOL/USDC con Jupiter.")
    parser.add_argument("--config", default="solbot.toml", help="archivo de configuración (por defecto solbot.toml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="interfaz web + bot")
    serve.add_argument("--port", type=int)
    serve.add_argument("--no-browser", action="store_true", help="no abrir el navegador")
    serve.set_defaults(fn=cmd_serve)

    sub.add_parser("backtest", help="compara las estrategias").set_defaults(fn=cmd_backtest)
    sub.add_parser("wallet", help="dirección y saldos").set_defaults(fn=cmd_wallet)

    rehearse = sub.add_parser("ensayo", help="orden real completa SIN enviarla")
    rehearse.add_argument("--usd", type=float, default=1.0, help="tamaño del ensayo en USD (por defecto 1)")
    rehearse.add_argument("--side", choices=["sell", "buy"], default="sell", help="sell = SOL→USDC, buy = USDC→SOL")
    rehearse.set_defaults(fn=cmd_rehearse)

    sub.add_parser("run-once", help="un ciclo del bot (para cron)").set_defaults(fn=cmd_run_once)
    sub.add_parser("status", help="resumen del bot").set_defaults(fn=cmd_status)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.fn(args)


if __name__ == "__main__":
    main()
