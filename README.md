# Trading Bot — multi-agent futures bot with a built-in dark admin panel

**فارسی، خلاصه:** این ربات روی VPS اوبونتو نصب می‌شود، با پنل وب تاریک خودش (بدون تلگرام) مدیریت می‌شود،
به‌صورت پیش‌فرض در حالت **Paper** (معامله شبیه‌سازی‌شده با قیمت واقعی) شروع می‌کند و هیچ تنظیمات شبکه/فایروال/VPN
سرور را تغییر نمی‌دهد. نصب: پوشه را روی سرور باز کنید و `sudo bash install.sh` را بزنید؛ آدرس پنل و رمز ورود در پایان چاپ می‌شود.
پنل به‌طور پیش‌فرض فقط روی `127.0.0.1` گوش می‌دهد (با تونل SSH باز می‌شود)؛ برای دسترسی مستقیم `--public` بدهید.

---

## Read this first (honest limits)

* **LBank futures cannot be traded live by this bot.** CCXT supports LBank *spot* only, and LBank's public contract-API
  documentation does not describe the order / position / leverage endpoints. With `BOT_EXCHANGE=lbank` (the default) the bot
  runs in **paper mode with real LBank prices** and refuses to switch to live mode, with a clear message. Live futures
  trading is implemented through CCXT for exchanges that support swaps (`BOT_EXCHANGE=binance`, `bybit`, `okx`, …) and has
  been tested only against a fake exchange, never against a real one. Start with the exchange's testnet / tiny size.
* LBank paper prices come from the **spot** market (the proxy CCXT exposes), so they track, but are not identical to, the perpetual price. Funding fees are not simulated.
* **No profit is promised.** The model is validated on unseen recent candles and must beat both a minimum accuracy and the
  "always guess the majority" baseline before it may trade. On most real markets most of the time it will *not* pass, and the
  bot will correctly sit out. The panel shows each pair's model status. Treat paper results as a smoke test, not a forecast.

## Install (Ubuntu 20.04+/Debian 11+)

```bash
unzip trading-bot.zip && cd trading-bot
sudo bash install.sh                    # panel on 127.0.0.1:8888 (private)
sudo bash install.sh --port 9100        # another port
sudo bash install.sh --public           # bind 0.0.0.0 (put HTTPS/VPN in front; the script does not open ports)
```

One line, fetching everything from your own host (the downloads resume if the connection drops):

```bash
for i in $(seq 8); do curl -fL -C - -o install.sh https://YOUR.HOST/install.sh && break || sleep 3; done; \
  sudo BOT_ARCHIVE_URL=https://YOUR.HOST/trading-bot.zip bash install.sh
```

When it finishes it prints the panel URL, the `admin` password (shown once) and how to reach the private panel through an SSH
tunnel: `ssh -L 8888:127.0.0.1:8888 root@SERVER`, then open `http://127.0.0.1:8888`. Re-running the installer upgrades the code and keeps
your data, settings and login. `sudo bash install.sh --uninstall` removes the service (`--purge` also deletes data).
Forgot the password: `sudo -u tradingbot /opt/trading-bot/venv/bin/python /opt/trading-bot/main.py --reset-password`.

### What the installer touches — and what it never touches

| It does | It never does |
|---|---|
| apt: `ca-certificates curl python3.11+ venv` (adds the deadsnakes PPA only on Ubuntu 22.04, only if Python ≥ 3.11 is missing) | change firewall, iptables/nftables, routes, DNS, sysctl, proxies |
| create system user `tradingbot` (no shell, no login) | touch Xray, V2Ray, WireGuard, Nginx, SSH or any other service/port |
| private virtualenv in `/opt/trading-bot/venv` | install packages system-wide with pip |
| one systemd unit `trading-bot.service` | listen on anything except the one panel port (auto-picks the next free port if yours is taken) |

The service is capped so it cannot starve the VPS: `Nice=10`, `CPUQuota=50%`, `CPUWeight=20`, `IOWeight=20`,
`MemoryHigh/Max` chosen from the machine's RAM (280M/350M on ≤ 640 MB servers), `TasksMax=96`, `OOMScoreAdjust=500`
(under memory pressure the kernel kills this bot **before** your VPN or web server), plus sandboxing
(`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, no capabilities, only IP/Unix sockets). The process
itself also lowers its own priority and restarts cleanly if its memory passes a soft/hard limit. Measured: about 140 MB RSS
with CCXT loaded. Restart policy is `always`, so it comes back after crashes and reboots.

### Resumable downloads

* **install.sh** — `apt` resumes partial `.deb` files and retries 10×; every other file (project archive, each Python wheel)
  is fetched with `curl -C -` in a retry loop (`wget -c` fallback) and checked against its sha256. Wheels are resolved with
  `pip install --dry-run --report`, downloaded one by one (resumable), then installed offline from a local wheelhouse. If the
  server cannot resume, the file restarts cleanly instead of corrupting.
* **Python** — `download.py` (`fetch_resumable`) does HTTP `Range` requests into a `.part` file with retries, backoff and
  checksum verification. Historical candle backfill is resumable too: every page is committed to SQLite as it arrives and
  the next run continues from the newest stored candle.

## The panel

| Page | What it has |
|---|---|
| **Overview** | status (Trading / Paused / Halted), equity, today, since start, win rate, drawdown, equity curve, open positions (close one with a click), pairs with live price and model status, CPU / RAM / disk |
| **Trades** | win rate, profit factor, reward-to-risk, expectancy, average R, max drawdown, fees, streaks; cumulative and daily result charts; candlestick chart with entry/exit markers and live stop/target lines; by pair / by exit reason; filterable history with CSV export |
| **System** | server resources, health of every agent, backups: create, download, restore (typed confirmation), delete, **export database now**, **restore from file**; restart |
| **Settings** | pairs, timeframe, leverage, risk %, stop/target, drawdown & daily-loss limits, fees, model gates, web port, backup schedule; encrypted exchange API keys; password change; paper-account reset |
| **Logs** | latest model decisions (why a signal was or was not traded) and the activity log with level filter |

**Emergency stop** (top right, every page) blocks new trades and closes every open position at market.
The panel is plain HTML/CSS/JS with canvas charts and **no external scripts, fonts or CDNs**, so it works offline and on networks that block CDNs.
Security: scrypt-hashed password, signed `HttpOnly`/`SameSite=Strict` cookie, CSRF header on every change, login lockout after 5 failures, strict
Content-Security-Policy, WebSocket origin check, API keys encrypted at rest and never sent back to the browser.

## How the agents work together

```
 Data Agent ──closed candle──► Strategy & ML Agent ──signal──► Risk Agent ──order plan──► Execution Agent
 (CCXT REST, resumable          features → online logistic         size from risk budget,        paper broker (fees, slippage,
  backfill, SQLite)             regression, retrained hourly,      leverage vs liquidation,      liquidation) or CCXT live broker;
        │                       validated on unseen candles,       drawdown / daily-loss         SL / TP / time-stop watched on
        └──── prices ───────────trend + volatility filters         limits, cooldowns             every tick and candle
                                         ▲
 Supervisor & System Monitor: restarts crashed agents, psutil metrics, memory guard, log rotation, hourly cleanup,
 automatic SQLite backups with rotation, kill-switch, pause/resume, halts (never resumes by itself after a max-drawdown halt)
```

* Pausing blocks **new** entries only — open trades keep their stops. Kill switch = pause + close everything.
* Position size = `equity × risk% ÷ (stop distance + round-trip fees + slippage)`, capped by free margin. Leverage is lowered automatically so the stop sits inside 70 % of the distance to liquidation.
* Live safety: an order that times out is never blindly retried (the bot halts and tells you to check the exchange); every minute the bot compares its positions with the exchange and halts on any mismatch; protective stop orders are also placed on the exchange when it supports them.

## Files

```
main.py            entry point (--init, --reset-password)       config.py   .env + validated runtime settings + encrypted secrets
data_agent.py      market feed, resumable backfill, polling      strategy_agent.py   features, model, signals
risk_agent.py      sizing, limits                                execution_agent.py  paper + live brokers, SL/TP monitor
supervisor.py      supervision, metrics, backups, kill-switch    db.py / context.py / models.py / utils.py / download.py
web_panel/         FastAPI app, auth, analytics, static UI       install.sh  installer      tests/  80+ automated tests
```

## Configuration (`.env`, all optional)

`BOT_HOST` (127.0.0.1) · `BOT_PORT` (8888) · `BOT_EXCHANGE` (lbank) · `BOT_DATA_DIR` · `BOT_LOG_DIR` ·
`BOT_MEM_SOFT_MB` / `BOT_MEM_HARD_MB` · `BOT_LOG_LEVEL` · `BOT_DATA_SOURCE=sim` (synthetic prices, tests/demos only; the panel shows a banner).
Everything else is changed in the panel. Changing the port in Settings applies after *System → Restart bot*.

## Development

```bash
python -m venv venv && . venv/bin/activate && pip install -r requirements-dev.txt
BOT_DATA_SOURCE=sim BOT_SIM_SPEED=300 BOT_DATA_DIR=/tmp/bot python main.py --init && BOT_DATA_SOURCE=sim BOT_SIM_SPEED=300 BOT_DATA_DIR=/tmp/bot python main.py
python -m pytest tests -q
```

## Troubleshooting

* *Panel says "Market data problem"* — the server cannot reach the exchange API (blocked or DNS). The bot keeps retrying and resumes backfill from where it stopped. Check `journalctl -u trading-bot -f`.
* *A pair shows an error in "Markets and models"* — the exchange has no such market; fix the pair in Settings.
* *Everything says "Not trading"* — the model did not beat the baseline on recent data. That is the safety gate working; lower `Min validation accuracy` only if you understand the risk (the baseline check cannot be switched off).
* *Port in use* — the installer picks the next free port and prints it.
