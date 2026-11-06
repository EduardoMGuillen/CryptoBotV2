# Solbot

Bot de SOL/USDC sobre Jupiter con backtest honesto, modo papel, modo real e interfaz web local.

Una vez al día, cuando cierra la vela diaria de SOL (00:00 UTC, 6:00 p. m. en Honduras), la
estrategia decide qué parte de su dinero va en SOL y qué parte en USDC, y el bot rebalancea con
un swap en Jupiter. El mismo código decide en el backtest, en papel y en real.

## Qué dicen los datos (feb-2021 → nov-2026)

| Con $1,000 al inicio | Valor final | Anual | Caída máxima | Prueba 2024→hoy |
|---|---:|---:|---:|---:|
| Comprar y mantener SOL | $6,568 | +39% | −96% | −12.3% |
| Tendencia SMA 50 (la que usa el bot) | $32,815 | +85% | −69% | −3.5% |

- La estrategia se eligió **solo** con 2021–2023 (mejor Sharpe) y se juzgó en 2024–hoy, datos
  que la elección nunca vio. En esa prueba perdió menos que mantener SOL, pero no ganó.
- Ninguna estrategia evita caídas grandes en SOL. Las de tendencia las acortan: en 2022 la
  SMA 50 perdió −29% contra −94% de mantener.
- Los costos de Jupiter (≈0.02% por swap en SOL/USDC) casi no cambian los resultados.
- El pasado no garantiza nada. El detalle completo está en la pestaña **Backtest**.

## Uso

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp solbot.example.toml solbot.toml      # y ajústalo
.venv/bin/solbot serve                  # interfaz en http://127.0.0.1:8787 + bot
```

| Comando | Qué hace |
|---|---|
| `solbot serve` | Interfaz web y bot (revisa cada minuto si cerró una vela nueva) |
| `solbot backtest` | Compara todas las estrategias con datos reales |
| `solbot ensayo --usd 1` | Recorre una orden real con tu wallet (cotiza, valida, simula y firma) **sin enviarla** |
| `solbot wallet` | Dirección y saldos (nunca muestra la llave) |
| `solbot run-once` | Un ciclo del bot (para cron) |
| `solbot status` | Resumen en la terminal |

## Dinero real

La wallet es la de `~/.solana-paper-bot` (`wallet.address` + `wallet.json` en formato
solana-keygen, permisos 600). La llave nunca se copia al proyecto ni aparece en la interfaz o en
los registros. El RPC (`SOLANA_RPC_URL`) se lee de `~/.config/PolloTraderBot/config.env`.

Antes de firmar cualquier orden, el bot:

1. Revisa el archivo `KILL` (parada de emergencia) y el tope de operaciones por día.
2. Verifica que los saldos reales de la wallet cuadren con lo que el bot cree tener y que la
   reserva de SOL para comisiones quede intacta.
3. Pide la orden a Jupiter (`/swap/v2/order`) y la valida: par, monto, wallet, diferencia de
   precio con Binance, impacto de precio, slippage y tope por operación.
4. Simula la transacción en el RPC.
5. Anota la orden en el diario (`data/live/journal.jsonl`), firma localmente y la envía
   (`/swap/v2/execute`).

Si no se sabe si una orden se ejecutó, el bot se pausa y la interfaz te pide revisar en Solscan
y marcar si salió o no. Así nunca opera dos veces la misma señal.

Los límites (`[live]` en `solbot.toml`) solo se cambian en el archivo: la interfaz los muestra
pero no puede subirlos. Iniciar el modo real desde la interfaz pide los últimos 4 caracteres de
la wallet.

**Parada de emergencia:** `touch ~/.solana-paper-bot/KILL` (funciona aunque la interfaz no
responda). Para quitarla: `rm ~/.solana-paper-bot/KILL`.

## Avisos por Telegram

Igual que PolloTraderBot: llena `TELEGRAM_BOT_TOKEN` (de @BotFather) y `TELEGRAM_CHAT_ID` en
`~/.config/PolloTraderBot/config.env`. Se leen en cada aviso, así que no hace falta reiniciar.
Llega un mensaje por cada operación, por cada cierre diario sin operar y de inmediato si una
orden queda sin confirmar.

## Dejarlo corriendo

`solbot serve` corre mientras su terminal esté abierta. Para que arranque solo con la sesión y
se reinicie si falla, crea `~/.config/systemd/user/solbot.service`:

```ini
[Unit]
Description=Solbot: bot SOL/USDC con Jupiter
After=network-online.target

[Service]
WorkingDirectory=/home/edu/Crypto1
ExecStart=/home/edu/Crypto1/.venv/bin/solbot serve --no-browser
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now solbot
journalctl --user -u solbot -f      # registros
```

Si la computadora se suspende, el bot no corre; al volver decide con la última vela cerrada.

## Tests

```bash
.venv/bin/pytest
```

Incluyen las reglas que protegen el dinero: que ninguna estrategia vea el futuro, que una orden
insegura nunca abra la llave, que la reserva de SOL no se toque y que una señal no se opere dos
veces.

---

Gráficas: [TradingView Lightweight Charts](https://www.tradingview.com/lightweight-charts/)
(Apache 2.0, licencia en `solbot/web/vendor/`). Esto no es asesoría financiera: el bot puede
perder dinero.
