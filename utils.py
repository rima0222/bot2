"""Small shared helpers: time, timeframes, async retry with backoff."""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Awaitable, Callable

TF_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}


def now_ms() -> int:
    return int(time.time() * 1000)


def tf_to_ms(tf: str) -> int:
    try:
        return TF_MS[tf]
    except KeyError:
        raise ValueError(f"unsupported timeframe: {tf}") from None


async def retry_async(
    fn: Callable[..., Awaitable[Any]],
    *args: Any,
    tries: int = 5,
    base: float = 1.0,
    cap: float = 30.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    **kwargs: Any,
) -> Any:
    """Call ``fn`` with exponential backoff + jitter. Re-raises after ``tries``."""
    attempt = 0
    while True:
        try:
            return await fn(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except retry_on as exc:  # noqa: PERF203
            attempt += 1
            if attempt >= tries:
                raise
            delay = min(cap, base * (2 ** (attempt - 1))) * (0.7 + 0.6 * random.random())
            if on_retry:
                on_retry(attempt, exc, delay)
            await asyncio.sleep(delay)


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x
