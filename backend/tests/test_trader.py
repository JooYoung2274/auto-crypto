"""Trader 태오 + PositionMonitor: 분할 진입 래더 발주, 멱등성(중복 주문 0건),
RiskEngine 게이트, 체결 리컨실(filled_fraction·TP 재계산), 복리 금지 출금
불변식, 4h 종가 손절 판정 시 플랜 자식 주문 전량 취소, TTL 원가격 재큐.

전부 오프라인 — 합성 봉을 ohlcv_cache에 직접 시딩하고 PaperBroker의 주입
가능한 clock으로 시간을 제어한다.
"""
from __future__ import annotations

import asyncio
import json
import time

import pandas as pd
import pytest

from app.agents.risk import Risk
from app.agents.trader import Trader, client_order_id, parse_client_order_id
from app.broker.paper import PaperBroker
from app.config import Settings
from app.events import EventBus
from app.monitor import PositionMonitor
from app.risk.plan import PlanLeg, TradePlan
from app.strategies.base import StrategySpec, build_plan

from tests.conftest import seed_ohlcv_cache

pytestmark = pytest.mark.asyncio

SYMBOL = "BTCUSDT"
MARK = 100.0
SEED = 10_000.0

#: 실시간 앵커 — trade_plans.created_at(datetime('now'))과 정합되도록
#: 테스트 타임스탬프를 현재 시각 기준으로 만든다.
NOW_MS = int(time.time() * 1000)
MIN_MS = 60_000
H4_MS = 4 * 3_600_000


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        db_path=str(tmp_path / "trader.db"),
        universe=["BTCUSDT", "ETHUSDT"],
        execution_timeframe="15m",
        initial_seed_usdt=SEED,
        order_ttl_bars=96,  # 모니터 판정 테스트가 TTL에 먼저 걸리지 않게
        plan_ttl_bars=960,
        _env_file=None,
    )
    base.update(overrides)
    return Settings(**base)


def seed_mark(db, symbol: str = SYMBOL, price: float = MARK) -> None:
    """시세 소스: 15m 완결 봉 1개 (quote = 마지막 캐시 종가)."""
    df = pd.DataFrame(
        {
            "open": [price],
            "high": [price],
            "low": [price],
            "close": [price],
            "volume": [1000.0],
            "quote_volume": [1000.0 * price],
        },
        index=pd.DatetimeIndex([pd.Timestamp(NOW_MS - 900_000, unit="ms")]),
    )
    seed_ohlcv_cache(db, symbol, "15m", df)


def seed_1m_bars(db, bars: list[tuple[int, float, float, float, float]],
                 symbol: str = SYMBOL) -> None:
    """(ts_ms, open, high, low, close) 1m 봉들을 캐시에 시딩."""
    df = pd.DataFrame(
        [
            {"open": o, "high": h, "low": lo, "close": c,
             "volume": 100.0, "quote_volume": 100.0 * c}
            for _, o, h, lo, c in bars
        ],
        index=pd.DatetimeIndex([pd.Timestamp(ts, unit="ms") for ts, *_ in bars]),
    )
    seed_ohlcv_cache(db, symbol, "1m", df)


def fixed_long_plan(spec, frames, regime, symbol):
    """결정론 롱 플랜: mark 100, stop 94, 50/25/25 래더 (99.4/98.5/97.3),
    R-배수 익절 — 모든 정적 게이트 통과."""
    return build_plan(
        symbol=symbol,
        side="long",
        mark=MARK,
        stop=94.0,
        evidence=["지지선 리테스트", "RSI 과매도 반등"],
        leverage=5,
        tp_r1=3.2,
        tp_r2=5.0,
    )


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def broker(db, settings) -> PaperBroker:
    seed_mark(db)
    return PaperBroker(db, None, settings, clock=lambda: NOW_MS)


@pytest.fixture
def trader(db) -> Trader:
    return Trader(EventBus(db))


def spec() -> StrategySpec:
    return StrategySpec("box_range", {"pivot_k": 3})


async def run_execute(trader, db, broker, settings, **kw):
    return await trader.execute(
        spec(), {SYMBOL: {}}, db, broker, settings, "long_alt", **kw
    )


def make_monitor(db, settings, broker, now_ms: int) -> PositionMonitor:
    return PositionMonitor(
        db=db,
        bus=EventBus(db),
        settings=settings,
        broker_provider=lambda: broker,
        trade_lock=asyncio.Lock(),
        clock=lambda: now_ms,
    )


# -- ladder placement -------------------------------------------------------------
async def test_execute_places_entry_ladder(db, settings, broker, trader, monkeypatch):
    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_long_plan))
    orders = await run_execute(trader, db, broker, settings)

    assert len(orders) == 3
    assert [o.status for o in orders] == ["open"] * 3
    assert [o.side for o in orders] == ["buy"] * 3
    assert [o.limit_price for o in orders] == [99.4, 98.5, 97.3]
    # 멱등 client_order_id = {plan_id}-entry-{leg}-{attempt}.
    assert [o.client_order_id for o in orders] == [
        "1-entry-0-0", "1-entry-1-0", "1-entry-2-0",
    ]
    # 50/25/25 분할 (복리 금지 사이징: min(지갑, 시드)/최대 동시 포지션).
    margin = SEED / settings.max_concurrent_positions
    notional = [o.qty * o.limit_price for o in orders]
    assert notional[0] == pytest.approx(margin * 5 * 0.5, rel=1e-3)
    assert notional[1] == pytest.approx(margin * 5 * 0.25, rel=1e-3)
    assert notional[2] == pytest.approx(margin * 5 * 0.25, rel=1e-3)

    plans = db.execute("SELECT * FROM trade_plans")
    assert len(plans) == 1
    assert plans[0]["status"] == "approved"
    plan = TradePlan.from_json(plans[0]["plan_json"])
    assert plan.margin_usdt == pytest.approx(margin)

    logs = [r["message"] for r in db.execute("SELECT message FROM activity_log")]
    assert any("분할 진입" in m for m in logs)


async def test_execute_idempotent_when_plan_open(db, settings, broker, trader, monkeypatch):
    """같은 심볼에 오픈 플랜이 있으면 재실행이 중복 주문을 내지 않는다."""
    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_long_plan))
    first = await run_execute(trader, db, broker, settings)
    assert len(first) == 3

    second = await run_execute(trader, db, broker, settings)
    assert second == []
    rows = db.execute("SELECT COUNT(*) AS n FROM paper_orders")
    assert rows[0]["n"] == 3
    assert len(db.execute("SELECT * FROM trade_plans")) == 1
    logs = [r["message"] for r in db.execute("SELECT message FROM activity_log")]
    assert any("중복 진입 방지" in m for m in logs)


async def test_execute_risk_rejection_persists_reason(db, settings, broker, trader, monkeypatch):
    """레버리지 캡 위반 플랜은 RiskEngine이 거부 — rejected 행 + 사유 기록,
    주문 0건. 로건(risk_agent)이 판정 로그를 남긴다."""
    def over_levered(spec, frames, regime, symbol):
        return TradePlan(
            symbol=symbol,
            side="long",
            evidence=["근거1", "근거2"],
            entries=[PlanLeg("entry", 99.0, 0.5), PlanLeg("entry", 98.0, 0.5)],
            stop=PlanLeg("stop", 94.0, 1.0),
            tps=[PlanLeg("tp", 110.0, 0.5), PlanLeg("tp", 120.0, 0.5)],
            leverage=20,  # BTC 캡 10배 초과
            margin_usdt=1000.0,
        )

    monkeypatch.setattr(Trader, "_build_plan", staticmethod(over_levered))
    risk = Risk(EventBus(db))
    orders = await run_execute(trader, db, broker, settings, risk_agent=risk)

    assert orders == []
    assert db.execute("SELECT * FROM paper_orders") == []
    plans = db.execute("SELECT * FROM trade_plans")
    assert len(plans) == 1
    assert plans[0]["status"] == "rejected"
    assert "레버리지" in plans[0]["reject_reason"]
    logs = [r["message"] for r in db.execute("SELECT message FROM activity_log")]
    assert any("플랜 거부" in m and "레버리지" in m for m in logs)


# -- fill reconciliation ------------------------------------------------------------
async def test_reconcile_updates_filled_fraction_and_places_tps(
    db, settings, broker, trader, monkeypatch
):
    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_long_plan))
    await run_execute(trader, db, broker, settings)

    # 첫 레그(99.4)만 관통하는 1m 봉 — low < 99.4, 나머지 레그는 미달.
    seed_1m_bars(db, [(NOW_MS + MIN_MS, 99.9, 100.0, 99.0, 99.6)])
    changed = await trader.settle(broker, NOW_MS + 3 * MIN_MS)
    assert [o.status for o in changed] == ["filled"]

    await trader.reconcile(db, broker, settings)

    plan_row = db.execute("SELECT * FROM trade_plans")[0]
    assert plan_row["status"] == "active"
    assert plan_row["filled_fraction"] == pytest.approx(0.5)

    # TP reduce-only 레그가 실제 체결 수량 기준(50/50)으로 발주됐다.
    tps = db.execute(
        "SELECT * FROM paper_orders WHERE reduce_only = 1 AND status = 'open' "
        "ORDER BY id"
    )
    assert len(tps) == 2
    filled = db.execute(
        "SELECT filled_qty FROM paper_orders WHERE status = 'filled'"
    )[0]["filled_qty"]
    for tp in tps:
        assert tp["qty"] == pytest.approx(filled * 0.5, rel=1e-6)
        leg = parse_client_order_id(tp["client_order_id"])
        assert leg is not None and leg[1] == "tp"

    logs = [r["message"] for r in db.execute("SELECT message FROM activity_log")]
    assert any("분할 진입" in m and "체결" in m for m in logs)


async def test_withdrawal_invariant_profit_does_not_grow_margin(
    db, settings, broker, trader, monkeypatch
):
    """복리 금지: 수익 사이클 후에도 다음 플랜 마진 예산은 시드 기준 불변.
    시드 초과 수익은 출금 원장으로 분리된다."""
    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_long_plan))

    # 수익이 난 상태 시뮬 — 지갑을 시드 + 700으로.
    db.execute(
        "UPDATE paper_state SET value = ? WHERE key = 'wallet'", (str(SEED + 700.0),)
    )
    await trader.settle(broker)  # settle + skim (UTC 일 1회)

    ledger = db.execute("SELECT * FROM withdrawal_ledger")
    assert len(ledger) == 1
    assert ledger[0]["amount"] == pytest.approx(700.0)
    assert "복리 금지" in ledger[0]["reason"]
    logs = [r["message"] for r in db.execute("SELECT message FROM activity_log")]
    assert any("복리 금지" in m for m in logs)

    orders = await run_execute(trader, db, broker, settings)
    plan = TradePlan.from_json(db.execute("SELECT * FROM trade_plans")[0]["plan_json"])
    # 마진 예산 = min(지갑, 시드)/3 — 수익 후에도 시드 고정.
    assert plan.margin_usdt == pytest.approx(SEED / settings.max_concurrent_positions)
    assert len(orders) == 3


# -- PositionMonitor ------------------------------------------------------------------
async def test_monitor_4h_stop_cancels_siblings_and_places_stop_exit(
    db, settings, broker, trader, monkeypatch
):
    """4h 종가가 손절선 이탈 → 판정 후 plan_id 공유 미체결 주문 전량 취소 +
    공격적 reduce-only 스탑엑싯 발주, 플랜 stopped."""
    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_long_plan))
    await run_execute(trader, db, broker, settings)
    seed_1m_bars(db, [(NOW_MS + MIN_MS, 99.9, 100.0, 99.0, 99.6)])
    await trader.settle(broker, NOW_MS + 3 * MIN_MS)
    await trader.reconcile(db, broker, settings)  # active + TP 레그 존재

    # 완결된 4h봉: 종가 93.0 < 손절선 94.0 (이탈).
    df = pd.DataFrame(
        {"open": [95.0], "high": [96.0], "low": [92.5], "close": [93.0],
         "volume": [1.0], "quote_volume": [93.0]},
        index=pd.DatetimeIndex([pd.Timestamp(NOW_MS + MIN_MS, unit="ms")]),
    )
    seed_ohlcv_cache(db, SYMBOL, "4h", df)
    judge_ms = NOW_MS + MIN_MS + H4_MS + 1000  # 4h 마감 후 첫 틱
    broker.clock = lambda: judge_ms

    monitor = make_monitor(db, settings, broker, judge_ms)
    await monitor.tick(judge_ms)

    plan_row = db.execute("SELECT * FROM trade_plans")[0]
    assert plan_row["status"] == "stopped"
    # 잔여 진입 레그 + TP 레그 전량 취소.
    open_rows = db.execute(
        "SELECT * FROM paper_orders WHERE status = 'open' ORDER BY id"
    )
    assert len(open_rows) == 1  # 스탑엑싯만 남는다
    exit_row = open_rows[0]
    assert exit_row["aggressive"] == 1 and exit_row["reduce_only"] == 1
    assert exit_row["side"] == "sell"
    leg = parse_client_order_id(exit_row["client_order_id"])
    assert leg is not None and leg[1] == "stop-exit"
    cancelled = db.execute(
        "SELECT reason FROM paper_orders WHERE status = 'cancelled'"
    )
    assert cancelled and all("손절 판정" in r["reason"] for r in cancelled)
    logs = [r["message"] for r in db.execute("SELECT message FROM activity_log")]
    assert any("손절" in m and "이탈" in m for m in logs)

    # 스탑엑싯은 다음 1m 시가에 taker 체결 → 포지션 종료.
    seed_1m_bars(db, [(judge_ms + MIN_MS, 92.8, 93.0, 92.0, 92.5)])
    broker.clock = lambda: judge_ms + 3 * MIN_MS
    await monitor.tick(judge_ms + 3 * MIN_MS)
    assert await broker.get_positions() == []
    exit_done = db.execute(
        "SELECT * FROM paper_orders WHERE id = ?", (exit_row["id"],)
    )[0]
    assert exit_done["status"] == "filled"
    assert exit_done["avg_fill_price"] == pytest.approx(92.8)  # 다음 1m 시가


async def test_monitor_pre_entry_invalidation_abandons_plan(
    db, settings, broker, trader, monkeypatch
):
    """진입 전(체결 0) 4h 종가 손절선 이탈 → 래더 전량 취소, abandoned."""
    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_long_plan))
    await run_execute(trader, db, broker, settings)

    df = pd.DataFrame(
        {"open": [95.0], "high": [96.0], "low": [92.5], "close": [93.0],
         "volume": [1.0], "quote_volume": [93.0]},
        index=pd.DatetimeIndex([pd.Timestamp(NOW_MS + MIN_MS, unit="ms")]),
    )
    seed_ohlcv_cache(db, SYMBOL, "4h", df)
    judge_ms = NOW_MS + MIN_MS + H4_MS + 1000

    monitor = make_monitor(db, settings, broker, judge_ms)
    await monitor.tick(judge_ms)

    plan_row = db.execute("SELECT * FROM trade_plans")[0]
    assert plan_row["status"] == "abandoned"
    assert db.execute("SELECT * FROM paper_orders WHERE status = 'open'") == []
    cancelled = db.execute("SELECT reason FROM paper_orders WHERE status = 'cancelled'")
    assert len(cancelled) == 3
    assert all("무효화" in r["reason"] for r in cancelled)


async def test_monitor_4h_close_above_stop_no_action(
    db, settings, broker, trader, monkeypatch
):
    """4h 종가가 손절선 위(정상) → 아무 조치 없음 (같은 봉은 재판정 금지)."""
    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_long_plan))
    await run_execute(trader, db, broker, settings)
    df = pd.DataFrame(
        {"open": [95.0], "high": [96.0], "low": [94.5], "close": [95.5],
         "volume": [1.0], "quote_volume": [95.5]},
        index=pd.DatetimeIndex([pd.Timestamp(NOW_MS + MIN_MS, unit="ms")]),
    )
    seed_ohlcv_cache(db, SYMBOL, "4h", df)
    judge_ms = NOW_MS + MIN_MS + H4_MS + 1000

    monitor = make_monitor(db, settings, broker, judge_ms)
    await monitor.tick(judge_ms)
    await monitor.tick(judge_ms + MIN_MS)  # 같은 4h봉 재판정 없음

    assert db.execute("SELECT * FROM trade_plans")[0]["status"] == "approved"
    assert len(db.execute("SELECT * FROM paper_orders WHERE status = 'open'")) == 3


async def test_monitor_ttl_requeue_at_original_price(db, tmp_path, monkeypatch):
    """주문 TTL 만료 → **원래 플랜 레그 가격 그대로** 재큐 (가격 추격 금지)."""
    settings = make_settings(tmp_path, order_ttl_bars=2, plan_ttl_bars=960)
    seed_mark(db)
    broker = PaperBroker(db, None, settings, clock=lambda: NOW_MS)
    trader = Trader(EventBus(db))
    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_long_plan))
    await run_execute(trader, db, broker, settings)

    # TTL(2 × 15m) 경과 후 마감된 1m 봉 — 어떤 레그도 관통하지 않음 → 만료.
    expire_ms = NOW_MS + 2 * 900_000 + MIN_MS
    seed_1m_bars(db, [(expire_ms, 100.0, 100.2, 99.9, 100.1)])
    now = expire_ms + 2 * MIN_MS
    monitor = make_monitor(db, settings, broker, now)
    await monitor.tick(now)

    expired = db.execute("SELECT * FROM paper_orders WHERE status = 'expired'")
    assert len(expired) == 3
    requeued = db.execute(
        "SELECT * FROM paper_orders WHERE status = 'open' ORDER BY id"
    )
    assert len(requeued) == 3
    assert [o["limit_price"] for o in requeued] == [99.4, 98.5, 97.3]  # 원가격
    assert [o["client_order_id"] for o in requeued] == [
        "1-entry-0-1", "1-entry-1-1", "1-entry-2-1",  # attempt+1
    ]
    logs = [r["message"] for r in db.execute("SELECT message FROM activity_log")]
    assert any("원가격 재큐" in m for m in logs)
    # 플랜은 여전히 살아 있다 (plan_ttl 이내).
    assert db.execute("SELECT * FROM trade_plans")[0]["status"] == "approved"


async def test_monitor_plan_ttl_abandons_unfilled_ladder(db, tmp_path, monkeypatch):
    """진입 전 plan_ttl_bars 경과 → 래더 전량 취소 (abandoned)."""
    settings = make_settings(tmp_path, order_ttl_bars=960, plan_ttl_bars=4)
    seed_mark(db)
    broker = PaperBroker(db, None, settings, clock=lambda: NOW_MS)
    trader = Trader(EventBus(db))
    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_long_plan))
    await run_execute(trader, db, broker, settings)

    now = NOW_MS + 5 * 900_000  # 5 × 15m > plan_ttl 4봉
    monitor = make_monitor(db, settings, broker, now)
    await monitor.tick(now)

    assert db.execute("SELECT * FROM trade_plans")[0]["status"] == "abandoned"
    assert db.execute("SELECT * FROM paper_orders WHERE status = 'open'") == []
    logs = [r["message"] for r in db.execute("SELECT message FROM activity_log")]
    assert any("TTL 만료" in m for m in logs)


async def test_monitor_final_tp_closes_plan_and_cancels_siblings(
    db, settings, broker, trader, monkeypatch
):
    """최종 익절로 포지션 종료 → 잔여 진입 레그 전량 취소, 플랜 closed."""
    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_long_plan))
    await run_execute(trader, db, broker, settings)
    seed_1m_bars(db, [(NOW_MS + MIN_MS, 99.9, 100.0, 99.0, 99.6)])
    await trader.settle(broker, NOW_MS + 3 * MIN_MS)
    await trader.reconcile(db, broker, settings)
    tps = db.execute("SELECT * FROM paper_orders WHERE reduce_only = 1 ORDER BY id")
    assert len(tps) == 2
    tp_prices = sorted(float(o["limit_price"]) for o in tps)

    # 두 TP 모두 관통하는 랠리 1m 봉.
    rally_ms = NOW_MS + 5 * MIN_MS
    top = tp_prices[-1] * 1.02
    seed_1m_bars(db, [(rally_ms, tp_prices[0] * 0.999, top, tp_prices[0] * 0.99, top)])
    now = rally_ms + 2 * MIN_MS
    broker.clock = lambda: now
    monitor = make_monitor(db, settings, broker, now)
    await monitor.tick(now)

    assert await broker.get_positions() == []
    plan_row = db.execute("SELECT * FROM trade_plans")[0]
    assert plan_row["status"] == "closed"
    # 잔여 진입 레그(2개)는 최종 익절 사유로 취소됐다.
    cancelled = db.execute(
        "SELECT reason FROM paper_orders WHERE status = 'cancelled'"
    )
    assert len(cancelled) == 2
    assert all("최종 익절" in r["reason"] for r in cancelled)


async def test_monitor_funding_event_and_liquidation_warning(
    db, tmp_path, monkeypatch
):
    """펀딩 정산(8h 경계) → funding_payment 이벤트('펀딩'), 청산가 접근 →
    liquidation_warning 이벤트('청산 경고')."""
    settings = make_settings(tmp_path)
    seed_mark(db)
    boundary = (NOW_MS // (8 * 3_600_000)) * (8 * 3_600_000) + 8 * 3_600_000
    broker = PaperBroker(db, None, settings, clock=lambda: NOW_MS)
    trader = Trader(EventBus(db))
    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_long_plan))
    await run_execute(trader, db, broker, settings)
    # 레그1 체결 → 롱 포지션. 남은 레그는 취소해 하락 봉이 추가 체결로
    # 평단/청산가를 움직이지 않게 한다.
    seed_1m_bars(db, [(NOW_MS + MIN_MS, 99.9, 100.0, 99.0, 99.6)])
    await trader.settle(broker, NOW_MS + 3 * MIN_MS)
    for o in await broker.get_open_orders():
        await broker.cancel_order(o.id, SYMBOL)

    # 8h 경계 1m 봉(펀딩) + 청산가 코앞까지 하락한 봉 (관통은 안 함).
    pos = (await broker.get_positions())[0]
    near_liq = pos.liq_price * 1.02  # 청산가 2% 위 — 경고 밴드(10%) 안
    seed_1m_bars(db, [(boundary, near_liq, near_liq, near_liq, near_liq)])
    now = boundary + 2 * MIN_MS
    broker.clock = lambda: now
    monitor = make_monitor(db, settings, broker, now)
    await monitor.tick(now)

    assert len(db.execute("SELECT * FROM funding_payments")) == 1
    events = db.execute(
        "SELECT event_type, message FROM activity_log ORDER BY id"
    )
    assert any(
        r["event_type"] == "funding_payment" and "펀딩" in r["message"]
        for r in events
    )
    warnings = [r for r in events if r["event_type"] == "liquidation_warning"]
    assert warnings and "청산 경고" in warnings[0]["message"]
    # 같은 밴드에 머무는 동안 경고는 1회만.
    await monitor.tick(now + MIN_MS)
    warnings2 = db.execute(
        "SELECT * FROM activity_log WHERE event_type = 'liquidation_warning'"
    )
    assert len(warnings2) == 1


async def test_client_order_id_roundtrip():
    coid = client_order_id(7, "stop-exit", 0, 2)
    assert coid == "7-stop-exit-0-2"
    assert parse_client_order_id(coid) == (7, "stop-exit", 0, 2)
    assert parse_client_order_id("3-entry-1-0") == (3, "entry", 1, 0)
    assert parse_client_order_id(None) is None
    assert parse_client_order_id("garbage") is None


def test_daily_pnl_yesterday_baseline_and_skim_addback(db):
    # 기준선 = 어제 마지막 스냅샷 (오늘 첫 스냅샷 아님) — 자정~첫 스냅샷
    # 사이 손실 누락 방지. 오늘 출금 스윕은 손실로 계산되지 않는다.
    db.execute(
        "INSERT INTO portfolio_snapshots "
        "(ts, wallet_balance, available, margin_used, unrealized_pnl, "
        " funding_cum, total_value) "
        "VALUES (datetime('now', '-1 day'), 10000, 10000, 0, 0, 0, 10000)"
    )
    db.execute(
        "INSERT INTO withdrawal_ledger (ts, amount, reason) "
        "VALUES (datetime('now'), 50, '출금 스윕')"
    )
    assert Trader._daily_realized_pnl(db, 9650.0) == pytest.approx(-300.0)


# -- 일손실 서킷브레이커 (모니터 배선, 라이브 한정) --------------------------------------
class _FakeLiveBroker:
    """check_daily_loss/settle/get_balance를 갖춘 최소 라이브 브로커 스텁 —
    모니터의 서킷브레이커 배선을 검증한다 (paper 브로커에는 check_daily_loss가
    없어 no-op)."""

    def __init__(self, wallet: float, settings: Settings, db):
        self._wallet = wallet
        self.settings = settings
        self.db = db
        self.kill_switch = False

    def settle(self, now_ms=None):
        return []

    async def get_balance(self):
        from app.broker.base import Balance

        return Balance(self._wallet, self._wallet, 0.0, 0.0)

    async def get_positions(self):
        return []

    async def get_quote(self, symbol: str):
        from app.broker.base import Quote

        return Quote(symbol, MARK, "")

    def check_daily_loss(self, pnl: float) -> bool:
        limit = self.settings.live_max_loss_pct * self.settings.initial_seed_usdt
        if pnl <= -limit:
            self.kill_switch = True
        return self.kill_switch


async def test_monitor_circuit_breaker_trips_kill_switch(db, settings):
    """오늘 실현손익이 한도(−live_max_loss_pct×시드)를 넘으면 모니터 틱이
    브로커 킬스위치를 발동시키고 1회 경고 이벤트를 발행한다."""
    seed_mark(db)
    db.execute(
        "INSERT INTO portfolio_snapshots "
        "(ts, wallet_balance, available, margin_used, unrealized_pnl, "
        " funding_cum, total_value) "
        "VALUES (datetime('now', '-1 day'), 10000, 10000, 0, 0, 0, 10000)"
    )
    # 어제 10,000 → 오늘 지갑 9,000 = −1,000 손실 (한도 −500 초과).
    broker = _FakeLiveBroker(9_000.0, settings, db)
    monitor = make_monitor(db, settings, broker, NOW_MS)
    await monitor.tick(NOW_MS)
    assert broker.kill_switch is True
    warns = db.execute(
        "SELECT * FROM activity_log WHERE event_type = 'liquidation_warning'"
    )
    assert warns and "서킷브레이커" in warns[0]["message"]


async def test_monitor_circuit_breaker_holds_when_within_limit(db, settings):
    """손실이 한도 이내면 킬스위치는 발동하지 않고 경고도 없다."""
    seed_mark(db)
    db.execute(
        "INSERT INTO portfolio_snapshots "
        "(ts, wallet_balance, available, margin_used, unrealized_pnl, "
        " funding_cum, total_value) "
        "VALUES (datetime('now', '-1 day'), 10000, 10000, 0, 0, 0, 10000)"
    )
    broker = _FakeLiveBroker(9_800.0, settings, db)  # −200 손실 (< 500 한도)
    monitor = make_monitor(db, settings, broker, NOW_MS)
    await monitor.tick(NOW_MS)
    assert broker.kill_switch is False
    warns = db.execute(
        "SELECT * FROM activity_log WHERE event_type = 'liquidation_warning'"
    )
    assert warns == []


async def test_stop_exit_recovery_skips_when_newer_plan_owns_position(
    db, settings, broker
):
    """포지션 소유권 가드 (2026-07-21 실계정 사고 회귀): 같은 심볼에 더 새로운
    플랜이 있으면 과거 stopped 플랜의 스탑엑싯 복구가 발동하지 않는다 —
    새 플랜의 포지션을 옛 플랜의 '복구'가 청산해선 안 된다."""
    plan_json = json.dumps(
        {"symbol": SYMBOL, "side": "short", "leverage": 3,
         "entries": [], "tps": [], "stop": {"kind": "stop", "price": 999.0, "fraction": 1.0},
         "evidence": [], "margin_usdt": 100.0}
    )
    # 옛 플랜 #A: stopped (이미 손절 완료), 새 플랜 #B: active (현 포지션 소유).
    db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status, filled_fraction) "
        "VALUES (?, 'short', ?, 'stopped', 1.0)", (SYMBOL, plan_json),
    )
    db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status, filled_fraction) "
        "VALUES (?, 'short', ?, 'active', 1.0)", (SYMBOL, plan_json),
    )
    db.execute(
        "INSERT INTO paper_positions (symbol, side, qty, avg_entry, leverage, "
        "isolated_margin, liq_price) VALUES (?, 'short', 10.0, 100.0, 3, 333.0, 130.0)",
        (SYMBOL,),
    )
    monitor = make_monitor(db, settings, broker, NOW_MS)
    before = len(db.execute("SELECT id FROM paper_orders"))
    await monitor._drive_stop_exits(broker, NOW_MS)
    after = len(db.execute("SELECT id FROM paper_orders"))
    assert after == before  # 복구 재발주 없음


async def test_stop_exit_recovery_fires_for_latest_stopped_plan(db, settings, broker):
    """정상 복구 경로: 심볼의 최신 플랜이 stopped인데 포지션이 남았고 오픈
    스탑엑싯이 없으면 복구 재발주가 나간다 (기존 동작 보존)."""
    plan_json = json.dumps(
        {"symbol": SYMBOL, "side": "short", "leverage": 3,
         "entries": [], "tps": [], "stop": {"kind": "stop", "price": 999.0, "fraction": 1.0},
         "evidence": [], "margin_usdt": 100.0}
    )
    db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status, filled_fraction) "
        "VALUES (?, 'short', ?, 'stopped', 1.0)", (SYMBOL, plan_json),
    )
    db.execute(
        "INSERT INTO paper_positions (symbol, side, qty, avg_entry, leverage, "
        "isolated_margin, liq_price) VALUES (?, 'short', 10.0, 100.0, 3, 333.0, 130.0)",
        (SYMBOL,),
    )
    seed_1m_bars(db, [(NOW_MS - MIN_MS, 100.0, 100.5, 99.5, 100.0)])  # 시세 소스
    monitor = make_monitor(db, settings, broker, NOW_MS)
    await monitor._drive_stop_exits(broker, NOW_MS)
    exits = db.execute(
        "SELECT * FROM paper_orders WHERE client_order_id LIKE '%-stop-exit-%'"
    )
    assert len(exits) == 1 and exits[0]["reduce_only"]


def _plan_json(side: str = "short") -> str:
    return json.dumps(
        {"symbol": SYMBOL, "side": side, "leverage": 3, "entries": [], "tps": [],
         "stop": {"kind": "stop", "price": 999.0, "fraction": 1.0},
         "evidence": [], "margin_usdt": 100.0}
    )


async def test_stop_exit_recovery_disowns_when_newer_terminal_plan(
    db, settings, broker
):
    """finding #1/#8: 같은 심볼에 더 새로운 플랜이 (closed/abandoned 같은) 종결
    상태로라도 존재하면, 과거 stopped 플랜의 복구는 발동하지 않는다 — 그리고
    최신 플랜이 종결됐는데 포지션이 남았으면 고아 경고만 내고 절대 발주하지
    않는다 (2026-07-21 사고 한 상태값 차이 재발 방지)."""
    db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status, filled_fraction) "
        "VALUES (?, 'short', ?, 'stopped', 1.0)", (SYMBOL, _plan_json()),
    )
    db.execute(  # 최신 플랜은 이미 익절 종료(closed) — 옛 stopped 가드에 없던 상태값
        "INSERT INTO trade_plans (symbol, side, plan_json, status, filled_fraction) "
        "VALUES (?, 'short', ?, 'closed', 1.0)", (SYMBOL, _plan_json()),
    )
    db.execute(
        "INSERT INTO paper_positions (symbol, side, qty, avg_entry, leverage, "
        "isolated_margin, liq_price) VALUES (?, 'short', 10.0, 100.0, 3, 333.0, 130.0)",
        (SYMBOL,),
    )
    monitor = make_monitor(db, settings, broker, NOW_MS)
    before = len(db.execute("SELECT id FROM paper_orders"))
    await monitor._drive_stop_exits(broker, NOW_MS)
    assert len(db.execute("SELECT id FROM paper_orders")) == before  # 발주 없음
    warns = db.execute(
        "SELECT * FROM activity_log WHERE event_type = 'liquidation_warning'"
    )
    assert warns and "고아" in warns[0]["message"]


async def test_stop_exit_recovery_skips_on_opposite_side_position(
    db, settings, broker
):
    """finding #5 (헤지 모드): stopped 롱 플랜이 최신이어도 심볼의 실제 포지션이
    (수동) 숏이면 복구가 발동하지 않는다 — 반대 방향 포지션을 옛 플랜이
    청산해선 안 된다."""
    db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status, filled_fraction) "
        "VALUES (?, 'long', ?, 'stopped', 1.0)", (SYMBOL, _plan_json("long")),
    )
    db.execute(  # 수동 반대 방향(숏) 포지션
        "INSERT INTO paper_positions (symbol, side, qty, avg_entry, leverage, "
        "isolated_margin, liq_price) VALUES (?, 'short', 10.0, 100.0, 3, 333.0, 130.0)",
        (SYMBOL,),
    )
    monitor = make_monitor(db, settings, broker, NOW_MS)
    before = len(db.execute("SELECT id FROM paper_orders"))
    await monitor._drive_stop_exits(broker, NOW_MS)
    assert len(db.execute("SELECT id FROM paper_orders")) == before  # 발주 없음


async def test_close_finished_marks_live_liquidation_suspect(db, settings, broker):
    """finding #2/#19: active 플랜의 포지션이 봇 청산(reduce-only) 체결 없이
    사라지면(라이브 강제 청산 서명) '최종 익절 closed'가 아니라 '강제 청산
    의심 stopped'로 처리한다."""
    db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status, filled_fraction) "
        "VALUES (?, 'long', ?, 'active', 1.0)", (SYMBOL, _plan_json("long")),
    )
    db.execute(  # 진입 체결만 있고 reduce-only 청산 체결은 없음
        "INSERT INTO paper_orders (symbol, side, qty, limit_price, filled_qty, "
        "avg_fill_price, reduce_only, status, plan_id, client_order_id, reason) "
        "VALUES (?, 'buy', 10, 100, 10, 100, 0, 'filled', 1, '1-entry-0-0', '')",
        (SYMBOL,),
    )
    monitor = make_monitor(db, settings, broker, NOW_MS)
    await monitor._close_finished_plans(broker, NOW_MS)
    row = db.execute(
        "SELECT status, reject_reason FROM trade_plans WHERE id = 1"
    )[0]
    assert row["status"] == "stopped"
    assert row["reject_reason"] == "강제 청산 의심"


async def test_close_finished_takeprofit_stays_closed(db, settings, broker):
    """대조군: reduce-only 익절 체결이 있으면 정상 종료(closed)로 분류된다
    (청산 폴백이 정상 익절을 오분류하지 않는다)."""
    db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status, filled_fraction) "
        "VALUES (?, 'long', ?, 'active', 1.0)", (SYMBOL, _plan_json("long")),
    )
    db.execute(
        "INSERT INTO paper_orders (symbol, side, qty, limit_price, filled_qty, "
        "avg_fill_price, reduce_only, status, plan_id, client_order_id, reason) "
        "VALUES (?, 'buy', 10, 100, 10, 100, 0, 'filled', 1, '1-entry-0-0', '')",
        (SYMBOL,),
    )
    db.execute(  # 익절 체결(reduce-only)
        "INSERT INTO paper_orders (symbol, side, qty, limit_price, filled_qty, "
        "avg_fill_price, reduce_only, status, plan_id, client_order_id, reason) "
        "VALUES (?, 'sell', 10, 120, 10, 120, 1, 'filled', 1, '1-tp-0-0', '')",
        (SYMBOL,),
    )
    monitor = make_monitor(db, settings, broker, NOW_MS)
    await monitor._close_finished_plans(broker, NOW_MS)
    row = db.execute("SELECT status FROM trade_plans WHERE id = 1")[0]
    assert row["status"] == "closed"


async def test_close_finished_stamps_liquidation_row_plan_id(db, settings, broker):
    """finding #3: 귀속한 plan_id-NULL 강제 청산 행에 plan_id를 스탬핑한다 —
    이후 trade-history가 심볼+윈도가 아니라 plan_id로만 청산을 집계할 수 있게."""
    db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status, filled_fraction) "
        "VALUES (?, 'long', ?, 'active', 1.0)", (SYMBOL, _plan_json("long")),
    )
    db.execute(
        "INSERT INTO paper_orders (symbol, side, qty, limit_price, filled_qty, "
        "avg_fill_price, reduce_only, status, plan_id, client_order_id, reason) "
        "VALUES (?, 'buy', 10, 100, 10, 100, 0, 'filled', 1, '1-entry-0-0', '')",
        (SYMBOL,),
    )
    db.execute(  # paper 강제 청산 행 — plan_id NULL (paper._liquidate 산출물)
        "INSERT INTO paper_orders (symbol, side, qty, limit_price, filled_qty, "
        "avg_fill_price, reduce_only, aggressive, status, reason) "
        "VALUES (?, 'sell', 10, 90, 10, 90, 1, 1, 'filled', "
        "'강제 청산 — 격리마진 전액 손실')", (SYMBOL,),
    )
    monitor = make_monitor(db, settings, broker, NOW_MS)
    await monitor._close_finished_plans(broker, NOW_MS)
    liq = db.execute(
        "SELECT plan_id FROM paper_orders WHERE reason LIKE '%강제 청산%'"
    )[0]
    assert liq["plan_id"] == 1
    row = db.execute("SELECT status, reject_reason FROM trade_plans WHERE id = 1")[0]
    assert row["status"] == "stopped" and row["reject_reason"] == "강제 청산"


async def test_plan_ttl_skips_abandon_when_position_held(db, tmp_path, broker):
    """finding #17: 미러 filled_fraction이 0이어도 같은 방향 실제 포지션이
    있으면 플랜을 abandon하지 않는다 — 라이브 부분 체결이 미러에 아직 안 잡힌
    포지션을 고아로 만들지 않게."""
    settings = make_settings(tmp_path, plan_ttl_bars=1)  # ttl = 1 × 15m
    db.execute(
        "INSERT INTO trade_plans (created_at, symbol, side, plan_json, status, "
        "filled_fraction) VALUES (datetime('now', '-1 hour'), ?, 'long', ?, "
        "'approved', 0.0)", (SYMBOL, _plan_json("long")),
    )
    db.execute(
        "INSERT INTO paper_positions (symbol, side, qty, avg_entry, leverage, "
        "isolated_margin, liq_price) VALUES (?, 'long', 5.0, 100.0, 5, 100.0, 80.0)",
        (SYMBOL,),
    )
    monitor = make_monitor(db, settings, broker, NOW_MS)
    await monitor._enforce_plan_ttl(broker, NOW_MS)
    assert db.execute("SELECT status FROM trade_plans WHERE id = 1")[0]["status"] == (
        "approved"
    )


async def test_plan_ttl_abandons_without_position(db, tmp_path, broker):
    """대조군: 포지션이 없으면 TTL 만료 플랜은 정상적으로 abandoned 된다."""
    settings = make_settings(tmp_path, plan_ttl_bars=1)
    db.execute(
        "INSERT INTO trade_plans (created_at, symbol, side, plan_json, status, "
        "filled_fraction) VALUES (datetime('now', '-1 hour'), ?, 'long', ?, "
        "'approved', 0.0)", (SYMBOL, _plan_json("long")),
    )
    monitor = make_monitor(db, settings, broker, NOW_MS)
    await monitor._enforce_plan_ttl(broker, NOW_MS)
    assert db.execute("SELECT status FROM trade_plans WHERE id = 1")[0]["status"] == (
        "abandoned"
    )


async def test_reconcile_counts_partial_fill_before_status_filled(
    db, settings, broker, trader, monkeypatch
):
    """finding #17: 진입 레그가 아직 'open'(라이브 partially_filled)이어도
    filled_qty > 0이면 그 레그 비중을 인정한다 — 실제 포지션을 든 플랜을
    filled_fraction=0으로 두어 TTL이 abandon하지 못하게."""
    monkeypatch.setattr(Trader, "_build_plan", staticmethod(fixed_long_plan))
    await run_execute(trader, db, broker, settings)
    plan_id = db.execute("SELECT id FROM trade_plans")[0]["id"]
    entry = db.execute(
        "SELECT id FROM paper_orders WHERE plan_id = ? "
        "AND client_order_id LIKE '%-entry-%' ORDER BY id LIMIT 1", (plan_id,),
    )[0]
    # 상태는 'open' 그대로, 체결량만 반영 (라이브 partially_filled 서명).
    db.execute(
        "UPDATE paper_orders SET filled_qty = 1.0 WHERE id = ?", (entry["id"],)
    )
    monitor = make_monitor(db, settings, broker, NOW_MS)
    await monitor._reconcile_fills()
    row = db.execute(
        "SELECT status, filled_fraction FROM trade_plans WHERE id = ?", (plan_id,)
    )[0]
    assert row["filled_fraction"] > 0
    assert row["status"] == "active"


async def test_maintain_tp_skips_rejected_leg_with_unchanged_qty(
    db, settings, broker, trader
):
    """finding #9/#18: TP 레그의 최근 시도가 같은 수량으로 거부됐으면(서브계약
    floor→0 등) 매 사이클 재발주하지 않는다 — 무한 rejected 루프 차단."""
    plan = fixed_long_plan(None, {}, "long_alt", SYMBOL)
    db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status, filled_fraction) "
        "VALUES (?, 'long', ?, 'active', 1.0)", (SYMBOL, plan.to_json()),
    )
    plan_id = 1
    filled_qty = 10.0
    desired0 = filled_qty * plan.tps[0].fraction  # 비-최종 레그 목표 수량
    # 레그 0의 최근 시도가 desired0 수량으로 이미 거부된 상태를 심는다.
    db.execute(
        "INSERT INTO paper_orders (symbol, side, qty, limit_price, reduce_only, "
        "status, plan_id, leg_kind, leg_index, client_order_id, reason) "
        "VALUES (?, 'sell', ?, ?, 1, 'rejected', ?, 'tp', 0, '1-tp-0-0', "
        "'수량 < 1계약')", (SYMBOL, desired0, plan.tps[0].price, plan_id),
    )
    orders = db.execute(
        "SELECT * FROM paper_orders WHERE plan_id = ? ORDER BY id", (plan_id,)
    )
    await trader._maintain_tp_orders(db, broker, plan_id, plan, filled_qty, orders)
    leg0 = db.execute(
        "SELECT status FROM paper_orders WHERE client_order_id LIKE '1-tp-0-%'"
    )
    # 레그 0은 재발주되지 않았다 — 여전히 거부 행 하나뿐.
    assert [r["status"] for r in leg0] == ["rejected"]


async def test_abandon_dead_ladder_frees_symbol(db, settings, broker):
    """래더 전 진입 레그가 취소로 끝나면(체결 0, 포지션 없음) 플랜을 즉시
    abandoned로 정리해 심볼 재진입 차단을 푼다 (XRP #49, 2026-07-22)."""
    plan_json = json.dumps({
        "symbol": SYMBOL, "side": "short", "leverage": 3,
        "entries": [
            {"kind": "entry", "price": 101.0, "fraction": 0.5},
            {"kind": "entry", "price": 101.1, "fraction": 0.25},
            {"kind": "entry", "price": 101.2, "fraction": 0.25},
        ],
        "tps": [{"kind": "tp", "price": 90.0, "fraction": 0.5},
                {"kind": "tp", "price": 88.0, "fraction": 0.5}],
        "stop": {"kind": "stop", "price": 101.3, "fraction": 1.0},
        "evidence": ["a", "b"], "margin_usdt": 100.0,
    })
    plan_id = db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status, filled_fraction) "
        "VALUES (?, 'short', ?, 'approved', 0.0)", (SYMBOL, plan_json),
    )[0]["id"]
    # 3레그 전량 배치 후 exchange 취소 (cancelled로 종결).
    for leg in range(3):
        db.execute(
            "INSERT INTO paper_orders (ts, symbol, side, qty, limit_price, "
            "reduce_only, status, plan_id, client_order_id) "
            "VALUES ('2026-07-22T05:45:07+00:00', ?, 'sell', 10, 101.0, 0, "
            "'cancelled', ?, ?)",
            (SYMBOL, plan_id, client_order_id(plan_id, "entry", leg, 0)),
        )
    monitor = make_monitor(db, settings, broker, NOW_MS)
    await monitor._abandon_dead_ladders(broker)
    status = db.execute(
        "SELECT status, reject_reason FROM trade_plans WHERE id = ?", (plan_id,)
    )[0]
    assert status["status"] == "abandoned"
    assert "래더" in status["reject_reason"]


async def test_dead_ladder_guard_spares_partially_placed_or_open(db, settings, broker):
    """오픈 레그가 남았거나 래더가 아직 전량 배치 전이면 abandon하지 않는다."""
    plan_json = json.dumps({
        "symbol": SYMBOL, "side": "short", "leverage": 3,
        "entries": [
            {"kind": "entry", "price": 101.0, "fraction": 0.5},
            {"kind": "entry", "price": 101.1, "fraction": 0.25},
            {"kind": "entry", "price": 101.2, "fraction": 0.25},
        ],
        "tps": [{"kind": "tp", "price": 90.0, "fraction": 0.5},
                {"kind": "tp", "price": 88.0, "fraction": 0.5}],
        "stop": {"kind": "stop", "price": 101.3, "fraction": 1.0},
        "evidence": ["a", "b"], "margin_usdt": 100.0,
    })
    plan_id = db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status, filled_fraction) "
        "VALUES (?, 'short', ?, 'approved', 0.0)", (SYMBOL, plan_json),
    )[0]["id"]
    # 레그 0·1 취소, 레그 2는 아직 open — 살아 있음.
    for leg, st in ((0, "cancelled"), (1, "cancelled"), (2, "open")):
        db.execute(
            "INSERT INTO paper_orders (ts, symbol, side, qty, limit_price, "
            "reduce_only, status, plan_id, client_order_id) "
            "VALUES ('2026-07-22T05:45:07+00:00', ?, 'sell', 10, 101.0, 0, ?, ?, ?)",
            (SYMBOL, st, plan_id, client_order_id(plan_id, "entry", leg, 0)),
        )
    monitor = make_monitor(db, settings, broker, NOW_MS)
    await monitor._abandon_dead_ladders(broker)
    assert db.execute(
        "SELECT status FROM trade_plans WHERE id = ?", (plan_id,)
    )[0]["status"] == "approved"
