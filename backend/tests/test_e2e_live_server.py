"""Offline live-server e2e.

Seeds synthetic multi-TF OHLCV into the cache DB, boots a real uvicorn server
on port 8765 (temp DB, fast meetings, DataLoader network access disabled),
runs a full research → trade cycle via the REST API, and verifies leaderboard /
reports / regime / portfolio / plans / logs plus the WebSocket event sequence
(snapshot → agent_state → meeting_start …). Zero external network access.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import uvicorn
import websockets

from app.config import Settings
from app.data.loader import DataLoader
from app.db import Database
from app.main import create_app

from tests.test_orchestrator_e2e import (
    SYMBOLS,
    box_candidates,
    fixed_short_plan,
    seed_market,
)

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
WS_URL = f"ws://127.0.0.1:{PORT}/ws"
CYCLE_TIMEOUT = 120.0


@pytest.fixture
def live_settings(tmp_path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "live_e2e.db"),
        universe=SYMBOLS,
        timeframes=["1d", "4h", "15m"],
        execution_timeframe="4h",
        candidates_per_cycle=6,
        min_trades=3,
        min_trades_per_year=0.0,  # activity filter off for the pipeline test
        max_mdd=0.95,
        _env_file=None,
    )


@pytest.fixture
async def live_server(live_settings, monkeypatch):
    """Real uvicorn server on PORT with a seeded temp DB and 0.2s meetings."""
    # Fully offline: no data source ever answers, cache is the only source.
    monkeypatch.setattr(
        DataLoader,
        "_fetch",
        lambda self, symbol, timeframe, start_ms=None, limit=1500: None,
    )
    # 결정론 e2e: 후보군을 box_range 샘플러로, trade 플랜을 고정 숏 플랜으로
    # (게이트·래더·브로커 경로는 전부 실코드 — test_orchestrator_e2e와 동일).
    monkeypatch.setattr("app.agents.strategist.random_candidates", box_candidates)
    from app.agents.trader import Trader

    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_short_plan))

    seed_db = Database(live_settings.db_path)
    seed_market(seed_db)
    seed_db.close()

    app = create_app(settings=live_settings, meeting_seconds=0.2)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "uvicorn failed to start on port %d" % PORT
    try:
        yield app
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)


async def _collect_ws_events(collected: list[dict], stop: asyncio.Event) -> None:
    async with websockets.connect(WS_URL) as ws:
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            collected.append(json.loads(raw))


async def test_live_server_full_cycle(live_server):
    ws_events: list[dict] = []
    ws_stop = asyncio.Event()

    async with httpx.AsyncClient(base_url=BASE, timeout=10.0) as client:
        # Idle before start.
        status = (await client.get("/api/status")).json()
        assert status["trading_mode"] == "paper"
        assert status["cycle"] is None
        assert len(status["agents"]) == 7

        # Connect /ws before the cycle so we capture the choreography.
        ws_task = asyncio.create_task(_collect_ws_events(ws_events, ws_stop))
        await asyncio.sleep(0.2)  # let the snapshot arrive

        resp = await client.post("/api/cycle/start")
        assert resp.status_code == 200
        cycle_id = resp.json()["cycle_id"]
        assert cycle_id >= 1

        # Duplicate start while running → 409.
        dup = await client.post("/api/cycle/start")
        assert dup.status_code == 409

        # Mode switch while a cycle runs → 409 (flat-and-idle 게이트).
        mode = await client.post(
            "/api/trading-mode", json={"mode": "live", "confirm": "LIVE"}
        )
        assert mode.status_code == 409
        assert "사이클 실행 중" in mode.json()["detail"]

        # Poll /api/status until the cycle completes.
        async def _wait_done() -> None:
            while True:
                s = (await client.get("/api/status")).json()
                if s["cycle"] is None:
                    return
                await asyncio.sleep(0.25)

        await asyncio.wait_for(_wait_done(), timeout=CYCLE_TIMEOUT)

        # Cycle really finished as 'done'.
        db = live_server.state.db
        cycle = db.execute("SELECT status FROM cycles WHERE id = ?", (cycle_id,))[0]
        assert cycle["status"] == "done"

        # Leaderboard non-empty with the spec §7 shape.
        board = (await client.get("/api/leaderboard", params={"limit": 20})).json()
        assert board, "leaderboard must not be empty after a cycle"
        assert set(board[0]) == {
            "strategy_id", "template", "params", "avg_metrics",
            "low_confidence", "low_activity", "status",
        }

        # At least one report exists and is readable markdown.
        reports = (await client.get("/api/reports")).json()
        assert reports, "at least one report must exist"
        report = (await client.get(f"/api/reports/{reports[0]['id']}")).json()
        assert "markdown" in report and report["markdown"].strip()

        # Regime cache was populated by the research cycle (엔지니어드 → short).
        regime = (await client.get("/api/regime")).json()
        assert regime["regime"] == "short"
        assert regime["date"]

        # Research places no orders; a trade cycle exercises the trader.
        assert db.execute("SELECT * FROM paper_orders") == []
        trade = await client.post("/api/cycle/start", json={"kind": "trade"})
        assert trade.status_code == 200
        assert trade.json()["kind"] == "trade"

        async def _wait_trade_done() -> None:
            while True:
                s = (await client.get("/api/status")).json()
                if s["cycle"] is None:
                    return
                await asyncio.sleep(0.25)

        await asyncio.wait_for(_wait_trade_done(), timeout=CYCLE_TIMEOUT)

        ws_stop.set()
        await asyncio.wait_for(ws_task, timeout=10)

        # 50/25/25 passive ladders were placed (2 symbols × 3 legs, resting).
        orders = db.execute("SELECT * FROM paper_orders ORDER BY id")
        assert len(orders) == 6
        assert all(o["side"] == "sell" for o in orders)  # 숏 레짐 → 숏 래더

        # Plans persisted as approved and served with ladder detail.
        plans = db.execute("SELECT id FROM trade_plans ORDER BY id")
        assert len(plans) == 2
        plan = (await client.get(f"/api/plans/{plans[0]['id']}")).json()
        assert plan["status"] in ("approved", "active")
        assert len(plan["entries"]) == 3
        assert plan["filled_fraction"] == 0.0

        # Portfolio: futures wallet summary + snapshot history (스펙 §7).
        portfolio = (await client.get("/api/portfolio")).json()
        assert set(portfolio) == {
            "wallet_balance", "available", "margin_used", "unrealized_pnl",
            "funding_cum", "withdrawn_cum", "positions", "snapshots",
        }
        assert portfolio["wallet_balance"] == pytest.approx(10_000.0)
        assert portfolio["snapshots"], "trade cycle must snapshot the portfolio"
        # Resting limit orders → no fills yet → no open positions.
        assert portfolio["positions"] == []
        assert (await client.get("/api/positions")).json() == []

        logs = (await client.get("/api/logs", params={"limit": 200})).json()
        assert logs, "GET /api/logs must be non-empty"
        trader_logs = [l for l in logs if l["agent"] == "trader"]
        assert trader_logs, "trade cycle must leave trader logs"

    # WS sequence: snapshot first (positions/margin/regime 포함), then
    # agent_state and meeting_start events.
    assert ws_events, "WS produced no events"
    snap = ws_events[0]
    assert snap["type"] == "snapshot"
    assert {a["id"] for a in snap["agents"]} == {
        "pm", "data", "strategist", "quant", "risk", "analyst", "trader",
    }
    assert "positions" in snap and "margin" in snap and "regime" in snap
    types = [e["type"] for e in ws_events]
    assert "agent_state" in types, "expected at least one agent_state on /ws"
    assert "meeting_start" in types, "expected at least one meeting_start on /ws"
    # snapshot arrives before any cycle choreography.
    assert types.index("agent_state") > 0
