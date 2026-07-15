"""목표 탐색 모드(goal-seek): goal_met criteria + the research/validate loop.

The loop e2e tests reuse the offline synthetic crypto harness (zero network)
with tiny settings (validate every research cycle, 2-cycle budget)."""
from __future__ import annotations

import random

import pytest

from app.config import Settings
from app.data.loader import DataLoader
from app.db import Database
from app.events import EventBus
from app.orchestrator import (
    GOAL_MIN_TRADES,
    GoalInProgressError,
    Orchestrator,
    goal_met,
)

from tests.test_orchestrator_e2e import SYMBOLS, box_candidates, seed_market

pytestmark = pytest.mark.usefixtures("offline_loader", "box_only")


@pytest.fixture
def offline_loader(monkeypatch):
    monkeypatch.setattr(
        DataLoader,
        "_fetch",
        lambda self, symbol, timeframe, start_ms=None, limit=1500: None,
    )


@pytest.fixture
def box_only(monkeypatch):
    monkeypatch.setattr(
        "app.agents.strategist.random_candidates", box_candidates
    )


def _goal_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        db_path=str(tmp_path / "goal.db"),
        universe=SYMBOLS,
        timeframes=["1d", "4h", "15m"],
        execution_timeframe="4h",
        candidates_per_cycle=4,
        min_trades=3,
        min_trades_per_year=0.0,
        max_mdd=0.95,
        goal_validate_every=1,
        goal_max_cycles=2,
        _env_file=None,
    )
    base.update(overrides)
    return Settings(**base)


# -- goal_met criteria ----------------------------------------------------------
def _settings(**kw) -> Settings:
    return Settings(goal_win_rate=0.65, max_mdd=0.30, _env_file=None, **kw)


def _oos(win_rate=0.7, total_return=0.2, trade_count=GOAL_MIN_TRADES, mdd=0.1) -> dict:
    return {
        "win_rate": win_rate,
        "total_return": total_return,
        "trade_count": trade_count,
        "mdd": mdd,
        "sharpe": 1.0,
        "funding_paid": 1.0,
        "liquidation_count": 0,
    }


def test_goal_met_passes_when_all_criteria_satisfied():
    assert goal_met(_oos(), _settings()) is True


@pytest.mark.parametrize(
    "oos",
    [
        None,
        {},  # missing every metric
        _oos(win_rate=0.60),  # below target win rate
        _oos(total_return=0.0),  # not profitable OOS (strict >)
        _oos(total_return=-0.05),
        _oos(trade_count=GOAL_MIN_TRADES - 1),  # too few OOS trades
        _oos(mdd=0.40),  # MDD over the 30% limit
        _oos(win_rate=None),  # champion not found → missing metric
    ],
)
def test_goal_met_fails(oos):
    assert goal_met(oos, _settings()) is False


def test_goal_met_boundaries_are_inclusive_except_return():
    s = _settings()
    # win_rate == target and mdd == limit both pass; total_return must be > 0.
    assert goal_met(_oos(win_rate=0.65, mdd=0.30), s) is True
    assert goal_met(_oos(total_return=1e-9), s) is True


# -- goal loop e2e --------------------------------------------------------------
async def test_goal_loop_stops_on_budget(tmp_path):
    """With an unreachable target the loop spends its 2-research-cycle budget
    (validating after each) and stops with an exhaustion log."""
    settings = _goal_settings(tmp_path, goal_win_rate=0.999)
    db = Database(settings.db_path)
    seed_market(db)
    bus = EventBus(db)
    orch = Orchestrator(db, bus, settings, meeting_seconds=0.0, rng=random.Random(5))
    try:
        orch.start_goal()
        assert orch.goal_running
        # Duplicate start → 409 semantics.
        with pytest.raises(GoalInProgressError):
            orch.start_goal()
        assert orch._goal_task is not None
        await orch._goal_task

        assert not orch.goal_running
        status = orch.goal_status()
        assert status["running"] is False
        assert status["cycles_done"] == 2
        assert status["target_win_rate"] == 0.999
        assert status["max_cycles"] == 2

        # research + validate ran for each budgeted cycle (2 of each), and the
        # loop chained one trade cycle (모의거래 자동 실행) after exhausting budget.
        kinds = [
            r["kind"]
            for r in db.execute("SELECT kind FROM cycles ORDER BY id")
        ]
        assert kinds.count("research") == 2
        assert kinds.count("validate") == 2
        assert kinds.count("trade") == 1
        assert kinds[-1] == "trade"  # trade runs after the loop finishes

        logs = [
            r["message"]
            for r in db.execute(
                "SELECT message FROM activity_log WHERE event_type = 'log'"
            )
        ]
        assert any("목표 탐색 시작" in m for m in logs)
        assert any("목표 탐색 종료" in m for m in logs)
        assert any("목표 미달" in m for m in logs)
        assert any("모의거래 자동 실행" in m for m in logs)
    finally:
        await orch.stop_goal()
        db.close()


async def test_goal_loop_announces_success_and_stops(tmp_path, monkeypatch):
    """When a validation meets the goal the loop announces 목표 달성 and stops
    before spending the budget. goal_met is patched to accept the first real
    OOS result (non-None), so the announce runs on genuine validate metrics."""
    settings = _goal_settings(tmp_path, goal_win_rate=0.0)
    db = Database(settings.db_path)
    seed_market(db)
    bus = EventBus(db)
    orch = Orchestrator(db, bus, settings, meeting_seconds=0.0, rng=random.Random(7))
    # Accept the first validation that actually produced OOS metrics.
    monkeypatch.setattr("app.orchestrator.goal_met", lambda oos, s: oos is not None)
    try:
        orch.start_goal()
        await orch._goal_task

        assert not orch.goal_running
        status = orch.goal_status()
        # Stopped after the first validate (research_done == 1 < budget 2).
        assert status["cycles_done"] == 1

        logs = [
            r["message"]
            for r in db.execute(
                "SELECT message FROM activity_log WHERE event_type = 'log'"
            )
        ]
        assert any("🎯 목표 달성" in m for m in logs)
        assert any("모의거래를 시작하세요" in m for m in logs)
        assert any("모의거래 자동 실행" in m for m in logs)
        # It stopped early: one research + one validate, then the auto trade.
        kinds = [r["kind"] for r in db.execute("SELECT kind FROM cycles ORDER BY id")]
        assert kinds.count("research") == 1
        assert kinds.count("validate") == 1
        assert kinds.count("trade") == 1
        assert kinds[-1] == "trade"
    finally:
        await orch.stop_goal()
        db.close()


async def test_goal_status_none_until_started(tmp_path):
    settings = _goal_settings(tmp_path)
    db = Database(settings.db_path)
    orch = Orchestrator(db, EventBus(db), settings, meeting_seconds=0.0)
    try:
        assert orch.goal_status() is None
        assert orch.status()["goal"] is None
    finally:
        db.close()
