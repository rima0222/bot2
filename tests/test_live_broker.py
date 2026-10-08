"""LiveBroker against a fake CCXT exchange (no network, no real orders)."""
import asyncio

import pytest

from conftest import make_plan
from execution_agent import LiveBroker, LiveNotSupported, OrderUncertain
import ccxt.async_support as ccxt


class FakeEx:
    def __init__(self, fail_first=None, has_swap=True):
        self.calls = []
        self.has = {"swap": has_swap, "createOrder": True, "setLeverage": True, "fetchPositions": True,
                    "fetchBalance": True}
        self.markets = {"BTC/USDT:USDT": {"contractSize": 0.001}, "BTC/USDT": {}}
        self.fail_first = fail_first
        self.n_orders = 0
        self.positions = []

    def market(self, sym):
        return self.markets[sym]

    def amount_to_precision(self, sym, amt):
        return str(round(amt, 3))

    def price_to_precision(self, sym, px):
        return str(round(px, 2))

    async def set_margin_mode(self, mode, sym):
        self.calls.append(("margin", mode))

    async def set_leverage(self, lev, sym):
        self.calls.append(("leverage", lev, sym))

    async def create_order(self, sym, typ, side, amount, price, params):
        self.n_orders += 1
        self.calls.append(("order", side, amount, dict(params)))
        if self.fail_first and self.n_orders == 1:
            raise self.fail_first
        if params.get("stopLossPrice") or params.get("takeProfitPrice"):
            return {"id": f"prot{self.n_orders}"}
        return {"id": f"o{self.n_orders}", "average": 100.5, "filled": amount, "status": "closed",
                "fee": {"cost": 0.4, "currency": "USDT"}}

    async def cancel_order(self, oid, sym):
        self.calls.append(("cancel", oid))

    async def fetch_balance(self):
        return {"USDT": {"total": 1000.0, "used": 100.0, "free": 900.0}}

    async def fetch_positions(self):
        return self.positions

    async def close(self):
        pass


def make_broker(env, ex):
    b = LiveBroker(env.ctx)
    b.ex, b._ccxt = ex, ccxt
    return b


def test_open_sets_leverage_places_order_and_protective_stops(env):
    ex = FakeEx()
    b = make_broker(env, ex)
    plan = make_plan("long", qty=0.5, lev=3)  # 0.5 BTC = 500 contracts of 0.001

    fill = asyncio.run(b.open(plan, 100.0))
    kinds = [c[0] for c in ex.calls]
    assert kinds[:3] == ["margin", "leverage", "order"]
    assert ("leverage", 3, "BTC/USDT:USDT") in ex.calls
    entry = next(c for c in ex.calls if c[0] == "order")
    assert entry[1] == "buy" and entry[2] == pytest.approx(500.0)
    stops = [c for c in ex.calls if c[0] == "order" and "stopLossPrice" in c[3]]
    tps = [c for c in ex.calls if c[0] == "order" and "takeProfitPrice" in c[3]]
    assert stops and stops[0][1] == "sell" and stops[0][3]["reduceOnly"] is True
    assert tps and tps[0][3]["reduceOnly"] is True
    assert fill.price == 100.5 and fill.qty == pytest.approx(0.5) and fill.fee == 0.4
    assert len(fill.meta["protective"]) == 2


def test_close_is_reduce_only_and_cancels_protective_orders(env):
    ex = FakeEx()
    b = make_broker(env, ex)
    env.ctx.prices["BTC/USDT"] = 100.0

    async def scenario():
        await env.execution.start()
        env.execution.broker = b
        ok, msg = await env.execution.execute(make_plan("long", qty=0.5))
        assert ok, msg
        pos = next(iter(env.execution.positions.values()))
        assert pos.mode == "live"
        await env.execution.close_position(pos, 101.0, "manual")

    asyncio.run(scenario())
    closing = [c for c in ex.calls if c[0] == "order" and c[3].get("reduceOnly") is True and "stopLossPrice" not in c[3]
               and "takeProfitPrice" not in c[3]]
    assert closing and closing[-1][1] == "sell"
    assert [c for c in ex.calls if c[0] == "cancel"]
    assert env.db.one("SELECT COUNT(*) n FROM trades")["n"] == 1


def test_timeout_halts_instead_of_blindly_retrying(env):
    ex = FakeEx(fail_first=ccxt.RequestTimeout("timeout"))
    b = make_broker(env, ex)
    env.ctx.prices["BTC/USDT"] = 100.0

    async def scenario():
        env.execution.broker = b
        return await env.execution.execute(make_plan("long", qty=0.5))

    ok, msg = asyncio.run(scenario())
    assert not ok and "unknown" in msg
    assert env.ctx.state.halted.startswith("order_uncertain")
    assert ex.n_orders == 1  # exactly one attempt: no duplicate-order risk
    assert not env.execution.positions


def test_rate_limit_is_retried(env, monkeypatch):
    async def fast_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    ex = FakeEx(fail_first=ccxt.RateLimitExceeded("slow down"))
    fill = asyncio.run(make_broker(env, ex).open(make_plan("short", qty=0.5), 100.0))
    assert fill.price == 100.5
    assert ex.n_orders >= 2  # refused once, then accepted


def test_lbank_live_is_refused_with_clear_message(env, monkeypatch):
    env.secrets_set = env.ctx.secrets
    env.ctx.secrets.set("api_key", "k" * 12)
    env.ctx.secrets.set("api_secret", "s" * 12)

    class Spot(FakeEx):
        def __init__(self):
            super().__init__(has_swap=False)

        async def load_markets(self):
            return self.markets

    monkeypatch.setattr(ccxt, "lbank", lambda params: Spot(), raising=False)
    env.settings.update({})
    with pytest.raises(ValueError, match="does not support live futures"):
        asyncio.run(env.execution.switch_mode("live"))
    assert env.settings.mode == "paper"


def test_live_requires_keys(env):
    with pytest.raises(ValueError, match="API key"):
        asyncio.run(env.execution.switch_mode("live"))


def test_reconcile_halts_on_mismatch(env):
    ex = FakeEx()
    ex.positions = [{"symbol": "ETH/USDT:USDT", "contracts": 3}]
    ex.markets["ETH/USDT:USDT"] = {"contractSize": 1}
    b = make_broker(env, ex)

    async def scenario():
        env.execution.broker = b
        return await env.execution.reconcile()

    problems = asyncio.run(scenario())
    assert problems and "does not track" in problems[0]
    assert env.ctx.state.halted.startswith("reconcile")


def test_mode_switch_blocked_with_open_positions(env):
    env.ctx.prices["BTC/USDT"] = 100.0

    async def scenario():
        await env.execution.execute(make_plan("long", qty=1))
        await env.execution.switch_mode("live")

    with pytest.raises(ValueError, match="Close all open positions"):
        asyncio.run(scenario())
