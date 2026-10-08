"""Supervisor & System Monitor Agent.

* starts every agent as a guarded task (crash -> logged -> restarted with backoff)
* server metrics (psutil), memory guard, equity sampling
* automatic SQLite backups with rotation, log/DB pruning
* pause / resume / kill-switch / halt handling and settings application
"""
from __future__ import annotations

import asyncio
import gc
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable

import psutil

from config import SECRET_NAMES
from context import Context
from data_agent import DataAgent
from execution_agent import ExecutionAgent
from risk_agent import RiskAgent
from strategy_agent import StrategyAgent
from utils import now_ms

BACKUP_LIMITS = {"auto": None, "manual": 20, "pre-restore": 5, "pre-reset": 5}


class Supervisor:
    name = "supervisor"

    def __init__(self, ctx: Context, data: DataAgent, strategy: StrategyAgent, risk: RiskAgent,
                 execution: ExecutionAgent):
        self.ctx = ctx
        self.data, self.strategy, self.risk, self.execution = data, strategy, risk, execution
        self.log = ctx.getlog("supervisor")
        self.tasks: dict[str, asyncio.Task] = {}
        self.restarts: dict[str, deque[float]] = {}
        self.proc = psutil.Process(os.getpid())
        self.proc.cpu_percent(None)
        psutil.cpu_percent(None)
        self.on_exit: Callable[[], None] | None = None  # set by main (graceful shutdown hook)

    # ---- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        await self.execution.start()
        self.data.price_listeners.append(self.execution.on_prices)
        self.data.candle_listeners.append(self.execution.on_candle)
        factories: dict[str, Callable[[], Awaitable[None]]] = {
            "data": self.data.run, "strategy": self.strategy.run, "retrain": self.strategy.retrain_loop,
            "risk": self.risk.run, "risk-monitor": self.risk.monitor_loop, "account": self.execution.account_loop,
            "metrics": self.metrics_loop, "equity": self.equity_loop, "backup": self.backup_loop,
            "maintenance": self.maintenance_loop,
        }
        for name, factory in factories.items():
            self.tasks[name] = asyncio.create_task(self._guard(name, factory), name=name)
        self.log.info("started %d tasks in %s mode on %s (%s data)", len(self.tasks), self.ctx.settings.mode,
                      self.ctx.static.exchange_id, self.ctx.static.data_source)

    async def _guard(self, name: str, factory: Callable[[], Awaitable[None]]) -> None:
        window = self.restarts.setdefault(name, deque(maxlen=6))
        while True:
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                now = time.time()
                window.append(now)
                crashes = sum(1 for t in window if now - t < 300)
                self.log.exception("task '%s' crashed (%d in the last 5 min)", name, crashes)
                if crashes >= 5 and not self.ctx.state.halted:
                    self.ctx.state.set_halt(f"unstable: '{name}' keeps crashing")
                    self.log.error("halting new trades: '%s' crashed %d times", name, crashes)
                await asyncio.sleep(min(60, 2 ** crashes))

    async def shutdown(self) -> None:
        for t in self.tasks.values():
            t.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for closer in (self.data.close, self.execution.stop):
            try:
                await closer()
            except Exception:  # noqa: BLE001
                pass
        self.ctx.db.close()

    # ---- operator actions ------------------------------------------------------------
    def pause(self) -> None:
        self.ctx.state.set_enabled(False)
        self.log.warning("bot paused by operator (open trades remain managed)")

    def resume(self) -> None:
        if self.ctx.state.halted:
            raise ValueError(f"Bot is halted ({self.ctx.state.halted}). Clear the halt first.")
        self.ctx.state.set_enabled(True)
        self.log.info("bot resumed")

    def clear_halt(self) -> None:
        reason = self.ctx.state.halted
        self.ctx.state.set_halt(None)
        self.risk.reset_baselines()
        self.log.warning("halt cleared by operator (was: %s); drawdown baseline reset", reason)

    async def kill_switch(self) -> int:
        """Emergency stop: block new trades, then flatten every open position."""
        self.ctx.state.set_enabled(False)
        self.log.error("KILL SWITCH activated")
        n = await self.execution.close_all("kill_switch")
        left = len(self.execution.positions)
        if left:
            self.log.error("kill switch: %d position(s) could not be closed - check the exchange now", left)
        return n

    def request_restart(self) -> None:
        self.ctx.restart_requested = True
        self.log.warning("restart requested")
        if self.on_exit:
            self.on_exit()

    async def apply_settings(self, changes: dict[str, Any], secrets: dict[str, str] | None = None,
                             clear_secrets: bool = False, confirm_live: bool = False) -> dict[str, Any]:
        s = self.ctx.settings
        clean = s.validate(changes)
        new_mode = clean.get("mode", s.mode)
        if new_mode != s.mode:
            if new_mode == "live" and not confirm_live:
                raise ValueError("Type ENABLE LIVE to confirm switching to live trading with real money.")
        if clear_secrets:
            self.ctx.secrets.delete_all()
        for name, value in (secrets or {}).items():
            if name in SECRET_NAMES and isinstance(value, str) and value.strip():
                self.ctx.secrets.set(name, value.strip())
        if new_mode != s.mode:
            await self.execution.switch_mode(new_mode)  # raises ValueError if it cannot
            self.log.warning("trading mode switched to %s", new_mode.upper())
        if clean:
            s.update(clean)
        if "timeframe" in clean or "pairs" in clean:
            self.log.info("market config changed; new data is backfilled automatically")
        return clean

    # ---- backups ---------------------------------------------------------------------
    def backup_dir(self) -> Path:
        return self.ctx.static.backup_dir

    def make_backup(self, kind: str = "manual") -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        dest = self.backup_dir() / f"{kind}-{stamp}.db"
        n = 1
        while dest.exists():
            dest = self.backup_dir() / f"{kind}-{stamp}-{n}.db"
            n += 1
        self.ctx.db.backup(dest)
        self._rotate(kind)
        self.log.info("backup created: %s (%.1f KB)", dest.name, dest.stat().st_size / 1024)
        return dest

    def _rotate(self, kind: str) -> None:
        keep = self.ctx.settings.backup_keep if kind == "auto" else BACKUP_LIMITS.get(kind, 20)
        files = sorted(self.backup_dir().glob(f"{kind}-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            old.unlink(missing_ok=True)

    def list_backups(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.backup_dir().glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True):
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "mtime": int(st.st_mtime * 1000),
                        "kind": next((k for k in BACKUP_LIMITS if p.name.startswith(k + "-")), "other")})
        return out

    def backup_path(self, name: str) -> Path:
        p = (self.backup_dir() / name).resolve()
        if p.parent != self.backup_dir().resolve() or p.suffix != ".db" or not p.is_file():
            raise FileNotFoundError(name)
        return p

    async def restore_backup(self, src: Path) -> None:
        """Validate -> safety backup -> pause -> swap database -> reload state."""
        await asyncio.to_thread(self.ctx.db.validate_backup, src)
        was_enabled = self.ctx.state.enabled
        self.ctx.state.enabled = False  # in-memory only: block entries during the swap
        try:
            async with self.execution.lock:
                await asyncio.to_thread(self.make_backup, "pre-restore")
                await asyncio.to_thread(self.ctx.db.restore, src)
            self.ctx.state.enabled = bool(self.ctx.db.kv_get("bot_enabled", True))
            self.ctx.state.halted = self.ctx.db.kv_get("halt", None)
            from config import Settings

            fresh = Settings(self.ctx.db, {"web_port": self.ctx.static.port})
            self.ctx.settings._v = fresh._v  # keep the same object other agents hold
            self.execution._load_positions(self.ctx.settings.mode)
            if hasattr(self.execution.broker, "balance"):
                self.execution.broker.balance = float(self.ctx.db.kv_get("paper_balance", self.execution.broker.balance))
            self.strategy.models.clear()
            self.strategy.meta.clear()
            self.strategy._load_models()
            self.log.warning("database restored from %s", Path(src).name)
        except Exception:
            self.ctx.state.enabled = was_enabled
            raise

    async def backup_loop(self) -> None:
        last = self.ctx.db.kv_get("last_auto_backup", 0) or 0
        while True:
            self.ctx.beat("backup")
            if time.time() - last >= self.ctx.settings.backup_hours * 3600:
                try:
                    await asyncio.to_thread(self.make_backup, "auto")
                    last = time.time()
                    self.ctx.db.kv_set("last_auto_backup", last)
                except Exception:  # noqa: BLE001
                    self.log.exception("automatic backup failed")
                    last = time.time() - self.ctx.settings.backup_hours * 3600 + 900  # retry in 15 min
            await asyncio.sleep(60)

    # ---- monitoring --------------------------------------------------------------------
    async def metrics_loop(self) -> None:
        while True:
            try:
                self.ctx.metrics.update(self.collect_metrics())
                await self._memory_guard()
            except Exception:  # noqa: BLE001
                self.log.exception("metrics failed")
            self.ctx.beat("metrics")
            await asyncio.sleep(5)

    def collect_metrics(self) -> dict[str, Any]:
        vm = psutil.virtual_memory()
        du = psutil.disk_usage(str(self.ctx.static.data_dir))
        try:
            load = os.getloadavg()
        except (AttributeError, OSError):
            load = (0.0, 0.0, 0.0)
        rss = self.proc.memory_info().rss
        return {
            "cpu_pct": psutil.cpu_percent(None),
            "proc_cpu_pct": self.proc.cpu_percent(None),
            "ram_total_mb": round(vm.total / 1048576),
            "ram_used_mb": round((vm.total - vm.available) / 1048576),
            "ram_pct": vm.percent,
            "proc_rss_mb": round(rss / 1048576, 1),
            "disk_total_gb": round(du.total / 1e9, 1),
            "disk_used_gb": round(du.used / 1e9, 1),
            "disk_pct": du.percent,
            "load1": round(load[0], 2),
            "db_mb": round(self.ctx.db.size_bytes() / 1048576, 2),
            "uptime_s": int(time.time() - self.ctx.started_at),
            "threads": self.proc.num_threads(),
            "ts": now_ms(),
        }

    async def _memory_guard(self) -> None:
        rss_mb = self.proc.memory_info().rss / 1048576
        if rss_mb > self.ctx.static.mem_hard_mb:
            self.log.error("memory %.0f MB above hard limit %d MB - restarting cleanly",
                           rss_mb, self.ctx.static.mem_hard_mb)
            self.request_restart()
        elif rss_mb > self.ctx.static.mem_soft_mb:
            gc.collect()
            self.log.warning("memory %.0f MB above soft limit %d MB - ran GC", rss_mb, self.ctx.static.mem_soft_mb)

    async def equity_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                a = self.execution.account()
                if a["equity"] > 0:
                    self.ctx.db.execute(
                        "INSERT OR REPLACE INTO equity(ts,mode,equity,balance,unrealized) VALUES(?,?,?,?,?)",
                        (now_ms(), self.ctx.settings.mode, a["equity"], a["balance"], a["unrealized"]))
            except Exception:  # noqa: BLE001
                self.log.exception("equity sampling failed")
            self.ctx.beat("equity")

    async def maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await asyncio.to_thread(self.ctx.db.prune)
                await asyncio.to_thread(self.ctx.db.checkpoint)
                gc.collect()
            except Exception:  # noqa: BLE001
                self.log.exception("maintenance failed")
            self.ctx.beat("maintenance")

    def health(self) -> dict[str, Any]:
        now = time.time()
        agents = {}
        for name, t in self.heartbeat_map().items():
            agents[name] = {"age_s": round(now - t, 1) if t else None}
        return {"agents": agents, "tasks": {n: (not t.done()) for n, t in self.tasks.items()}}

    def heartbeat_map(self) -> dict[str, float]:
        return {k: v for k, v in self.ctx.heartbeat.items()}
