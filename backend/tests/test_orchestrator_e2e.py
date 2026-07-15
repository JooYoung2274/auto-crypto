"""Orchestrator e2e: 오프라인 합성 시드로 research → trade 사이클 전체 파이프
(멀티TF 캐시 + 엔지니어드 일봉 → 레짐 'short' + box_range 후보군) — 네트워크 0.

후보군은 box_range 템플릿 샘플러로 고정한다: 나머지 템플릿은 합성 랜덤워크
데이터에서 구조적으로 관망(None)이라 파이프라인 e2e의 결정성을 해친다.
plan 생성·게이트·체결·리포트 경로는 전부 실제 코드다.
"""
from __future__ import annotations

import asyncio
import json
import random
import threading
import time

import numpy as np
import pandas as pd
import pytest

from app.config import Settings
from app.data.loader import DataLoader
from app.db import Database
from app.events import EventBus
from app.orchestrator import CycleInProgressError, Orchestrator, compute_leaderboard
from app.strategies.base import StrategySpec, build_plan, mark_price
from app.strategies.registry import TEMPLATES, _sample

from tests.conftest import make_multi_tf_frames, seed_ohlcv_cache

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
DAYS = 30  # 인트라데이 히스토리 (4h 봉 180개 — 검증 분할 요건 충족)


def box_candidates(n: int, rng: random.Random) -> list[StrategySpec]:
    """box_range 전용 후보 샘플러 (실제 파라미터 그리드에서 추출)."""
    return [
        StrategySpec(
            "box_range",
            {k: _sample(pr, rng) for k, pr in TEMPLATES["box_range"].items()},
        )
        for _ in range(n)
    ]


def make_daily(symbol: str, end: pd.Timestamp, n: int = 240) -> pd.DataFrame:
    """레짐 프록시용 엔지니어드 일봉 240개: 알트(ETH) 하락 + BTC 횡보 →
    시장↓ + 도미넌스↑ = 'short' 레짐 (스펙 §3.1)."""
    idx = pd.date_range(end=end, periods=n, freq="1D")
    t = np.arange(n)
    if symbol == "BTCUSDT":
        close = 40_000.0 * (1.0 + 0.0002 * np.sin(t / 7.0))
    else:
        close = 3_000.0 * (0.997 ** t)
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1_000.0),
            "quote_volume": close * 1_000.0,
        },
        index=idx,
    )


def seed_market(db: Database, days: int = DAYS) -> None:
    """심볼별 15m/4h 합성 캐시 + 엔지니어드 1d 캐시 시딩."""
    for i, symbol in enumerate(SYMBOLS):
        frames = make_multi_tf_frames(seed=11 + i, days=days, drift=0.0, tfs=("15m", "4h"))
        for tf in ("15m", "4h"):
            seed_ohlcv_cache(db, symbol, tf, frames[tf])
        end_day = frames["4h"].index[-1].normalize()
        seed_ohlcv_cache(db, symbol, "1d", make_daily(symbol, end_day))


def fixed_short_plan(spec, frames, regime, symbol):
    """trade 사이클용 결정론 숏 플랜 빌더 — 캐시 mark 기준 실제 build_plan
    (게이트·래더·브로커 경로는 전부 실코드)."""
    mark = mark_price(frames)
    if mark is None:
        return None
    return build_plan(
        symbol=symbol,
        side="short",
        mark=mark,
        stop=mark * 1.06,
        evidence=["4h 저항 구간 확인", "상대강도 하위 숏 후보"],
        leverage=4,
        tp_r1=3.2,
        tp_r2=5.0,
    )


@pytest.fixture
def offline_loader(monkeypatch):
    """No data source ever answers: the loader always serves from cache."""
    monkeypatch.setattr(
        DataLoader,
        "_fetch",
        lambda self, symbol, timeframe, start_ms=None, limit=1500: None,
    )


@pytest.fixture
def box_only(monkeypatch):
    """Strategist의 랜덤 탐색을 box_range 샘플러로 고정 (결정론 e2e)."""
    monkeypatch.setattr(
        "app.agents.strategist.random_candidates", box_candidates
    )


@pytest.fixture
def e2e_settings(tmp_path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "e2e.db"),
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
def seeded_db(e2e_settings) -> Database:
    database = Database(e2e_settings.db_path)
    seed_market(database)
    yield database
    database.close()


def make_orch(db, settings, seed=42, meeting_seconds=0.0) -> Orchestrator:
    return Orchestrator(
        db, EventBus(db), settings,
        meeting_seconds=meeting_seconds, rng=random.Random(seed),
    )


# -- research -------------------------------------------------------------------
async def test_research_cycle_e2e(offline_loader, box_only, e2e_settings, seeded_db):
    """Research cycle: regime 판정 + 상대강도 랭킹 + 플랜 구동 백테스트로
    챔피언을 발굴하고 리포트를 쓰되 주문은 내지 않는다."""
    db = seeded_db
    orch = make_orch(db, e2e_settings)
    events = orch.bus.subscribe()

    cycle_id = await orch.start_cycle()  # default kind == research
    assert cycle_id >= 1

    # Duplicate start while running → CycleInProgressError (single runner mutex).
    with pytest.raises(CycleInProgressError):
        await orch.start_cycle()

    status = orch.status()
    assert status["cycle"] == {
        "id": cycle_id, "status": "running", "step": "start", "kind": "research",
    }
    assert [a["id"] for a in status["agents"]] == [
        "pm", "data", "strategist", "quant", "risk", "analyst", "trader",
    ]
    assert [a["name"] for a in status["agents"]] == [
        "준", "다온", "세라", "민", "로건", "하나", "태오",
    ]

    await orch.cycle_task

    # Cycle row finished.
    cycle = db.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,))[0]
    assert cycle["status"] == "done"
    assert cycle["finished_at"] is not None
    summary = json.loads(cycle["summary_json"])
    assert summary["candidates"] == 6
    assert summary["passed"] + summary["rejected"] == 6
    assert summary["regime"] == "short"  # 엔지니어드 일봉 → 숏 레짐
    ranking = summary["symbol_ranking"]
    assert [r["symbol"] for r in ranking] == ["BTCUSDT", "ETHUSDT"]  # 상대강도
    assert ranking[1]["relative"] < 0  # ETH가 BTC보다 약하다

    # Strategies persisted, exactly one champion exists.
    strategies = db.execute(
        "SELECT status FROM strategies WHERE cycle_id = ?", (cycle_id,)
    )
    assert len(strategies) == 6
    champions = db.execute("SELECT * FROM strategies WHERE status = 'champion'")
    assert len(champions) == 1
    assert champions[0]["template"] == "box_range"

    # Backtests: 6 candidates × 2 symbols, with USDT-denominated trades.
    assert db.execute("SELECT COUNT(*) AS n FROM backtests")[0]["n"] == 12
    trades = db.execute("SELECT * FROM trades")
    assert trades
    row = trades[0]
    assert row["side"] in ("long", "short")
    assert row["timeframe"] == "4h"
    assert row["leverage"] >= 3

    # Report written: USDT 포맷 + side/레버리지/펀딩/청산 컬럼 + TF 어트리뷰션.
    reports = db.execute("SELECT * FROM reports WHERE cycle_id = ?", (cycle_id,))
    assert len(reports) == 1
    md = reports[0]["markdown"]
    assert f"전략 발굴 리포트 — 사이클 #{cycle_id}" in md
    assert "시장 레짐: short" in md
    assert "상대강도" in md
    assert "## 챔피언 거래 내역" in md
    assert "| 심볼 | 방향 | 레버리지 | TF |" in md
    assert "## 타임프레임별 성과" in md
    assert "USDT" in md and "펀딩" in md and "청산" in md

    # Research places NO orders and takes no portfolio snapshot — trading is
    # the trade cycle's job.
    assert db.execute("SELECT * FROM paper_orders") == []
    assert db.execute("SELECT COUNT(*) AS n FROM portfolio_snapshots")[0]["n"] == 0

    # Leaderboard shape (새 지표 계약).
    board = compute_leaderboard(
        db, limit=20, min_trades=e2e_settings.min_trades, settings=e2e_settings
    )
    assert board
    row = board[0]
    assert set(row) == {
        "strategy_id", "template", "params", "avg_metrics",
        "low_confidence", "low_activity", "status",
    }
    assert set(row["avg_metrics"]) >= {
        "win_rate", "sharpe", "mdd", "cagr", "profit_factor", "total_return",
        "funding_paid", "fee_paid", "trade_count", "liquidation_count",
        "trades_per_year",
    }
    assert row["status"] == "champion"

    # Event choreography: 5 meetings, agent states, progress to done.
    collected = []
    while not events.empty():
        collected.append(events.get_nowait())
    types = [e["type"] for e in collected]
    assert types.count("meeting_start") == 5
    assert types.count("meeting_end") == 5
    meetings = [e for e in collected if e["type"] == "meeting_start"]
    assert [m["agents"] for m in meetings] == [
        ["pm", "strategist"],
        ["strategist", "quant"],
        ["quant", "risk"],
        ["risk", "strategist"],
        ["analyst", "pm"],
    ]
    assert all("meeting_id" in m and "topic" in m for m in meetings)
    assert any(
        e["type"] == "agent_state" and e["state"] == "working" for e in collected
    )
    progress = [e for e in collected if e["type"] == "cycle_progress"]
    assert progress[0]["step"] == "start" and progress[-1]["step"] == "done"
    assert progress[-1]["pct"] == 100
    assert any(e["type"] == "leaderboard_update" and e["top"] for e in collected)

    # After completion: status cycle is null, all agents idle.
    status = orch.status()
    assert status["cycle"] is None
    assert all(a["state"] == "idle" for a in status["agents"])


# -- trade ----------------------------------------------------------------------
async def test_trade_cycle_e2e(
    offline_loader, box_only, e2e_settings, seeded_db, monkeypatch
):
    """Trade cycle: research가 세운 챔피언으로 심볼별 TradePlan을 만들어
    50/25/25 패시브 래더를 발주한다; 같은 플랜이 열려 있는 동안 재실행은
    중복 주문 0건. (합성 랜덤워크의 마지막 봉에서 박스 셋업이 선다는 보장이
    없으므로 플랜 빌더만 결정론 build_plan으로 고정 — 게이트·플랜 영속화·
    브로커 발주·멱등성 경로는 전부 실코드다.)"""
    from app.agents.trader import Trader

    db = seeded_db
    orch = make_orch(db, e2e_settings)

    # A research cycle establishes the champion.
    await orch.start_cycle("research")
    await orch.cycle_task
    assert len(db.execute("SELECT * FROM strategies WHERE status = 'champion'")) == 1
    assert db.execute("SELECT * FROM paper_orders") == []  # research placed none

    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_short_plan))
    events = orch.bus.subscribe()
    cycle_id = await orch.start_cycle("trade")
    with pytest.raises(CycleInProgressError):
        await orch.start_cycle("validate")
    assert orch.status()["cycle"]["kind"] == "trade"
    await orch.cycle_task

    cycle = db.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,))[0]
    assert cycle["status"] == "done"
    assert cycle["kind"] == "trade"
    summary = json.loads(cycle["summary_json"])
    assert summary["orders"] == 6  # 2 심볼 × 3레그 래더
    assert summary["regime"] == "short"

    orders = db.execute("SELECT * FROM paper_orders ORDER BY id")
    assert len(orders) == 6
    assert all(o["status"] == "open" for o in orders)  # 패시브 지정가 레스팅
    assert all(o["side"] == "sell" for o in orders)  # 숏 레짐 → 숏 래더
    for symbol in SYMBOLS:
        legs = [o for o in orders if o["symbol"] == symbol]
        assert len(legs) == 3
        notional = [o["qty"] * o["limit_price"] for o in legs]
        assert notional[0] == pytest.approx(2 * notional[1], rel=1e-2)  # 50/25/25

    plans = db.execute("SELECT * FROM trade_plans ORDER BY id")
    assert [p["status"] for p in plans] == ["approved", "approved"]
    assert sorted(p["symbol"] for p in plans) == sorted(SYMBOLS)
    assert all(p["filled_fraction"] == 0 for p in plans)

    # 포트폴리오 스냅샷 + WS snapshot에 포지션/마진/레짐 포함 (스펙 §7).
    assert db.execute("SELECT COUNT(*) AS n FROM portfolio_snapshots")[0]["n"] >= 1
    snap = orch.snapshot()
    assert set(snap) >= {"agents", "cycle", "meeting", "positions", "margin", "regime"}
    assert snap["regime"] == "short"
    assert snap["margin"] is not None and snap["margin"]["wallet_balance"] > 0

    # Meeting choreography: exactly the pm+trader handoff.
    collected = []
    while not events.empty():
        collected.append(events.get_nowait())
    meetings = [e for e in collected if e["type"] == "meeting_start"]
    assert [m["agents"] for m in meetings] == [["pm", "trader"]]
    progress = [e for e in collected if e["type"] == "cycle_progress"]
    assert all(p["kind"] == "trade" for p in progress)
    assert progress[-1]["step"] == "done"

    # Second trade run while the plans are open: no duplicates (재기동 후
    # 중복 주문 0건 불변식의 사이클 레벨 형태).
    await orch.start_cycle("trade")
    await orch.cycle_task
    assert len(db.execute("SELECT * FROM paper_orders")) == 6
    assert len(db.execute("SELECT * FROM trade_plans")) == 2


async def test_trade_cycle_no_champion_finishes_cleanly(
    offline_loader, box_only, e2e_settings, seeded_db
):
    """Trade cycle with no champion logs a hint and finishes 'done' cleanly."""
    db = seeded_db
    orch = make_orch(db, e2e_settings, seed=9)

    cycle_id = await orch.start_cycle("trade")
    await orch.cycle_task
    cycle = db.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,))[0]
    assert cycle["status"] == "done"
    assert db.execute("SELECT * FROM paper_orders") == []
    logs = db.execute(
        "SELECT message FROM activity_log WHERE event_type = 'log' AND agent = 'trader'"
    )
    assert any("챔피언 없음" in r["message"] for r in logs)


# -- validate -------------------------------------------------------------------
async def test_validate_cycle_e2e(offline_loader, box_only, e2e_settings, seeded_db):
    """Validate cycle: 워크포워드 검증 리포트(kind='validation')를 쓰고 실제
    리더보드 테이블을 오염시키지 않는다."""
    db = seeded_db
    orch = make_orch(db, e2e_settings, seed=5)
    events = orch.bus.subscribe()

    cycle_id = await orch.start_cycle("validate")
    assert orch.status()["cycle"]["kind"] == "validate"
    await orch.cycle_task

    cycle = db.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,))[0]
    assert cycle["status"] == "done"
    assert cycle["kind"] == "validate"
    summary = json.loads(cycle["summary_json"])
    assert set(summary) >= {"symbols", "champion", "oos_metrics", "verdict"}
    assert summary["verdict"]["reason"]

    # A validation report exists with a 판정 and the USDT trade table.
    reports = db.execute("SELECT * FROM reports WHERE kind = 'validation'")
    assert len(reports) == 1
    md = reports[0]["markdown"]
    assert "판정" in md
    assert "학습 구간" in md and "검증 구간(OOS)" in md
    assert "## 검증(OOS) 거래 내역" in md
    assert "| 심볼 | 방향 | 레버리지 | TF |" in md
    assert "펀딩" in md and "강제 청산" in md

    # No leaderboard pollution: validate wrote no strategies/backtests rows.
    assert db.execute("SELECT COUNT(*) AS n FROM strategies")[0]["n"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM backtests")[0]["n"] == 0
    assert compute_leaderboard(db, settings=e2e_settings) == []

    # Meeting choreography for the validate cycle.
    collected = []
    while not events.empty():
        collected.append(events.get_nowait())
    meetings = [e["agents"] for e in collected if e["type"] == "meeting_start"]
    assert meetings == [["pm", "quant"], ["quant", "risk"], ["analyst", "pm"]]
    progress = [e for e in collected if e["type"] == "cycle_progress"]
    assert all(p["kind"] == "validate" for p in progress)
    assert progress[-1]["step"] == "done"


async def test_validate_cycle_fails_on_insufficient_data(
    offline_loader, box_only, e2e_settings, tmp_path
):
    """실행 TF 봉 수가 MIN_VALIDATE_BARS 미만인 심볼뿐이면 검증 실패 +
    한국어 에러 로그, 리포트 없음."""
    db = Database(e2e_settings.db_path)
    try:
        seed_market(db, days=10)  # 4h 봉 60개 < 160
        orch = make_orch(db, e2e_settings, seed=2)
        cycle_id = await orch.start_cycle("validate")
        await orch.cycle_task
        cycle = db.execute("SELECT * FROM cycles WHERE id = ?", (cycle_id,))[0]
        assert cycle["status"] == "failed"
        assert db.execute("SELECT * FROM reports WHERE kind = 'validation'") == []
        logs = db.execute(
            "SELECT message FROM activity_log WHERE event_type = 'log' AND level = 'error'"
        )
        assert any("검증 불가" in r["message"] for r in logs)
    finally:
        db.close()


# -- champion lifecycle ------------------------------------------------------------
async def test_second_cycle_keeps_single_champion(
    offline_loader, box_only, e2e_settings, seeded_db
):
    db = seeded_db
    orch = make_orch(db, e2e_settings, seed=7)
    await orch.start_cycle()
    await orch.cycle_task
    champion1 = db.execute("SELECT id FROM strategies WHERE status = 'champion'")
    assert len(champion1) == 1

    await orch.start_cycle()
    await orch.cycle_task
    champions = db.execute("SELECT id FROM strategies WHERE status = 'champion'")
    assert len(champions) == 1  # old champion demoted if replaced

    # 챔피언 재위는 champion_history에 정확히 하나 열려 있다.
    open_reigns = db.execute(
        "SELECT * FROM champion_history WHERE demoted_at IS NULL"
    )
    assert len(open_reigns) == 1
    assert open_reigns[0]["strategy_id"] == champions[0]["id"]


async def test_stop_cycle_marks_aborted(offline_loader, box_only, e2e_settings, seeded_db):
    db = seeded_db
    orch = make_orch(db, e2e_settings, seed=1, meeting_seconds=5.0)
    cycle_id = await orch.start_cycle()

    await asyncio.sleep(0.05)  # let the cycle enter its first meeting
    await orch.stop_cycle()
    assert not orch.running
    cycle = db.execute("SELECT status FROM cycles WHERE id = ?", (cycle_id,))[0]
    assert cycle["status"] == "aborted"
    assert orch.status()["cycle"] is None


async def test_stop_cycle_mid_backtest_waits_for_worker_thread(
    offline_loader, box_only, e2e_settings, seeded_db, monkeypatch
):
    """stop_cycle() must not orphan the asyncio.to_thread backtest worker:
    the thread must have exited before stop_cycle returns, so no DB writes
    happen after stop and an immediate db.close() cannot race the worker
    (sqlite3.ProgrammingError)."""
    from app.agents.quant import Quant

    orig = Quant._backtest_one
    state = {"active": 0, "entered": threading.Event()}

    def instrumented(self, *args, **kwargs):
        state["active"] += 1
        state["entered"].set()
        try:
            time.sleep(0.1)  # keep the worker busy so stop lands mid-backtest
            return orig(self, *args, **kwargs)
        finally:
            state["active"] -= 1

    monkeypatch.setattr(Quant, "_backtest_one", instrumented)

    db = seeded_db
    orch = make_orch(db, e2e_settings, seed=3)
    cycle_id = await orch.start_cycle()
    # Wait until the backtest worker thread is actually running.
    assert await asyncio.to_thread(state["entered"].wait, 10.0)

    await orch.stop_cycle()

    # The worker thread exited before stop_cycle returned.
    assert state["active"] == 0
    assert not orch.running
    cycle = db.execute("SELECT status FROM cycles WHERE id = ?", (cycle_id,))[0]
    assert cycle["status"] == "aborted"

    # No orphan writes after stop.
    n_before = db.execute("SELECT COUNT(*) AS n FROM backtests")[0]["n"]
    await asyncio.sleep(0.3)
    assert db.execute("SELECT COUNT(*) AS n FROM backtests")[0]["n"] == n_before

    # Immediate close (as the lifespan shutdown does) must not blow up in a
    # still-running worker thread.
    db.close()


async def test_champion_history_first_crowning(
    offline_loader, box_only, e2e_settings, seeded_db
):
    """A research cycle that crowns a champion opens exactly one reign in
    champion_history (crowned, not yet demoted)."""
    db = seeded_db
    orch = make_orch(db, e2e_settings)
    await orch.start_cycle("research")
    await orch.cycle_task

    champ = db.execute("SELECT id FROM strategies WHERE status = 'champion'")
    assert len(champ) == 1
    rows = db.execute("SELECT * FROM champion_history")
    assert len(rows) == 1
    assert rows[0]["strategy_id"] == champ[0]["id"]
    assert rows[0]["crowned_at"] is not None
    assert rows[0]["demoted_at"] is None


def test_record_champion_change_closes_and_opens(tmp_path):
    """_record_champion: unchanged → no-op; changed → close the open reign
    (set demoted_at) and open a new one."""
    db = Database(str(tmp_path / "ch.db"))
    try:
        settings = Settings(db_path=str(tmp_path / "ch.db"), _env_file=None)
        orch = Orchestrator(db, EventBus(db), settings, meeting_seconds=0.0)

        orch._record_champion(1)
        rows = db.execute("SELECT * FROM champion_history ORDER BY id")
        assert len(rows) == 1
        assert rows[0]["strategy_id"] == 1 and rows[0]["demoted_at"] is None

        orch._record_champion(1)  # unchanged
        assert len(db.execute("SELECT * FROM champion_history")) == 1

        orch._record_champion(2)  # changed
        rows = db.execute("SELECT * FROM champion_history ORDER BY id")
        assert len(rows) == 2
        assert rows[0]["strategy_id"] == 1 and rows[0]["demoted_at"] is not None
        assert rows[1]["strategy_id"] == 2 and rows[1]["demoted_at"] is None
    finally:
        db.close()


def test_backfill_champion_history(tmp_path):
    """A champion crowned before the table existed gets exactly one open reign,
    and the backfill is idempotent."""
    db = Database(str(tmp_path / "bf.db"))
    try:
        settings = Settings(db_path=str(tmp_path / "bf.db"), _env_file=None)
        orch = Orchestrator(db, EventBus(db), settings, meeting_seconds=0.0)
        db.execute(
            "INSERT INTO strategies (template, params_json, status) "
            "VALUES ('box_range', '{\"pivot_k\": 3}', 'champion')"
        )

        orch._backfill_champion_history()
        rows = db.execute("SELECT * FROM champion_history WHERE demoted_at IS NULL")
        assert len(rows) == 1

        orch._backfill_champion_history()  # idempotent
        assert len(db.execute("SELECT * FROM champion_history")) == 1
    finally:
        db.close()


async def test_champion_demoted_on_rolling_drawdown(tmp_path):
    """롤링 드로다운 강등 게이트: 재위 중 페이퍼 에쿼티가 max_mdd 이상
    무너지면 챔피언 강등 — 재위 종료(demoted_at) + 전략 archived + 경고 로그."""
    settings = Settings(
        db_path=str(tmp_path / "demote.db"), max_mdd=0.30, _env_file=None
    )
    db = Database(settings.db_path)
    try:
        orch = Orchestrator(db, EventBus(db), settings, meeting_seconds=0.0)
        rows = db.execute(
            "INSERT INTO strategies (template, params_json, status) "
            "VALUES ('box_range', '{}', 'champion')"
        )
        sid = int(rows[0]["id"])
        db.execute(
            "INSERT INTO champion_history (strategy_id, crowned_at) "
            "VALUES (?, '2020-01-01T00:00:00')",
            (sid,),
        )
        # 재위 기간 스냅샷: 10,000 → 6,000 (드로다운 40% > 한도 30%).
        for value in (10_000.0, 9_000.0, 6_000.0):
            db.execute(
                "INSERT INTO portfolio_snapshots (wallet_balance, available, "
                "margin_used, unrealized_pnl, total_value) VALUES (?, ?, 0, 0, ?)",
                (value, value, value),
            )

        await orch._maybe_demote_champion()

        assert db.execute("SELECT * FROM strategies WHERE status = 'champion'") == []
        assert db.execute(
            "SELECT status FROM strategies WHERE id = ?", (sid,)
        )[0]["status"] == "archived"
        reign = db.execute("SELECT * FROM champion_history")[0]
        assert reign["demoted_at"] is not None
        logs = db.execute(
            "SELECT message FROM activity_log WHERE level = 'warning'"
        )
        assert any("챔피언 강등" in r["message"] for r in logs)

        # 드로다운이 한도 안이면 no-op (멱등성 겸 경계 검증).
        await orch._maybe_demote_champion()
        assert len(db.execute("SELECT * FROM champion_history")) == 1
    finally:
        db.close()
