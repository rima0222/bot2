"""Plain data types shared between agents."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

LONG = "long"
SHORT = "short"


@dataclass
class Signal:
    ts: int
    symbol: str
    side: str  # long | short
    prob: float  # model P(up)
    price: float
    atr_pct: float  # ATR as a fraction of price (0.004 = 0.4 %)
    tf: str
    signal_id: int = 0


@dataclass
class OrderPlan:
    symbol: str
    side: str
    qty: float  # base-currency quantity
    signal_price: float
    sl: float
    tp: float
    leverage: int
    notional: float
    margin: float
    risk_amount: float
    signal_id: int = 0


@dataclass
class Position:
    id: int
    symbol: str
    side: str
    qty: float
    entry: float
    sl: float
    tp: float
    leverage: int
    margin: float
    liq: float
    risk_amount: float
    fee_open: float
    opened_at: int
    mode: str
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def direction(self) -> int:
        return 1 if self.side == LONG else -1

    def unrealized(self, price: float) -> float:
        return self.direction * (price - self.entry) * self.qty

    def public(self, price: float | None, now: int) -> dict[str, Any]:
        d = asdict(self)
        d.pop("meta", None)
        up = self.unrealized(price) if price else 0.0
        d["price"] = price
        d["upnl"] = up
        d["upnl_pct"] = (up / self.margin * 100.0) if self.margin else 0.0
        d["age_s"] = max(0, (now - self.opened_at) // 1000)
        return d


def liquidation_price(side: str, entry: float, leverage: int, maint: float = 0.005) -> float:
    """Isolated-margin liquidation estimate (maintenance margin ``maint``)."""
    dist = max(1.0 / max(leverage, 1) - maint, 0.0)
    return entry * (1.0 - dist) if side == LONG else entry * (1.0 + dist)
