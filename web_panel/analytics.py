"""Trade statistics for the Analytics page (pure functions over trade rows)."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any, Iterable


def _group(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})
    for r in rows:
        g = out[str(r[key])]
        g["trades"] += 1
        g["pnl"] += r["pnl"]
        g["wins"] += 1 if r["pnl"] > 0 else 0
    return {k: {"trades": int(v["trades"]), "pnl": v["pnl"], "win_rate": v["wins"] / v["trades"] * 100}
            for k, v in out.items()}


def compute(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(trades, key=lambda r: r["closed_at"])
    n = len(rows)
    if n == 0:
        return {"trades": 0}
    pnls = [r["pnl"] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)

    cum, peak, max_dd, curve = 0.0, 0.0, 0.0, []
    streak = worst_streak = 0
    for r in rows:
        cum += r["pnl"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        curve.append({"ts": r["closed_at"], "cum": cum})
        streak = streak + 1 if r["pnl"] <= 0 else 0
        worst_streak = max(worst_streak, streak)

    daily: dict[str, float] = defaultdict(float)
    for r in rows:
        daily[dt.datetime.fromtimestamp(r["closed_at"] / 1000, dt.timezone.utc).strftime("%Y-%m-%d")] += r["pnl"]

    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = -gross_loss / len(losses) if losses else 0.0
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n * 100,
        "net_pnl": sum(pnls),
        "gross_profit": gross_win,
        "gross_loss": -gross_loss,
        "fees": sum(r["fees"] for r in rows),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "risk_reward": (avg_win / -avg_loss) if avg_loss < 0 else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "expectancy": sum(pnls) / n,
        "avg_r": sum(r["r_mult"] or 0.0 for r in rows) / n,
        "best": max(pnls),
        "worst": min(pnls),
        "max_drawdown": max_dd,
        "max_loss_streak": worst_streak,
        "avg_hold_min": sum((r["closed_at"] - r["opened_at"]) for r in rows) / n / 60000,
        "by_symbol": _group(rows, "symbol"),
        "by_side": _group(rows, "side"),
        "by_reason": _group(rows, "reason"),
        "daily": [{"date": d, "pnl": v} for d, v in sorted(daily.items())][-60:],
        "curve": curve[-500:],
    }
