#!/usr/bin/env python3
"""Entry point.

  python main.py                 run the bot + web panel
  python main.py --init          create .env / database / admin login (used by install.sh)
  python main.py --reset-password   generate a new admin password
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import secrets
import sys
from pathlib import Path


def _upsert_env(path: Path, key: str, value: str) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if pat.match(line):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def cmd_init(args: argparse.Namespace) -> int:
    from config import BASE_DIR

    env = Path(os.environ.get("BOT_ENV_FILE", BASE_DIR / ".env"))
    text = env.read_text() if env.exists() else ""
    if "BOT_SECRET_KEY=" not in text:
        _upsert_env(env, "BOT_SECRET_KEY", secrets.token_urlsafe(48))
    if "BOT_EXCHANGE=" not in text:
        _upsert_env(env, "BOT_EXCHANGE", "lbank")
    if args.host:
        _upsert_env(env, "BOT_HOST", args.host)
    if args.port:
        _upsert_env(env, "BOT_PORT", str(args.port))
    for k in ("BOT_HOST", "BOT_PORT"):
        if k not in env.read_text():
            _upsert_env(env, k, "127.0.0.1" if k == "BOT_HOST" else "8888")
    from config import load_static
    from db import Database
    from web_panel.auth import ensure_admin

    static = load_static()
    db = Database(static.db_path)
    password = ensure_admin(db)
    db.close()
    print(json.dumps({"user": "admin", "password": password, "host": static.host, "port": static.port}))
    return 0


def cmd_reset_password(_: argparse.Namespace) -> int:
    from config import load_static
    from db import Database
    from web_panel.auth import set_admin

    static = load_static()
    db = Database(static.db_path)
    password = secrets.token_urlsafe(12)
    set_admin(db, "admin", password)
    db.close()
    print(json.dumps({"user": "admin", "password": password}))
    return 0


async def amain() -> int:
    import uvicorn

    from config import SecretStore, Settings, load_static
    from context import BotState, Context, setup_logging
    from data_agent import CcxtFeed, DataAgent, SimFeed
    from db import Database
    from execution_agent import ExecutionAgent
    from risk_agent import RiskAgent
    from strategy_agent import StrategyAgent
    from supervisor import Supervisor
    from web_panel.app import create_app
    from web_panel.auth import ensure_admin

    static = load_static()
    db = Database(static.db_path)
    setup_logging(static, db)
    log = logging_get("bot.main")
    settings = Settings(db, {"web_port": static.port})
    ctx = Context(static, db, settings, SecretStore(db, static.secret_key), BotState(db))

    fresh_password = ensure_admin(db)
    if fresh_password:
        log.warning("no admin account existed - created one. Password: %s (run --reset-password to change)",
                    fresh_password)

    if static.data_source == "sim":
        feed = SimFeed(static.sim_speed)
        log.warning("USING SIMULATED MARKET DATA (BOT_DATA_SOURCE=sim) - for testing only")
    else:
        feed = CcxtFeed(static.exchange_id, prefer_swap=static.exchange_id != "lbank")
    data = DataAgent(ctx, feed)
    if static.data_source == "sim":
        data.price_poll = max(0.5, 5.0 / static.sim_speed)
        data.candle_poll = max(1.0, 15.0 / static.sim_speed)
    execution = ExecutionAgent(ctx)
    strategy = StrategyAgent(ctx, data)
    risk = RiskAgent(ctx, execution, data)
    sup = Supervisor(ctx, data, strategy, risk, execution)
    ctx.agents = {"data": data, "strategy": strategy, "risk": risk, "execution": execution, "supervisor": sup}

    app = create_app(sup)
    port = settings.web_port
    server = uvicorn.Server(uvicorn.Config(
        app, host=static.host, port=port, log_level="warning", access_log=False, server_header=False,
        timeout_graceful_shutdown=5, ws_max_size=1 << 20, limit_concurrency=64, loop="asyncio"))
    sup.on_exit = lambda: setattr(server, "should_exit", True)

    await sup.start()
    log.info("panel listening on http://%s:%d", static.host, port)
    try:
        await server.serve()
    except SystemExit as exc:
        log.error("web panel could not start (port %d in use?). Change it with BOT_PORT in .env.", port)
        return int(exc.code or 1)
    finally:
        await sup.shutdown()
    log.info("stopped%s", " (restart requested)" if ctx.restart_requested else "")
    return 0


def logging_get(name: str):
    import logging

    return logging.getLogger(name)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", action="store_true", help="create config, database and admin login, then exit")
    ap.add_argument("--reset-password", action="store_true", help="set a new random admin password, then exit")
    ap.add_argument("--host", help="with --init: panel bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, help="with --init: panel port (default 8888)")
    args = ap.parse_args()
    if args.init:
        return cmd_init(args)
    if args.reset_password:
        return cmd_reset_password(args)
    with contextlib.suppress(OSError, AttributeError):
        os.nice(10)  # be polite to the other services on this VPS
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
