import asyncio
from collections import deque

import numpy as np
import pytest

from data_agent import SimFeed
from models import LONG, SHORT
from strategy_agent import (FEATURE_NAMES, N_FEATURES, OnlineLogReg, WARMUP, build_dataset, compute_features,
                            train_model)
from utils import now_ms


def ohlcv_from_sim(n=1500, seed=3):
    feed = SimFeed(1, seed=seed)
    rows = asyncio.run(feed.fetch_ohlcv("BTC/USDT", "5m", None, n + 1))[:-1]
    a = np.array(rows, dtype=float)
    return {"ts": a[:, 0], "o": a[:, 1], "h": a[:, 2], "l": a[:, 3], "c": a[:, 4], "v": a[:, 5]}


def random_walk(n=1500, seed=0, ar=0.0):
    rng = np.random.default_rng(seed)
    r = np.zeros(n)
    for i in range(1, n):
        r[i] = ar * r[i - 1] + 0.002 * rng.standard_normal()
    c = 100 * np.exp(np.cumsum(r))
    o = np.concatenate(([100.0], c[:-1]))
    h, l = np.maximum(o, c) * 1.0008, np.minimum(o, c) * 0.9992
    return {"ts": np.arange(n) * 300_000.0, "o": o, "h": h, "l": l, "c": c, "v": 100 + 10 * rng.random(n)}


def test_features_shape_and_warmup():
    a = ohlcv_from_sim(300)
    X, aux = compute_features(a["o"], a["h"], a["l"], a["c"], a["v"])
    assert X.shape == (300, N_FEATURES) == (300, len(FEATURE_NAMES))
    assert np.isnan(X[:WARMUP]).all() and not np.isnan(X[WARMUP:]).any()
    assert (aux["atr_pct"] > 0).all() and X[WARMUP:, FEATURE_NAMES.index("rsi")].min() >= -1


def test_features_do_not_peek_into_the_future():
    a = ohlcv_from_sim(400)
    X1, _ = compute_features(a["o"], a["h"], a["l"], a["c"], a["v"])
    cut = 300
    X2, _ = compute_features(a["o"][:cut], a["h"][:cut], a["l"][:cut], a["c"][:cut], a["v"][:cut])
    np.testing.assert_allclose(X1[:cut], X2, equal_nan=True)  # row t depends only on data up to t


def test_dataset_labels_are_forward_looking_and_aligned():
    a = random_walk(600, ar=0.3)
    X, y, idx = build_dataset(a, horizon=3)
    assert len(X) == len(y) == len(idx) and idx.max() < 600 - 3 and idx.min() >= WARMUP
    fwd = np.log(a["c"][idx + 3] / a["c"][idx])
    assert ((fwd > 0).astype(float) == y).all()


def edge_over_seeds(ar, seeds):
    out = []
    for seed in seeds:
        result = train_model(random_walk(1800, seed=seed, ar=ar), horizon=3)
        assert result is not None
        out.append(result[1]["acc"] - result[1]["base_rate"])
    return np.array(out)


def test_model_learns_real_momentum_and_beats_baseline():
    edge = edge_over_seeds(0.4, range(10))
    assert edge.mean() > 0.03 and (edge > 0).mean() >= 0.7  # unseen-data accuracy clearly above "guess the majority"


def test_model_finds_nothing_in_pure_noise():
    edge = edge_over_seeds(0.0, range(100, 110))
    assert edge.mean() < 0.0 and (edge > 0.03).mean() <= 0.1  # no phantom edge on a driftless random walk


def test_too_little_data_returns_none():
    assert train_model(random_walk(120), horizon=3) is None


def test_model_serialization_roundtrip_and_online_update():
    a = random_walk(1500, ar=0.4)
    model, _ = train_model(a, 3)
    clone = OnlineLogReg.from_json(model.to_json())
    X, y, _ = build_dataset(a, 3)
    np.testing.assert_allclose(model.predict_many(X[:50]), clone.predict_many(X[:50]))
    before = clone.proba(X[0])
    for _ in range(50):
        clone.partial_fit(X[0], 1.0 - float(round(before)))  # push against the current opinion
    assert abs(clone.proba(X[0]) - before) > 0.02 and clone.updates == 50


# ---- signal decision logic ------------------------------------------------------------------
def prime_agent(env, closes, prob, validated=True):
    s, tf = env.settings, "5m"
    c = np.asarray(closes, dtype=float)
    o = np.concatenate(([c[0]], c[:-1]))
    rows = [(i * 300_000, o[i], max(o[i], c[i]) * 1.001, min(o[i], c[i]) * 0.999, c[i], 100.0) for i in range(len(c))]
    env.data.buffers[("BTC/USDT", tf)] = deque(rows, maxlen=2000)
    model = OnlineLogReg()
    model.proba = lambda x: prob  # type: ignore[method-assign]
    env.strategy.models[("BTC/USDT", tf)] = model
    env.strategy.meta[("BTC/USDT", tf)] = {"acc": 0.60 if validated else 0.49, "n_test": 300, "base_rate": 0.52,
                                           "horizon": s.horizon_bars, "trained_at": now_ms()}
    env.settings.update({"max_atr_pct": 10})


def uptrend(n=200):
    rng = np.random.default_rng(1)
    return 100 * np.exp(np.cumsum(0.0012 + 0.0004 * rng.standard_normal(n)))


def test_signal_emitted_when_model_and_trend_agree(env):
    prime_agent(env, uptrend(), prob=0.85)
    env.strategy.on_close("BTC/USDT", "5m")
    sig = env.ctx.signal_q.get_nowait()
    assert sig.side == LONG and sig.prob == 0.85
    assert env.db.one("SELECT status FROM signals WHERE id=?", (sig.signal_id,))["status"] == "new"


def test_signal_filtered_when_against_trend(env):
    prime_agent(env, uptrend(), prob=0.10)  # model says short in a clear uptrend
    env.strategy.on_close("BTC/USDT", "5m")
    assert env.ctx.signal_q.empty()
    row = env.db.one("SELECT side,status,reason FROM signals")
    assert row["side"] == SHORT and row["status"] == "filtered" and "trend" in row["reason"]


def test_no_signal_without_edge_unvalidated_model_or_when_paused(env):
    prime_agent(env, uptrend(), prob=0.52)
    env.strategy.on_close("BTC/USDT", "5m")
    assert env.ctx.signal_q.empty()  # inside the no-trade band around 0.5
    prime_agent(env, uptrend(), prob=0.9, validated=False)
    env.strategy.on_close("BTC/USDT", "5m")
    assert env.ctx.signal_q.empty()  # model failed validation
    prime_agent(env, uptrend(), prob=0.9)
    env.sup.pause()
    env.strategy.on_close("BTC/USDT", "5m")
    assert env.ctx.signal_q.empty()  # paused


def test_validation_requires_beating_majority_baseline(env):
    key = ("BTC/USDT", "5m")
    env.strategy.models[key] = OnlineLogReg()
    base = {"n_test": 300, "horizon": 3}
    env.strategy.meta[key] = {**base, "acc": 0.53, "base_rate": 0.53}
    assert not env.strategy.is_validated(key)  # as good as always guessing the majority
    env.strategy.meta[key] = {**base, "acc": 0.56, "base_rate": 0.53}
    assert env.strategy.is_validated(key)
    env.strategy.meta[key] = {**base, "acc": 0.56, "base_rate": 0.53, "n_test": 20}
    assert not env.strategy.is_validated(key)
