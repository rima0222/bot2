"""Risk Agent: position sizing, leverage control, SL/TP, drawdown & daily-loss limits.

Every signal passes through ``evaluate``; nothing reaches the exchange without
an explicit stop-loss, a take-profit and a size derived from the risk budget.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import math

from context import Context
from data_agent import DataAgent
from execution_agent import ExecutionAgent
from models import LONG, OrderPlan, Signal
from utils import now_ms, tf_to_ms

MAINT_MARGIN = 0.005
LIQ_SAFETY = 0.7  # stop must sit inside 70 % of the distance to liquidation
MARGIN_USE = 0.9  # never commit more than 90 % of free margin


class RiskAgent:
    name = "risk"

    def __init__(self, ctx: Context, execution: ExecutionAgent, data: DataAgent):
        self.ctx = ctx
        self.execution = execution
        self.data = data
        self.log = ctx.getlog("risk")

    # ---- sizing ----------------------------------------------------------------
    def evaluate(self, sig: Signal) -> OrderPlan | str:
        """Return an OrderPlan, or a human-readable rejection reason."""
        s, ctx = self.ctx.settings, self.ctx
        if not ctx.state.trading_allowed:
            return f"bot is {ctx.state.status}"
        if now_ms() - sig.ts > 2 * tf_to_ms(sig.tf):
            return "signal is stale"
        if any(p.symbol == sig.symbol for p in self.execution.positions.values()):
            return "position already open"
        if len(self.execution.positions) >= s.max_positions:
            return f"max open positions ({s.max_positions}) reached"
        last = self.execution.last_close.get(sig.symbol)
        if last and now_ms() - last < s.cooldown_bars * tf_to_ms(sig.tf):
            return "cooldown after last trade"

        acct = self.execution.account()
        equity, free = acct["equity"], acct["free"]
        if equity <= 0 or free <= 0:
            return "no free margin"

        price = sig.price
        sl_pct = max(sig.atr_pct * s.sl_atr_mult, s.min_sl_pct / 100.0)
        tp_pct = sl_pct * s.rr
        if tp_pct * 100 < 2.5 * s.fee_pct:
            return "take-profit too small versus fees"
        d = 1 if sig.side == LONG else -1
        sl = price * (1 - d * sl_pct)
        tp = price * (1 + d * tp_pct)

        # leverage: keep the stop well inside the liquidation distance
        max_lev = int(1.0 / (sl_pct / LIQ_SAFETY + MAINT_MARGIN))
        lev = max(1, min(s.leverage, max_lev))

        # size so that a stop-out *including* round-trip fees and slippage costs the risk budget
        cost_pct = (2 * s.fee_pct + 2 * s.slippage_pct) / 100.0
        risk_amount = equity * s.risk_pct / 100.0
        notional = risk_amount / (sl_pct + cost_pct)
        notional = min(notional, equity * lev * MARGIN_USE, free * lev * MARGIN_USE)
        feed = self.data.feed
        qty = feed.round_amount(sig.symbol, notional / price)
        notional = qty * price
        if qty <= 0 or notional < feed.min_notional(sig.symbol, price):
            return f"position too small ({notional:.2f} USDT)"
        return OrderPlan(symbol=sig.symbol, side=sig.side, qty=qty, signal_price=price, sl=sl, tp=tp,
                         leverage=lev, notional=notional, margin=notional / lev,
                         risk_amount=notional * (sl_pct + cost_pct), signal_id=sig.signal_id)

    async def run(self) -> None:
        while True:
            sig: Signal = await self.ctx.signal_q.get()
            self.ctx.beat(self.name)
            try:
                await self.handle(sig)
            except Exception:  # noqa: BLE001
                self.log.exception("risk error handling %s", sig.symbol)
                self._mark(sig, "error", "internal error")

    async def handle(self, sig: Signal) -> None:
        verdict = self.evaluate(sig)
        if isinstance(verdict, str):
            self.log.info("rejected %s %s: %s", sig.side, sig.symbol, verdict)
            self._mark(sig, "rejected", verdict)
            return
        ok, msg = await self.execution.execute(verdict)
        self._mark(sig, "executed" if ok else "rejected", "" if ok else msg)

    def _mark(self, sig: Signal, status: str, reason: str) -> None:
        if sig.signal_id:
            self.ctx.db.execute("UPDATE signals SET status=?, reason=? WHERE id=?", (status, reason, sig.signal_id))

    # ---- portfolio limits ---------------------------------------------------------
    async def monitor_loop(self) -> None:
        while True:
            try:
                await self.check_limits()
            except Exception:  # noqa: BLE001
                self.log.exception("limit check failed")
            self.ctx.beat("risk-monitor")
            await asyncio.sleep(5)

    async def check_limits(self) -> None:
        ctx, s, db = self.ctx, self.ctx.settings, self.ctx.db
        mode = s.mode
        equity = self.execution.account()["equity"]
        if equity <= 0:
            return
        peak_key, day_key, start_key = f"risk_peak:{mode}", f"risk_day:{mode}", f"risk_day_start:{mode}"
        peak = max(float(db.kv_get(peak_key, equity)), equity)
        db.kv_set(peak_key, peak)

        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        if db.kv_get(day_key) != today:
            db.kv_set(day_key, today)
            db.kv_set(start_key, equity)
            if (ctx.state.halted or "").startswith("daily_loss"):
                ctx.state.set_halt(None)
                self.log.info("new UTC day: daily-loss halt lifted")
        day_start = float(db.kv_get(start_key, equity))

        dd = (peak - equity) / peak * 100.0
        day_loss = (day_start - equity) / day_start * 100.0 if day_start > 0 else 0.0
        ctx.metrics["drawdown_pct"] = round(dd, 2)
        ctx.metrics["day_pnl_pct"] = round(-day_loss, 2)

        if dd >= s.max_drawdown_pct and not ctx.state.halted:
            self.log.error("MAX DRAWDOWN %.2f%% >= %.2f%% - closing everything and halting", dd, s.max_drawdown_pct)
            ctx.state.set_halt(f"max_drawdown: {dd:.1f}% from peak")
            await self.execution.close_all("max_drawdown")
        elif day_loss >= s.daily_loss_pct and not ctx.state.halted:
            self.log.error("DAILY LOSS %.2f%% >= %.2f%% - no new trades until next UTC day", day_loss, s.daily_loss_pct)
            ctx.state.set_halt(f"daily_loss: {day_loss:.1f}% today")

    def reset_baselines(self) -> None:
        """Called when the operator clears a halt or resets the paper account."""
        db = self.ctx.db
        for mode in ("paper", "live"):
            for k in ("risk_peak", "risk_day", "risk_day_start"):
                db.kv_del(f"{k}:{mode}")
