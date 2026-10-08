import sqlite3
import time

import pytest

from config import SecretStore, Settings
from db import Database
from web_panel import auth


def test_backup_and_restore_roundtrip_keeps_admin_login(env):
    db = env.db
    auth.set_admin(db, "admin", "first-password-123")
    db.kv_set("marker", "before")
    db.execute("INSERT INTO trades(symbol,side,pnl,closed_at,mode) VALUES('BTC/USDT','long',5.0,1,'paper')")
    snap = env.sup.make_backup("manual")
    assert snap.exists() and snap.stat().st_mode & 0o077 == 0  # private file

    db.kv_set("marker", "after")
    db.execute("DELETE FROM trades")
    auth.set_admin(db, "admin", "second-password-456")  # password changed AFTER the backup

    db.restore(snap)
    assert db.kv_get("marker") == "before"
    assert db.one("SELECT COUNT(*) n FROM trades")["n"] == 1
    admin = auth.get_admin(db)
    assert auth.verify_password("second-password-456", admin["hash"])  # login not rolled back
    assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_restore_rejects_garbage_and_foreign_databases(env, tmp_path):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not sqlite" * 50)
    with pytest.raises(ValueError):
        env.db.restore(junk)
    foreign = tmp_path / "foreign.db"
    c = sqlite3.connect(foreign)
    c.execute("CREATE TABLE users(id)")
    c.commit(); c.close()
    with pytest.raises(ValueError, match="not a bot backup"):
        env.db.restore(foreign)
    env.db.kv_set("still", "here")
    assert env.db.kv_get("still") == "here"  # live database untouched


def test_backup_rotation_and_path_safety(env):
    env.settings.update({"backup_keep": 2})
    for _ in range(4):
        env.sup.make_backup("auto")
        time.sleep(0.01)
    assert len([b for b in env.sup.list_backups() if b["kind"] == "auto"]) == 2
    name = env.sup.list_backups()[0]["name"]
    assert env.sup.backup_path(name).name == name
    for bad in ("../bot.db", "..%2Fbot.db", "/etc/passwd", "nope.db", name + "/../../bot.db"):
        with pytest.raises(FileNotFoundError):
            env.sup.backup_path(bad)


def test_prune_bounds_tables(env):
    db = env.db
    db.executemany("INSERT INTO logs(ts,level,agent,msg) VALUES(?,?,?,?)", [(i, "INFO", "t", "m") for i in range(300)])
    db.executemany("INSERT INTO candles(symbol,tf,ts,o,h,l,c,v) VALUES('X','5m',?,1,1,1,1,1)", [(i,) for i in range(500)])
    db.prune(candle_keep=100, logs_keep=50)
    assert db.one("SELECT COUNT(*) n FROM logs")["n"] == 50
    assert db.one("SELECT COUNT(*) n FROM candles")["n"] == 101  # newest `keep` plus the boundary row


def test_settings_validation(env):
    s = env.settings
    assert s.leverage == 5 and s.mode == "paper"
    for bad in ({"leverage": 0}, {"leverage": 2.5}, {"leverage": "abc"}, {"risk_pct": 50}, {"mode": "yolo"},
                {"pairs": "BTCUSDT"}, {"pairs": ""}, {"nonsense": 1}, {"timeframe": "7m"},
                {"min_atr_pct": 5, "max_atr_pct": 1}, {"risk_pct": float("nan")}, {"leverage": True}):
        with pytest.raises(ValueError):
            s.update(bad)
    s.update({"pairs": "btc/usdt, eth/usdt\nsol/usdt, btc/usdt", "leverage": "3", "risk_pct": 0.5})
    assert s.pairs == ["BTC/USDT", "ETH/USDT", "SOL/USDT"] and s.leverage == 3 and s.risk_pct == 0.5
    with pytest.raises(ValueError, match="at most"):
        s.update({"pairs": ",".join(f"A{i}/USDT" for i in range(11))})
    # persisted across restarts, invalid stored values fall back to defaults
    env.db.kv_set("settings", {**s.as_dict(), "leverage": 9999})
    again = Settings(env.db)
    assert again.leverage == 5 and again.pairs[:2] == ["BTC/USDT", "ETH/USDT"]


def test_secrets_are_encrypted_masked_and_key_bound(env):
    store = env.ctx.secrets
    store.set("api_key", "ABCDEFGHIJKLMNOP")
    store.set("api_secret", "supersecretvalue1234")
    raw = bytes(env.db.one("SELECT value FROM secrets WHERE name='api_secret'")["value"])
    assert b"supersecret" not in raw
    assert store.get("api_secret") == "supersecretvalue1234"
    assert store.masked()["api_key"] == "ABC…NOP" and store.masked()["api_password"] is None
    assert store.credentials() == {"apiKey": "ABCDEFGHIJKLMNOP", "secret": "supersecretvalue1234"}
    other = SecretStore(env.db, "a-different-secret-key")
    assert other.get("api_key") is None  # restored on another server: unreadable, not a crash
    store.delete_all()
    assert store.credentials() is None
    with pytest.raises(ValueError):
        store.set("bogus", "x")


def test_password_hashing_and_session_tokens(env):
    db = env.db
    assert auth.ensure_admin(db) is not None and auth.ensure_admin(db) is None  # created once
    with pytest.raises(ValueError):
        auth.set_admin(db, "admin", "short")
    auth.set_admin(db, "admin", "correct horse battery")
    admin = auth.get_admin(db)
    assert auth.verify_password("correct horse battery", admin["hash"])
    assert not auth.verify_password("wrong", admin["hash"]) and not auth.verify_password("x", "garbage")

    sess = auth.Sessions(db, "secret-1")
    token = sess.login("1.1.1.1", "admin", "correct horse battery")
    assert sess.verify(token) == "admin"
    assert sess.verify(token + "x") is None and sess.verify(None) is None and sess.verify("a.b") is None
    assert auth.Sessions(db, "other-secret").verify(token) is None  # signed with the server key
    auth.set_admin(db, "admin", "brand new password!")
    assert sess.verify(token) is None  # password change logs out old sessions


def test_login_lockout(env):
    auth.set_admin(env.db, "admin", "correct horse battery")
    sess = auth.Sessions(env.db, "k")
    for _ in range(auth.LOCK_AFTER):
        assert sess.login("9.9.9.9", "admin", "nope") is None
    assert sess.locked_for("9.9.9.9") > 0
    assert sess.locked_for("8.8.8.8") == 0
