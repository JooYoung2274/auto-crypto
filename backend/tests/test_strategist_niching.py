"""Per-template niching: elites keep non-champion templates in the gene pool."""
from __future__ import annotations

import json
import random

import pytest

from app.events import EventBus
from app.agents.strategist import Strategist
from app.orchestrator import Orchestrator
from app.config import Settings
from app.strategies.base import StrategySpec

pytestmark = pytest.mark.asyncio

CHAMPION_PARAMS = {
    "window": 100, "band": 0.02, "vp_window": 120, "stop_pad": 0.015,
    "tp_r1": 3.0, "tp_r2": 5.0, "leverage": 5,
}
BOX_PARAMS = {
    "pivot_k": 3, "entry_q": 0.25, "stop_buf": 0.05, "tp1_frac": 0.35,
    "leverage": 4,
}


async def test_propose_mutates_template_elites(db):
    strategist = Strategist(EventBus(db))
    champion = StrategySpec("vwma_support", dict(CHAMPION_PARAMS))
    elites = [
        StrategySpec("box_range", dict(BOX_PARAMS)),
        # champion's template — must be dropped
        StrategySpec("vwma_support", {**CHAMPION_PARAMS, "window": 80}),
    ]
    specs = await strategist.propose(60, champion, random.Random(7), elites=elites)
    assert len(specs) == 60
    by_template: dict[str, int] = {}
    for sp in specs:
        by_template[sp.template] = by_template.get(sp.template, 0) + 1
    # 40% champion mutations are vwma_support; 30% elite mutations all go to
    # the one non-champion-template elite (box_range); rest random.
    assert by_template.get("box_range", 0) >= 18  # 60 * 0.3 = 18 elite slots
    assert by_template.get("vwma_support", 0) >= 24  # 60 * 0.4 champion slots


async def test_propose_without_elites_keeps_legacy_split(db):
    strategist = Strategist(EventBus(db))
    champion = StrategySpec("vwma_support", dict(CHAMPION_PARAMS))
    specs = await strategist.propose(60, champion, random.Random(7), elites=[])
    assert len(specs) == 60
    assert sum(1 for sp in specs if sp.template == "vwma_support") >= 42  # 70% mutations


def test_load_template_elites_picks_best_per_template(db):
    settings = Settings(_env_file=None)
    orch = Orchestrator(db, EventBus(db), settings)

    def insert(template: str, params: dict, sharpe: float, trades: int, status: str = "rejected") -> None:
        sid = db.execute(
            "INSERT INTO strategies (template, params_json, status) VALUES (?, ?, ?)",
            (template, json.dumps(params), status),
        )[0]["id"]
        db.execute(
            "INSERT INTO backtests (strategy_id, symbol, metrics_json) VALUES (?, ?, ?)",
            (sid, "BTCUSDT", json.dumps({"sharpe": sharpe, "trade_count": trades})),
        )

    insert("box_range", dict(BOX_PARAMS), 0.9, 50)
    insert("box_range", {**BOX_PARAMS, "pivot_k": 2, "entry_q": 0.2}, 0.2, 200)
    insert("candle_breakout", {"body_mult": 2.0, "lookback": 5, "leverage": 4}, 0.1, 100)
    insert("candle_breakout", {"body_mult": 2.5, "lookback": 8, "leverage": 3}, 0.4, 5)  # < 10 trades → excluded

    elites = orch._load_template_elites()
    by_template = {e.template: e for e in elites}
    assert by_template["box_range"].params["pivot_k"] == 3  # sharpe 0.9 wins
    assert by_template["candle_breakout"].params["body_mult"] == 2.0  # the ≥10-trade one
