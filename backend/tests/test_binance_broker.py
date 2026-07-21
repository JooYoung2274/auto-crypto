"""BinanceBroker 테스트 — httpx.MockTransport, 네트워크·실키 없음.

커버: 키 없이 live 기동 거부, 테스트넷 base URL, HMAC 서명·recvWindow·
서버시간 동기, post-only(GTX)/aggressive(GTC) 매핑, 심볼 필터 반올림·
minNotional, 플랜 없는 주문 거부(ABC 공통), 안전장치(노셔널 상한,
rolling-24h 주문 수, 일손실 서킷브레이커 → reduce-only 킬스위치),
429/418 백오프, 기동 리컨실(isolated+레버리지), 스탑엑싯 체이스
(cancel-replace K회 → reduce-only IOC 폴백).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import urllib.parse

import httpx
import pytest

from app.broker.base import OrderRequest
from app.broker.binance import (
    MAX_RETRIES,
    RECV_WINDOW,
    TESTNET_URL,
    BinanceBroker,
    BinanceConfigError,
)
from app.config import Settings
from app.db import Database

# asyncio_mode=auto (pytest.ini) — async 테스트는 마크 없이 실행된다.

API_KEY = "test-api-key"
API_SECRET = "test-api-secret"
PRICE = 100.0
SERVER_OFFSET_MS = 120_000  # 서버시간 = 로컬 + 2분 (드리프트 시뮬)


def make_settings(**overrides) -> Settings:
    kwargs = dict(
        trading_mode="live",
        binance_api_key=API_KEY,
        binance_api_secret=API_SECRET,
        live_max_order_usdt=10_000.0,
        db_path=":memory:",
    )
    kwargs.update(overrides)
    return Settings(_env_file=None, **kwargs)


class MockBinanceApi:
    """Stateful httpx.MockTransport handler emulating Binance fapi."""

    def __init__(self):
        self.order_posts: list[dict] = []
        self.order_post_attempts = 0
        self.order_429_remaining = 0
        self.order_418_remaining = 0
        self.order_5xx_remaining = 0  # POST → HTTP 500 (모호한 실패)
        self.order_1021_remaining = 0  # POST → -1021 (recvWindow 밖) 후 재시도
        self.order_query_absent = False  # GET /fapi/v1/order → 404 (미접수)
        self.order_query_5xx = False  # GET /fapi/v1/order → 500 (조회 실패)
        self.cancels: list[dict] = []
        self.leverage_calls: list[dict] = []
        self.margin_type_calls: list[dict] = []
        self.signature_checks: list[bool] = []
        self.api_keys_seen: list[str] = []
        self.timestamps_seen: list[int] = []
        self.order_response_status = "NEW"  # GTX 주문 응답 상태
        self.query_status = "NEW"  # GET /fapi/v1/order 응답 상태
        self.time_requests = 0

    def _params(self, request: httpx.Request) -> dict:
        return dict(urllib.parse.parse_qsl(request.url.query.decode()))

    def _verify_signed(self, request: httpx.Request) -> dict:
        """서명 검증 + recvWindow/timestamp/API 키 기록."""
        raw = request.url.query.decode()
        assert "&signature=" in raw
        payload, signature = raw.rsplit("&signature=", 1)
        expected = hmac.new(
            API_SECRET.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        self.signature_checks.append(signature == expected)
        self.api_keys_seen.append(request.headers.get("X-MBX-APIKEY", ""))
        params = dict(urllib.parse.parse_qsl(payload))
        assert params["recvWindow"] == str(RECV_WINDOW)
        self.timestamps_seen.append(int(params["timestamp"]))
        return params

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if path == "/fapi/v1/time":
            self.time_requests += 1
            return httpx.Response(
                200, json={"serverTime": int(time.time() * 1000) + SERVER_OFFSET_MS}
            )

        if path == "/fapi/v1/ticker/price":
            params = self._params(request)
            return httpx.Response(
                200, json={"symbol": params.get("symbol", ""), "price": str(PRICE)}
            )

        if path == "/fapi/v2/account":
            self._verify_signed(request)
            return httpx.Response(
                200,
                json={
                    "totalWalletBalance": "10000.0",
                    "availableBalance": "9000.0",
                    "totalPositionInitialMargin": "1000.0",
                    "totalUnrealizedProfit": "50.0",
                },
            )

        if path == "/fapi/v2/positionRisk":
            self._verify_signed(request)
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "0.010",
                        "entryPrice": "100.0",
                        "leverage": "10",
                        "isolatedMargin": "0.1",
                        "liquidationPrice": "90.36",
                        "markPrice": "101.0",
                        "unRealizedProfit": "0.01",
                    },
                    {"symbol": "ETHUSDT", "positionAmt": "0.000", "entryPrice": "0.0",
                     "leverage": "5", "isolatedMargin": "0", "liquidationPrice": "0",
                     "markPrice": "0", "unRealizedProfit": "0"},
                ],
            )

        if path == "/fapi/v1/order" and request.method == "POST":
            self.order_post_attempts += 1
            if self.order_5xx_remaining > 0:
                self.order_5xx_remaining -= 1
                return httpx.Response(500, json={"code": -1000, "msg": "server error"})
            if self.order_1021_remaining > 0:
                self.order_1021_remaining -= 1
                return httpx.Response(
                    400,
                    json={"code": -1021, "msg": "Timestamp outside recvWindow"},
                )
            if self.order_429_remaining > 0:
                self.order_429_remaining -= 1
                return httpx.Response(429, json={"code": -1003, "msg": "rate limit"})
            if self.order_418_remaining > 0:
                self.order_418_remaining -= 1
                return httpx.Response(418, json={"code": -1003, "msg": "banned"})
            params = self._verify_signed(request)
            self.order_posts.append(params)
            status = (
                "FILLED"
                if params.get("timeInForce") in ("IOC", "GTC")
                else self.order_response_status
            )
            return httpx.Response(
                200,
                json={
                    "orderId": 1000 + len(self.order_posts),
                    "symbol": params["symbol"],
                    "clientOrderId": params.get("newClientOrderId", "auto"),
                    "status": status,
                    "price": params["price"],
                    "origQty": params["quantity"],
                    "executedQty": params["quantity"] if status == "FILLED" else "0",
                    "avgPrice": params["price"] if status == "FILLED" else "0",
                },
            )

        if path == "/fapi/v1/order" and request.method == "GET":
            params = self._verify_signed(request)
            if self.order_query_5xx:
                return httpx.Response(500, json={"code": -1000, "msg": "server error"})
            if self.order_query_absent:
                return httpx.Response(404, json={"code": -2013, "msg": "Order does not exist"})
            filled = self.query_status == "FILLED"
            return httpx.Response(
                200,
                json={
                    "orderId": 999,
                    "symbol": params["symbol"],
                    "clientOrderId": params.get("origClientOrderId", ""),
                    "status": self.query_status,
                    "price": str(PRICE),
                    "origQty": "0.01",
                    "executedQty": "0.01" if filled else "0",
                    "avgPrice": str(PRICE) if filled else "0",
                    "reduceOnly": True,
                },
            )

        if path == "/fapi/v1/order" and request.method == "DELETE":
            params = self._verify_signed(request)
            self.cancels.append(params)
            return httpx.Response(
                200,
                json={
                    "orderId": 999,
                    "symbol": params["symbol"],
                    "clientOrderId": params.get("origClientOrderId", ""),
                    "status": "CANCELED",
                    "price": str(PRICE),
                    "origQty": "0.01",
                    "executedQty": "0",
                },
            )

        if path == "/fapi/v1/openOrders":
            self._verify_signed(request)
            return httpx.Response(200, json=[])

        if path == "/fapi/v1/leverage":
            params = self._verify_signed(request)
            self.leverage_calls.append(params)
            return httpx.Response(200, json={"symbol": params["symbol"],
                                             "leverage": params["leverage"]})

        if path == "/fapi/v1/marginType":
            params = self._verify_signed(request)
            self.margin_type_calls.append(params)
            return httpx.Response(200, json={"code": 200, "msg": "success"})

        return httpx.Response(404, json={"msg": f"unknown path {path}"})


@pytest.fixture
def api() -> MockBinanceApi:
    return MockBinanceApi()


def make_broker(api, settings=None, **kwargs) -> BinanceBroker:
    return BinanceBroker(
        settings or make_settings(),
        transport=httpx.MockTransport(api.handler),
        retry_base_delay=0.0,
        **kwargs,
    )


@pytest.fixture
async def broker(api):
    b = make_broker(api, plan_lookup=lambda plan_id: "approved")
    yield b
    await b.aclose()


def entry_req(**overrides) -> OrderRequest:
    # 0.02 BTC × 60,000 = 1,200 USDT — minNotional(100) 이상, 기본 노셔널 상한 이하.
    kwargs = dict(
        symbol="BTCUSDT", side="buy", qty=0.02, limit_price=60_000.0,
        plan_id=1, client_order_id="1-entry-0-0",
    )
    kwargs.update(overrides)
    return OrderRequest(**kwargs)


# -- config guard (live 키 없이 기동 거부) ---------------------------------------------


def test_missing_keys_raise_config_error():
    with pytest.raises(BinanceConfigError):
        BinanceBroker(make_settings(binance_api_key="", binance_api_secret=""))
    with pytest.raises(BinanceConfigError):
        BinanceBroker(make_settings(binance_api_secret=""))


def test_testnet_base_url_flag(api):
    b = make_broker(api, make_settings(binance_testnet=True))
    assert b.base_url == TESTNET_URL


# -- signing / time sync ------------------------------------------------------------------


async def test_hmac_signature_and_api_key_header(broker, api):
    order = await broker.place_order(entry_req())
    assert order.status == "open"
    assert api.signature_checks and all(api.signature_checks)
    assert api.api_keys_seen[-1] == API_KEY


async def test_server_time_sync_offsets_timestamp(broker, api):
    await broker.place_order(entry_req())
    assert api.time_requests == 1  # 오프셋은 캐시
    local_now = int(time.time() * 1000)
    ts = api.timestamps_seen[-1]
    # timestamp ≈ 로컬 + 서버 오프셋 (recvWindow 밖으로 밀리지 않게 동기)
    assert abs(ts - (local_now + SERVER_OFFSET_MS)) < 5_000
    await broker.get_balance()
    assert api.time_requests == 1  # 재조회 없음


# -- order mapping --------------------------------------------------------------------------


async def test_passive_order_is_post_only_gtx(broker, api):
    await broker.place_order(entry_req())
    body = api.order_posts[0]
    assert body["timeInForce"] == "GTX"  # post-only maker
    assert body["reduceOnly"] == "false"
    assert body["type"] == "LIMIT"
    assert body["newClientOrderId"] == "1-entry-0-0"


async def test_aggressive_reduce_only_is_gtc_crossing(broker, api):
    order = await broker.place_order(
        entry_req(side="sell", reduce_only=True, aggressive=True,
                  client_order_id="1-stop-0-0")
    )
    assert order.status == "filled"  # mock: GTC/IOC → FILLED
    body = api.order_posts[0]
    assert body["timeInForce"] == "GTC"
    assert body["reduceOnly"] == "true"


async def test_symbol_filter_rounding_and_min_notional(broker, api):
    order = await broker.place_order(
        entry_req(qty=0.0114, limit_price=60_000.03)
    )
    assert order.status == "open"
    body = api.order_posts[0]
    assert float(body["price"]) == pytest.approx(60_000.0)  # tick 0.1 반올림
    assert float(body["quantity"]) == pytest.approx(0.011)  # step 0.001 내림

    # 0.001 × 60,000 = 60 USDT < minNotional 100 → 거부
    rejected = await broker.place_order(entry_req(qty=0.001))
    assert rejected.status == "rejected"
    assert "minNotional" in rejected.reason
    assert len(api.order_posts) == 1  # 전송 안 됨


# -- plan gate (ABC 공통: 시나리오 없는 주문은 브로커가 거부) --------------------------------


async def test_planless_order_rejected_locally(api):
    b = make_broker(api)  # plan_lookup 없음 → 안전 기본값
    try:
        no_plan = await b.place_order(entry_req(plan_id=None, client_order_id=None))
        assert no_plan.status == "rejected"
        assert "시나리오" in no_plan.reason
        with_plan = await b.place_order(entry_req())
        assert with_plan.status == "rejected"  # 조회 불가 → 거부
        assert "플랜" in with_plan.reason
        assert api.order_post_attempts == 0
    finally:
        await b.aclose()


async def test_rejected_plan_status_blocks_order(api):
    b = make_broker(api, plan_lookup=lambda plan_id: "rejected")
    try:
        order = await b.place_order(entry_req())
        assert order.status == "rejected"
        assert "플랜" in order.reason
        assert api.order_post_attempts == 0
    finally:
        await b.aclose()


# -- safety guards ---------------------------------------------------------------------------


async def test_max_order_notional_guard(api):
    b = make_broker(
        api,
        make_settings(live_max_order_usdt=100.0),
        plan_lookup=lambda plan_id: "approved",
    )
    try:
        # 0.002 × 60,000 = 120 USDT > live_max_order_usdt 100 → 거부
        order = await b.place_order(entry_req(qty=0.002))
        assert order.status == "rejected"
        assert "live_max_order_usdt" in order.reason
        assert api.order_post_attempts == 0
    finally:
        await b.aclose()


async def test_rolling_24h_order_limit(api):
    b = make_broker(
        api,
        make_settings(live_daily_order_limit=2),
        plan_lookup=lambda plan_id: "approved",
    )
    try:
        first = await b.place_order(entry_req(client_order_id="1-entry-0-0"))
        second = await b.place_order(entry_req(client_order_id="1-entry-1-0"))
        third = await b.place_order(entry_req(client_order_id="1-entry-2-0"))
        assert first.status == "open" and second.status == "open"
        assert third.status == "rejected"
        assert "24" in third.reason
        assert len(api.order_posts) == 2
        # 24h 경과 시뮬 → 창에서 밀려나 다시 허용
        b._order_times[0] -= 90_000
        b._order_times[1] -= 90_000
        fourth = await b.place_order(entry_req(client_order_id="1-entry-3-0"))
        assert fourth.status == "open"
    finally:
        await b.aclose()


async def test_daily_loss_kill_switch_reduce_only_mode(broker, api):
    # seed 10,000 × live_max_loss_pct 5% = 500 USDT
    assert broker.check_daily_loss(-499.0) is False
    assert broker.check_daily_loss(-500.0) is True
    assert broker.kill_switch
    entry = await broker.place_order(entry_req())
    assert entry.status == "rejected"
    assert "킬스위치" in entry.reason
    # reduce-only 청산 주문은 계속 허용
    exit_order = await broker.place_order(
        entry_req(side="sell", reduce_only=True, aggressive=True,
                  client_order_id="1-stop-0-1")
    )
    assert exit_order.status == "filled"


# -- backoff -----------------------------------------------------------------------------------


async def test_429_backoff_then_success(broker, api):
    api.order_429_remaining = 1
    order = await broker.place_order(entry_req())
    assert order.status == "open"
    assert api.order_post_attempts == 2


async def test_418_backoff_then_success(broker, api):
    api.order_418_remaining = 1
    order = await broker.place_order(entry_req())
    assert order.status == "open"
    assert api.order_post_attempts == 2


async def test_429_exhausted_rejects_order(broker, api):
    api.order_429_remaining = 10
    order = await broker.place_order(entry_req())
    assert order.status == "rejected"
    assert "429" in order.reason
    assert api.order_post_attempts == MAX_RETRIES + 1


# -- reconcile ----------------------------------------------------------------------------------


async def test_reconcile_sets_isolated_and_leverage_caps(broker, api):
    result = await broker.reconcile()
    settings = broker.settings
    assert {c["symbol"] for c in api.margin_type_calls} == set(settings.universe)
    assert all(c["marginType"] == "ISOLATED" for c in api.margin_type_calls)
    by_symbol = {c["symbol"]: int(c["leverage"]) for c in api.leverage_calls}
    assert by_symbol["BTCUSDT"] == settings.btc_max_leverage  # 10
    assert by_symbol["ETHUSDT"] == settings.alt_max_leverage  # 5
    assert by_symbol["SOLUSDT"] == settings.alt_max_leverage
    assert result["open_orders"] == []
    positions = result["positions"]
    assert len(positions) == 1  # positionAmt 0 제외
    assert positions[0].symbol == "BTCUSDT" and positions[0].side == "long"


# -- balance / positions parsing -------------------------------------------------------------------


async def test_balance_and_position_parsing(broker):
    bal = await broker.get_balance()
    assert bal.wallet_balance == 10_000.0
    assert bal.available == 9_000.0
    assert bal.margin_used == 1_000.0
    assert bal.unrealized_pnl == 50.0
    positions = await broker.get_positions()
    pos = positions[0]
    assert (pos.qty, pos.leverage, pos.liq_price) == (0.010, 10, 90.36)
    assert pos.mark_price == 101.0


# -- stop-exit chase ----------------------------------------------------------------------------------


async def test_stop_exit_chase_cancel_replace_then_ioc_fallback(broker, api):
    api.order_response_status = "NEW"  # 패시브 reduce-only 리밋은 계속 미체결
    api.query_status = "NEW"
    order = await broker.stop_exit_chase(
        "BTCUSDT", "sell", 0.01, plan_id=7, attempts=2, wait_seconds=0.0
    )
    # cancel-replace 2회 후 IOC 폴백
    assert len(api.order_posts) == 3
    assert [p["timeInForce"] for p in api.order_posts] == ["GTX", "GTX", "IOC"]
    assert all(p["reduceOnly"] == "true" for p in api.order_posts)
    assert len(api.cancels) == 2
    assert api.order_posts[0]["newClientOrderId"] == "7-stop-exit-0-0"
    assert api.order_posts[2]["newClientOrderId"] == "7-stop-exit-0-2"
    assert order.status == "filled"  # IOC는 체결


async def test_stop_exit_chase_returns_early_when_filled(broker, api):
    api.query_status = "FILLED"  # 첫 대기 후 체결 확인
    order = await broker.stop_exit_chase(
        "BTCUSDT", "sell", 0.01, plan_id=7, attempts=3, wait_seconds=0.0
    )
    assert order.status == "filled"
    assert len(api.order_posts) == 1  # 재발주 없음
    assert not api.cancels


async def test_stop_exit_chase_skips_wait_on_immediate_expired(broker, api):
    """finding 8: 크래시장에서 GTX post-only가 즉시 EXPIRED되면 wait_seconds를
    태우지 않고 곧장 다음 시도/IOC 폴백으로 넘어가야 한다 (버그였다면 999초
    sleep에 걸려 이 테스트가 멈춘다)."""
    api.order_response_status = "EXPIRED"  # 크로스 → 즉시 소멸
    order = await broker.stop_exit_chase(
        "BTCUSDT", "sell", 0.01, plan_id=7, attempts=2, wait_seconds=999.0
    )
    assert [p["timeInForce"] for p in api.order_posts] == ["GTX", "GTX", "IOC"]
    assert not api.cancels  # 레스팅 주문이 없었으니 취소도 없음
    assert order.status == "filled"  # IOC 체결


# -- live order mirror + settle (db 주입 시, finding 1/2/4/7) ---------------------------


@pytest.fixture
def live_db():
    db = Database(":memory:")
    db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status) "
        "VALUES ('BTCUSDT', 'long', '{}', 'approved')"
    )
    yield db
    db.close()


def make_db_broker(api, db, settings=None, **kwargs) -> BinanceBroker:
    return BinanceBroker(
        settings or make_settings(),
        transport=httpx.MockTransport(api.handler),
        retry_base_delay=0.0,
        db=db,
        **kwargs,
    )


async def test_live_order_is_mirrored_into_paper_orders(api, live_db):
    """finding 1: place_order가 범용 paper_orders에 미러 행을 남긴다
    (plan_id/leg 필드 파싱 포함) — 모니터/트레이더가 라이브를 관리할 수 있게."""
    broker = make_db_broker(api, live_db)
    try:
        order = await broker.place_order(entry_req())
        assert order.status == "open"
        rows = live_db.execute(
            "SELECT * FROM paper_orders WHERE client_order_id = '1-entry-0-0'"
        )
        assert len(rows) == 1
        r = rows[0]
        assert r["status"] == "open"
        assert r["plan_id"] == 1
        assert r["leg_kind"] == "entry" and r["leg_index"] == 0
        assert r["reduce_only"] == 0
        # 반환된 Order.id는 (모니터가 쓰는) 로컬 미러 행 id.
        assert order.id == str(r["id"])
    finally:
        await broker.aclose()


async def test_live_settle_reconciles_fill_from_exchange(api, live_db):
    """finding 1: settle()이 오픈 미러 주문을 거래소 상태로 리컨실하고 상태가
    바뀐 Order를 반환한다 (모니터의 settle 훅 계약)."""
    broker = make_db_broker(api, live_db)
    try:
        await broker.place_order(entry_req())
        api.query_status = "FILLED"  # 거래소가 이제 체결 보고
        changed = await asyncio.to_thread(broker.settle, None)
        assert len(changed) == 1
        assert changed[0].status == "filled"
        assert changed[0].filled_qty == pytest.approx(0.01)
        row = live_db.execute(
            "SELECT status FROM paper_orders WHERE client_order_id = '1-entry-0-0'"
        )[0]
        assert row["status"] == "filled"
    finally:
        await broker.aclose()


async def test_live_cancel_by_mirror_row_id_maps_to_client_order_id(api, live_db):
    """finding 1: 모니터는 미러 행의 로컬 id로 cancel_order를 부른다 →
    거래소에는 origClientOrderId로 취소하고 미러 행을 갱신한다."""
    broker = make_db_broker(api, live_db)
    try:
        await broker.place_order(entry_req())
        row_id = live_db.execute(
            "SELECT id FROM paper_orders WHERE client_order_id = '1-entry-0-0'"
        )[0]["id"]
        res = await broker.cancel_order(str(row_id), "BTCUSDT")
        assert res.status == "cancelled"
        assert api.cancels[-1].get("origClientOrderId") == "1-entry-0-0"
        assert "orderId" not in api.cancels[-1]
        row = live_db.execute(
            "SELECT status FROM paper_orders WHERE client_order_id = '1-entry-0-0'"
        )[0]
        assert row["status"] == "cancelled"
    finally:
        await broker.aclose()


async def test_live_snapshot_writes_portfolio_snapshot_row(api, live_db):
    """finding 2: snapshot()이 portfolio_snapshots에 기록한다 (일손실 기준선 +
    /portfolio 히스토리 소스) — 라이브에서 hasattr(broker,'snapshot') 경로 동작."""
    broker = make_db_broker(api, live_db)
    try:
        snap = await asyncio.to_thread(broker.snapshot)
        assert snap["wallet_balance"] == 10_000.0
        rows = live_db.execute("SELECT * FROM portfolio_snapshots")
        assert len(rows) == 1
        assert rows[0]["wallet_balance"] == 10_000.0
        assert rows[0]["total_value"] == pytest.approx(10_000.0 + 50.0)
    finally:
        await broker.aclose()


async def test_live_ambiguous_5xx_adopts_existing_order(api, live_db):
    """finding 4: 타임아웃/5xx 후 origClientOrderId로 조회해 거래소에 실제로
    접수됐으면 채택 (rejected로 보고해 중복 재제출하는 일 방지)."""
    broker = make_db_broker(api, live_db)
    try:
        api.order_5xx_remaining = 5  # POST는 계속 500
        api.query_status = "NEW"  # 하지만 조회하면 존재 (접수됨)
        order = await broker.place_order(entry_req())
        assert order.status == "open"  # 거부 아님 — 채택
        assert "실패" not in order.reason
        # 미러도 open으로 남는다.
        row = live_db.execute(
            "SELECT status FROM paper_orders WHERE client_order_id = '1-entry-0-0'"
        )[0]
        assert row["status"] == "open"
    finally:
        await broker.aclose()


async def test_live_ambiguous_5xx_rejects_when_order_absent(api, live_db):
    """finding 4: 조회 결과 거래소에 없으면 그제서야 rejected."""
    broker = make_db_broker(api, live_db)
    try:
        api.order_5xx_remaining = 5
        api.order_query_absent = True  # 조회 404 → 미접수 확인
        order = await broker.place_order(entry_req())
        assert order.status == "rejected"
        assert "500" in order.reason
    finally:
        await broker.aclose()


async def test_live_minus_1021_refreshes_time_and_retries(api, live_db):
    """finding 5: 서명 요청이 -1021(recvWindow 밖)이면 서버시간을 재동기하고
    1회 재시도한다 (클럭 스텝으로 서명 엔드포인트가 영구 고장나지 않게)."""
    broker = make_db_broker(api, live_db)
    try:
        await broker.place_order(entry_req())  # 오프셋 워밍
        assert api.time_requests == 1
        api.order_1021_remaining = 1  # 다음 POST는 -1021 1회
        order = await broker.place_order(entry_req(client_order_id="1-entry-1-0"))
        assert order.status == "open"  # 재동기 후 재시도 성공
        assert api.time_requests == 2  # 오프셋 재조회됨
    finally:
        await broker.aclose()


async def test_live_kill_switch_and_counter_persist_across_restart(api, live_db):
    """finding 7: 킬스위치와 rolling-24h 주문 카운터는 paper_state에 지속되어
    재기동/모드 토글 후에도 복원된다 (조용한 리셋 금지)."""
    broker = make_db_broker(api, live_db)
    try:
        await broker.place_order(entry_req())  # 주문 시각 1건 기록
        assert broker.check_daily_loss(-10_000.0) is True  # 킬스위치 발동
    finally:
        await broker.aclose()

    # 같은 db로 새 브로커 인스턴스 → 지속 상태 복원.
    broker2 = make_db_broker(api, live_db)
    try:
        assert broker2.kill_switch is True
        assert len(broker2._order_times) == 1
        rejected = await broker2.place_order(entry_req(client_order_id="1-entry-9-0"))
        assert rejected.status == "rejected"
        assert "킬스위치" in rejected.reason
    finally:
        await broker2.aclose()


# -- GTX 크로싱 미러 + pending-unknown + IOC 폴백 실패 (finding #6/#7/#16) ----------------


async def test_gtx_immediate_expiry_mirrors_rejected(api, live_db):
    """finding #6: GTX(post-only) 진입 레그가 배치 즉시 크로싱으로 EXPIRED되면
    'expired'가 아니라 'rejected'로 미러한다 — 모니터의 원가격 재큐 무한 루프를
    끊는다 (rejected로 끝난 레그는 재큐하지 않음)."""
    broker = make_db_broker(api, live_db)
    try:
        api.order_response_status = "EXPIRED"  # 크로스 → 즉시 소멸
        order = await broker.place_order(entry_req())
        assert order.status == "rejected"
        assert "크로싱" in order.reason
        row = live_db.execute(
            "SELECT status FROM paper_orders WHERE client_order_id = '1-entry-0-0'"
        )[0]
        assert row["status"] == "rejected"
    finally:
        await broker.aclose()


async def test_ambiguous_placement_query_failure_mirrors_pending_open(api, live_db):
    """finding #7: 모호한 발주(5xx) + 채택 조회 자체가 실패하면 rejected가 아니라
    pending-unknown(open)으로 미러하고, 이후 미접수가 확정되면 settle이
    rejected로 해소한다."""
    broker = make_db_broker(api, live_db)
    try:
        api.order_5xx_remaining = 5  # POST 계속 500
        api.order_query_5xx = True  # 채택 조회도 500 (같은 네트워크 장애)
        order = await broker.place_order(entry_req())
        assert order.status == "open"
        assert "발주 확인 불가" in order.reason
        row = live_db.execute(
            "SELECT status, reason FROM paper_orders "
            "WHERE client_order_id = '1-entry-0-0'"
        )[0]
        assert row["status"] == "open" and "발주 확인 불가" in row["reason"]

        api.order_5xx_remaining = 0
        api.order_query_5xx = False
        api.order_query_absent = True  # 조회 404 → 미접수 확정
        changed = await asyncio.to_thread(broker.settle, None)
        assert any(c.status == "rejected" for c in changed)
        row = live_db.execute(
            "SELECT status FROM paper_orders WHERE client_order_id = '1-entry-0-0'"
        )[0]
        assert row["status"] == "rejected"
    finally:
        await broker.aclose()


async def test_stop_exit_chase_ioc_failure_mirrors_rejected(api, live_db):
    """finding #16: IOC 폴백 POST가 실패하면 예외를 밖으로 던지지 않고 rejected로
    미러해 모니터의 가드가 볼 수 있게 한다."""
    broker = make_db_broker(api, live_db)
    try:
        api.query_status = "NEW"  # 패시브 리밋 미체결 → cancel-replace 후 IOC
        api.order_5xx_remaining = 9  # 모든 POST 500 → IOC 폴백도 실패
        order = await broker.stop_exit_chase(
            "BTCUSDT", "sell", 0.01, plan_id=1, attempts=1, wait_seconds=0.0
        )
        assert order.status == "rejected"
        assert "IOC 폴백 실패" in order.reason
    finally:
        await broker.aclose()


async def test_stop_exit_chase_uses_parseable_generation_coids(api, live_db):
    """finding #11/#12: 라이브 체이스 coid는 정규형 {plan}-stop-exit-0-{n}이며
    재호출마다 세대가 올라가 신선한 clOrdId를 쓴다."""
    from app.agents.trader import parse_client_order_id

    broker = make_db_broker(api, live_db)
    try:
        api.query_status = "NEW"
        await broker.stop_exit_chase(
            "BTCUSDT", "sell", 0.01, plan_id=1, attempts=2, wait_seconds=0.0
        )
        coids = [
            r["client_order_id"]
            for r in live_db.execute(
                "SELECT client_order_id FROM paper_orders "
                "WHERE client_order_id LIKE '%-stop-exit-%' ORDER BY id"
            )
        ]
        assert coids == ["1-stop-exit-0-0", "1-stop-exit-0-1", "1-stop-exit-0-2"]
        for c in coids:
            parsed = parse_client_order_id(c)
            assert parsed is not None and parsed[0] == 1 and parsed[1] == "stop-exit"
    finally:
        await broker.aclose()
