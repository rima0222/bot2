"""Configuration.

* Static config  -> ``.env`` / environment (host, data dir, exchange id, ...).
* Runtime config -> editable from the web panel, validated, stored in SQLite.
* Secrets        -> exchange API keys, encrypted at rest (Fernet) in SQLite.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import re
import secrets as _secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from db import Database
from utils import TF_MS

BASE_DIR = Path(__file__).resolve().parent
PAIR_RE = re.compile(r"^[A-Z0-9]{2,15}/[A-Z0-9]{2,10}(:[A-Z0-9]{2,10})?$")
MAX_PAIRS = 10  # resource cap for small VPS


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))  # real env wins


@dataclass(frozen=True)
class StaticConfig:
    base_dir: Path
    env_file: Path
    data_dir: Path
    db_path: Path
    backup_dir: Path
    log_dir: Path
    host: str
    port: int
    secret_key: str
    exchange_id: str
    data_source: str  # "live" (real exchange market data) | "sim" (synthetic, for tests)
    log_level: str
    mem_soft_mb: int
    mem_hard_mb: int
    sim_speed: float


def _persisted_secret(data_dir: Path) -> str:
    """Secret key lives in .env; fall back to a 0600 file so it survives restarts."""
    f = data_dir / ".secret_key"
    if f.exists():
        return f.read_text().strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    key = _secrets.token_urlsafe(48)
    f.write_text(key)
    with contextlib.suppress(OSError):
        os.chmod(f, 0o600)
    return key


def load_static() -> StaticConfig:
    env_file = Path(os.environ.get("BOT_ENV_FILE", BASE_DIR / ".env"))
    _load_env_file(env_file)
    data_dir = Path(os.environ.get("BOT_DATA_DIR", BASE_DIR / "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir = Path(os.environ.get("BOT_LOG_DIR", BASE_DIR / "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    source = os.environ.get("BOT_DATA_SOURCE", "live").lower()
    if source not in ("live", "sim"):
        raise SystemExit("BOT_DATA_SOURCE must be 'live' or 'sim'")
    return StaticConfig(
        base_dir=BASE_DIR,
        env_file=env_file,
        data_dir=data_dir,
        db_path=data_dir / "bot.db",
        backup_dir=backup_dir,
        log_dir=log_dir,
        host=os.environ.get("BOT_HOST", "127.0.0.1"),
        port=int(os.environ.get("BOT_PORT", "8888")),
        secret_key=os.environ.get("BOT_SECRET_KEY") or _persisted_secret(data_dir),
        exchange_id=os.environ.get("BOT_EXCHANGE", "lbank").lower(),
        data_source=source,
        log_level=os.environ.get("BOT_LOG_LEVEL", "INFO").upper(),
        mem_soft_mb=int(os.environ.get("BOT_MEM_SOFT_MB", "300")),
        mem_hard_mb=int(os.environ.get("BOT_MEM_HARD_MB", "380")),
        sim_speed=float(os.environ.get("BOT_SIM_SPEED", "1")),
    )


# --------------------------------------------------------------------------
# Runtime settings
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Spec:
    kind: str  # int | float | choice | list
    default: Any
    label: str
    group: str
    help: str = ""
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()
    restart: bool = False

    def coerce(self, value: Any) -> Any:
        if self.kind in ("int", "float"):
            if isinstance(value, bool) or value in (None, ""):
                raise ValueError("must be a number")
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise ValueError("must be a number") from None
            if num != num or num in (float("inf"), float("-inf")):
                raise ValueError("must be a finite number")
            if self.kind == "int":
                if num != int(num):
                    raise ValueError("must be a whole number")
                num = int(num)
            if self.lo is not None and num < self.lo:
                raise ValueError(f"must be at least {self.lo:g}")
            if self.hi is not None and num > self.hi:
                raise ValueError(f"must be at most {self.hi:g}")
            return num
        if self.kind == "choice":
            if value not in self.choices:
                raise ValueError("must be one of: " + ", ".join(self.choices))
            return value
        if self.kind == "list":
            items = value.replace("\n", ",").split(",") if isinstance(value, str) else list(value or [])
            out: list[str] = []
            for raw in items:
                s = str(raw).strip().upper()
                if not s:
                    continue
                if not PAIR_RE.match(s):
                    raise ValueError(f"'{s}' is not a valid pair (example: BTC/USDT)")
                if s not in out:
                    out.append(s)
            if not out:
                raise ValueError("add at least one pair")
            if len(out) > MAX_PAIRS:
                raise ValueError(f"at most {MAX_PAIRS} pairs on this server")
            return out
        raise ValueError("unknown setting type")


_TFS = tuple(TF_MS)
SCHEMA: dict[str, Spec] = {
    # Trading
    "mode": Spec("choice", "paper", "Trading mode", "Trading", "Paper simulates fills; Live sends real orders.",
                 choices=("paper", "live")),
    "pairs": Spec("list", ["BTC/USDT", "ETH/USDT"], "Trading pairs", "Trading",
                  "Comma separated, e.g. BTC/USDT, ETH/USDT"),
    "timeframe": Spec("choice", "5m", "Candle timeframe", "Trading", choices=_TFS),
    # Risk
    "leverage": Spec("int", 5, "Max leverage (x)", "Risk",
                     "Automatically reduced when the stop would sit too close to liquidation.", 1, 50),
    "risk_pct": Spec("float", 1.0, "Risk per trade (% of equity)", "Risk", lo=0.05, hi=10),
    "sl_atr_mult": Spec("float", 1.5, "Stop distance (x ATR)", "Risk", lo=0.3, hi=10),
    "min_sl_pct": Spec("float", 0.5, "Minimum stop distance (%)", "Risk", lo=0.05, hi=20),
    "rr": Spec("float", 1.5, "Take-profit (x risk)", "Risk", lo=0.5, hi=10),
    "max_positions": Spec("int", 2, "Max open positions", "Risk", lo=1, hi=20),
    "max_drawdown_pct": Spec("float", 15.0, "Max drawdown stop (%)", "Risk",
                             "Closes everything and halts the bot until you clear it.", 1, 90),
    "daily_loss_pct": Spec("float", 5.0, "Daily loss limit (%)", "Risk",
                           "Blocks new trades until the next UTC day.", 0.5, 50),
    "max_hold_bars": Spec("int", 24, "Max bars in a trade", "Risk", lo=1, hi=1000),
    "cooldown_bars": Spec("int", 3, "Cooldown after a trade (bars)", "Risk", lo=0, hi=100),
    # Execution
    "fee_pct": Spec("float", 0.06, "Taker fee (%, paper)", "Execution", lo=0, hi=1),
    "slippage_pct": Spec("float", 0.02, "Simulated slippage (%, paper)", "Execution", lo=0, hi=2),
    "max_slippage_pct": Spec("float", 0.3, "Skip entry if price moved more than (%)", "Execution", lo=0.01, hi=5),
    "paper_equity": Spec("float", 1000.0, "Paper start balance (USDT)", "Execution",
                         "Applied when you reset the paper account.", 10, 1e9),
    # Strategy / ML
    "edge": Spec("float", 0.06, "Required model edge", "Strategy",
                 "Trade only when P(up) is at least 0.5 + edge (long) or at most 0.5 - edge (short).", 0.01, 0.45),
    "min_model_acc": Spec("float", 0.52, "Min validation accuracy", "Strategy",
                          "The model must reach this accuracy on unseen recent candles before it may trade.", 0.5, 0.9),
    "retrain_minutes": Spec("int", 60, "Retrain every (minutes)", "Strategy", lo=5, hi=1440),
    "horizon_bars": Spec("int", 3, "Prediction horizon (bars)", "Strategy", lo=1, hi=30),
    "min_atr_pct": Spec("float", 0.05, "Min volatility ATR (%)", "Strategy", lo=0, hi=10),
    "max_atr_pct": Spec("float", 3.0, "Max volatility ATR (%)", "Strategy", lo=0.1, hi=50),
    # System
    "backup_hours": Spec("int", 24, "Auto-backup every (hours)", "System", lo=1, hi=720),
    "backup_keep": Spec("int", 7, "Auto-backups to keep", "System", lo=1, hi=100),
    "web_port": Spec("int", 8888, "Web panel port", "System", "Applied after a restart.", 1024, 65535, restart=True),
}


class Settings:
    """Validated runtime settings; attribute access (``settings.leverage``)."""

    def __init__(self, db: Database, overrides: dict[str, Any] | None = None):
        self._db = db
        self._v: dict[str, Any] = {k: s.default for k, s in SCHEMA.items()}
        for k, v in (overrides or {}).items():
            if k in SCHEMA:
                self._v[k] = SCHEMA[k].coerce(v)
        for k, v in (db.kv_get("settings", {}) or {}).items():
            if k in SCHEMA:
                with contextlib.suppress(ValueError):
                    self._v[k] = SCHEMA[k].coerce(v)

    def __getattr__(self, name: str) -> Any:
        v = self.__dict__.get("_v")
        if v is not None and name in v:
            return v[name]
        raise AttributeError(name)

    def as_dict(self) -> dict[str, Any]:
        return dict(self._v)

    def validate(self, changes: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for k, raw in changes.items():
            spec = SCHEMA.get(k)
            if spec is None:
                errors[k] = "unknown setting"
                continue
            try:
                clean[k] = spec.coerce(raw)
            except ValueError as exc:
                errors[k] = str(exc)
        merged = {**self._v, **clean}
        if merged["min_atr_pct"] >= merged["max_atr_pct"]:
            errors["max_atr_pct"] = "must be greater than the minimum volatility"
        if errors:
            raise ValueError("; ".join(f"{k}: {m}" for k, m in errors.items()))
        return clean

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        clean = self.validate(changes)
        new = {**self._v, **clean}
        self._db.kv_set("settings", new)
        self._v = new  # atomic swap
        return clean

    @staticmethod
    def schema_public() -> list[dict[str, Any]]:
        return [
            {"key": k, "kind": s.kind, "label": s.label, "group": s.group, "help": s.help,
             "min": s.lo, "max": s.hi, "choices": list(s.choices), "restart": s.restart}
            for k, s in SCHEMA.items()
        ]


# --------------------------------------------------------------------------
# Secrets (exchange API credentials)
# --------------------------------------------------------------------------
SECRET_NAMES = ("api_key", "api_secret", "api_password")


class SecretStore:
    def __init__(self, db: Database, secret_key: str):
        digest = hashlib.sha256(secret_key.encode()).digest()
        self._f = Fernet(base64.urlsafe_b64encode(digest))
        self._db = db

    def set(self, name: str, value: str) -> None:
        if name not in SECRET_NAMES:
            raise ValueError("unknown secret")
        self._db.execute("INSERT OR REPLACE INTO secrets(name,value) VALUES(?,?)",
                         (name, self._f.encrypt(value.encode())))

    def get(self, name: str) -> str | None:
        row = self._db.one("SELECT value FROM secrets WHERE name=?", (name,))
        if row is None:
            return None
        try:
            return self._f.decrypt(bytes(row["value"])).decode()
        except InvalidToken:
            return None  # encrypted with another SECRET_KEY (e.g. restored on a new server)

    def delete_all(self) -> None:
        self._db.execute("DELETE FROM secrets")

    def masked(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for n in SECRET_NAMES:
            v = self.get(n)
            out[n] = None if not v else (v[:3] + "…" + v[-3:] if len(v) > 10 else "•••••")
        return out

    def credentials(self) -> dict[str, str] | None:
        key, sec = self.get("api_key"), self.get("api_secret")
        if not key or not sec:
            return None
        creds = {"apiKey": key, "secret": sec}
        pw = self.get("api_password")
        if pw:
            creds["password"] = pw
        return creds
