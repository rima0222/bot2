"""FastAPI admin panel: JSON API + WebSocket + static single-page UI."""
from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import os
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from config import Settings
from supervisor import Supervisor
from utils import now_ms, tf_to_ms
from web_panel import analytics
from web_panel.auth import MIN_PASSWORD, Sessions, get_admin, set_admin, verify_password

STATIC = Path(__file__).parent / "static"
COOKIE = "bot_session"
MAX_UPLOAD = 200 * 1024 * 1024
MAX_WS = 5

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def create_app(sup: Supervisor) -> FastAPI:
    ctx, db = sup.ctx, sup.ctx.db
    execution, strategy, data = sup.execution, sup.strategy, sup.data
    sessions = Sessions(db, ctx.static.secret_key)
    running_port = ctx.static.port
    app = FastAPI(title="Trading Bot Panel", docs_url=None, redoc_url=None, openapi_url=None)
    ws_count = {"n": 0}

    # ---- helpers -----------------------------------------------------------------
    def client_ip(request: Request | WebSocket) -> str:
        return request.client.host if request.client else "unknown"

    def authed(request: Request) -> str:
        user = sessions.verify(request.cookies.get(COOKIE))
        if not user:
            raise HTTPException(401, "Sign in required")
        return user

    def need_csrf(request: Request) -> None:
        if request.headers.get("x-requested-with") != "bot-panel":
            raise HTTPException(403, "Missing X-Requested-With header")

    def guard(request: Request, write: bool = False) -> str:
        user = authed(request)
        if write:
            need_csrf(request)
        return user

    async def body(request: Request) -> dict[str, Any]:
        try:
            b = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "Invalid JSON body") from None
        if not isinstance(b, dict):
            raise HTTPException(400, "JSON object expected")
        return b

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp: Response = await call_next(request)
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/api") or request.url.path == "/":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    # ---- pages -----------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True, "status": ctx.state.status}

    # ---- auth --------------------------------------------------------------------------
    @app.post("/api/login")
    async def login(request: Request):
        need_csrf(request)
        ip = client_ip(request)
        wait = sessions.locked_for(ip)
        if wait:
            raise HTTPException(429, f"Too many failed attempts. Try again in {wait // 60 + 1} min.")
        b = await body(request)
        token = await asyncio.to_thread(sessions.login, ip, str(b.get("username", "")), str(b.get("password", "")))
        if not token:
            raise HTTPException(401, "Wrong username or password")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=12 * 3600,
                        secure=request.url.scheme == "https", path="/")
        return resp

    @app.post("/api/logout")
    async def logout(request: Request):
        need_csrf(request)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.get("/api/me")
    async def me(request: Request):
        return {"user": authed(request)}

    @app.post("/api/password")
    async def change_password(request: Request):
        user = guard(request, write=True)
        b = await body(request)
        admin = get_admin(db)
        if not admin or not await asyncio.to_thread(verify_password, str(b.get("current", "")), admin["hash"]):
            raise HTTPException(400, "Current password is wrong")
        new = str(b.get("new", ""))
        await asyncio.to_thread(set_admin, db, user, new)
        resp = JSONResponse({"ok": True, "message": "Password changed. Sign in again."})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # ---- overview --------------------------------------------------------------------------
    def overview() -> dict[str, Any]:
        s, now = ctx.settings, now_ms()
        mode = s.mode
        acct = execution.account()
        start = db.kv_get(f"start_equity:{mode}")
        if start is None and acct["equity"] > 0:
            start = acct["equity"]
            db.kv_set(f"start_equity:{mode}", start)
        start = float(start or acct["equity"] or 0.0)
        stats = db.one("SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl, COALESCE(SUM(pnl>0),0) w FROM trades WHERE mode=?",
                       (mode,))
        day_start = db.kv_get(f"risk_day_start:{mode}")
        pos = [p.public(ctx.prices.get(p.symbol), now) for p in execution.positions.values()]
        pairs = {}
        for sym in s.pairs:
            ts = ctx.price_ts.get(sym)
            pairs[sym] = {"price": ctx.prices.get(sym), "age_s": round((now - ts) / 1000, 1) if ts else None,
                          "bars": data.n_bars(sym, s.timeframe), "error": data.pair_errors.get(sym)}
        return {
            "ts": now,
            "bot": {"status": ctx.state.status, "halted": ctx.state.halted, "enabled": ctx.state.enabled,
                    "mode": mode, "exchange": ctx.static.exchange_id, "data_source": ctx.static.data_source,
                    "timeframe": s.timeframe, "feed_error": ctx.feed_error,
                    "broker_ready": execution.broker is not None,
                    "uptime_s": int(time.time() - ctx.started_at)},
            "account": {**acct, "start_equity": start,
                        "pnl_total": acct["equity"] - start if start else 0.0,
                        "pnl_total_pct": (acct["equity"] - start) / start * 100 if start else 0.0,
                        "pnl_today": acct["equity"] - float(day_start) if day_start is not None else 0.0,
                        "realized": stats["pnl"], "drawdown_pct": ctx.metrics.get("drawdown_pct", 0.0)},
            "stats": {"trades": stats["n"], "win_rate": (stats["w"] / stats["n"] * 100) if stats["n"] else None},
            "positions": pos,
            "pairs": pairs,
            "models": strategy.status(),
            "system": ctx.metrics,
            "backfill": {k: v for k, v in data.backfill_state.items()},
        }

    @app.get("/api/overview")
    async def api_overview(request: Request):
        guard(request)
        return overview()

    # ---- trades & analytics ------------------------------------------------------------------
    def trade_rows(mode: str, limit: int | None = None, offset: int = 0, symbol: str | None = None,
                   side: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM trades WHERE mode=?", [mode]
        if symbol:
            sql += " AND symbol=?"
            args.append(symbol)
        if side in ("long", "short"):
            sql += " AND side=?"
            args.append(side)
        sql += " ORDER BY closed_at DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args += [limit, offset]
        return [dict(r) for r in db.query(sql, args)]

    def qint(request: Request, name: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(request.query_params.get(name, default))))
        except ValueError:
            return default

    @app.get("/api/trades")
    async def api_trades(request: Request):
        guard(request)
        q = request.query_params
        limit, offset = qint(request, "limit", 50, 1, 500), qint(request, "offset", 0, 0, 10_000_000)
        mode = ctx.settings.mode
        rows = trade_rows(mode, limit, offset, q.get("symbol") or None, q.get("side") or None)
        total = db.one("SELECT COUNT(*) n FROM trades WHERE mode=?", (mode,))["n"]
        return {"trades": rows, "total": total}

    @app.get("/api/trades.csv")
    async def api_trades_csv(request: Request):
        guard(request)
        rows = trade_rows(ctx.settings.mode)
        buf = io.StringIO()
        cols = ["id", "symbol", "side", "qty", "entry", "exit", "sl", "tp", "leverage", "pnl", "fees", "r_mult",
                "opened_at", "closed_at", "reason", "mode"]
        w = csv.writer(buf)
        w.writerow(cols)
        for r in rows:
            w.writerow([r[c] for c in cols])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="trades.csv"'})

    @app.get("/api/analytics")
    async def api_analytics(request: Request):
        guard(request)
        return analytics.compute(trade_rows(ctx.settings.mode))

    @app.get("/api/equity")
    async def api_equity(request: Request):
        guard(request)
        hours = qint(request, "hours", 168, 1, 24 * 120)
        since = now_ms() - hours * 3_600_000
        rows = db.query("SELECT ts, equity FROM equity WHERE mode=? AND ts>=? ORDER BY ts", (ctx.settings.mode, since))
        step = max(1, len(rows) // 600)
        pts = [{"ts": r["ts"], "equity": r["equity"]} for r in rows[::step]]
        if rows and (len(rows) - 1) % step:
            pts.append({"ts": rows[-1]["ts"], "equity": rows[-1]["equity"]})
        return {"points": pts}

    @app.get("/api/candles")
    async def api_candles(request: Request):
        guard(request)
        s = ctx.settings
        sym = request.query_params.get("symbol", s.pairs[0])
        if sym not in s.pairs:
            raise HTTPException(400, "Unknown pair")
        limit = qint(request, "limit", 240, 20, 1000)
        rows = db.query("SELECT ts,o,h,l,c,v FROM candles WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT ?",
                        (sym, s.timeframe, limit))[::-1]
        first = rows[0]["ts"] if rows else 0
        marks = [dict(r) for r in db.query(
            "SELECT side, entry, exit, opened_at, closed_at, pnl FROM trades WHERE mode=? AND symbol=? AND closed_at>=?",
            (s.mode, sym, first))]
        opens = [{"side": p.side, "entry": p.entry, "opened_at": p.opened_at, "sl": p.sl, "tp": p.tp}
                 for p in execution.positions.values() if p.symbol == sym]
        return {"symbol": sym, "tf": s.timeframe, "tf_ms": tf_to_ms(s.timeframe),
                "candles": [[r["ts"], r["o"], r["h"], r["l"], r["c"], r["v"]] for r in rows],
                "trades": marks, "open": opens}

    @app.get("/api/signals")
    async def api_signals(request: Request):
        guard(request)
        n = qint(request, "limit", 40, 1, 200)
        return {"signals": [dict(r) for r in db.query("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (n,))]}

    @app.get("/api/logs")
    async def api_logs(request: Request):
        guard(request)
        n = qint(request, "limit", 200, 1, 1000)
        level = request.query_params.get("level", "")
        order = {"INFO": ("INFO", "WARNING", "ERROR", "CRITICAL"), "WARNING": ("WARNING", "ERROR", "CRITICAL"),
                 "ERROR": ("ERROR", "CRITICAL")}
        levels = order.get(level)
        if levels:
            marks = ",".join("?" * len(levels))
            rows = db.query(f"SELECT * FROM logs WHERE level IN ({marks}) ORDER BY id DESC LIMIT ?", (*levels, n))
        else:
            rows = db.query("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (n,))
        return {"logs": [dict(r) for r in rows]}

    # ---- bot control ---------------------------------------------------------------------------------
    @app.post("/api/bot/{action}")
    async def bot_action(action: str, request: Request):
        guard(request, write=True)
        if action == "pause":
            sup.pause()
        elif action == "resume":
            sup.resume()
        elif action == "kill":
            n = await sup.kill_switch()
            return {"ok": True, "closed": n, "remaining": len(execution.positions)}
        elif action == "clear-halt":
            sup.clear_halt()
        elif action == "restart":
            asyncio.get_running_loop().call_later(0.5, sup.request_restart)
            return {"ok": True, "message": "Restarting. The panel reconnects in a few seconds."}
        else:
            raise HTTPException(404, "Unknown action")
        return {"ok": True, "status": ctx.state.status}

    @app.post("/api/positions/{pid}/close")
    async def close_position(pid: int, request: Request):
        guard(request, write=True)
        pos = execution.positions.get(pid)
        if not pos:
            raise HTTPException(404, "No such open position")
        px = ctx.prices.get(pos.symbol) or pos.entry
        ok = await execution.close_position(pos, px, "manual")
        if not ok:
            raise HTTPException(409, "Could not close the position; see the logs")
        return {"ok": True}

    @app.post("/api/paper/reset")
    async def paper_reset(request: Request):
        guard(request, write=True)
        b = await body(request)
        if b.get("confirm") != "RESET":
            raise ValueError("Type RESET to confirm.")
        if ctx.settings.mode != "paper":
            raise ValueError("Switch to paper mode first.")
        if execution.positions:
            raise ValueError("Close all open positions first.")
        await asyncio.to_thread(sup.make_backup, "pre-reset")
        execution.reset_paper()
        db.kv_set("start_equity:paper", float(ctx.settings.paper_equity))
        sup.risk.reset_baselines()
        ctx.log.warning("paper account reset to %.2f USDT", ctx.settings.paper_equity)
        return {"ok": True}

    # ---- settings ---------------------------------------------------------------------------------------
    @app.get("/api/settings")
    async def get_settings(request: Request):
        guard(request)
        return {"values": ctx.settings.as_dict(), "schema": Settings.schema_public(),
                "secrets": ctx.secrets.masked(), "exchange": ctx.static.exchange_id,
                "running_port": running_port, "host": ctx.static.host,
                "live_supported": ctx.static.exchange_id != "lbank"}

    @app.post("/api/settings")
    async def post_settings(request: Request):
        guard(request, write=True)
        b = await body(request)
        changes = b.get("settings") or {}
        if not isinstance(changes, dict):
            raise ValueError("settings must be an object")
        secrets = b.get("secrets") or {}
        if not isinstance(secrets, dict):
            raise ValueError("secrets must be an object")
        applied = await sup.apply_settings(changes, secrets, bool(b.get("clear_secrets")),
                                           confirm_live=b.get("confirm_live") == "ENABLE LIVE")
        return {"ok": True, "applied": applied, "secrets": ctx.secrets.masked(),
                "restart_required": "web_port" in applied and applied["web_port"] != running_port}

    # ---- system & backups ------------------------------------------------------------------------------------
    @app.get("/api/system")
    async def api_system(request: Request):
        guard(request)
        return {"metrics": ctx.metrics, "health": sup.health(), "backups": sup.list_backups(),
                "db_bytes": db.size_bytes(), "python": sys.version.split()[0],
                "log_file": str(ctx.static.log_dir / "bot.log")}

    @app.post("/api/backups")
    async def create_backup(request: Request):
        guard(request, write=True)
        p = await asyncio.to_thread(sup.make_backup, "manual")
        return {"ok": True, "name": p.name}

    @app.get("/api/backups/{name}")
    async def download_backup(name: str, request: Request):
        guard(request)
        try:
            p = sup.backup_path(name)
        except FileNotFoundError:
            raise HTTPException(404, "No such backup") from None
        return FileResponse(p, media_type="application/octet-stream", filename=p.name)

    @app.get("/api/export")
    async def export_now(request: Request):
        """One-click: take a fresh snapshot and download it."""
        guard(request)
        p = await asyncio.to_thread(sup.make_backup, "manual")
        return FileResponse(p, media_type="application/octet-stream", filename=p.name)

    @app.delete("/api/backups/{name}")
    async def delete_backup(name: str, request: Request):
        guard(request, write=True)
        try:
            sup.backup_path(name).unlink()
        except FileNotFoundError:
            raise HTTPException(404, "No such backup") from None
        return {"ok": True}

    @app.post("/api/backups/{name}/restore")
    async def restore_existing(name: str, request: Request):
        guard(request, write=True)
        b = await body(request)
        if b.get("confirm") != "RESTORE":
            raise ValueError("Type RESTORE to confirm.")
        try:
            p = sup.backup_path(name)
        except FileNotFoundError:
            raise HTTPException(404, "No such backup") from None
        await sup.restore_backup(p)
        return {"ok": True, "message": "Database restored. Your admin login was kept."}

    @app.post("/api/restore")
    async def restore_upload(request: Request):
        """Raw-body upload (no multipart dependency). The file is validated before it touches the live DB."""
        guard(request, write=True)
        if request.headers.get("x-confirm") != "RESTORE":
            raise ValueError("Type RESTORE to confirm.")
        tmp = sup.backup_dir() / f".upload-{os.getpid()}-{int(time.time())}.tmp"
        size = 0
        try:
            with open(tmp, "wb") as fh:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise ValueError("File is larger than 200 MB.")
                    fh.write(chunk)
            if size == 0:
                raise ValueError("Empty upload.")
            await sup.restore_backup(tmp)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
        return {"ok": True, "message": "Database restored from the uploaded file. Your admin login was kept."}

    # ---- websocket ------------------------------------------------------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        origin = ws.headers.get("origin")
        host = ws.headers.get("host")
        if origin and host and origin.split("://", 1)[-1] != host:
            await ws.close(code=4403)
            return
        if not sessions.verify(ws.cookies.get(COOKIE)) or ws_count["n"] >= MAX_WS:
            await ws.close(code=4401)
            return
        await ws.accept()
        ws_count["n"] += 1
        try:
            while True:
                await ws.send_json(overview())
                await asyncio.sleep(2)
        except Exception:  # noqa: BLE001 - client went away
            pass
        finally:
            ws_count["n"] -= 1
            with contextlib.suppress(Exception):
                await ws.close()

    return app
