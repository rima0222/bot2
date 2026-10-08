import io

import pytest
from fastapi.testclient import TestClient

from conftest import make_plan
from web_panel import auth
from web_panel.app import create_app

PW = "test-password-123"
H = {"X-Requested-With": "bot-panel"}


@pytest.fixture
def client(env):
    auth.set_admin(env.db, "admin", PW)
    app = create_app(env.sup)
    with TestClient(app, base_url="http://panel.test") as c:
        c.env = env
        yield c


def login(c, password=PW):
    return c.post("/api/login", json={"username": "admin", "password": password}, headers=H)


def test_everything_requires_login(client):
    for path in ("/api/overview", "/api/trades", "/api/analytics", "/api/settings", "/api/system", "/api/logs",
                 "/api/export", "/api/trades.csv", "/api/candles", "/api/me"):
        assert client.get(path).status_code == 401, path
    for path in ("/api/bot/kill", "/api/settings", "/api/backups", "/api/restore", "/api/paper/reset"):
        assert client.post(path, json={}, headers=H).status_code == 401, path
    assert client.get("/healthz").status_code == 200  # only a bare liveness probe is public
    assert "status" in client.get("/healthz").json() and "equity" not in client.get("/healthz").text
    with pytest.raises(Exception):
        with client.websocket_connect("/ws"):
            pass


def test_login_cookie_flags_and_csrf_header(client):
    assert client.post("/api/login", json={"username": "admin", "password": PW}).status_code == 403  # no header
    r = login(client)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/me").json() == {"user": "admin"}
    assert client.post("/api/bot/pause", json={}).status_code == 403  # authenticated but no CSRF header
    assert client.post("/api/bot/pause", json={}, headers=H).status_code == 200


def test_wrong_password_and_lockout(client):
    for _ in range(5):
        assert login(client, "bad").status_code == 401
    r = login(client, PW)  # even the right password is refused while locked
    assert r.status_code == 429 and "Too many" in r.json()["error"]


def test_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "Trading Bot" in r.text
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/static/app.js").status_code == 200 and client.get("/static/../main.py").status_code in (400, 404)


def test_page_has_no_inline_script_or_style(client):
    html = client.get("/").text
    assert "style=\"" not in html and "<style" not in html
    import re
    assert all("src=" in tag for tag in re.findall(r"<script[^>]*>", html))
    assert "cdn." not in html and "http://" not in html and "https://" not in html  # nothing external


def test_overview_and_analytics_with_trades(client):
    env = client.env
    login(client)
    env.ctx.prices.update({"BTC/USDT": 100.0, "ETH/USDT": 50.0})
    import asyncio
    asyncio.run(env.execution.execute(make_plan(qty=1)))
    o = client.get("/api/overview").json()
    assert o["bot"]["status"] == "active" and o["bot"]["mode"] == "paper"
    assert o["positions"][0]["symbol"] == "BTC/USDT" and o["account"]["margin_used"] == pytest.approx(20.0, rel=1e-3)  # 100 notional / 5x (+ slippage)
    assert set(o["models"]) == {"BTC/USDT", "ETH/USDT"}
    asyncio.run(env.execution.close_all("manual"))
    a = client.get("/api/analytics").json()
    assert a["trades"] == 1 and "by_symbol" in a and a["curve"][0]["cum"] == pytest.approx(a["net_pnl"])
    t = client.get("/api/trades?limit=5").json()
    assert t["total"] == 1 and t["trades"][0]["reason"] == "manual"
    csv = client.get("/api/trades.csv")
    assert csv.status_code == 200 and csv.text.splitlines()[0].startswith("id,symbol,side")
    assert client.get("/api/candles?symbol=DOGE/USDT").status_code == 400  # not a configured pair


def test_pause_resume_kill_and_halt_flow(client):
    env = client.env
    login(client)
    assert client.post("/api/bot/pause", json={}, headers=H).json()["status"] == "paused"
    assert client.post("/api/bot/resume", json={}, headers=H).json()["status"] == "active"
    env.ctx.prices["BTC/USDT"] = 100.0
    import asyncio
    asyncio.run(env.execution.execute(make_plan(qty=1)))
    r = client.post("/api/bot/kill", json={}, headers=H).json()
    assert r["closed"] == 1 and r["remaining"] == 0
    assert env.ctx.state.status == "paused"  # kill switch also blocks new trades
    env.ctx.state.set_halt("max_drawdown: test")
    assert client.post("/api/bot/resume", json={}, headers=H).status_code == 400  # must clear the halt first
    assert client.post("/api/bot/clear-halt", json={}, headers=H).status_code == 200
    assert client.post("/api/bot/nonsense", json={}, headers=H).status_code == 404


def test_settings_api_validation_and_live_guard(client):
    login(client)
    s = client.get("/api/settings").json()
    assert s["values"]["leverage"] == 5 and s["secrets"]["api_key"] is None and len(s["schema"]) == len(s["values"])
    r = client.post("/api/settings", json={"settings": {"leverage": 99}}, headers=H)
    assert r.status_code == 400 and "leverage" in r.json()["error"]
    r = client.post("/api/settings", json={"settings": {"risk_pct": 2, "pairs": "BTC/USDT"}}, headers=H)
    assert r.status_code == 200 and client.get("/api/settings").json()["values"]["risk_pct"] == 2
    r = client.post("/api/settings", json={"settings": {"mode": "live"}}, headers=H)
    assert r.status_code == 400 and "ENABLE LIVE" in r.json()["error"]
    r = client.post("/api/settings", json={"settings": {"mode": "live"}, "confirm_live": "ENABLE LIVE"}, headers=H)
    assert r.status_code == 400 and "API key" in r.json()["error"]  # confirmed but no keys
    assert client.get("/api/settings").json()["values"]["mode"] == "paper"  # nothing changed
    r = client.post("/api/settings", json={"settings": {"web_port": 9123}}, headers=H)
    assert r.json()["restart_required"] is True


def test_api_keys_are_never_returned(client):
    login(client)
    client.post("/api/settings", json={"settings": {}, "secrets": {"api_key": "AAAAAAAAAAAAAA", "api_secret": "SECRETSECRETSECRET"}}, headers=H)
    body = client.get("/api/settings").text
    assert "SECRETSECRET" not in body and "AAAAAAAAAAAAAA" not in body
    assert client.get("/api/settings").json()["secrets"]["api_key"] == "AAA…AAA"
    client.post("/api/settings", json={"settings": {}, "clear_secrets": True}, headers=H)
    assert client.get("/api/settings").json()["secrets"]["api_key"] is None


def test_backup_download_export_and_traversal(client):
    env = client.env
    login(client)
    name = client.post("/api/backups", json={}, headers=H).json()["name"]
    got = client.get(f"/api/backups/{name}")
    assert got.status_code == 200 and got.content[:15] == b"SQLite format 3"
    assert client.get("/api/export").content[:15] == b"SQLite format 3"
    for bad in ("..%2F..%2Fetc%2Fpasswd", "%2e%2e%2fbot.db", "bot.db"):
        assert client.get(f"/api/backups/{bad}").status_code == 404
    assert client.delete(f"/api/backups/{name}", headers=H).status_code == 200
    assert client.get(f"/api/backups/{name}").status_code == 404


def test_restore_upload_validates_then_swaps(client):
    env = client.env
    login(client)
    env.db.kv_set("marker", "original")
    snap = client.get("/api/export").content
    env.db.kv_set("marker", "changed")

    hdr = {**H, "X-Confirm": "RESTORE", "Content-Type": "application/octet-stream"}
    assert client.post("/api/restore", content=snap, headers=H).status_code == 400  # confirmation missing
    bad = client.post("/api/restore", content=b"not a database" * 100, headers=hdr)
    assert bad.status_code == 400 and env.db.kv_get("marker") == "changed"  # live data untouched
    ok = client.post("/api/restore", content=snap, headers=hdr)
    assert ok.status_code == 200 and env.db.kv_get("marker") == "original"
    assert client.get("/api/me").status_code == 200  # still logged in after the swap
    assert any(b["kind"] == "pre-restore" for b in client.get("/api/system").json()["backups"])
    assert not list(env.static.backup_dir.glob(".upload-*"))  # temp upload cleaned up


def test_restore_existing_backup_needs_typed_confirmation(client):
    login(client)
    name = client.post("/api/backups", json={}, headers=H).json()["name"]
    assert client.post(f"/api/backups/{name}/restore", json={}, headers=H).status_code == 400
    assert client.post(f"/api/backups/{name}/restore", json={"confirm": "RESTORE"}, headers=H).status_code == 200


def test_paper_reset(client):
    env = client.env
    login(client)
    assert client.post("/api/paper/reset", json={}, headers=H).status_code == 400
    env.ctx.prices["BTC/USDT"] = 100.0
    import asyncio
    asyncio.run(env.execution.execute(make_plan(qty=1)))
    assert client.post("/api/paper/reset", json={"confirm": "RESET"}, headers=H).status_code == 400  # open position
    asyncio.run(env.execution.close_all("manual"))
    assert client.post("/api/paper/reset", json={"confirm": "RESET"}, headers=H).status_code == 200
    assert env.db.one("SELECT COUNT(*) n FROM trades")["n"] == 0 and env.execution.broker.balance == 1000.0


def test_password_change_invalidates_session(client):
    login(client)
    assert client.post("/api/password", json={"current": "wrong", "new": "another-long-pass"}, headers=H).status_code == 400
    assert client.post("/api/password", json={"current": PW, "new": "short"}, headers=H).status_code == 400
    assert client.post("/api/password", json={"current": PW, "new": "another-long-pass"}, headers=H).status_code == 200
    client.cookies.clear()
    assert login(client, PW).status_code == 401 and login(client, "another-long-pass").status_code == 200


def test_websocket_streams_overview_and_checks_origin(client):
    login(client)
    cookie = {"cookie": "bot_session=" + client.cookies.get("bot_session")}  # browsers send this automatically
    with client.websocket_connect("/ws", headers=cookie) as ws:
        msg = ws.receive_json()
        assert msg["bot"]["status"] == "active" and "account" in msg
    with client.websocket_connect("/ws", headers={**cookie, "host": "panel.test", "origin": "http://panel.test"}) as ws:
        assert "bot" in ws.receive_json()  # same-origin page is allowed
    with pytest.raises(Exception):
        with client.websocket_connect("/ws", headers={**cookie, "host": "panel.test", "origin": "http://evil.example"}):
            pass
    with pytest.raises(Exception):
        with client.websocket_connect("/ws", headers={"cookie": "bot_session=forged"}):
            pass


def test_log_and_signal_endpoints(client):
    env = client.env
    login(client)
    env.ctx.log.warning("hello <script>alert(1)</script>")  # stored verbatim; the UI escapes on render
    env.db.execute("INSERT INTO logs(ts,level,agent,msg) VALUES(1,'ERROR','risk','boom')")
    assert [l["msg"] for l in client.get("/api/logs?level=ERROR").json()["logs"]] == ["boom"]
    assert client.get("/api/logs?limit=abc").status_code == 200
    assert client.get("/api/signals").json() == {"signals": []}
