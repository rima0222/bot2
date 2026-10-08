"""Single-admin authentication: scrypt password hashes + signed expiring cookies."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from db import Database

SESSION_TTL = 12 * 3600
MIN_PASSWORD = 10
LOCK_AFTER = 5
LOCK_SECONDS = 300


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def get_admin(db: Database) -> dict | None:
    return db.kv_get("admin")


def set_admin(db: Database, user: str, password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Password must be at least {MIN_PASSWORD} characters.")
    db.kv_set("admin", {"user": user, "hash": hash_password(password)})


def ensure_admin(db: Database, user: str = "admin") -> str | None:
    """Create the admin account if missing. Returns the generated password (shown once)."""
    if get_admin(db):
        return None
    password = secrets.token_urlsafe(12)
    set_admin(db, user, password)
    return password


class Sessions:
    def __init__(self, db: Database, secret_key: str):
        self.db = db
        self.key = secret_key.encode()
        self._fails: dict[str, tuple[int, float]] = {}

    def _fingerprint(self, admin: dict) -> str:
        return hashlib.sha256(admin["hash"].encode()).hexdigest()[:10]

    def issue(self, admin: dict) -> str:
        payload = f"{admin['user']}|{int(time.time()) + SESSION_TTL}|{self._fingerprint(admin)}"
        sig = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(payload.encode()).decode() + "." + sig

    def verify(self, token: str | None) -> str | None:
        """Return the username for a valid token, else None."""
        if not token or "." not in token:
            return None
        try:
            b64, sig = token.rsplit(".", 1)
            payload = base64.urlsafe_b64decode(b64.encode()).decode()
            good = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, good):
                return None
            user, exp, fp = payload.split("|")
            admin = get_admin(self.db)
            if not admin or admin["user"] != user or fp != self._fingerprint(admin) or int(exp) < time.time():
                return None
            return user
        except (ValueError, TypeError, UnicodeError):
            return None

    # ---- brute-force throttle (per client IP) ----------------------------------------
    def locked_for(self, ip: str) -> int:
        n, until = self._fails.get(ip, (0, 0.0))
        return max(0, int(until - time.time())) if n >= LOCK_AFTER else 0

    def record(self, ip: str, ok: bool) -> None:
        if ok:
            self._fails.pop(ip, None)
            return
        n, until = self._fails.get(ip, (0, 0.0))
        if n >= LOCK_AFTER and until < time.time():
            n = 0
        n += 1
        self._fails[ip] = (n, time.time() + LOCK_SECONDS if n >= LOCK_AFTER else 0.0)
        if len(self._fails) > 500:  # bound memory
            self._fails = dict(list(self._fails.items())[-200:])

    def login(self, ip: str, user: str, password: str) -> str | None:
        admin = get_admin(self.db)
        # always run a hash so timing doesn't reveal whether the username exists
        stored = admin["hash"] if admin else hash_password("x" * MIN_PASSWORD)
        ok = verify_password(password, stored) and bool(admin) and hmac.compare_digest(user, admin["user"])
        self.record(ip, ok)
        return self.issue(admin) if ok else None
