"""Execution Agent: order bridge with a paper broker and a CCXT live broker.

SL / TP / time-stop / liquidation are always monitored by the bot itself
(on every price tick and every closed candle), so protection works the same in
both modes. In live mode exchange-side stop orders are *also* placed on a
best-effort basis.

Live trading needs an exchange whose CCXT driver supports swaps, leverage
and positions (Binance, Bybit, OKX, ...). LBank futures is NOT supported by
CCXT, so LBank runs in paper mode only (with real LBank prices).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from context import Context
from models import LONG, OrderPlan, Position, liquidation_price
from utils import now_ms, tf_to_ms


class LiveNotSupported(RuntimeError):
    pass


class OrderUncertain(RuntimeError):
    """An order timed out - it may or may not have reached the exchange."""


@dataclass
class Fill:
    price: float
    fee: float
    qty: float
    gross: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Paper broker
# --------------------------------------------------------------------------
class PaperBroker:
    mode = "paper"

    def __init__(self, ctx: Context):
        self.ctx = ctx
        bal = ctx.db.kv_get("paper_balance")
        if bal is None:
            bal = ctx.settings.paper_equity
            ctx.db.kv_set("paper_balance", bal)
            ctx.db.kv_set("start_equity:paper", float(bal))
        self.balance = float(bal)

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    def save(self) -> None:
        self.ctx.db.kv_set("paper_balance", self.balance)

    async def open(self, plan: OrderPlan, price: float) -> Fill:
        s = self.ctx.settings
        slip = s.slippage_pct / 100.0
        d = 1 if plan.side == LONG else -1
        fill = price * (1 + d * slip)
        fee = plan.qty * fill * s.fee_pct / 100.0
        self.balance -= fee
        return Fill(price=fill, fee=fee, qty=plan.qty)

    async def close(self, pos: Position, price: float, slip: bool, reason: str) -> Fill:
        s = self.ctx.settings
        d = pos.direction
        fill = price * (1 - d * s.slippage_pct / 100.0) if slip else price
        if reason == "liquidation":
            gross, fee = -pos.margin, 0.0
        else:
            gross = d * (fill - pos.entry) * pos.qty
            fee = pos.qty * fill * s.fee_pct / 100.0
        self.balance += gross - fee
        return Fill(price=fill, fee=fee, qty=pos.qty, gross=gross)

    def account(self, positions: dict[int, Position], prices: dict[str, float]) -> dict[str, float]:
        unreal = sum(p.unrealized(prices.get(p.symbol, p.entry)) for p in positions.values())
        margin = sum(p.margin for p in positions.values())
        equity = self.balance + unreal
        return {"equity": equity, "balance": self.balance, "unrealized": unreal,
                "margin_used": margin, "free": max(equity - margin, 0.0)}


# --------------------------------------------------------------------------
# Live broker (CCXT)
# --------------------------------------------------------------------------
class LiveBroker:
    mode = "live"
    _NOOP = ("not modified", "no need", "same", "unchanged", "already", "110043", "-4046")

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.ex: Any = None
        self._ccxt: Any = None
        self.cache = {"equity": 0.0, "balance": 0.0, "unrealized": 0.0, "margin_used": 0.0, "free": 0.0}
        self.log = ctx.getlog("execution")

    async def start(self) -> None:
        eid = self.ctx.static.exchange_id
        creds = self.ctx.secrets.credentials()
        if not creds:
            raise LiveNotSupported("Add your exchange API key and secret in Settings first.")
        import ccxt.async_support as ccxt

        self._ccxt = ccxt
        if not hasattr(ccxt, eid):
            raise LiveNotSupported(f"Unknown exchange '{eid}'.")
        ex = getattr(ccxt, eid)({**creds, "enableRateLimit": True, "timeout": 20000,
                                 "options": {"defaultType": "swap"}})
        try:
            await ex.load_markets()
            need = ("createOrder", "setLeverage", "fetchPositions", "fetchBalance")
            if not ex.has.get("swap") or any(not ex.has.get(k) for k in need):
                raise LiveNotSupported(
                    f"CCXT does not support live futures trading on '{eid}' (swap, leverage and positions "
                    "are required). Use paper mode, or set BOT_EXCHANGE to binance, bybit or okx.")
        except Exception:
            await ex.close()
            raise
        self.ex = ex
        await self.refresh_account({})

    async def stop(self) -> None:
        if self.ex is not None:
            await self.ex.close()
            self.ex = None

    def sym(self, symbol: str) -> str:
        quote = symbol.split("/")[1]
        cand = f"{symbol}:{quote}"
        return cand if cand in self.ex.markets else symbol

    def _cs(self, sym: str) -> float:
        return float(self.ex.market(sym).get("contractSize") or 1.0)

    async def refresh_account(self, positions: dict[int, Position], prices: dict[str, float] | None = None) -> None:
        bal = await self.ex.fetch_balance()
        u = bal.get("USDT") or bal.get("USDC") or {}
        total = float(u.get("total") or 0.0)
        unreal = sum(p.unrealized((prices or {}).get(p.symbol, p.entry)) for p in positions.values())
        self.cache = {"equity": total, "balance": total - unreal, "unrealized": unreal,
                      "margin_used": float(u.get("used") or 0.0), "free": float(u.get("free") or 0.0)}

    def account(self, positions: dict[int, Position], prices: dict[str, float]) -> dict[str, float]:
        unreal = sum(p.unrealized(prices.get(p.symbol, p.entry)) for p in positions.values())
        c = dict(self.cache)
        c["equity"] = c["balance"] + unreal
        c["unrealized"] = unreal
        return c

    async def _order(self, sym: str, side: str, amount: float, params: dict[str, Any]) -> dict[str, Any]:
        ccxt, attempt = self._ccxt, 0
        while True:
            try:
                return await self.ex.create_order(sym, "market", side, amount, None, params)
            except (ccxt.RateLimitExceeded, ccxt.DDoSProtection, ccxt.ExchangeNotAvailable) as exc:
                attempt += 1  # the request was refused -> safe to retry
                if attempt >= 4:
                    raise
                self.log.warning("order refused (%s); retry %d/3", type(exc).__name__, attempt)
                await asyncio.sleep(1.5 * attempt)
            except ccxt.RequestTimeout as exc:
                raise OrderUncertain(f"{side} {sym}: request timed out, order state unknown") from exc

    async def _settle(self, order: dict[str, Any], sym: str) -> dict[str, Any]:
        if order.get("average") and order.get("filled"):
            return order
        for _ in range(5):
            await asyncio.sleep(0.6)
            try:
                order = await self.ex.fetch_order(order["id"], sym)
            except Exception:  # noqa: BLE001
                continue
            if order.get("status") in ("closed", "canceled", "expired") or order.get("average"):
                break
        return order

    def _fee(self, order: dict[str, Any], notional: float) -> float:
        f = order.get("fee") or {}
        if f.get("cost") and f.get("currency") in (None, "USDT", "USDC"):
            return float(f["cost"])
        fees = [x for x in (order.get("fees") or []) if x.get("cost")]
        if fees:
            return float(sum(x["cost"] for x in fees))
        return notional * self.ctx.settings.fee_pct / 100.0

    async def open(self, plan: OrderPlan, price: float) -> Fill:
        ex, ccxt = self.ex, self._ccxt
        sym = self.sym(plan.symbol)
        cs = self._cs(sym)
        amount = float(ex.amount_to_precision(sym, plan.qty / cs))
        if amount <= 0:
            raise ValueError("order amount rounds to zero")
        try:
            await ex.set_margin_mode("isolated", sym)
        except ccxt.BaseError as exc:
            if not any(t in str(exc).lower() for t in self._NOOP):
                self.log.warning("could not set isolated margin on %s: %s", sym, exc)
        try:
            await ex.set_leverage(plan.leverage, sym)
        except ccxt.ExchangeError as exc:
            if not any(t in str(exc).lower() for t in self._NOOP):
                raise
        side = "buy" if plan.side == LONG else "sell"
        order = await self._settle(await self._order(sym, side, amount, {}), sym)
        avg = float(order.get("average") or order.get("price") or price)
        filled = float(order.get("filled") or amount) * cs
        fee = self._fee(order, filled * avg)
        ids: list[str] = []
        opp = "sell" if plan.side == LONG else "buy"
        for key, px in (("stopLossPrice", plan.sl), ("takeProfitPrice", plan.tp)):
            try:
                o = await self._order(sym, opp, float(ex.amount_to_precision(sym, filled / cs)),
                                      {key: float(ex.price_to_precision(sym, px)), "reduceOnly": True})
                ids.append(str(o["id"]))
            except Exception as exc:  # noqa: BLE001
                self.log.warning("exchange-side %s not placed (%s); the bot's own monitor still protects the trade",
                                 key, exc)
        return Fill(price=avg, fee=fee, qty=filled, meta={"protective": ids})

    async def close(self, pos: Position, price: float, slip: bool, reason: str) -> Fill:
        ex = self.ex
        sym = self.sym(pos.symbol)
        cs = self._cs(sym)
        side = "sell" if pos.side == LONG else "buy"
        order = await self._settle(
            await self._order(sym, side, float(ex.amount_to_precision(sym, pos.qty / cs)), {"reduceOnly": True}), sym)
        avg = float(order.get("average") or order.get("price") or price)
        for oid in pos.meta.get("protective", []):
            try:
                await ex.cancel_order(oid, sym)
            except Exception:  # noqa: BLE001
                pass
        return Fill(price=avg, fee=self._fee(order, pos.qty * avg), qty=pos.qty,
                    gross=pos.direction * (avg - pos.entry) * pos.qty)

    async def exchange_positions(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in await self.ex.fetch_positions():
            c = abs(float(p.get("contracts") or 0))
            if c > 0:
                out[p["symbol"]] = c * self._cs(p["symbol"])
        return out


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------
class ExecutionAgent:
    name = "execution"

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.log = ctx.getlog("execution")
        self.broker: PaperBroker | LiveBroker | None = None
        self.positions: dict[int, Position] = {}
        self.last_close: dict[str, int] = {}
        self.lock = asyncio.Lock()
        self._reconcile_at = 0.0

    # ---- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        mode = self.ctx.settings.mode
        self._load_positions(mode)
        try:
            self.broker = await self._make_broker(mode)
        except LiveNotSupported as exc:
            self.log.error("live broker unavailable: %s", exc)
            self.ctx.state.set_halt(f"live_unavailable: {exc}"[:200])
            self.broker = None

    async def _make_broker(self, mode: str) -> PaperBroker | LiveBroker:
        broker = PaperBroker(self.ctx) if mode == "paper" else LiveBroker(self.ctx)
        await broker.start()
        return broker

    def _load_positions(self, mode: str) -> None:
        self.positions.clear()
        for r in self.ctx.db.query("SELECT * FROM positions"):
            if r["mode"] != mode:
                self.log.warning("ignoring %s position %s %s stored while in %s mode", r["mode"], r["symbol"], r["side"], mode)
                continue
            meta = json.loads(r["meta"] or "{}")
            self.positions[r["id"]] = Position(
                id=r["id"], symbol=r["symbol"], side=r["side"], qty=r["qty"], entry=r["entry"], sl=r["sl"],
                tp=r["tp"], leverage=r["leverage"], margin=r["margin"], liq=r["liq"],
                risk_amount=r["risk_amount"], fee_open=r["fee_open"], opened_at=r["opened_at"], mode=mode, meta=meta)

    async def switch_mode(self, mode: str) -> None:
        async with self.lock:
            if self.positions:
                raise ValueError("Close all open positions before switching mode.")
            try:
                new = await self._make_broker(mode)
            except LiveNotSupported as exc:
                raise ValueError(str(exc)) from exc
            old, self.broker = self.broker, new
            if old is not None:
                await old.stop()
            self.last_close.clear()

    async def stop(self) -> None:
        if self.broker is not None:
            await self.broker.stop()

    # ---- account -------------------------------------------------------------------
    def account(self) -> dict[str, float]:
        if self.broker is None:
            return {"equity": 0.0, "balance": 0.0, "unrealized": 0.0, "margin_used": 0.0, "free": 0.0}
        return self.broker.account(self.positions, self.ctx.prices)

    async def account_loop(self) -> None:
        """Live mode only: keep balances fresh and reconcile positions with the exchange."""
        while True:
            try:
                if isinstance(self.broker, LiveBroker) and self.broker.ex is not None:
                    await self.broker.refresh_account(self.positions, self.ctx.prices)
                    if asyncio.get_running_loop().time() - self._reconcile_at > 60:
                        self._reconcile_at = asyncio.get_running_loop().time()
                        await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.log.warning("account refresh failed: %s", exc)
            self.ctx.beat("execution")
            await asyncio.sleep(15)

    async def reconcile(self) -> list[str]:
        assert isinstance(self.broker, LiveBroker)
        remote = await self.broker.exchange_positions()
        mine = {self.broker.sym(p.symbol): p for p in self.positions.values()}
        problems = [f"exchange has {k} that the bot does not track" for k in remote if k not in mine]
        problems += [f"bot tracks {k} but the exchange shows no position" for k in mine if k not in remote]
        if problems and not self.ctx.state.halted:
            self.log.error("position mismatch: %s", "; ".join(problems))
            self.ctx.state.set_halt("reconcile: " + "; ".join(problems)[:180])
        return problems

    # ---- opening -----------------------------------------------------------------------
    async def execute(self, plan: OrderPlan) -> tuple[bool, str]:
        s, ctx = self.ctx.settings, self.ctx
        async with self.lock:
            if self.broker is None:
                return False, "no broker available"
            if not ctx.state.trading_allowed:
                return False, f"bot is {ctx.state.status}"
            if any(p.symbol == plan.symbol for p in self.positions.values()):
                return False, "position already open"
            if len(self.positions) >= s.max_positions:
                return False, "max open positions reached"
            price = ctx.prices.get(plan.symbol)
            if not price:
                return False, "no live price"
            moved = abs(price - plan.signal_price) / plan.signal_price * 100.0
            if moved > s.max_slippage_pct:
                return False, f"price moved {moved:.2f}% since the signal"
            d = 1 if plan.side == LONG else -1
            if not (d * (price - plan.sl) > 0 and d * (plan.tp - price) > 0):
                return False, "price is already beyond the stop or target"
            try:
                fill = await self.broker.open(plan, price)
            except OrderUncertain as exc:
                self.log.error("%s", exc)
                ctx.state.set_halt(f"order_uncertain: {exc}"[:200])
                return False, str(exc)
            except Exception as exc:  # noqa: BLE001
                self.log.error("order failed for %s: %s", plan.symbol, exc)
                return False, f"order failed: {exc}"[:200]
            entry = fill.price
            margin = fill.qty * entry / plan.leverage
            liq = liquidation_price(plan.side, entry, plan.leverage)
            opened = now_ms()
            with ctx.db.tx():
                pid = ctx.db.execute(
                    "INSERT INTO positions(symbol,side,qty,entry,sl,tp,leverage,margin,liq,risk_amount,fee_open,"
                    "opened_at,mode,meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (plan.symbol, plan.side, fill.qty, entry, plan.sl, plan.tp, plan.leverage, margin, liq,
                     plan.risk_amount, fill.fee, opened, self.broker.mode, json.dumps(fill.meta)))
                if isinstance(self.broker, PaperBroker):
                    self.broker.save()
            self.positions[pid] = Position(pid, plan.symbol, plan.side, fill.qty, entry, plan.sl, plan.tp,
                                           plan.leverage, margin, liq, plan.risk_amount, fill.fee, opened,
                                           self.broker.mode, fill.meta)
            self.log.info("OPEN %s %s qty=%.6g @ %.6g  SL %.6g  TP %.6g  %dx  risk %.2f USDT",
                          plan.side.upper(), plan.symbol, fill.qty, entry, plan.sl, plan.tp, plan.leverage,
                          plan.risk_amount)
            return True, "opened"

    # ---- monitoring ----------------------------------------------------------------------
    async def on_prices(self, prices: dict[str, float]) -> None:
        for pos in list(self.positions.values()):
            px = prices.get(pos.symbol)
            if px:
                await self._check(pos, px, px, px, tick=True)

    async def on_candle(self, symbol: str, candle: tuple) -> None:
        ts, _o, h, l, c, _v = candle
        for pos in list(self.positions.values()):
            # only candles that *started after* entry can be trusted for intrabar extremes
            if pos.symbol == symbol and ts >= pos.opened_at:
                await self._check(pos, l, h, c, tick=False)

    async def _check(self, pos: Position, lo: float, hi: float, price: float, tick: bool) -> None:
        if pos.id not in self.positions:
            return
        long_ = pos.direction == 1
        liq_hit = (lo <= pos.liq) if long_ else (hi >= pos.liq)
        sl_hit = (lo <= pos.sl) if long_ else (hi >= pos.sl)
        tp_hit = (hi >= pos.tp) if long_ else (lo <= pos.tp)
        paper = pos.mode == "paper"
        if sl_hit:  # if SL and TP are both inside one candle assume the worse outcome
            ref = (min(pos.sl, price) if long_ else max(pos.sl, price)) if tick else pos.sl
            if paper and ((ref <= pos.liq) if long_ else (ref >= pos.liq)):
                await self.close_position(pos, pos.liq, "liquidation", slip=False)  # gapped through liquidation
            else:
                await self.close_position(pos, ref, "stop_loss", slip=True)
        elif paper and liq_hit:  # only possible if the stop was placed beyond liquidation
            await self.close_position(pos, pos.liq, "liquidation", slip=False)
        elif tp_hit:
            await self.close_position(pos, pos.tp, "take_profit", slip=False)
        elif tick:
            limit = self.ctx.settings.max_hold_bars * tf_to_ms(self.ctx.settings.timeframe)
            if now_ms() - pos.opened_at >= limit:
                await self.close_position(pos, price, "time_stop", slip=True)

    # ---- closing -------------------------------------------------------------------------------
    async def close_position(self, pos: Position, price: float, reason: str, slip: bool = True) -> bool:
        async with self.lock:
            if pos.id not in self.positions or self.broker is None:
                return False
            try:
                fill = await self.broker.close(pos, price, slip, reason)
            except OrderUncertain as exc:
                self.ctx.state.set_halt(f"order_uncertain: {exc}"[:200])
                self.log.error("%s", exc)
                return False
            except Exception as exc:  # noqa: BLE001
                if isinstance(self.broker, LiveBroker) and await self._gone_on_exchange(pos):
                    fill = Fill(price=price, fee=0.0, qty=pos.qty,
                                gross=pos.direction * (price - pos.entry) * pos.qty)
                    reason = "closed_on_exchange"
                else:
                    self.log.error("close failed for %s: %s (will retry)", pos.symbol, exc)
                    return False
            net = fill.gross - pos.fee_open - fill.fee
            r_mult = net / pos.risk_amount if pos.risk_amount else 0.0
            closed = now_ms()
            with self.ctx.db.tx():
                self.ctx.db.execute(
                    "INSERT INTO trades(symbol,side,qty,entry,exit,sl,tp,leverage,margin,pnl,fees,r_mult,opened_at,"
                    "closed_at,reason,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pos.symbol, pos.side, pos.qty, pos.entry, fill.price, pos.sl, pos.tp, pos.leverage, pos.margin,
                     net, pos.fee_open + fill.fee, r_mult, pos.opened_at, closed, reason, pos.mode))
                self.ctx.db.execute("DELETE FROM positions WHERE id=?", (pos.id,))
                if isinstance(self.broker, PaperBroker):
                    self.broker.save()
            del self.positions[pos.id]
            self.last_close[pos.symbol] = closed
            self.log.info("CLOSE %s %s (%s) @ %.6g  net %+.2f USDT  (%.2fR)", pos.side.upper(), pos.symbol,
                          reason, fill.price, net, r_mult)
            return True

    async def _gone_on_exchange(self, pos: Position) -> bool:
        try:
            assert isinstance(self.broker, LiveBroker)
            return self.broker.sym(pos.symbol) not in await self.broker.exchange_positions()
        except Exception:  # noqa: BLE001
            return False

    async def close_all(self, reason: str) -> int:
        n = 0
        for pos in list(self.positions.values()):
            px = self.ctx.prices.get(pos.symbol) or pos.entry
            if await self.close_position(pos, px, reason, slip=True):
                n += 1
        return n

    # ---- paper account ------------------------------------------------------------------------------
    def reset_paper(self) -> None:
        if self.positions:
            raise ValueError("Close all open positions before resetting the paper account.")
        db = self.ctx.db
        with db.tx():
            db.execute("DELETE FROM trades WHERE mode='paper'")
            db.execute("DELETE FROM equity WHERE mode='paper'")
            db.execute("DELETE FROM positions WHERE mode='paper'")
            db.kv_set("paper_balance", self.ctx.settings.paper_equity)
            db.kv_set("start_equity:paper", float(self.ctx.settings.paper_equity))
        if isinstance(self.broker, PaperBroker):
            self.broker.balance = float(self.ctx.settings.paper_equity)
        self.last_close.clear()
