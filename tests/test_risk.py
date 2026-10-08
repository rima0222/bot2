import asyncio

import pytest

from conftest import make_plan
from models import OrderPlan, Signal
from risk_agent import LIQ_SAFETY, MAINT_MARGIN
from utils import now_ms


def sig(side="long", price=100.0, atr_pct=0.004, symbol="BTC/USDT", age_ms=0):
    return Signal(ts=now_ms() - age_ms, symbol=symbol, side=side, prob=0.7 if side == "long" else 0.3,
                  price=price, atr_pct=atr_pct, tf="5m")


def test_size_is_derived_from_risk_budget_including_costs(env):
    s = env.settings
    plan = env.risk.evaluate(sig(price=100.0, atr_pct=0.004))
    assert isinstance(plan, OrderPlan)
    sl_pct = max(0.004 * s.sl_atr_mult, s.min_sl_pct / 100)
    cost = (2 * s.fee_pct + 2 * s.slippage_pct) / 100
    loss_at_stop = plan.qty * plan.signal_price * (sl_pct + cost)
    assert loss_at_stop == pytest.approx(1000 * s.risk_pct / 100, rel=0.01)  # 1 % of 1000 USDT
    assert plan.risk_amount == pytest.approx(loss_at_stop, rel=1e-6)
    assert plan.sl < plan.signal_price < plan.tp
    assert (plan.tp - plan.signal_price) == pytest.approx((plan.signal_price - plan.sl) * s.rr)


def test_short_levels_are_mirrored(env):
    plan = env.risk.evaluate(sig("short"))
    assert plan.tp < plan.signal_price < plan.sl


def test_leverage_is_reduced_to_keep_stop_inside_liquidation(env):
    env.settings.update({"leverage": 50, "min_sl_pct": 3.0})
    plan = env.risk.evaluate(sig(atr_pct=0.001))
    sl_pct = 0.03
    liq_dist = 1 / plan.leverage - MAINT_MARGIN
    assert plan.leverage < 50
    assert sl_pct <= LIQ_SAFETY * liq_dist + 1e-9


def test_position_never_exceeds_available_margin(env):
    env.settings.update({"risk_pct": 10.0, "leverage": 2, "min_sl_pct": 0.1})
    plan = env.risk.evaluate(sig(atr_pct=0.0005))  # tiny stop -> huge notional wanted
    assert plan.margin <= 1000 * 0.9 + 1e-6
    assert plan.notional <= 1000 * 2 * 0.9 + 1e-6


@pytest.mark.parametrize("mutate, expect", [
    (lambda e: e.sup.pause(), "paused"),
    (lambda e: e.ctx.state.set_halt("max_drawdown"), "halted"),
    (lambda e: e.settings.update({"rr": 0.5, "fee_pct": 0.5}), "fees"),
])
def test_rejections(env, mutate, expect):
    mutate(env)
    out = env.risk.evaluate(sig())
    assert isinstance(out, str) and expect in out


def test_stale_signal_rejected(env):
    assert "stale" in env.risk.evaluate(sig(age_ms=20 * 60_000))


def test_cooldown_and_open_position_rejections(env):
    env.execution.last_close["BTC/USDT"] = now_ms()
    assert "cooldown" in env.risk.evaluate(sig())
    env.execution.last_close.clear()
    env.ctx.prices["BTC/USDT"] = 100.0
    asyncio.run(env.execution.execute(make_plan(qty=1)))
    assert "already open" in env.risk.evaluate(sig())
    env.settings.update({"max_positions": 1})
    assert "max open" in env.risk.evaluate(sig(symbol="ETH/USDT")) or "already open" in env.risk.evaluate(sig(symbol="ETH/USDT"))


def test_too_small_position_rejected(env):
    env.db.kv_set("paper_balance", 3.0)
    env.execution.broker.balance = 3.0
    assert "too small" in env.risk.evaluate(sig())


def test_max_drawdown_closes_everything_and_halts(env):
    env.ctx.prices["BTC/USDT"] = 100.0

    async def scenario():
        await env.execution.execute(make_plan(qty=1))
        await env.risk.check_limits()  # establishes peak
        env.execution.broker.balance -= 200  # -20 % > 15 % limit
        await env.risk.check_limits()

    asyncio.run(scenario())
    assert env.ctx.state.halted and "max_drawdown" in env.ctx.state.halted
    assert not env.execution.positions
    assert env.db.one("SELECT reason FROM trades")["reason"] == "max_drawdown"


def test_daily_loss_halts_new_trades_but_keeps_positions(env):
    env.ctx.prices["BTC/USDT"] = 100.0

    async def scenario():
        await env.execution.execute(make_plan(qty=1))
        await env.risk.check_limits()
        env.execution.broker.balance -= 60  # -6 % today, below the 15 % drawdown limit
        await env.risk.check_limits()

    asyncio.run(scenario())
    assert env.ctx.state.halted.startswith("daily_loss")
    assert len(env.execution.positions) == 1  # still protected by its own SL/TP


def test_clear_halt_resets_baseline(env):
    async def scenario():
        await env.risk.check_limits()
        env.execution.broker.balance -= 200
        await env.risk.check_limits()

    asyncio.run(scenario())
    assert env.ctx.state.halted
    env.sup.clear_halt()
    assert env.ctx.state.halted is None
    asyncio.run(env.risk.check_limits())
    assert env.ctx.state.halted is None  # new baseline = current equity
