"""REST + WebSocket contract tests (spec §7) over the assembled app,
fully offline (synthetic multi-TF OHLCV seeded into the cache).

Shapes must match frontend/src/lib/types.ts exactly — the frontend treats the
backend as the source of truth. 신규 라우트: trading-mode 전환(핫스왑),
positions(청산가·마진비율·펀딩 카운트다운), regime, econ-events, plans."""
from __future__ import annotations

import datetime as dt
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.broker.paper import FUNDING_INTERVAL_MS, PaperBroker
from app.config import Settings
from app.data.loader import DataLoader
from app.db import Database
from app.main import create_app
from app.strategies.base import build_plan

from tests.test_orchestrator_e2e import (
    box_candidates,
    fixed_short_plan,
    seed_market,
)

SYMBOLS = ["BTCUSDT", "ETHUSDT"]


def make_test_plan(symbol: str = "BTCUSDT"):
    """결정론 숏 플랜 (실제 build_plan — 50/25/25 래더 + RR 게이트 통과 형태)."""
    return build_plan(
        symbol=symbol,
        side="short",
        mark=100.0,
        stop=106.0,
        evidence=["4h 저항 구간 확인", "상대강도 하위 숏 후보"],
        leverage=4,
        tp_r1=3.2,
        tp_r2=5.0,
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Fully offline: no data source ever answers, cache is the only source.
    monkeypatch.setattr(
        DataLoader,
        "_fetch",
        lambda self, symbol, timeframe, start_ms=None, limit=1500: None,
    )
    # 결정론 e2e: 후보군을 box_range 샘플러로 고정 (합성 랜덤워크에서 나머지
    # 템플릿은 구조적으로 관망이라 사이클 결정성을 해친다).
    monkeypatch.setattr("app.agents.strategist.random_candidates", box_candidates)

    db_path = str(tmp_path / "api.db")
    seed_db = Database(db_path)
    seed_market(seed_db)
    seed_db.close()
    settings = Settings(
        db_path=db_path,
        universe=SYMBOLS,
        timeframes=["1d", "4h", "15m"],
        execution_timeframe="4h",
        candidates_per_cycle=6,
        min_trades=3,
        min_trades_per_year=0.0,  # activity filter off for the contract test
        max_mdd=0.95,
        _env_file=None,
    )
    app = create_app(settings, meeting_seconds=0.01)
    with TestClient(app) as c:
        yield c


def run_cycle_to_completion(
    client: TestClient, kind: str | None = None, timeout: float = 60.0
) -> int:
    body = {"kind": kind} if kind else None
    resp = client.post("/api/cycle/start", json=body)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["kind"] == (kind or "research")
    cycle_id = payload["cycle_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get("/api/status").json()
        if status["cycle"] is None:
            return cycle_id
        time.sleep(0.05)
    raise AssertionError("cycle did not finish in time")


def test_status_shape(client):
    status = client.get("/api/status").json()
    assert status["trading_mode"] == "paper"
    assert status["cycle"] is None
    agents = status["agents"]
    assert [a["id"] for a in agents] == [
        "pm", "data", "strategist", "quant", "risk", "analyst", "trader",
    ]
    assert [a["name"] for a in agents] == [
        "준", "다온", "세라", "민", "로건", "하나", "태오",
    ]
    for a in agents:
        assert set(a) == {"id", "name", "role", "state", "detail"}
        assert a["state"] == "idle"


def test_config_get_and_put(client):
    cfg = client.get("/api/config").json()
    assert cfg["trading_mode"] == "paper"
    assert cfg["universe"] == SYMBOLS
    assert cfg["timeframes"] == ["1d", "4h", "15m"]
    assert cfg["execution_timeframe"] == "4h"
    assert cfg["candidates_per_cycle"] == 6
    assert cfg["auto_trade_after_research"] is False
    assert cfg["bar_close_trade_enabled"] is False
    assert cfg["max_concurrent_positions"] == 3
    assert cfg["daily_max_loss_pct"] == 0.05
    assert cfg["blackout_hours"] == 12.0

    updated = client.put(
        "/api/config",
        json={
            "candidates_per_cycle": 12,
            "min_trades": 5,
            "auto_trade_after_research": True,
            "bar_close_trade_enabled": True,
            "max_concurrent_positions": 2,
        },
    ).json()
    assert updated["candidates_per_cycle"] == 12
    assert updated["min_trades"] == 5
    assert updated["auto_trade_after_research"] is True
    assert updated["bar_close_trade_enabled"] is True
    assert updated["max_concurrent_positions"] == 2
    assert updated["trading_mode"] == "paper"  # read-only, untouched
    reread = client.get("/api/config").json()
    assert reread["candidates_per_cycle"] == 12
    assert reread["bar_close_trade_enabled"] is True


def test_config_put_cannot_change_trading_mode(client):
    """trading_mode는 config PUT으로 못 바꾼다 — POST /api/trading-mode 전용."""
    updated = client.put(
        "/api/config", json={"trading_mode": "live", "min_trades": 4}
    ).json()
    assert updated["trading_mode"] == "paper"
    assert updated["min_trades"] == 4
    assert client.get("/api/status").json()["trading_mode"] == "paper"


def test_duplicate_cycle_start_returns_409(client):
    assert client.post("/api/cycle/start").status_code == 200
    assert client.post("/api/cycle/start").status_code == 409
    client.post("/api/cycle/stop")


def test_invalid_cycle_kind_returns_400(client):
    assert client.post("/api/cycle/start", json={"kind": "bogus"}).status_code == 400


def test_full_cycle_rest_contract(client, monkeypatch):
    from app.agents.trader import Trader

    cycle_id = run_cycle_to_completion(client)

    # Leaderboard (스펙 §7 shape — 새 지표 계약).
    board = client.get("/api/leaderboard?limit=20").json()
    assert board
    row = board[0]
    assert set(row) == {
        "strategy_id", "template", "params", "avg_metrics", "low_confidence", "low_activity", "status",
    }
    assert set(row["avg_metrics"]) >= {
        "win_rate", "sharpe", "mdd", "cagr", "profit_factor", "total_return",
        "funding_paid", "fee_paid", "trade_count", "liquidation_count",
        "trades_per_year",
    }
    assert client.get("/api/leaderboard?limit=1").json() == board[:1]

    # Strategy detail + per-symbol backtests.
    detail = client.get(f"/api/strategies/{row['strategy_id']}").json()
    assert detail["strategy_id"] == row["strategy_id"]
    assert detail["template"] == row["template"]
    assert len(detail["backtests"]) == len(SYMBOLS)
    assert {b["symbol"] for b in detail["backtests"]} == set(SYMBOLS)

    # Backtest detail: metrics + equity curve + USDT perp trades (exact DB shape).
    bt = client.get(f"/api/backtests/{detail['backtests'][0]['id']}").json()
    assert set(bt) >= {"id", "strategy_id", "symbol", "timeframe", "metrics", "equity_curve", "trades"}
    assert bt["symbol"] in SYMBOLS
    assert bt["equity_curve"], "downsampled equity curve must not be empty"
    assert isinstance(bt["equity_curve"][0], list) and len(bt["equity_curve"][0]) == 2
    assert bt["trades"], "box_range 챔피언은 거래가 있어야 한다"
    assert set(bt["trades"][0]) == {
        "entry_ts", "exit_ts", "entry_price", "exit_price", "net_ret",
        "holding_hours", "side", "leverage", "timeframe", "funding_paid",
        "fee_paid",
    }
    assert bt["trades"][0]["side"] in ("long", "short")
    assert bt["trades"][0]["leverage"] >= 3

    # Reports (rows carry a kind; research → 'research').
    reports = client.get("/api/reports").json()
    assert reports and reports[0]["cycle_id"] == cycle_id
    assert reports[0]["kind"] == "research"
    report = client.get(f"/api/reports/{reports[0]['id']}").json()
    assert f"전략 발굴 리포트 — 사이클 #{cycle_id}" in report["markdown"]
    assert report["kind"] == "research"

    # Logs: shape, agent filter, before_id pagination.
    logs = client.get("/api/logs?limit=10").json()
    assert logs
    assert set(logs[0]) == {"id", "ts", "agent", "level", "event_type", "message", "data"}
    quant_logs = client.get("/api/logs?agent=quant").json()
    assert quant_logs and all(entry["agent"] == "quant" for entry in quant_logs)
    page2 = client.get(f"/api/logs?limit=10&before_id={logs[-1]['id']}").json()
    assert all(entry["id"] < logs[-1]["id"] for entry in page2)

    # Regime: 엔지니어드 일봉 → 'short' (research가 market_regime 캐시를 채운다).
    regime = client.get("/api/regime").json()
    assert set(regime) == {"date", "regime", "alt_index", "dom_proxy"}
    assert regime["regime"] == "short"
    assert regime["date"]

    # A trade cycle exercises the trader: 50/25/25 passive ladders + snapshot.
    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_short_plan))
    run_cycle_to_completion(client, kind="trade")

    # Portfolio: futures wallet summary + snapshots (frontend PortfolioResponse).
    pf = client.get("/api/portfolio").json()
    assert set(pf) == {
        "wallet_balance", "available", "margin_used", "unrealized_pnl",
        "funding_cum", "withdrawn_cum", "realized_pnl_cum", "closed_trades",
        "win_trades", "positions", "snapshots",
    }
    assert pf["wallet_balance"] == pytest.approx(10_000.0)
    assert pf["withdrawn_cum"] == 0.0  # 아직 스윕 전
    assert pf["snapshots"], "trade cycle must snapshot the portfolio"
    assert set(pf["snapshots"][0]) == {
        "ts", "wallet_balance", "available", "margin_used", "unrealized_pnl",
        "funding_cum", "total_value",
    }
    # 패시브 지정가 레스팅 — 체결 전이므로 포지션은 없다.
    assert pf["positions"] == []
    assert client.get("/api/positions").json() == []

    # Plans: the trade cycle persisted approved ladders (one per symbol).
    db = client.app.state.db
    plan_rows = db.execute("SELECT id FROM trade_plans ORDER BY id")
    assert len(plan_rows) == len(SYMBOLS)
    plan = client.get(f"/api/plans/{plan_rows[0]['id']}").json()
    assert plan["side"] == "short"
    assert plan["status"] == "approved"
    assert len(plan["entries"]) == 3
    assert [leg["fraction"] for leg in plan["entries"]] == [0.5, 0.25, 0.25]
    assert plan["stop"]["kind"] == "stop"
    assert len(plan["tps"]) >= 2
    assert len(plan["evidence"]) >= 2
    assert plan["filled_fraction"] == 0.0

    # 대기 주문 탭: GET /api/plans — 오픈 플랜 목록 + 자식 주문 (레그별).
    open_plans_payload = client.get("/api/plans").json()
    assert len(open_plans_payload) == len(SYMBOLS)
    first = open_plans_payload[0]
    assert first["status"] in ("approved", "active")
    assert len(first["orders"]) == 3  # 진입 래더 3레그 (TP는 체결 후 발주)
    assert set(first["orders"][0]) == {
        "id", "side", "qty", "limit_price", "status", "leg_kind",
        "leg_index", "filled_qty", "reduce_only", "ts",
    }
    assert all(o["status"] == "open" for o in first["orders"])

    # 404s.
    assert client.get("/api/strategies/999999").status_code == 404
    assert client.get("/api/backtests/999999").status_code == 404
    assert client.get("/api/reports/999999").status_code == 404
    assert client.get("/api/plans/999999").status_code == 404


def test_champions_endpoint(client, monkeypatch):
    from app.agents.trader import Trader

    # Before any cycle: no champion, empty history.
    empty = client.get("/api/champions").json()
    assert empty == {"current": None, "history": []}

    run_cycle_to_completion(client)
    data = client.get("/api/champions").json()
    assert set(data) == {"current", "history"}

    cur = data["current"]
    assert cur is not None
    assert set(cur) == {
        "strategy_id", "template", "params", "avg_metrics", "low_confidence",
        "low_activity", "status", "crowned_at", "stop_pct", "take_profit_pct",
        "active_plan", "backtests",
    }
    assert cur["status"] == "champion"
    assert cur["crowned_at"] is not None
    assert {b["symbol"] for b in cur["backtests"]} == set(SYMBOLS)
    for b in cur["backtests"]:
        assert set(b) == {"id", "symbol", "metrics"}
    # 오픈 플랜이 없으면 stop/tp 거리와 active_plan 은 null.
    assert cur["active_plan"] is None
    assert cur["stop_pct"] is None
    assert cur["take_profit_pct"] is None
    # Only one reign so far → history (demoted reigns) is empty.
    assert data["history"] == []

    # After a trade cycle the champion carries its active plan + stop/TP 거리.
    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_short_plan))
    run_cycle_to_completion(client, kind="trade")
    cur = client.get("/api/champions").json()["current"]
    assert cur["active_plan"] is not None
    assert cur["active_plan"]["status"] in ("approved", "active")
    assert cur["active_plan"]["filled_fraction"] == 0.0
    assert cur["stop_pct"] is not None and cur["stop_pct"] > 0
    # take_profit = stop 거리 × 플랜 RR (알트/메이저 RR 게이트 통과분).
    assert cur["take_profit_pct"] > cur["stop_pct"]


# -- positions --------------------------------------------------------------------
def test_positions_endpoint_shape(client):
    db = client.app.state.db
    db.execute(
        "INSERT INTO paper_positions "
        "(symbol, side, qty, avg_entry, leverage, isolated_margin, liq_price) "
        "VALUES ('BTCUSDT', 'long', 0.5, 100.0, 4, 12.5, 76.0)"
    )
    positions = client.get("/api/positions").json()
    assert len(positions) == 1
    pos = positions[0]
    assert set(pos) == {
        "symbol", "side", "qty", "avg_entry", "leverage", "isolated_margin",
        "liq_price", "mark_price", "unrealized_pnl", "margin_ratio",
        "funding_paid", "next_funding_ts", "tp_lines", "stop_price",
    }
    assert pos["symbol"] == "BTCUSDT"
    assert pos["side"] == "long"
    assert pos["liq_price"] == 76.0
    assert pos["mark_price"] is not None  # 캐시 종가 마크
    # 마진비율 = 유지마진 / (격리마진 + 미실현) — 0 근처의 양수.
    assert pos["margin_ratio"] is not None and 0 < pos["margin_ratio"] < 1
    assert pos["funding_paid"] == 0.0
    # 부호 규약: 원장 payment(+ = 수취 현금흐름) → API funding_paid(+ = 지불 비용).
    db.execute(
        "INSERT INTO funding_payments (symbol, side, rate, payment) "
        "VALUES ('BTCUSDT', 'long', -0.0001, 2.0)"  # 수취 +2.0
    )
    pos = client.get("/api/positions").json()[0]
    assert pos["funding_paid"] == -2.0  # 지불 관점 −2.0 (= 2.0 수익)
    # 펀딩 카운트다운: 다음 8h UTC 정산 경계, 미래 시각.
    nxt = dt.datetime.fromisoformat(pos["next_funding_ts"])
    assert nxt > dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    assert (int(nxt.replace(tzinfo=dt.timezone.utc).timestamp() * 1000)
            % FUNDING_INTERVAL_MS == 0)


# -- regime -----------------------------------------------------------------------
def test_regime_endpoint_empty_defaults_to_cash(client):
    regime = client.get("/api/regime").json()
    assert regime == {
        "date": None, "regime": "cash", "alt_index": None, "dom_proxy": None,
    }


def test_regime_endpoint_returns_latest_row(client):
    db = client.app.state.db
    for date, alt, dom, regime in [
        ("2026-07-12", 1.00, 1.00, "cash"),
        ("2026-07-13", 1.02, 0.98, "long_alt"),
    ]:
        db.execute(
            "INSERT OR REPLACE INTO market_regime "
            "(date, alt_index, dom_proxy, regime) VALUES (?, ?, ?, ?)",
            (date, alt, dom, regime),
        )
    latest = client.get("/api/regime").json()
    assert latest["date"] == "2026-07-13"
    assert latest["regime"] == "long_alt"
    assert latest["alt_index"] == pytest.approx(1.02)
    assert latest["dom_proxy"] == pytest.approx(0.98)


# -- econ events --------------------------------------------------------------------
def test_econ_events_get_put_roundtrip(client):
    assert client.get("/api/econ-events").json() == []

    events = [
        {"ts": "2026-07-15T12:30:00", "name": "CPI"},
        {"ts": "2026-07-20T18:00:00", "name": "FOMC"},
    ]
    put = client.put("/api/econ-events", json=events)
    assert put.status_code == 200
    rows = put.json()
    assert [r["name"] for r in rows] == ["CPI", "FOMC"]
    assert all(set(r) == {"id", "ts", "name"} for r in rows)
    assert client.get("/api/econ-events").json() == rows

    # PUT은 전체 교체 (블랙아웃 소스는 항상 단일 진실).
    replaced = client.put(
        "/api/econ-events", json=[{"ts": "2026-08-01T00:00:00", "name": "NFP"}]
    ).json()
    assert [r["name"] for r in replaced] == ["NFP"]
    assert len(client.get("/api/econ-events").json()) == 1


def test_econ_events_put_rejects_bad_timestamp(client):
    bad = client.put(
        "/api/econ-events", json=[{"ts": "not-a-date", "name": "X"}]
    )
    assert bad.status_code == 400
    assert "이벤트 시각" in bad.json()["detail"]
    assert client.get("/api/econ-events").json() == []  # nothing was written


# -- plans --------------------------------------------------------------------------
def test_plan_endpoint_serves_ladder_detail(client):
    assert client.get("/api/plans/1").status_code == 404
    plan = make_test_plan()
    assert plan is not None
    db = client.app.state.db
    rows = db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status, filled_fraction) "
        "VALUES (?, ?, ?, 'active', 0.5)",
        (plan.symbol, plan.side, plan.to_json()),
    )
    plan_id = int(rows[0]["id"])

    payload = client.get(f"/api/plans/{plan_id}").json()
    assert payload["id"] == plan_id
    assert payload["symbol"] == "BTCUSDT"
    assert payload["side"] == "short"
    assert payload["status"] == "active"
    assert payload["filled_fraction"] == 0.5
    assert [leg["fraction"] for leg in payload["entries"]] == [0.5, 0.25, 0.25]
    assert payload["stop"] == {"kind": "stop", "price": plan.stop.price, "fraction": 1.0}
    assert len(payload["tps"]) == 2
    assert len(payload["evidence"]) >= 2
    assert payload["leverage"] == plan.leverage


# -- trading mode --------------------------------------------------------------------
class FakeLiveBroker:
    """핫스왑 테스트용 가짜 BinanceBroker — 키 검증/리컨실 성공 경로."""

    def __init__(self, settings, db=None, **kwargs):
        self.settings = settings
        self.db = db
        self.reconciled = False
        self.closed = False

    async def reconcile(self) -> dict:
        self.reconciled = True
        return {"open_orders": [], "positions": []}

    async def get_open_orders(self, symbol: str | None = None) -> list:
        return []

    async def get_positions(self) -> list:
        return []

    async def aclose(self) -> None:
        self.closed = True


def test_trading_mode_same_mode_is_noop(client):
    resp = client.post("/api/trading-mode", json={"mode": "paper"})
    assert resp.status_code == 200
    assert resp.json() == {"trading_mode": "paper"}


def test_trading_mode_live_requires_confirm(client):
    resp = client.post("/api/trading-mode", json={"mode": "live"})
    assert resp.status_code == 400
    assert "confirm" in resp.json()["detail"]
    assert client.get("/api/status").json()["trading_mode"] == "paper"


def test_trading_mode_live_without_keys_returns_400(client):
    resp = client.post("/api/trading-mode", json={"mode": "live", "confirm": "LIVE"})
    assert resp.status_code == 400
    assert "키" in resp.json()["detail"]  # Binance API 키 미설정
    assert client.get("/api/status").json()["trading_mode"] == "paper"


def test_trading_mode_409_while_cycle_running(client):
    assert client.post("/api/cycle/start").status_code == 200
    resp = client.post("/api/trading-mode", json={"mode": "live", "confirm": "LIVE"})
    assert resp.status_code == 409
    assert "사이클 실행 중" in resp.json()["detail"]
    client.post("/api/cycle/stop")


def test_trading_mode_409_with_open_orders(client):
    db = client.app.state.db
    plan = make_test_plan()
    rows = db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status) "
        "VALUES ('BTCUSDT', 'short', ?, 'approved')",
        (plan.to_json(),),
    )
    db.execute(
        "INSERT INTO paper_orders "
        "(symbol, side, qty, limit_price, plan_id, status) "
        "VALUES ('BTCUSDT', 'sell', 0.01, 101.0, ?, 'open')",
        (rows[0]["id"],),
    )
    resp = client.post("/api/trading-mode", json={"mode": "live", "confirm": "LIVE"})
    assert resp.status_code == 409
    assert "오픈 주문/포지션 존재" in resp.json()["detail"]
    assert client.get("/api/status").json()["trading_mode"] == "paper"


def test_trading_mode_409_with_open_position(client):
    db = client.app.state.db
    db.execute(
        "INSERT INTO paper_positions "
        "(symbol, side, qty, avg_entry, leverage, isolated_margin, liq_price) "
        "VALUES ('ETHUSDT', 'long', 1.0, 100.0, 4, 25.0, 76.0)"
    )
    resp = client.post("/api/trading-mode", json={"mode": "live", "confirm": "LIVE"})
    assert resp.status_code == 409
    assert "오픈 주문/포지션 존재" in resp.json()["detail"]


def test_trading_mode_hot_swap_live_and_back(client, monkeypatch):
    """live 전환 = 키 검증+리컨실 성공 후 app.state.broker 핫스왑 —
    broker_provider()가 즉시 새 브로커를 돌려준다 (스펙 §5)."""
    import app.broker.binance as binance_mod

    monkeypatch.setattr(binance_mod, "BinanceBroker", FakeLiveBroker)
    state = client.app.state
    old_paper = state.broker
    assert isinstance(old_paper, PaperBroker)

    resp = client.post("/api/trading-mode", json={"mode": "live", "confirm": "LIVE"})
    assert resp.status_code == 200
    assert resp.json() == {"trading_mode": "live"}
    assert isinstance(state.broker, FakeLiveBroker)
    assert state.broker.reconciled  # 리컨실 성공 후에만 활성화
    assert state.get_broker() is state.broker  # 핫스왑 즉시 반영
    assert client.get("/api/status").json()["trading_mode"] == "live"
    assert client.get("/api/config").json()["trading_mode"] == "live"

    # Back to paper: the live client is closed, a fresh PaperBroker serves.
    live_broker = state.broker
    back = client.post("/api/trading-mode", json={"mode": "paper"})
    assert back.status_code == 200
    assert back.json() == {"trading_mode": "paper"}
    assert isinstance(state.broker, PaperBroker)
    assert live_broker.closed
    assert client.get("/api/status").json()["trading_mode"] == "paper"


def test_trading_mode_reconcile_failure_keeps_paper(client, monkeypatch):
    """리컨실 실패 시 400 — 브로커는 교체되지 않고 paper 유지."""
    import app.broker.binance as binance_mod

    class FailingBroker(FakeLiveBroker):
        async def reconcile(self) -> dict:
            raise RuntimeError("invalid api key")

    monkeypatch.setattr(binance_mod, "BinanceBroker", FailingBroker)
    resp = client.post("/api/trading-mode", json={"mode": "live", "confirm": "LIVE"})
    assert resp.status_code == 400
    assert "live 전환 실패" in resp.json()["detail"]
    assert isinstance(client.app.state.broker, PaperBroker)
    assert client.get("/api/status").json()["trading_mode"] == "paper"


# -- websocket ----------------------------------------------------------------------
def test_ws_snapshot_then_events(client):
    with client.websocket_connect("/ws") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot"
        assert {a["id"] for a in snap["agents"]} == {
            "pm", "data", "strategist", "quant", "risk", "analyst", "trader",
        }
        for a in snap["agents"]:
            assert set(a) == {"id", "state", "detail"}
        assert snap["cycle"] is None
        assert snap["meeting"] is None
        # 스냅샷에 포지션·마진·레짐 포함 (스펙 §7).
        assert snap["positions"] == []
        assert snap["margin"] is None  # no portfolio snapshot yet
        assert snap["regime"] == "cash"  # no regime history yet

        assert client.post("/api/cycle/start").status_code == 200

        seen: set[str] = set()
        meeting_payload = None
        for _ in range(300):
            msg = ws.receive_json()
            seen.add(msg["type"])
            if msg["type"] == "meeting_start" and meeting_payload is None:
                meeting_payload = msg
            if {"agent_state", "meeting_start", "meeting_end", "log",
                    "cycle_progress"} <= seen:
                break
        assert {"agent_state", "meeting_start", "meeting_end", "log",
                "cycle_progress"} <= seen
        assert meeting_payload is not None
        assert set(meeting_payload) == {"type", "meeting_id", "agents", "topic"}
        assert meeting_payload["agents"] == ["pm", "strategist"]


# -- trade history (손절/익절 실현 내역) --------------------------------------------
def test_trade_history_rollup(client):
    db = client.app.state.db
    plan_json = json.dumps({"leverage": 5})
    # 익절로 종결된 숏 플랜: 진입 2레그(avg 100), 익절 2레그(avg 94).
    pid = db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status, filled_fraction) "
        "VALUES ('SOLUSDT', 'short', ?, 'closed', 1.0)",
        (plan_json,),
    )[0]["id"]
    db.executemany(
        "INSERT INTO paper_orders (ts, symbol, side, qty, limit_price, filled_qty, "
        "avg_fill_price, reduce_only, status, plan_id) VALUES (?, 'SOLUSDT', ?, ?, ?, ?, ?, ?, 'filled', ?)",
        [
            ("2026-07-20T01:00:00+00:00", "sell", 6.0, 101.0, 6.0, 101.0, 0, pid),
            ("2026-07-20T01:05:00+00:00", "sell", 6.0, 99.0, 6.0, 99.0, 0, pid),
            ("2026-07-20T09:00:00+00:00", "buy", 6.0, 95.0, 6.0, 95.0, 1, pid),
            ("2026-07-20T10:00:00+00:00", "buy", 6.0, 93.0, 6.0, 93.0, 1, pid),
        ],
    )
    # 보유 구간 펀딩 수취 +1.2 (payment 현금흐름 +) — 비용 관점 -1.2.
    db.execute(
        "INSERT INTO funding_payments (ts, symbol, side, rate, payment) "
        "VALUES ('2026-07-20T08:00:00+00:00', 'SOLUSDT', 'short', -0.0001, 1.2)"
    )
    rows = client.get("/api/trade-history").json()
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "SOLUSDT" and r["side"] == "short"
    assert r["exit_reason"] == "익절"
    assert r["avg_entry"] == pytest.approx(100.0)
    assert r["avg_exit"] == pytest.approx(94.0)
    # 숏: (100-94)×12 = +72, 펀딩 수취 +1.2 → 73.2
    assert r["pnl_usdt"] == pytest.approx(73.2)
    assert r["funding_paid"] == pytest.approx(-1.2)
    assert r["ret_on_margin"] == pytest.approx(73.2 / (100.0 * 12 / 5))
    # 손절(stopped) 라벨.
    pid2 = db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status, filled_fraction, reject_reason) "
        "VALUES ('XRPUSDT', 'short', ?, 'stopped', 0.5, '')",
        (plan_json,),
    )[0]["id"]
    db.executemany(
        "INSERT INTO paper_orders (ts, symbol, side, qty, limit_price, filled_qty, "
        "avg_fill_price, reduce_only, status, plan_id) VALUES (?, 'XRPUSDT', ?, ?, ?, ?, ?, ?, 'filled', ?)",
        [
            ("2026-07-20T02:00:00+00:00", "sell", 100.0, 1.10, 100.0, 1.10, 0, pid2),
            ("2026-07-20T06:00:00+00:00", "buy", 100.0, 1.13, 100.0, 1.13, 1, pid2),
        ],
    )
    rows = client.get("/api/trade-history").json()
    stop_row = next(r for r in rows if r["symbol"] == "XRPUSDT")
    assert stop_row["exit_reason"] == "손절"
    assert stop_row["pnl_usdt"] == pytest.approx(-3.0)  # (1.10-1.13)×100

    # 포트폴리오 누적 실현 손익 = 익절(+73.2) + 손절(-3.0) = +70.2, 2거래 1승.
    pf = client.get("/api/portfolio").json()
    assert pf["realized_pnl_cum"] == pytest.approx(70.2)
    assert pf["closed_trades"] == 2
    assert pf["win_trades"] == 1


def test_trade_history_liquidation_bounded_by_next_plan(client):
    """finding #3/#13: 재진입 심볼에서 plan_id-NULL 강제 청산 행은 다음 플랜
    생성 시각을 상한으로 귀속된다 — 나중 플랜의 청산을 옛 플랜에 이중계상하지
    않는다."""
    db = client.app.state.db
    pj = json.dumps({"leverage": 3})
    a = db.execute(
        "INSERT INTO trade_plans (created_at, symbol, side, plan_json, status, "
        "filled_fraction, reject_reason) VALUES ('2026-07-20T00:00:00+00:00', "
        "'DOGEUSDT', 'long', ?, 'stopped', 1.0, '강제 청산')", (pj,),
    )[0]["id"]
    db.execute(
        "INSERT INTO paper_orders (ts, symbol, side, qty, limit_price, filled_qty, "
        "avg_fill_price, reduce_only, status, plan_id) VALUES "
        "('2026-07-20T00:10:00+00:00','DOGEUSDT','buy',1000,0.10,1000,0.10,0,'filled',?)",
        (a,),
    )
    db.execute(  # 청산 A (plan_id NULL) — T1
        "INSERT INTO paper_orders (ts, symbol, side, qty, limit_price, filled_qty, "
        "avg_fill_price, reduce_only, status, reason) VALUES "
        "('2026-07-20T01:00:00+00:00','DOGEUSDT','sell',1000,0.08,1000,0.08,1,'filled',"
        "'강제 청산 — 격리마진 전액 손실')"
    )
    b = db.execute(  # 재진입 플랜 B (created T2 > T1)
        "INSERT INTO trade_plans (created_at, symbol, side, plan_json, status, "
        "filled_fraction, reject_reason) VALUES ('2026-07-20T02:00:00+00:00', "
        "'DOGEUSDT', 'long', ?, 'stopped', 1.0, '강제 청산')", (pj,),
    )[0]["id"]
    db.execute(
        "INSERT INTO paper_orders (ts, symbol, side, qty, limit_price, filled_qty, "
        "avg_fill_price, reduce_only, status, plan_id) VALUES "
        "('2026-07-20T02:10:00+00:00','DOGEUSDT','buy',1000,0.09,1000,0.09,0,'filled',?)",
        (b,),
    )
    db.execute(  # 청산 B (plan_id NULL) — T3
        "INSERT INTO paper_orders (ts, symbol, side, qty, limit_price, filled_qty, "
        "avg_fill_price, reduce_only, status, reason) VALUES "
        "('2026-07-20T03:00:00+00:00','DOGEUSDT','sell',1000,0.07,1000,0.07,1,'filled',"
        "'강제 청산 — 격리마진 전액 손실')"
    )
    rows = {r["plan_id"]: r for r in client.get("/api/trade-history").json()}
    # 플랜 A는 L1(0.08)만, 플랜 B는 L2(0.07)만 — 섞이지 않는다.
    assert rows[a]["avg_exit"] == pytest.approx(0.08)
    assert rows[b]["avg_exit"] == pytest.approx(0.07)


def test_position_info_stopped_plan_keeps_funding_window(client):
    """finding #4: 포지션을 소유한 플랜이 'stopped'(스탑엑싯 체이스 중)여도 현
    포지션에 귀속된 펀딩 윈도·tp_lines·stop_price를 유지한다 — 심볼 전체 이력을
    무한 합산하지 않는다."""
    db = client.app.state.db
    old_pj = json.dumps({"leverage": 3, "stop": {"price": 80.0}, "tps": []})
    db.execute(  # 옛 종료 플랜 + 그 시기 펀딩 (현 포지션에 귀속되면 안 됨)
        "INSERT INTO trade_plans (created_at, symbol, side, plan_json, status, "
        "filled_fraction) VALUES ('2026-07-01T00:00:00+00:00','BTCUSDT','long', ?, "
        "'closed', 1.0)", (old_pj,),
    )
    db.execute(
        "INSERT INTO funding_payments (ts, symbol, side, rate, payment) "
        "VALUES ('2026-07-01T08:00:00+00:00','BTCUSDT','long',-0.0001, 5.0)"
    )
    stopped_pj = json.dumps(
        {"leverage": 3, "stop": {"price": 90.0},
         "tps": [{"price": 110.0, "fraction": 1.0}]}
    )
    db.execute(  # 현 포지션을 소유한 stopped 플랜 (최신)
        "INSERT INTO trade_plans (created_at, symbol, side, plan_json, status, "
        "filled_fraction) VALUES ('2026-07-20T00:00:00+00:00','BTCUSDT','long', ?, "
        "'stopped', 1.0)", (stopped_pj,),
    )
    db.execute(  # stopped 플랜 시기 펀딩 (윈도 안)
        "INSERT INTO funding_payments (ts, symbol, side, rate, payment) "
        "VALUES ('2026-07-20T08:00:00+00:00','BTCUSDT','long',-0.0001, 2.0)"
    )
    db.execute(
        "INSERT INTO paper_positions (symbol, side, qty, avg_entry, leverage, "
        "isolated_margin, liq_price) VALUES ('BTCUSDT','long',0.5,100.0,3,16.6,90.0)"
    )
    pos = client.get("/api/positions").json()[0]
    assert pos["funding_paid"] == pytest.approx(-2.0)  # 옛 5.0은 제외
    assert pos["stop_price"] == 90.0
    assert pos["tp_lines"] and pos["tp_lines"][0]["price"] == 110.0


def test_prune_activity_log_normalizes_t_format():
    """finding #15: activity_log.ts는 'T' 구분자로 기록되고 보존 임계는 SQLite
    공백 구분자라 바이트 비교가 어긋난다 — 정규화로 경계일 행이 하루 더 남지
    않게 정상 삭제한다."""
    db = Database(":memory:")
    try:
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now - dt.timedelta(days=2)
        boundary = (cutoff - dt.timedelta(seconds=1)).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
        fresh = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        db.execute(
            "INSERT INTO activity_log (ts, event_type, message) "
            "VALUES (?, 'log', 'boundary')", (boundary,)
        )
        db.execute(
            "INSERT INTO activity_log (ts, event_type, message) "
            "VALUES (?, 'log', 'fresh')", (fresh,)
        )
        db.prune_activity_log(days=2)
        msgs = [r["message"] for r in db.execute("SELECT message FROM activity_log")]
        assert "boundary" not in msgs  # 'T' 형식 경계 행이 정상 삭제
        assert "fresh" in msgs
    finally:
        db.close()
