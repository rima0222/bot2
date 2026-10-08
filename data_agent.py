"""Data Agent: market feed (CCXT or simulator), resumable backfill, live polling.

Design notes for small VPSes:
* REST polling (one batched ticker call + 3 candles per pair) instead of
  exchange websockets - far fewer moving parts and a flat memory profile.
* Backfill writes every page to SQLite as it arrives and always continues from
  the newest stored candle, so an interrupted run resumes instead of restarting.
* Only *closed* candles live in the in-memory buffers (bounded deques).
"""
from __future__ import annotations

import asyncio
import math
import random
import time
import zlib
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, Awaitable, Callable

import numpy as np

from context import Context
from utils import now_ms, retry_async, tf_to_ms

BUFFER_BARS = 2000
BACKFILL_BARS = 1500
BACKFILL_MAX_BARS = 3000
PAGE = 1000

Candle = tuple  # (ts, o, h, l, c, v)


# --------------------------------------------------------------------------
# Feeds
# --------------------------------------------------------------------------
class MarketFeed(ABC):
    name = "feed"

    async def load(self) -> None: ...

    @abstractmethod
    async def fetch_ohlcv(self, symbol: str, tf: str, since: int | None, limit: int) -> list[list[float]]: ...

    @abstractmethod
    async def fetch_prices(self, symbols: list[str]) -> dict[str, float]: ...

    def min_notional(self, symbol: str, price: float) -> float:
        return 5.0

    def round_amount(self, symbol: str, qty: float) -> float:
        return math.floor(qty * 1e6) / 1e6

    async def close(self) -> None: ...


class CcxtFeed(MarketFeed):
    """Public market data through CCXT (no API keys needed)."""

    def __init__(self, exchange_id: str, prefer_swap: bool = False):
        import ccxt.async_support as ccxt  # imported lazily: ~100 MB RSS

        self._ccxt = ccxt
        if not hasattr(ccxt, exchange_id):
            raise SystemExit(f"CCXT has no exchange called '{exchange_id}'")
        self.ex = getattr(ccxt, exchange_id)({"enableRateLimit": True, "timeout": 20000})
        self.name = exchange_id
        self.prefer_swap = prefer_swap
        self._map: dict[str, str] = {}

    async def load(self) -> None:
        await retry_async(self.ex.load_markets, tries=3, base=2, retry_on=(self._ccxt.NetworkError,))

    def resolve(self, symbol: str) -> str:
        if symbol in self._map:
            return self._map[symbol]
        resolved = symbol
        markets = self.ex.markets or {}
        if self.prefer_swap and self.ex.has.get("swap"):
            quote = symbol.split("/")[1]
            cand = f"{symbol}:{quote}"
            if cand in markets:
                resolved = cand
        if resolved not in markets:
            raise self._ccxt.BadSymbol(f"{self.name} has no market {symbol}")
        self._map[symbol] = resolved
        return resolved

    async def fetch_ohlcv(self, symbol, tf, since, limit):
        sym = self.resolve(symbol)
        return await retry_async(self.ex.fetch_ohlcv, sym, tf, since, limit,
                                 tries=5, base=2, retry_on=(self._ccxt.NetworkError,))

    async def fetch_prices(self, symbols):
        syms = [self.resolve(s) for s in symbols]
        rev = {self.resolve(s): s for s in symbols}
        out: dict[str, float] = {}
        try:
            tickers = await retry_async(self.ex.fetch_tickers, syms, tries=4, base=2,
                                        retry_on=(self._ccxt.NetworkError,))
            for k, t in tickers.items():
                if k in rev and t.get("last"):
                    out[rev[k]] = float(t["last"])
        except (self._ccxt.NotSupported, self._ccxt.ExchangeError):
            pass
        for s in symbols:  # fall back per symbol for anything missing
            if s not in out:
                t = await retry_async(self.ex.fetch_ticker, self.resolve(s), tries=4, base=2,
                                      retry_on=(self._ccxt.NetworkError,))
                if t.get("last"):
                    out[s] = float(t["last"])
        return out

    def min_notional(self, symbol, price):
        try:
            m = self.ex.market(self.resolve(symbol))
            cost = (m.get("limits", {}).get("cost", {}) or {}).get("min")
            amt = (m.get("limits", {}).get("amount", {}) or {}).get("min")
            need = max(cost or 0.0, (amt or 0.0) * price)
            return float(need) if need else 5.0
        except Exception:  # noqa: BLE001
            return 5.0

    def round_amount(self, symbol, qty):
        try:
            return float(self.ex.amount_to_precision(self.resolve(symbol), qty))
        except Exception:  # noqa: BLE001
            return super().round_amount(symbol, qty)

    async def close(self):
        await self.ex.close()


class SimFeed(MarketFeed):
    """Deterministic synthetic market with mild momentum. For tests/demos only."""

    name = "sim"
    START = {"BTC/USDT": 60000.0, "ETH/USDT": 3000.0, "SOL/USDT": 150.0}
    HIST = 3000

    def __init__(self, speed: float = 1.0, seed: int = 7):
        self.speed = max(speed, 0.01)
        self.seed = seed
        self._t0 = time.time()
        self._start_ms = now_ms()
        self._series: dict[tuple[str, str], list[list[float]]] = {}
        self._base: dict[tuple[str, str], int] = {}
        self._rng: dict[tuple[str, str], random.Random] = {}
        self._r_prev: dict[tuple[str, str], float] = {}
        self._last_tf: dict[str, str] = {}

    def _vnow(self) -> float:
        return self._start_ms + (time.time() - self._t0) * 1000.0 * self.speed

    def _ensure(self, symbol: str, tf: str, k_to: int) -> None:
        key = (symbol, tf)
        tfms = tf_to_ms(tf)
        if key not in self._series:
            self._rng[key] = random.Random(zlib.crc32(f"{self.seed}|{symbol}|{tf}".encode()))
            self._base[key] = int(self._start_ms // tfms) - self.HIST
            self._series[key] = []
            self._r_prev[key] = 0.0
        s, rng = self._series[key], self._rng[key]
        sigma = 0.0018
        while self._base[key] + len(s) <= k_to:
            k = self._base[key] + len(s)
            o = s[-1][4] if s else self.START.get(symbol, 100.0)
            mu = 0.0003 * math.sin(k / 70.0)
            r = 0.4 * self._r_prev[key] + mu + sigma * rng.gauss(0, 1)
            self._r_prev[key] = r
            c = o * math.exp(r)
            h = max(o, c) * (1 + abs(rng.gauss(0, 1)) * sigma * 0.5)
            lo = min(o, c) * (1 - abs(rng.gauss(0, 1)) * sigma * 0.5)
            v = 100.0 * math.exp(0.4 * rng.gauss(0, 1)) * (1 + 0.3 * abs(r) / sigma)
            s.append([k * tfms, o, h, lo, c, v])

    def _rows(self, symbol: str, tf: str, since: int | None, limit: int) -> list[list[float]]:
        tfms = tf_to_ms(tf)
        vnow = self._vnow()
        k_cur = int(vnow // tfms)
        self._ensure(symbol, tf, k_cur)
        key = (symbol, tf)
        base = self._base[key]
        k_from = k_cur - limit + 1 if since is None else max(base, -(-int(since) // tfms))
        k_to = min(k_cur, k_from + limit - 1)
        rows = [list(self._series[key][k - base]) for k in range(max(k_from, base), k_to + 1)]
        if rows and int(rows[-1][0]) == k_cur * tfms:  # forming candle: show partial progress
            frac = (vnow - k_cur * tfms) / tfms
            ts, o, h, lo, c, v = rows[-1]
            pc = o * math.exp(math.log(c / o) * frac)
            rows[-1] = [ts, o, max(o, pc, o + (h - o) * frac), min(o, pc, o - (o - lo) * frac), pc, v * frac]
        return rows

    async def fetch_ohlcv(self, symbol, tf, since, limit):
        self._last_tf[symbol] = tf
        return self._rows(symbol, tf, since, limit)

    async def fetch_prices(self, symbols):
        out = {}
        for s in symbols:
            rows = self._rows(s, self._last_tf.get(s, "5m"), None, 1)
            out[s] = rows[-1][4]
        return out


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------
PriceListener = Callable[[dict], Awaitable[None]]
CandleListener = Callable[[str, Candle], Awaitable[None]]


class DataAgent:
    name = "data"

    def __init__(self, ctx: Context, feed: MarketFeed):
        self.ctx = ctx
        self.feed = feed
        self.log = ctx.getlog("data")
        self.buffers: dict[tuple[str, str], deque[Candle]] = {}
        self.forming: dict[tuple[str, str], Candle] = {}
        self.last_ok: dict[str, float] = {}
        self.backfill_state: dict[str, dict[str, Any]] = {}
        self.price_listeners: list[PriceListener] = []
        self.candle_listeners: list[CandleListener] = []
        self.price_poll = 5.0
        self.candle_poll = 15.0
        self._err_logged = 0.0
        self.pair_errors: dict[str, str] = {}
        self._retry_at: dict[str, float] = {}

    # ---- public helpers ----------------------------------------------------
    def arrays(self, symbol: str, tf: str) -> dict[str, np.ndarray] | None:
        buf = self.buffers.get((symbol, tf))
        if not buf:
            return None
        a = np.asarray(buf, dtype=np.float64)
        return {"ts": a[:, 0], "o": a[:, 1], "h": a[:, 2], "l": a[:, 3], "c": a[:, 4], "v": a[:, 5]}

    def n_bars(self, symbol: str, tf: str) -> int:
        return len(self.buffers.get((symbol, tf), ()))

    # ---- main loop -----------------------------------------------------------
    async def run(self) -> None:
        await self._load_markets()
        last_price = last_candle = 0.0
        while True:
            s = self.ctx.settings
            tf, pairs = s.timeframe, list(s.pairs)
            for sym in pairs:
                if (sym, tf) in self.buffers or time.monotonic() < self._retry_at.get(sym, 0):
                    continue
                try:
                    await self._backfill(sym, tf)
                    self.pair_errors.pop(sym, None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - bad pair or flaky network: report, retry later
                    self.pair_errors[sym] = str(exc)[:200]
                    self._retry_at[sym] = time.monotonic() + 30
                    self.log.warning("cannot load %s: %s (retrying in 30s)", sym, exc)
            now = time.monotonic()
            if now - last_price >= self.price_poll:
                last_price = now
                await self._guard(self._poll_prices, [p for p in pairs if (p, tf) in self.buffers])
            if now - last_candle >= self.candle_poll:
                last_candle = now
                await self._guard(self._poll_candles, pairs, tf)
            self.ctx.beat(self.name)
            await asyncio.sleep(0.5)

    async def _load_markets(self) -> None:
        delay = 2.0
        while True:
            try:
                await self.feed.load()
                self.ctx.feed_error = None
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.ctx.feed_error = f"cannot reach {self.feed.name}: {exc}"[:300]
                self.log.warning("%s - retrying in %.0fs", self.ctx.feed_error, delay)
                self.ctx.beat(self.name)
                await asyncio.sleep(delay)
                delay = min(60.0, delay * 2)

    async def _guard(self, fn, *args) -> None:
        try:
            await fn(*args)
            self.ctx.feed_error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - network hiccups must not kill the agent
            self.ctx.feed_error = f"{type(exc).__name__}: {exc}"[:300]
            if time.time() - self._err_logged > 60:
                self._err_logged = time.time()
                self.log.warning("market data error: %s", self.ctx.feed_error)

    # ---- backfill (resumable) ---------------------------------------------------
    async def _backfill(self, symbol: str, tf: str) -> None:
        tfms = tf_to_ms(tf)
        key = f"{symbol}|{tf}"
        row = self.ctx.db.one("SELECT MAX(ts) AS m FROM candles WHERE symbol=? AND tf=?", (symbol, tf))
        last = row["m"] if row and row["m"] is not None else None
        now = now_ms()
        floor = now - BACKFILL_MAX_BARS * tfms
        since = max(last + tfms, floor) if last else now - BACKFILL_BARS * tfms
        resumed = last is not None
        self.backfill_state[key] = {"done": False, "fetched": 0, "resumed": resumed}
        self.log.info("backfill %s %s from %s%s", symbol, tf, time.strftime("%Y-%m-%d %H:%M", time.gmtime(since / 1000)),
                      " (resuming)" if resumed else "")
        while since <= now:
            rows = await retry_async(self.feed.fetch_ohlcv, symbol, tf, since, PAGE, tries=8, base=2, cap=60)
            if not rows:
                break
            data = [(symbol, tf, int(r[0]), r[1], r[2], r[3], r[4], r[5] or 0.0) for r in rows if r[4]]
            await asyncio.to_thread(self.ctx.db.executemany,
                                    "INSERT OR REPLACE INTO candles(symbol,tf,ts,o,h,l,c,v) VALUES(?,?,?,?,?,?,?,?)", data)
            self.backfill_state[key]["fetched"] += len(data)
            newest = int(rows[-1][0])
            self.ctx.beat(self.name)
            if newest + tfms <= since:  # no forward progress
                break
            since = newest + tfms
            if len(rows) < 2:
                break
        rows = self.ctx.db.query(
            "SELECT ts,o,h,l,c,v FROM candles WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT ?",
            (symbol, tf, BUFFER_BARS))
        rows = list(reversed(rows))
        # The newest stored candle may still be forming -> keep it out of the closed buffer.
        closed = [tuple(r) for r in rows if r["ts"] + tfms <= now_ms() - 1000]
        self.buffers[(symbol, tf)] = deque(closed, maxlen=BUFFER_BARS)
        self.backfill_state[key]["done"] = True
        self.log.info("backfill %s %s done: %d bars in memory", symbol, tf, len(closed))

    # ---- polling -----------------------------------------------------------------
    async def _poll_prices(self, pairs: list[str]) -> None:
        prices = await self.feed.fetch_prices(pairs)
        ts = now_ms()
        for sym, px in prices.items():
            self.ctx.prices[sym] = px
            self.ctx.price_ts[sym] = ts
            self.last_ok[sym] = time.time()
        if prices:
            for fn in self.price_listeners:
                await fn(prices)

    async def _poll_candles(self, pairs: list[str], tf: str) -> None:
        tfms = tf_to_ms(tf)
        for sym in pairs:
            buf = self.buffers.get((sym, tf))
            if buf is None:
                continue
            rows = await self.feed.fetch_ohlcv(sym, tf, None, 3)
            if buf and rows and rows[0][0] > buf[-1][0] + tfms:  # gap -> catch up
                rows = await self.feed.fetch_ohlcv(sym, tf, buf[-1][0] + tfms, PAGE)
            if not rows:
                continue
            data = [(sym, tf, int(r[0]), r[1], r[2], r[3], r[4], r[5] or 0.0) for r in rows if r[4]]
            self.ctx.db.executemany(
                "INSERT OR REPLACE INTO candles(symbol,tf,ts,o,h,l,c,v) VALUES(?,?,?,?,?,?,?,?)", data)
            *closed, forming = rows
            self.forming[(sym, tf)] = tuple(forming)
            newest = None
            for r in closed:
                c = (int(r[0]), r[1], r[2], r[3], r[4], r[5] or 0.0)
                if not buf or c[0] > buf[-1][0]:
                    buf.append(c)
                    newest = c
                    for fn in self.candle_listeners:
                        await fn(sym, c)
            if newest is not None:
                try:
                    self.ctx.candle_q.put_nowait((sym, tf, newest[0]))
                except asyncio.QueueFull:
                    self.log.warning("strategy is behind; dropping candle event for %s", sym)
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        await self.feed.close()
