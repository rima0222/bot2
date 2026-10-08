import asyncio
import pathlib
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from config import SecretStore, Settings, load_static  # noqa: E402
from context import BotState, Context  # noqa: E402
from data_agent import DataAgent, SimFeed  # noqa: E402
from db import Database  # noqa: E402
from execution_agent import ExecutionAgent  # noqa: E402
from models import OrderPlan  # noqa: E402
from risk_agent import RiskAgent  # noqa: E402
from strategy_agent import StrategyAgent  # noqa: E402
from supervisor import Supervisor  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BOT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BOT_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("BOT_DATA_SOURCE", "sim")
    monkeypatch.delenv("BOT_SECRET_KEY", raising=False)
    static = load_static()
    db = Database(static.db_path)
    settings = Settings(db)
    ctx = Context(static, db, settings, SecretStore(db, static.secret_key), BotState(db))
    data = DataAgent(ctx, SimFeed(1))
    execution = ExecutionAgent(ctx)
    strategy = StrategyAgent(ctx, data)
    risk = RiskAgent(ctx, execution, data)
    sup = Supervisor(ctx, data, strategy, risk, execution)
    asyncio.run(execution.start())
    yield SimpleNamespace(static=static, db=db, settings=settings, ctx=ctx, data=data, execution=execution,
                          strategy=strategy, risk=risk, sup=sup, tmp=tmp_path)
    db.close()


def make_plan(side="long", price=100.0, sl_pct=0.01, rr=2.0, qty=10.0, lev=5, symbol="BTC/USDT") -> OrderPlan:
    d = 1 if side == "long" else -1
    notional = qty * price
    return OrderPlan(symbol=symbol, side=side, qty=qty, signal_price=price, sl=price * (1 - d * sl_pct),
                     tp=price * (1 + d * sl_pct * rr), leverage=lev, notional=notional, margin=notional / lev,
                     risk_amount=notional * sl_pct)
