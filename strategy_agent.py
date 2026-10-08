"""Strategy & ML Agent.

Pipeline per closed candle:
  features -> online logistic-regression P(up over the next N bars)
           -> price-action filters (trend alignment, volatility band, volume)
           -> Signal

The model is retrained periodically on a rolling window and validated on the
most recent, unseen 25 % of samples. It may only trade while that holdout
accuracy clears ``min_model_acc`` - a flat/unvalidated model produces no trades.
Between retrains it keeps learning one labelled bar at a time (SGD).

Pure numpy: no scikit-learn / LightGBM, so it fits on a 512 MB VPS.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from context import Context
from data_agent import DataAgent
from models import LONG, SHORT, Signal
from utils import now_ms

WARMUP = 40
MIN_BARS = 400
LABEL_FRAC = 0.25  # a move must exceed 0.25 x ATR to count as up/down
FEATURE_NAMES = ["ret1", "ret3", "ret5", "ret10", "ret20", "rsi", "ema_spread",
                 "atr_pct", "bollinger", "volume_z", "body"]
N_FEATURES = len(FEATURE_NAMES)


# --------------------------------------------------------------------------
# Indicators / features
# --------------------------------------------------------------------------
def _ema(x: np.ndarray, alpha: float) -> np.ndarray:
    out = np.empty_like(x)
    out[0] = x[0]
    prev = x[0]
    for i in range(1, len(x)):
        prev = alpha * x[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


def _roll(x: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    mean = np.full(len(x), np.nan)
    std = np.full(len(x), np.nan)
    if len(x) >= n:
        w = sliding_window_view(x, n)
        mean[n - 1:] = w.mean(axis=1)
        std[n - 1:] = w.std(axis=1)
    return mean, std


def compute_features(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray,
                     v: np.ndarray) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Return (X[n, F] with NaN warm-up rows, aux series)."""
    n = len(c)
    lc = np.log(c)

    def lag_ret(k: int) -> np.ndarray:
        out = np.full(n, np.nan)
        if n > k:
            out[k:] = lc[k:] - lc[:-k]
        return out

    prev_c = np.concatenate(([c[0]], c[:-1]))
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    atr = _ema(tr, 1.0 / 14.0)
    atr_pct = atr / c

    d = np.diff(c, prepend=c[0])
    up = _ema(np.where(d > 0, d, 0.0), 1.0 / 14.0)
    dn = _ema(np.where(d < 0, -d, 0.0), 1.0 / 14.0)
    rsi = 100.0 - 100.0 / (1.0 + up / (dn + 1e-12))

    ema_fast, ema_slow = _ema(c, 2 / 10), _ema(c, 2 / 22)
    ema_spread = (ema_fast - ema_slow) / ema_slow

    m20, s20 = _roll(c, 20)
    bb = np.clip((c - m20) / (2 * s20 + 1e-12), -3, 3)
    vm, vs = _roll(v, 20)
    vol_z = np.clip((v - vm) / (vs + 1e-12), -3, 3)
    body = (c - o) / (h - l + 1e-12)

    X = np.column_stack([lag_ret(1), lag_ret(3), lag_ret(5), lag_ret(10), lag_ret(20),
                         (rsi - 50.0) / 50.0, ema_spread, atr_pct, bb, vol_z, body])
    X[:WARMUP] = np.nan
    return X, {"atr_pct": atr_pct, "ema_fast": ema_fast, "ema_slow": ema_slow, "vol_z": vol_z, "lc": lc}


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------
def _sigmoid(z: np.ndarray | float) -> np.ndarray | float:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


class OnlineLogReg:
    def __init__(self, n_features: int = N_FEATURES):
        self.w = np.zeros(n_features)
        self.b = 0.0
        self.mu = np.zeros(n_features)
        self.sd = np.ones(n_features)
        self.updates = 0

    def fit(self, X: np.ndarray, y: np.ndarray, iters: int = 300, lr: float = 0.1, l2: float = 1e-2) -> None:
        self.mu = X.mean(axis=0)
        self.sd = X.std(axis=0) + 1e-9
        Z = (X - self.mu) / self.sd
        w, b, n = np.zeros(X.shape[1]), 0.0, len(y)
        for _ in range(iters):
            err = _sigmoid(Z @ w + b) - y
            w -= lr * (Z.T @ err / n + l2 * w)
            b -= lr * err.mean()
        self.w, self.b = w, float(b)

    def partial_fit(self, x: np.ndarray, y: float, lr: float = 0.02, l2: float = 1e-3) -> None:
        z = np.clip((x - self.mu) / self.sd, -6, 6)
        err = float(_sigmoid(z @ self.w + self.b)) - y
        self.w -= lr * (err * z + l2 * self.w)
        self.b -= lr * err
        self.updates += 1

    def proba(self, x: np.ndarray) -> float:
        z = np.clip((x - self.mu) / self.sd, -6, 6)
        return float(_sigmoid(z @ self.w + self.b))

    def predict_many(self, X: np.ndarray) -> np.ndarray:
        Z = np.clip((X - self.mu) / self.sd, -6, 6)
        return _sigmoid(Z @ self.w + self.b)

    def to_json(self) -> str:
        return json.dumps({"w": self.w.tolist(), "b": self.b, "mu": self.mu.tolist(), "sd": self.sd.tolist()})

    @classmethod
    def from_json(cls, blob: str) -> "OnlineLogReg":
        d = json.loads(blob)
        m = cls(len(d["w"]))
        m.w, m.b = np.array(d["w"]), float(d["b"])
        m.mu, m.sd = np.array(d["mu"]), np.array(d["sd"])
        return m


def build_dataset(arr: dict[str, np.ndarray], horizon: int) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    X, aux = compute_features(arr["o"], arr["h"], arr["l"], arr["c"], arr["v"])
    n = len(arr["c"])
    if n < WARMUP + horizon + 50:
        return None
    idx = np.arange(WARMUP, n - horizon)
    fwd = aux["lc"][idx + horizon] - aux["lc"][idx]
    thr = LABEL_FRAC * aux["atr_pct"][idx]
    keep = (np.abs(fwd) > thr) & ~np.isnan(X[idx]).any(axis=1)
    idx = idx[keep]
    return X[idx], (fwd[keep] > 0).astype(float), idx


def train_model(arr: dict[str, np.ndarray], horizon: int) -> tuple[OnlineLogReg, dict[str, Any]] | None:
    ds = build_dataset(arr, horizon)
    if ds is None:
        return None
    X, y, idx = ds
    if len(y) < 200:
        return None
    split = int(len(y) * 0.75)
    # purge: drop test rows whose label window overlaps the training window
    test_mask = idx >= idx[split - 1] + horizon
    test_mask[:split] = False
    Xte, yte = X[test_mask], y[test_mask]
    if len(yte) < 50:
        return None
    model = OnlineLogReg()
    model.fit(X[:split], y[:split])
    p_test = model.predict_many(Xte)
    acc = float(((p_test >= 0.5) == (yte > 0.5)).mean())
    p_train = model.predict_many(X[:split])
    acc_train = float(((p_train >= 0.5) == (y[:split] > 0.5)).mean())
    final = OnlineLogReg()
    final.fit(X, y)  # use all labelled data for live predictions
    meta = {"acc": acc, "acc_train": acc_train, "n_test": int(len(yte)), "n_train": int(split),
            "base_rate": float(max(yte.mean(), 1 - yte.mean())), "horizon": int(horizon),
            "trained_at": now_ms()}
    return final, meta


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------
class StrategyAgent:
    name = "strategy"

    def __init__(self, ctx: Context, data: DataAgent):
        self.ctx = ctx
        self.data = data
        self.log = ctx.getlog("strategy")
        self.models: dict[tuple[str, str], OnlineLogReg] = {}
        self.meta: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_learn: dict[tuple[str, str], float] = {}
        self._train_next: dict[tuple[str, str], float] = {}
        self._load_models()

    # ---- persistence ---------------------------------------------------------
    def _load_models(self) -> None:
        for r in self.ctx.db.query("SELECT * FROM models"):
            try:
                key = (r["symbol"], r["tf"])
                self.models[key] = OnlineLogReg.from_json(r["blob"])
                meta = dict(self.ctx.db.kv_get(f"model_meta:{r['symbol']}:{r['tf']}", {}) or {})
                meta.update({"acc": r["acc"], "n_test": r["n_test"], "n_train": r["n_train"],
                             "trained_at": r["trained_at"]})
                self.meta[key] = meta
            except Exception as exc:  # noqa: BLE001
                self.log.warning("could not load saved model: %s", exc)

    def _save_model(self, key: tuple[str, str]) -> None:
        m, meta = self.models[key], self.meta[key]
        self.ctx.db.execute(
            "INSERT OR REPLACE INTO models(symbol,tf,blob,trained_at,acc,n_test,n_train) VALUES(?,?,?,?,?,?,?)",
            (key[0], key[1], m.to_json(), meta["trained_at"], meta["acc"], meta["n_test"], meta["n_train"]))
        self.ctx.db.kv_set(f"model_meta:{key[0]}:{key[1]}", meta)

    # ---- status for the panel ------------------------------------------------
    def is_validated(self, key: tuple[str, str]) -> bool:
        # must beat BOTH the configured accuracy bar and the "always guess the majority class" baseline
        meta, s = self.meta.get(key), self.ctx.settings
        return bool(meta and key in self.models and meta.get("n_test", 0) >= 50
                    and meta.get("acc", 0) >= s.min_model_acc
                    and meta.get("acc", 0) >= meta.get("base_rate", 0.5) + 0.01
                    and meta.get("horizon", s.horizon_bars) == s.horizon_bars)

    def status(self) -> dict[str, Any]:
        s, out = self.ctx.settings, {}
        for sym in s.pairs:
            key = (sym, s.timeframe)
            meta = self.meta.get(key)
            out[sym] = {
                "trained": bool(meta), "validated": self.is_validated(key),
                "acc": meta.get("acc") if meta else None, "n_test": meta.get("n_test") if meta else None,
                "base_rate": meta.get("base_rate") if meta else None,
                "trained_at": meta.get("trained_at") if meta else None,
                "bars": self.data.n_bars(sym, s.timeframe),
                "online_updates": self.models[key].updates if key in self.models else 0,
            }
        return out

    # ---- training loop -------------------------------------------------------
    async def retrain_loop(self) -> None:
        last_run: dict[tuple[str, str], float] = {}
        while True:
            s = self.ctx.settings
            for sym in list(s.pairs):
                key = (sym, s.timeframe)
                if self.data.n_bars(*key) < MIN_BARS:
                    continue
                stale = time.time() - last_run.get(key, 0) > s.retrain_minutes * 60
                missing = key not in self.models or self.meta[key].get("horizon", s.horizon_bars) != s.horizon_bars
                if (stale or missing) and time.time() >= self._train_next.get(key, 0):
                    await self.train(sym, s.timeframe)
                    last_run[key] = time.time()
            self.ctx.beat("strategy-train")
            await asyncio.sleep(10)

    async def train(self, sym: str, tf: str) -> None:
        key = (sym, tf)
        arr = self.data.arrays(sym, tf)
        if arr is None:
            return
        horizon = self.ctx.settings.horizon_bars
        result = await asyncio.to_thread(train_model, arr, horizon)
        self._train_next[key] = time.time() + 120
        if result is None:
            self.log.info("not enough labelled data yet to train %s %s", sym, tf)
            return
        model, meta = result
        self.models[key], self.meta[key] = model, meta
        self._save_model(key)
        ok = self.is_validated(key)
        self.log.info("model %s %s: holdout accuracy %.3f on %d unseen bars (always-guess-majority = %.3f) -> %s",
                      sym, tf, meta["acc"], meta["n_test"], meta["base_rate"],
                      "VALIDATED" if ok else "not validated, will not trade")

    # ---- signal loop -----------------------------------------------------------
    async def run(self) -> None:
        while True:
            sym, tf, ts = await self.ctx.candle_q.get()
            self.ctx.beat(self.name)
            try:
                if tf == self.ctx.settings.timeframe and sym in self.ctx.settings.pairs:
                    self.on_close(sym, tf)
            except Exception:  # noqa: BLE001
                self.log.exception("strategy error on %s", sym)

    def on_close(self, sym: str, tf: str) -> None:
        key = (sym, tf)
        s = self.ctx.settings
        arr = self.data.arrays(sym, tf)
        H = s.horizon_bars
        if arr is None or len(arr["c"]) < WARMUP + H + 5:
            return
        X, aux = compute_features(arr["o"], arr["h"], arr["l"], arr["c"], arr["v"])
        n = len(arr["c"])
        model = self.models.get(key)
        if model is None:
            return

        # 1) online learning: the bar H steps back now has a known outcome
        i = n - 1 - H
        if i >= WARMUP and self._last_learn.get(key, 0) < arr["ts"][i] and not np.isnan(X[i]).any():
            fwd = aux["lc"][n - 1] - aux["lc"][i]
            if abs(fwd) > LABEL_FRAC * aux["atr_pct"][i]:
                model.partial_fit(X[i], 1.0 if fwd > 0 else 0.0)
            self._last_learn[key] = arr["ts"][i]

        # 2) decide
        if not self.ctx.state.trading_allowed or not self.is_validated(key) or np.isnan(X[-1]).any():
            return
        p = model.proba(X[-1])
        long_ok, short_ok = p >= 0.5 + s.edge, p <= 0.5 - s.edge
        if not (long_ok or short_ok):
            return
        side = LONG if long_ok else SHORT
        price = float(self.ctx.prices.get(sym) or arr["c"][-1])
        atr_pct = float(aux["atr_pct"][-1])

        why: list[str] = []
        if not (s.min_atr_pct <= atr_pct * 100 <= s.max_atr_pct):
            why.append(f"volatility {atr_pct * 100:.2f}% outside band")
        ef, es, close = aux["ema_fast"][-1], aux["ema_slow"][-1], arr["c"][-1]
        if side == LONG and not (ef > es and close > es):
            why.append("against trend")
        if side == SHORT and not (ef < es and close < es):
            why.append("against trend")
        if aux["vol_z"][-1] < -1.5:
            why.append("volume dried up")
        if why:
            self.ctx.db.execute(
                "INSERT INTO signals(ts,symbol,side,prob,price,status,reason) VALUES(?,?,?,?,?,?,?)",
                (now_ms(), sym, side, p, price, "filtered", "; ".join(why)))
            return
        sid = self.ctx.db.execute(
            "INSERT INTO signals(ts,symbol,side,prob,price,status,reason) VALUES(?,?,?,?,?,?,?)",
            (now_ms(), sym, side, p, price, "new", ""))
        try:
            self.ctx.signal_q.put_nowait(Signal(now_ms(), sym, side, p, price, atr_pct, tf, sid))
            self.log.info("signal %s %s p(up)=%.3f price=%s", side.upper(), sym, p, price)
        except asyncio.QueueFull:
            self.log.warning("risk agent is busy; dropped signal for %s", sym)
