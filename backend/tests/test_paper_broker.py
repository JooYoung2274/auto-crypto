"""PaperBroker — 1m 마감 봉 지정가 체결 시뮬 테스트 (오프라인, 합성 시드).

스펙 §5/§9 불변식: 관통 체결(터치 미체결)·갭스루 체결가, 발주 이후 봉만
매칭, post-only 크로싱 거부(aggressive reduce_only 예외 → 다음 1m 시가
taker), 청산 정확식 + 매 체결 재계산, 청산 intrabar 최우선, 펀딩 부호,
TTL 만료, 플랜 없는 주문 거부, client_order_id 멱등, 실현 잔고만 출금
(UTC 일 1회, 복리 금지).
"""
from __future__ import annotations

import pytest

from app.broker.base import OrderRequest
from app.broker.paper import FUNDING_INTERVAL_MS, PaperBroker
from app.config import Settings
from app.db import Database
from app.risk.plan import liquidation_price

pytestmark = pytest.mark.asyncio

MIN = 60_000  # 1m in ms
#: UTC 자정 정렬(펀딩 8h 경계 계산 용이) 기준 시각. 2026-07-10T00:00:00Z
T0 = 1_783_641_600_000 - 1_783_641_600_000 % FUNDING_INTERVAL_MS

SEED = 10_000.0
MAKER = 0.00025
TAKER = 0.0005


def make_settings(**overrides) -> Settings:
    kwargs = dict(
        db_path=":memory:",
        execution_timeframe="1m",  # TTL 계산을 1m 봉 기준으로 (테스트 편의)
        order_ttl_bars=1_000,
    )
    kwargs.update(overrides)
    return Settings(_env_file=None, **kwargs)


def seed_1m(db: Database, symbol: str, bars: list[tuple[int, float, float, float, float]]):
    """(ts, open, high, low, close) 1m 봉 시드."""
    db.executemany(
        "INSERT OR REPLACE INTO ohlcv_cache "
        "(symbol, timeframe, ts, open, high, low, close, volume, quote_volume) "
        "VALUES (?, '1m', ?, ?, ?, ?, ?, 1000, 0)",
        [(symbol, ts, o, h, l, c) for ts, o, h, l, c in bars],
    )


def make_plan(db: Database, symbol: str = "BTCUSDT", side: str = "long",
              status: str = "approved") -> int:
    rows = db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status) "
        "VALUES (?, ?, '{}', ?)",
        (symbol, side, status),
    )
    return rows[0]["id"]


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "paper.db"))
    yield database
    database.close()


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def broker(db, settings) -> PaperBroker:
    b = PaperBroker(db, None, settings)
    b.clock = lambda: T0 + MIN  # 기준 봉(T0) 마감 직후
    return b


def entry_req(plan_id: int, *, symbol="BTCUSDT", side="buy", qty=1.0,
              price=100.0, leverage=5, coid=None, aggressive=False,
              reduce_only=False) -> OrderRequest:
    return OrderRequest(
        symbol=symbol, side=side, qty=qty, limit_price=price,
        reduce_only=reduce_only, aggressive=aggressive, leverage=leverage,
        client_order_id=coid, plan_id=plan_id,
    )


async def open_long(broker, db, *, qty=1.0, price=100.0, leverage=5):
    """T0 봉(close 101) 기준 100 지정가 롱 진입을 만들어 체결까지 진행."""
    seed_1m(db, "BTCUSDT", [(T0, 101.0, 101.5, 100.5, 101.0)])
    plan_id = make_plan(db)
    order = await broker.place_order(
        entry_req(plan_id, qty=qty, price=price, leverage=leverage)
    )
    assert order.status == "open", order.reason
    # 다음 봉이 100을 관통 (low 99 < 100)
    seed_1m(db, "BTCUSDT", [(T0 + MIN, 100.4, 100.6, 99.0, 100.2)])
    changed = broker.settle(now_ms=T0 + 2 * MIN)
    assert any(o.id == order.id and o.status == "filled" for o in changed)
    return order, plan_id


# -- plan gate (규칙 §2: 시나리오 없는 주문은 브로커가 거부) ---------------------------


async def test_planless_order_rejected(broker, db):
    seed_1m(db, "BTCUSDT", [(T0, 101.0, 101.5, 100.5, 101.0)])
    order = await broker.place_order(
        OrderRequest(symbol="BTCUSDT", side="buy", qty=1.0, limit_price=100.0)
    )
    assert order.status == "rejected"
    assert "시나리오" in order.reason


async def test_draft_plan_order_rejected(broker, db):
    seed_1m(db, "BTCUSDT", [(T0, 101.0, 101.5, 100.5, 101.0)])
    plan_id = make_plan(db, status="draft")
    order = await broker.place_order(entry_req(plan_id))
    assert order.status == "rejected"
    assert "플랜" in order.reason


async def test_reduce_only_needs_no_plan(broker, db):
    await open_long(broker, db)
    tp = await broker.place_order(
        OrderRequest(symbol="BTCUSDT", side="sell", qty=1.0, limit_price=110.0,
                     reduce_only=True)
    )
    assert tp.status == "open"


# -- post-only / crossing ---------------------------------------------------------------


async def test_crossing_buy_rejected_post_only(broker, db):
    seed_1m(db, "BTCUSDT", [(T0, 101.0, 101.5, 100.5, 101.0)])  # mark=101
    plan_id = make_plan(db)
    order = await broker.place_order(entry_req(plan_id, price=101.5))
    assert order.status == "rejected"
    assert "post-only" in order.reason


async def test_crossing_sell_rejected_post_only(broker, db):
    await open_long(broker, db)
    # mark = 100.2 — sell limit 100.0은 크로싱 (passive TP가 아님)
    tp = await broker.place_order(
        OrderRequest(symbol="BTCUSDT", side="sell", qty=1.0, limit_price=100.0,
                     reduce_only=True)
    )
    assert tp.status == "rejected"
    assert "post-only" in tp.reason


async def test_aggressive_reduce_only_fills_next_open_at_taker(broker, db):
    await open_long(broker, db)  # qty 1 @100
    wallet_before = broker._get_wallet()
    stop = await broker.place_order(
        OrderRequest(symbol="BTCUSDT", side="sell", qty=1.0, limit_price=99.5,
                     reduce_only=True, aggressive=True)
    )
    assert stop.status == "open"  # 크로싱이지만 aggressive exit라 허용
    broker.clock = lambda: T0 + 3 * MIN
    seed_1m(db, "BTCUSDT", [(T0 + 2 * MIN, 99.8, 100.0, 99.2, 99.4)])
    changed = broker.settle()
    filled = [o for o in changed if o.id == stop.id]
    assert filled and filled[0].status == "filled"
    assert filled[0].avg_fill_price == pytest.approx(99.8)  # 다음 1m 시가
    pnl = (99.8 - 100.0) * 1.0
    fee = 99.8 * 1.0 * TAKER  # taker 요율
    assert broker._get_wallet() == pytest.approx(wallet_before + pnl - fee)


# -- trade-through fills --------------------------------------------------------------


async def test_touch_does_not_fill(broker, db):
    seed_1m(db, "BTCUSDT", [(T0, 101.0, 101.5, 100.5, 101.0)])
    plan_id = make_plan(db)
    order = await broker.place_order(entry_req(plan_id, price=100.0))
    # low == 100.0 (터치) → 미체결, 관통해야 체결
    seed_1m(db, "BTCUSDT", [(T0 + MIN, 100.4, 100.6, 100.0, 100.2)])
    changed = broker.settle(now_ms=T0 + 2 * MIN)
    assert all(o.id != order.id for o in changed)
    open_orders = await broker.get_open_orders("BTCUSDT")
    assert [o.id for o in open_orders] == [order.id]


async def test_trade_through_fills_at_limit(broker, db):
    order, _ = await open_long(broker, db)
    row = db.execute("SELECT * FROM paper_orders WHERE id = ?", (int(order.id),))[0]
    assert row["status"] == "filled"
    assert float(row["avg_fill_price"]) == pytest.approx(100.0)  # min(open=100.4, P=100)


async def test_gap_through_fills_at_open(broker, db):
    seed_1m(db, "BTCUSDT", [(T0, 101.0, 101.5, 100.5, 101.0)])
    plan_id = make_plan(db)
    order = await broker.place_order(entry_req(plan_id, price=100.0))
    # 갭다운: open 98 < P=100 → 체결가 = min(open, P) = 98
    seed_1m(db, "BTCUSDT", [(T0 + MIN, 98.0, 98.5, 97.0, 98.2)])
    changed = broker.settle(now_ms=T0 + 2 * MIN)
    filled = [o for o in changed if o.id == order.id]
    assert filled and filled[0].avg_fill_price == pytest.approx(98.0)


async def test_short_fill_symmetric(broker, db):
    seed_1m(db, "SOLUSDT", [(T0, 100.0, 100.4, 99.6, 100.0)])
    plan_id = make_plan(db, symbol="SOLUSDT", side="short")
    order = await broker.place_order(
        entry_req(plan_id, symbol="SOLUSDT", side="sell", qty=10.0, price=101.0,
                  leverage=3)
    )
    assert order.status == "open", order.reason
    # high 101.0 == P → 터치 미체결
    seed_1m(db, "SOLUSDT", [(T0 + MIN, 100.5, 101.0, 100.0, 100.6)])
    assert not broker.settle(now_ms=T0 + 2 * MIN)
    # high 101.6 > P → 체결가 = max(open=100.8, P=101) = 101
    seed_1m(db, "SOLUSDT", [(T0 + 2 * MIN, 100.8, 101.6, 100.4, 101.2)])
    changed = broker.settle(now_ms=T0 + 3 * MIN)
    filled = [o for o in changed if o.id == order.id]
    assert filled and filled[0].avg_fill_price == pytest.approx(101.0)
    pos = (await broker.get_positions())[0]
    assert pos.side == "short" and pos.qty == pytest.approx(10.0)


async def test_no_same_bar_fill(broker, db):
    """주문은 발주 이후에 open하는 봉부터만 매칭."""
    seed_1m(db, "BTCUSDT", [(T0, 101.0, 101.5, 99.0, 101.0)])  # low 99 < P=100
    plan_id = make_plan(db)
    broker.clock = lambda: T0 + 30_000  # T0 봉 도중 발주
    order = await broker.place_order(entry_req(plan_id, price=100.0))
    assert order.status == "open"
    # T0 봉은 발주 전에 open → 매칭 금지
    assert not broker.settle(now_ms=T0 + 2 * MIN)


# -- liquidation ----------------------------------------------------------------------


async def test_liquidation_exact_formula_and_margin_loss(broker, db):
    await open_long(broker, db, qty=1.0, price=100.0, leverage=10)
    pos = (await broker.get_positions())[0]
    expected_liq = liquidation_price(100.0, "long", 10, 100.0)
    assert pos.liq_price == pytest.approx(expected_liq)
    wallet_before = broker._get_wallet()
    # 청산가 관통 봉 → 강제 청산, 격리마진 전액 손실
    seed_1m(db, "BTCUSDT", [(T0 + 2 * MIN, 95.0, 95.5, expected_liq - 0.5, 91.0)])
    changed = broker.settle(now_ms=T0 + 3 * MIN)
    assert not await broker.get_positions()
    assert broker._get_wallet() == pytest.approx(wallet_before - pos.isolated_margin)
    liq_orders = [o for o in changed if "청산" in o.reason and o.status == "filled"]
    assert liq_orders and liq_orders[0].avg_fill_price == pytest.approx(expected_liq)


async def test_liquidation_priority_over_tp_fill(broker, db):
    await open_long(broker, db, qty=1.0, price=100.0, leverage=10)
    pos = (await broker.get_positions())[0]
    tp = await broker.place_order(
        OrderRequest(symbol="BTCUSDT", side="sell", qty=1.0, limit_price=101.0,
                     reduce_only=True)
    )
    assert tp.status == "open"
    # 같은 봉에서 TP(high 101.5 > 101)와 청산(low < liq) 동시 조건 → 청산 우선
    seed_1m(db, "BTCUSDT", [(T0 + 2 * MIN, 100.5, 101.5, pos.liq_price - 0.5, 91.0)])
    broker.settle(now_ms=T0 + 3 * MIN)
    assert not await broker.get_positions()
    tp_row = db.execute("SELECT * FROM paper_orders WHERE id = ?", (int(tp.id),))[0]
    assert tp_row["status"] == "cancelled"  # 청산으로 취소, 체결 아님


async def test_avg_entry_margin_liq_recomputed_per_fill(broker, db):
    """매 체결 이벤트마다 avg_entry·마진·청산가 재계산 (스펙 §4)."""
    await open_long(broker, db, qty=1.0, price=100.0, leverage=10)
    pos1 = (await broker.get_positions())[0]
    assert pos1.avg_entry == pytest.approx(100.0)
    assert pos1.isolated_margin == pytest.approx(10.0)
    assert pos1.liq_price == pytest.approx(liquidation_price(100.0, "long", 10, 100.0))

    plan_id = make_plan(db)
    # 청산가(≈90.36) 위의 눌림 진입 — 청산 없이 추가 체결만 발생.
    order2 = await broker.place_order(
        entry_req(plan_id, qty=1.5, price=95.0, leverage=10)
    )
    assert order2.status == "open", order2.reason
    seed_1m(db, "BTCUSDT", [(T0 + 2 * MIN, 95.5, 96.0, 94.5, 95.2)])
    broker.settle(now_ms=T0 + 3 * MIN)

    pos2 = (await broker.get_positions())[0]
    assert pos2.qty == pytest.approx(2.5)
    assert pos2.avg_entry == pytest.approx(97.0)  # (1×100 + 1.5×95) / 2.5
    assert pos2.isolated_margin == pytest.approx(10.0 + 14.25)
    assert pos2.liq_price == pytest.approx(
        liquidation_price(97.0, "long", 10, 2.5 * 97.0)
    )
    assert pos2.liq_price < pos1.liq_price  # 추가 진입으로 청산가 개선


async def test_partial_tp_releases_margin_pro_rata(broker, db):
    await open_long(broker, db, qty=2.0, price=100.0, leverage=5)
    tp = await broker.place_order(
        OrderRequest(symbol="BTCUSDT", side="sell", qty=1.0, limit_price=105.0,
                     reduce_only=True)
    )
    wallet_before = broker._get_wallet()
    seed_1m(db, "BTCUSDT", [(T0 + 2 * MIN, 104.0, 106.0, 103.5, 105.5)])
    changed = broker.settle(now_ms=T0 + 3 * MIN)
    filled = [o for o in changed if o.id == tp.id]
    assert filled and filled[0].avg_fill_price == pytest.approx(105.0)
    pos = (await broker.get_positions())[0]
    assert pos.qty == pytest.approx(1.0)
    assert pos.isolated_margin == pytest.approx(20.0)  # 40의 절반 해제
    pnl = (105.0 - 100.0) * 1.0
    fee = 105.0 * 1.0 * MAKER
    assert broker._get_wallet() == pytest.approx(wallet_before + pnl - fee)


# -- funding ---------------------------------------------------------------------------


async def test_funding_sign_long_pays_positive_rate(broker, db):
    await open_long(broker, db)
    funding_ts = T0 + FUNDING_INTERVAL_MS  # 8h 경계
    db.execute(
        "INSERT INTO funding_rates (symbol, ts, rate) VALUES (?, ?, ?)",
        ("BTCUSDT", funding_ts, 0.0001),
    )
    wallet_before = broker._get_wallet()
    seed_1m(db, "BTCUSDT", [(funding_ts, 100.0, 100.5, 99.8, 100.2)])
    broker.settle(now_ms=funding_ts + MIN)
    # long + 양수 rate = 지불: cash_flow = -1 × 0.0001 × (1 × 100) = -0.01
    assert broker._get_wallet() == pytest.approx(wallet_before - 0.01)
    payment = db.execute("SELECT * FROM funding_payments")[0]
    assert payment["symbol"] == "BTCUSDT" and payment["side"] == "long"
    assert payment["payment"] == pytest.approx(-0.01)


async def test_funding_sign_short_receives_positive_rate(broker, db):
    seed_1m(db, "SOLUSDT", [(T0, 100.0, 100.4, 99.6, 100.0)])
    plan_id = make_plan(db, symbol="SOLUSDT", side="short")
    await broker.place_order(
        entry_req(plan_id, symbol="SOLUSDT", side="sell", qty=10.0, price=101.0,
                  leverage=3)
    )
    seed_1m(db, "SOLUSDT", [(T0 + MIN, 100.8, 101.6, 100.4, 101.2)])
    broker.settle(now_ms=T0 + 2 * MIN)
    assert (await broker.get_positions())[0].side == "short"
    funding_ts = T0 + FUNDING_INTERVAL_MS
    wallet_before = broker._get_wallet()
    seed_1m(db, "SOLUSDT", [(funding_ts, 100.0, 100.5, 99.5, 100.0)])
    broker.settle(now_ms=funding_ts + MIN)
    # short + 양수 rate(기본 0.0001) = 수취: +0.0001 × (10 × 100) = +0.1
    assert broker._get_wallet() == pytest.approx(wallet_before + 0.1)


# -- TTL -------------------------------------------------------------------------------


async def test_order_ttl_expiry(db):
    settings = make_settings(order_ttl_bars=2)  # 1m TF → TTL 2분
    broker = PaperBroker(db, None, settings)
    broker.clock = lambda: T0 + MIN
    seed_1m(db, "BTCUSDT", [(T0, 101.0, 101.5, 100.5, 101.0)])
    plan_id = make_plan(db)
    order = await broker.place_order(entry_req(plan_id, qty=2.0, price=95.0))
    assert order.status == "open", order.reason
    # TTL 경계 이후 봉 — low가 관통해도 만료가 우선
    seed_1m(db, "BTCUSDT", [
        (T0 + 2 * MIN, 100.0, 100.5, 99.5, 100.0),
        (T0 + 3 * MIN, 99.0, 99.5, 94.0, 95.5),
    ])
    changed = broker.settle(now_ms=T0 + 4 * MIN)
    expired = [o for o in changed if o.id == order.id]
    assert expired and expired[0].status == "expired"
    assert "TTL" in expired[0].reason


# -- idempotency --------------------------------------------------------------------------


async def test_client_order_id_idempotent(broker, db):
    seed_1m(db, "BTCUSDT", [(T0, 101.0, 101.5, 100.5, 101.0)])
    plan_id = make_plan(db)
    coid = f"{plan_id}-entry-0-0"
    first = await broker.place_order(entry_req(plan_id, price=100.0, coid=coid))
    second = await broker.place_order(entry_req(plan_id, price=100.0, coid=coid))
    assert first.status == "open"
    assert second.id == first.id  # 재제출 → 기존 주문 반환
    rows = db.execute(
        "SELECT COUNT(*) AS n FROM paper_orders WHERE client_order_id = ?", (coid,)
    )
    assert rows[0]["n"] == 1  # 재기동 후 중복 주문 0건


# -- withdrawal skim (복리 금지) ------------------------------------------------------------


async def test_withdrawal_skims_realized_profit_once_per_day(broker, db):
    await open_long(broker, db, qty=1.0, price=100.0, leverage=5)
    tp = await broker.place_order(
        OrderRequest(symbol="BTCUSDT", side="sell", qty=1.0, limit_price=120.0,
                     reduce_only=True)
    )
    assert tp.status == "open", tp.reason
    seed_1m(db, "BTCUSDT", [(T0 + 2 * MIN, 119.0, 121.0, 118.0, 120.5)])
    broker.settle(now_ms=T0 + 3 * MIN)
    assert not await broker.get_positions()
    wallet = broker._get_wallet()
    assert wallet > SEED  # 실현 이익 반영

    skimmed = broker.skim_withdrawal()
    assert skimmed == pytest.approx(wallet - SEED)
    assert broker._get_wallet() == pytest.approx(SEED)  # 시드 고정 (복리 금지)
    ledger = db.execute("SELECT * FROM withdrawal_ledger")
    assert len(ledger) == 1
    assert ledger[0]["amount"] == pytest.approx(skimmed)
    assert "복리 금지" in ledger[0]["reason"]
    # 같은 UTC 일 재호출 → 0
    assert broker.skim_withdrawal() == 0.0


async def test_withdrawal_ignores_unrealized_profit(broker, db):
    await open_long(broker, db, qty=1.0, price=100.0, leverage=5)
    # 가격 급등 → 미실현 이익 크지만 실현 잔고는 seed − fee (마진은 잠김)
    seed_1m(db, "BTCUSDT", [(T0 + 2 * MIN, 150.0, 151.0, 149.0, 150.0)])
    broker.settle(now_ms=T0 + 3 * MIN)
    assert (await broker.get_balance()).unrealized_pnl > 0
    assert broker.skim_withdrawal() == 0.0  # 실현 잔고 기준 출금 없음
    assert not db.execute("SELECT * FROM withdrawal_ledger")


async def test_loss_is_not_withdrawn(broker, db):
    await open_long(broker, db, qty=1.0, price=100.0, leverage=5)
    await broker.place_order(
        OrderRequest(symbol="BTCUSDT", side="sell", qty=1.0, limit_price=95.0,
                     reduce_only=True, aggressive=True)
    )
    seed_1m(db, "BTCUSDT", [(T0 + 2 * MIN, 95.0, 95.5, 94.0, 94.5)])
    broker.settle(now_ms=T0 + 3 * MIN)
    assert broker._get_wallet() < SEED
    assert broker.skim_withdrawal() == 0.0  # 손실은 출금 없음 (사후 반전 없음)


# -- balance / snapshot ------------------------------------------------------------------


async def test_balance_and_snapshot_shapes(broker, db):
    await open_long(broker, db, qty=1.0, price=100.0, leverage=5)
    bal = await broker.get_balance()
    assert bal.margin_used == pytest.approx(20.0)
    assert bal.wallet_balance == pytest.approx(SEED - 100.0 * MAKER)
    assert bal.available == pytest.approx(bal.wallet_balance - 20.0)
    snap = broker.snapshot()
    row = db.execute("SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 1")[0]
    assert row["wallet_balance"] == pytest.approx(snap["wallet_balance"])
    assert row["margin_used"] == pytest.approx(20.0)
    assert row["total_value"] == pytest.approx(
        snap["wallet_balance"] + snap["unrealized_pnl"]
    )


async def test_insufficient_margin_rejected(broker, db):
    seed_1m(db, "BTCUSDT", [(T0, 101.0, 101.5, 100.5, 101.0)])
    plan_id = make_plan(db)
    # 1000 BTC × 100 / 5 = 20,000 USDT 증거금 > 시드
    order = await broker.place_order(entry_req(plan_id, qty=1_000.0, price=100.0))
    assert order.status == "rejected"
    assert "증거금 부족" in order.reason


async def test_symbol_filter_rounding_and_step_rejection(broker, db):
    seed_1m(db, "BTCUSDT", [(T0, 101.0, 101.5, 100.5, 101.0)])
    plan_id = make_plan(db)
    # qty 0.0005 < stepSize 0.001 → 내림 0 → 거부
    order = await broker.place_order(entry_req(plan_id, qty=0.0005, price=100.0))
    assert order.status == "rejected"
    # 가격 tick(0.1) 반올림 + 수량 step(0.001) 내림
    order = await broker.place_order(entry_req(plan_id, qty=1.0014, price=99.97))
    assert order.status == "open", order.reason
    assert order.limit_price == pytest.approx(100.0)
    assert order.qty == pytest.approx(1.001)


# -- settle이 1m 봉을 직접 최신화 (운영 갭 회귀: timeframes에 1m 없음) ---------------


async def test_settle_refreshes_1m_via_loader(db, settings):
    """운영에선 아무도 1m을 받지 않는다 — settle이 loader로 직접 당겨와야
    지정가 체결/TTL이 작동한다 (2026-07-15 운영 결함 회귀)."""

    class FakeLoader:
        def __init__(self):
            self.calls: list[tuple[str, str, int]] = []

        def get_ohlcv(self, symbol, timeframe="15m", limit=500):
            self.calls.append((symbol, timeframe, limit))
            # 실제 loader처럼 fetch 결과를 캐시에 쓴다: 발주 다음 봉이 관통.
            seed_1m(db, symbol, [(T0 + MIN, 100.4, 100.6, 99.0, 100.2)])

    loader = FakeLoader()
    broker = PaperBroker(db, loader, settings)
    broker.clock = lambda: T0 + MIN

    seed_1m(db, "BTCUSDT", [(T0, 101.0, 101.5, 100.5, 101.0)])
    plan_id = make_plan(db)
    order = await broker.place_order(entry_req(plan_id, qty=1.0, price=100.0))
    assert order.status == "open"

    changed = broker.settle(now_ms=T0 + 2 * MIN)

    assert loader.calls and loader.calls[0][:2] == ("BTCUSDT", "1m")
    assert any(o.id == order.id and o.status == "filled" for o in changed)


async def test_settle_survives_loader_failure(db, settings):
    """시세 소스 장애 시 settle은 죽지 않고 캐시만으로 동작 (기존 폴백)."""

    class BrokenLoader:
        def get_ohlcv(self, *a, **k):
            raise RuntimeError("network down")

    broker = PaperBroker(db, BrokenLoader(), settings)
    broker.clock = lambda: T0 + MIN
    seed_1m(db, "BTCUSDT", [(T0, 101.0, 101.5, 100.5, 101.0)])
    plan_id = make_plan(db)
    order = await broker.place_order(entry_req(plan_id, qty=1.0, price=100.0))
    seed_1m(db, "BTCUSDT", [(T0 + MIN, 100.4, 100.6, 99.0, 100.2)])
    changed = broker.settle(now_ms=T0 + 2 * MIN)  # raise 없이
    assert any(o.id == order.id and o.status == "filled" for o in changed)
