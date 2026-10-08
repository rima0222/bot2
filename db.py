"""SQLite (WAL) storage with atomic backup / restore."""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS secrets(name TEXT PRIMARY KEY, value BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS candles(
  symbol TEXT NOT NULL, tf TEXT NOT NULL, ts INTEGER NOT NULL,
  o REAL, h REAL, l REAL, c REAL, v REAL,
  PRIMARY KEY(symbol, tf, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, qty REAL, entry REAL,
  sl REAL, tp REAL, leverage INTEGER, margin REAL, liq REAL, risk_amount REAL,
  fee_open REAL, opened_at INTEGER, mode TEXT, meta TEXT);
CREATE TABLE IF NOT EXISTS trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, qty REAL, entry REAL,
  exit REAL, sl REAL, tp REAL, leverage INTEGER, margin REAL, pnl REAL, fees REAL,
  r_mult REAL, opened_at INTEGER, closed_at INTEGER, reason TEXT, mode TEXT);
CREATE INDEX IF NOT EXISTS ix_trades_closed ON trades(closed_at);
CREATE TABLE IF NOT EXISTS equity(
  ts INTEGER NOT NULL, mode TEXT NOT NULL, equity REAL, balance REAL, unrealized REAL,
  PRIMARY KEY(ts, mode)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, level TEXT, agent TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, symbol TEXT, side TEXT,
  prob REAL, price REAL, status TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS models(
  symbol TEXT NOT NULL, tf TEXT NOT NULL, blob TEXT, trained_at INTEGER,
  acc REAL, n_test INTEGER, n_train INTEGER, PRIMARY KEY(symbol, tf));
"""

REQUIRED_TABLES = {"kv", "candles", "positions", "trades", "equity"}


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = self._open(self.path)
        with self.lock:
            self.conn.executescript(SCHEMA_SQL)
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

    @staticmethod
    def _open(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA cache_size=-4096")  # 4 MB page cache
        conn.execute("PRAGMA wal_autocheckpoint=500")
        conn.execute("PRAGMA journal_size_limit=8388608")
        return conn

    # ---- basic ops --------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).lastrowid or 0

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.executemany(sql, rows)
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    @contextlib.contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Atomic multi-statement transaction (re-entrant lock held throughout)."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    # ---- key/value --------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, json.dumps(value)))

    def kv_del(self, key: str) -> None:
        self.execute("DELETE FROM kv WHERE key=?", (key,))

    # ---- maintenance ------------------------------------------------------
    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                total += os.path.getsize(str(self.path) + suffix)
        return total

    def checkpoint(self) -> None:
        with self.lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def prune(self, candle_keep: int = 3000, logs_keep: int = 5000, signals_keep: int = 2000,
              equity_days: int = 120) -> None:
        with self.lock:
            self.conn.execute(
                "DELETE FROM logs WHERE id <= (SELECT COALESCE(MAX(id),0) FROM logs) - ?", (logs_keep,))
            self.conn.execute(
                "DELETE FROM signals WHERE id <= (SELECT COALESCE(MAX(id),0) FROM signals) - ?", (signals_keep,))
            cutoff = int((__import__("time").time() - equity_days * 86400) * 1000)
            self.conn.execute("DELETE FROM equity WHERE ts < ?", (cutoff,))
            pairs = self.conn.execute("SELECT DISTINCT symbol, tf FROM candles").fetchall()
            for r in pairs:
                self.conn.execute(
                    "DELETE FROM candles WHERE symbol=? AND tf=? AND ts < "
                    "(SELECT ts FROM candles WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT 1 OFFSET ?)",
                    (r["symbol"], r["tf"], r["symbol"], r["tf"], candle_keep))

    # ---- backup / restore -------------------------------------------------
    def backup(self, dest: str | Path) -> Path:
        """Consistent snapshot using SQLite's online backup API."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.unlink(missing_ok=True)
        with self.lock:
            out = sqlite3.connect(str(tmp))
            try:
                self.conn.backup(out)
                out.execute("PRAGMA journal_mode=DELETE")  # single self-contained file
            finally:
                out.close()
        os.replace(tmp, dest)
        with contextlib.suppress(OSError):
            os.chmod(dest, 0o600)
        return dest

    @staticmethod
    def validate_backup(src: str | Path) -> None:
        """Raise ValueError unless ``src`` is a healthy database of ours."""
        try:
            conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise ValueError(f"cannot open file as SQLite database: {exc}") from exc
        try:
            try:
                check = conn.execute("PRAGMA integrity_check").fetchone()[0]
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            except sqlite3.Error as exc:
                raise ValueError(f"not a valid SQLite database: {exc}") from exc
            if check != "ok":
                raise ValueError(f"integrity check failed: {check}")
            missing = REQUIRED_TABLES - tables
            if missing:
                raise ValueError(f"not a bot backup (missing tables: {', '.join(sorted(missing))})")
        finally:
            conn.close()

    def restore(self, src: str | Path, keep_keys: tuple[str, ...] = ("admin",)) -> None:
        """Replace the live database with ``src`` (validated first).

        ``keep_keys`` are kv entries preserved from the current database so a
        restore never locks the operator out of the panel.
        """
        self.validate_backup(src)
        with self.lock:
            kept = {k: self.kv_get(k) for k in keep_keys}
            src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
            try:
                src_conn.backup(self.conn)
            finally:
                src_conn.close()
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA_SQL)
            for k, v in kept.items():
                if v is not None:
                    self.kv_set(k, v)

    def close(self) -> None:
        with self.lock:
            with contextlib.suppress(sqlite3.Error):
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.conn.close()
