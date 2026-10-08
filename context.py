"""Shared runtime context passed to every agent."""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import time
from typing import Any

from config import SecretStore, Settings, StaticConfig
from db import Database
from utils import now_ms


class BotState:
    """Persisted run-state. Pausing blocks *new entries* only; open trades stay managed."""

    def __init__(self, db: Database):
        self._db = db
        self.enabled: bool = bool(db.kv_get("bot_enabled", True))
        self.halted: str | None = db.kv_get("halt", None)

    @property
    def trading_allowed(self) -> bool:
        return self.enabled and not self.halted

    @property
    def status(self) -> str:
        return "halted" if self.halted else ("active" if self.enabled else "paused")

    def set_enabled(self, value: bool) -> None:
        self.enabled = value
        self._db.kv_set("bot_enabled", value)

    def set_halt(self, reason: str | None) -> None:
        self.halted = reason
        if reason:
            self._db.kv_set("halt", reason)
        else:
            self._db.kv_del("halt")


class DBLogHandler(logging.Handler):
    """Mirrors INFO+ records into SQLite so the panel can show them."""

    def __init__(self, db: Database):
        super().__init__(level=logging.INFO)
        self._db = db

    def emit(self, record: logging.LogRecord) -> None:
        try:
            agent = record.name.split(".", 1)[1] if "." in record.name else record.name
            self._db.execute("INSERT INTO logs(ts,level,agent,msg) VALUES(?,?,?,?)",
                             (now_ms(), record.levelname, agent, record.getMessage()[:2000]))
        except Exception:  # noqa: BLE001 - logging must never raise
            pass


def setup_logging(static: StaticConfig, db: Database | None) -> logging.Logger:
    root = logging.getLogger("bot")
    root.setLevel(getattr(logging, static.log_level, logging.INFO))
    root.handlers.clear()
    root.propagate = False
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(static.log_dir / "bot.log", maxBytes=5_000_000,
                                              backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    if db is not None:
        root.addHandler(DBLogHandler(db))
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    return root


class Context:
    def __init__(self, static: StaticConfig, db: Database, settings: Settings,
                 secrets: SecretStore, state: BotState):
        self.static = static
        self.db = db
        self.settings = settings
        self.secrets = secrets
        self.state = state
        self.log = logging.getLogger("bot")
        self.prices: dict[str, float] = {}
        self.price_ts: dict[str, int] = {}
        self.metrics: dict[str, Any] = {}
        self.heartbeat: dict[str, float] = {}
        self.agents: dict[str, Any] = {}
        self.feed_error: str | None = None
        self.started_at = time.time()
        self.candle_q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.signal_q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self.restart_requested = False

    def beat(self, agent: str) -> None:
        self.heartbeat[agent] = time.time()

    def getlog(self, name: str) -> logging.Logger:
        return logging.getLogger(f"bot.{name}")
