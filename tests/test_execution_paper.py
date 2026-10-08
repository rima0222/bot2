import asyncio

import pytest

from conftest import make_plan
from execution_agent import ExecutionAgent
from utils import now_ms


def run(coro):
    return asyncio.run(coro)


def zero_costs(env, fee=0.1, slip=0.0):
    env.settings.update({"fee_pct": fee, "slippage_pct": slip})


def trades(env):
    return [dict(r) for r in env.db.query("SELECT * FROM trades ORDER BY id")]


def test_long_take_profit_accounting(env):
    zero_costs(env)
    ex, ctx = env.execution, env.ctx
    ctx.prices["BTC/USDT"] = 100.0

    async def scenario():
        ok, msg = await ex.execute(make_plan("long", qty=10))
        assert ok, msg
        assert ex.account()["margin_used"] == pytest.approx(200.0)  # 1000 notional / 5x
        await ex.on_prices({"BTC/USDT": 102.0})  # hits TP at 102

    run(scenario())
    t = trades(env)
    assert len(t) == 1 and t[0]["reason"] == "take_profit"
    # gross 20, fees 1.00 + 1.02
    assert t[0]["pnl"] == pytest.approx(20 - 1.0 - 1.02)
    assert ex.broker.balance == pytest.approx(1000 + 20 - 1.0 - 1.02)
    assert t[0]["r_mult"] == pytest.approx(t[0]["pnl"] / 10.0)
    assert env.db.one("SELECT COUNT(*) n FROM positions")["n"] == 0
    assert env.db.kv_get("paper_balance") == pytest.approx(ex.broker.balance)


def test_short_stop_loss_with_slippage(env):
    zero_costs(env, fee=0.0, slip=0.05)
    ex, ctx = env.execution, env.ctx
    ctx.prices["ETH/USDT"] = 100.0

    async def scenario():
        assert (await ex.execute(make_plan("short", symbol="ETH/USDT", qty=5)))[0]
        pos = next(iter(ex.positions.values()))
        assert pos.entry == pytest.approx(99.95)  # sold slightly lower than market
        await ex.on_prices({"ETH/USDT": 101.0})  # SL at 101

    run(scenario())
    t = trades(env)[0]
    assert t["reason"] == "stop_loss" and t["pnl"] < 0
    assert t["exit"] == pytest.approx(101.0 * 1.0005)  # bought back slightly higher


def test_gap_through_stop_fills_at_worse_price(env):
    zero_costs(env, fee=0.0)
    ex, ctx = env.execution, env.ctx
    ctx.prices["BTC/USDT"] = 100.0

    async def scenario():
        await ex.execute(make_plan("long", qty=1))
        await ex.on_prices({"BTC/USDT": 97.0})  # gapped far below the 99 stop

    run(scenario())
    assert trades(env)[0]["exit"] == pytest.approx(97.0)


def test_candle_with_both_levels_assumes_stop_first(env):
    zero_costs(env, fee=0.0)
    ex, ctx = env.execution, env.ctx
    ctx.prices["BTC/USDT"] = 100.0

    async def scenario():
        await ex.execute(make_plan("long", qty=1))
        pos = next(iter(ex.positions.values()))
        await ex.on_candle("BTC/USDT", (pos.opened_at + 1, 100, 103, 98, 101, 1))  # touches SL 99 and TP 102

    run(scenario())
    assert trades(env)[0]["reason"] == "stop_loss"


def test_candle_started_before_entry_is_ignored(env):
    ex, ctx = env.execution, env.ctx
    ctx.prices["BTC/USDT"] = 100.0

    async def scenario():
        await ex.execute(make_plan("long", qty=1))
        pos = next(iter(ex.positions.values()))
        # this candle began before we entered; its low (90) happened before our entry
        await ex.on_candle("BTC/USDT", (pos.opened_at - 60_000, 100, 101, 90, 100, 1))
        assert len(ex.positions) == 1

    run(scenario())


def test_liquidation_loses_margin_and_gap_beyond_liq(env):
    zero_costs(env, fee=0.0)
    ex, ctx = env.execution, env.ctx
    ctx.prices["BTC/USDT"] = 100.0

    async def scenario():
        plan = make_plan("long", qty=1, lev=10, sl_pct=0.20)  # stop far beyond liquidation (misconfigured)
        await ex.execute(plan)
        pos = next(iter(ex.positions.values()))
        assert pos.liq == pytest.approx(100 * (1 - 0.1 + 0.005))
        await ex.on_prices({"BTC/USDT": 80.0})  # below both stop and liquidation

    run(scenario())
    t = trades(env)[0]
    assert t["reason"] == "liquidation"
    assert t["pnl"] == pytest.approx(-t["margin"])  # never loses more than the margin


def test_time_stop(env):
    ex, ctx = env.execution, env.ctx
    env.settings.update({"max_hold_bars": 1})
    ctx.prices["BTC/USDT"] = 100.0

    async def scenario():
        await ex.execute(make_plan("long", qty=1))
        pos = next(iter(ex.positions.values()))
        pos.opened_at = now_ms() - 10 * 60_000
        await ex.on_prices({"BTC/USDT": 100.1})

    run(scenario())
    assert trades(env)[0]["reason"] == "time_stop"


def test_entry_guards(env):
    ex, ctx = env.execution, env.ctx
    env.settings.update({"max_positions": 1})

    async def scenario():
        assert (await ex.execute(make_plan(symbol="BTC/USDT")))[1] == "no live price"
        ctx.prices.update({"BTC/USDT": 100.0, "ETH/USDT": 100.0})
        assert (await ex.execute(make_plan(symbol="BTC/USDT")))[0]
        assert (await ex.execute(make_plan(symbol="BTC/USDT")))[1] == "position already open"
        assert "max open" in (await ex.execute(make_plan(symbol="ETH/USDT")))[1]
        # price ran away from the signal
        env.settings.update({"max_positions": 2})
        ctx.prices["ETH/USDT"] = 100.0
        bad = make_plan(symbol="ETH/USDT")
        bad.signal_price = 90.0
        assert "moved" in (await ex.execute(bad))[1]
        # price already beyond the stop
        ctx.prices["ETH/USDT"] = 99.85  # inside the slippage guard, but below the 99.9 stop
        assert "beyond" in (await ex.execute(make_plan(symbol="ETH/USDT", sl_pct=0.001)))[1]
        # paused bot refuses
        ctx.prices["ETH/USDT"] = 100.0
        env.sup.pause()
        assert "paused" in (await ex.execute(make_plan(symbol="ETH/USDT")))[1]

    run(scenario())


def test_close_all_and_restart_recovers_positions(env):
    ex, ctx = env.execution, env.ctx
    ctx.prices.update({"BTC/USDT": 100.0, "ETH/USDT": 50.0})

    async def scenario():
        await ex.execute(make_plan(symbol="BTC/USDT", qty=1))
        await ex.execute(make_plan(symbol="ETH/USDT", price=50.0, qty=2))
        # a "restart": brand-new agent reads the same database
        ex2 = ExecutionAgent(ctx)
        await ex2.start()
        assert {p.symbol for p in ex2.positions.values()} == {"BTC/USDT", "ETH/USDT"}
        assert ex2.broker.balance == pytest.approx(ex.broker.balance)
        assert await ex.close_all("kill_switch") == 2

    run(scenario())
    assert {t["reason"] for t in trades(env)} == {"kill_switch"}
    assert not ex.positions


def test_reset_paper_requires_flat_and_restores_balance(env):
    ex, ctx = env.execution, env.ctx
    ctx.prices["BTC/USDT"] = 100.0

    async def scenario():
        await ex.execute(make_plan(qty=1))
        with pytest.raises(ValueError):
            ex.reset_paper()
        await ex.close_all("manual")

    run(scenario())
    env.settings.update({"paper_equity": 2500.0})
    ex.reset_paper()
    assert ex.broker.balance == 2500.0 and trades(env) == []
