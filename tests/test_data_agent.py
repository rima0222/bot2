import asyncio
import time

import pytest

from data_agent import BACKFILL_BARS, DataAgent, PAGE, SimFeed
from utils import tf_to_ms

_REAL_SLEEP = asyncio.sleep  # captured before the `fast` fixture patches it
TF = "5m"
TFMS = tf_to_ms(TF)


class FlakyFeed(SimFeed):
    def __init__(self):
        super().__init__(1)
        self.calls = []
        self.fail_from = None   # fail every call from this call number on
        self.bad_symbols = set()
        self.load_failures = 0

    async def load(self):
        if self.load_failures > 0:
            self.load_failures -= 1
            raise ConnectionError("exchange unreachable")

    async def fetch_ohlcv(self, symbol, tf, since, limit):
        self.calls.append((symbol, since))
        if symbol in self.bad_symbols:
            raise ValueError(f"no market {symbol}")
        if self.fail_from is not None and len(self.calls) >= self.fail_from:
            raise ConnectionError("network dropped")
        return await super().fetch_ohlcv(symbol, tf, since, limit)


@pytest.fixture
def fast(monkeypatch):
    real = asyncio.sleep

    async def quick(t, *a, **k):
        await real(0)

    monkeypatch.setattr(asyncio, "sleep", quick)


async def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        await _REAL_SLEEP(0.02)  # real time: backfill writes happen in a worker thread


def stored(env, sym="BTC/USDT"):
    return env.db.one("SELECT COUNT(*) n, MIN(ts) lo, MAX(ts) hi FROM candles WHERE symbol=? AND tf=?", (sym, TF))


def test_backfill_resumes_after_interruption_without_refetching(env, fast):
    feed = FlakyFeed()
    agent = DataAgent(env.ctx, feed)
    feed.fail_from = 2  # page 1 arrives, then the connection dies for good

    with pytest.raises(ConnectionError):
        asyncio.run(agent._backfill("BTC/USDT", TF))
    first = stored(env)
    assert 0 < first["n"] <= PAGE  # page 1 is already safely on disk

    feed.fail_from = None
    feed.calls.clear()
    agent2 = DataAgent(env.ctx, feed)  # e.g. after a process restart
    asyncio.run(agent2._backfill("BTC/USDT", TF))
    assert feed.calls[0][1] == first["hi"] + TFMS  # continued exactly after the last stored candle
    done = stored(env)
    assert done["n"] >= BACKFILL_BARS - 2
    assert agent2.backfill_state["BTC/USDT|5m"]["resumed"] is True
    # every stored candle is unique and contiguous
    ts = [r["ts"] for r in env.db.query("SELECT ts FROM candles WHERE symbol='BTC/USDT' ORDER BY ts")]
    assert all(b - a == TFMS for a, b in zip(ts, ts[1:]))
    assert len(agent2.buffers[("BTC/USDT", TF)]) >= BACKFILL_BARS - 3  # only closed candles in memory


def test_unreachable_exchange_is_reported_and_retried_not_fatal(env, fast):
    feed = FlakyFeed()
    feed.load_failures = 2
    agent = DataAgent(env.ctx, feed)

    async def scenario():
        task = asyncio.create_task(agent.run())
        await wait_for(lambda: ("BTC/USDT", TF) in agent.buffers)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert ("BTC/USDT", TF) in agent.buffers and env.ctx.feed_error is None  # recovered by itself


def test_one_bad_pair_does_not_block_the_others(env, fast):
    feed = FlakyFeed()
    feed.bad_symbols = {"ETH/USDT"}
    agent = DataAgent(env.ctx, feed)

    async def scenario():
        task = asyncio.create_task(agent.run())
        await wait_for(lambda: ("BTC/USDT", TF) in agent.buffers and "ETH/USDT" in agent.pair_errors)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert ("BTC/USDT", TF) in agent.buffers
    assert ("ETH/USDT", TF) not in agent.buffers and "no market" in agent.pair_errors["ETH/USDT"]


def test_poll_emits_each_closed_candle_once_and_catches_up_after_a_gap(env):
    feed = SimFeed(speed=1)
    agent = DataAgent(env.ctx, feed)
    asyncio.run(agent._backfill("BTC/USDT", TF))
    buf = agent.buffers[("BTC/USDT", TF)]
    closed_events = []

    async def on_candle(sym, c):
        closed_events.append(c[0])

    agent.candle_listeners.append(on_candle)
    # pretend the process slept for 4 candles: the buffer ends 4 candles in the past
    for _ in range(4):
        buf.pop()
    last = buf[-1][0]

    asyncio.run(agent._poll_candles(["BTC/USDT"], TF))
    assert closed_events and closed_events == sorted(set(closed_events))
    assert closed_events[0] == last + TFMS  # gap filled in order, nothing skipped
    n_before = len(closed_events)
    asyncio.run(agent._poll_candles(["BTC/USDT"], TF))
    assert len(closed_events) == n_before  # polling again yields no duplicates
    assert env.ctx.candle_q.qsize() >= 1
