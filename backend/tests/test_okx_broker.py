"""OKXBroker + OKXSource 테스트 — httpx.MockTransport, 네트워크·실키 없음.

test_binance_broker.py의 목 트랜스포트 패턴을 OKX 규약으로 옮긴다. 커버:
키 3종(key/secret/passphrase) 없이 live 기동 거부, base64 HMAC 서명
(OK-ACCESS-KEY/SIGN/TIMESTAMP/PASSPHRASE) + 데모 헤더(x-simulated-trading),
심볼/타임프레임 매핑, ctVal 계약 환산(양방향), 플랜 게이트, 안전장치(노셔널
상한·rolling-24h·킬스위치·지속), 라이브 미러(발주/취소/정산), 격리마진
set-leverage/set-margin-mode, DataLoader의 OKX 소스 선택+캐시 shape,
OKX 에러 봉투(code!='0'), 스탑엑싯 체이스(post_only 거부 시 대기 미소모).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import time

import httpx
import pandas as pd
import pytest

from app.broker.base import OrderRequest
from app.broker.okx import (
    BASE_URL,
    MAX_RETRIES,
    OKX_CT_VAL,
    OKXBroker,
    OKXConfigError,
    OKXError,
    from_okx_symbol,
    to_okx_symbol,
)
from app.config import Settings
from app.data.loader import DataLoader
from app.data.sources.okx import OKX_BASE, OKXSource, to_okx_bar
from app.db import Database
from tests.conftest import make_perp_ohlcv, ts_ms

# asyncio_mode=auto (pytest.ini) — async 테스트는 마크 없이 실행된다.

API_KEY = "test-okx-key"
API_SECRET = "test-okx-secret"
API_PASSPHRASE = "test-okx-pass"
PRICE = 100.0
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def make_settings(**overrides) -> Settings:
    kwargs = dict(
        trading_mode="live",
        okx_api_key=API_KEY,
        okx_api_secret=API_SECRET,
        okx_api_passphrase=API_PASSPHRASE,
        live_max_order_usdt=10_000.0,
        db_path=":memory:",
    )
    kwargs.update(overrides)
    return Settings(_env_file=None, **kwargs)


DEFAULT_POSITIONS = [
    {
        "instId": "BTC-USDT-SWAP",
        "pos": "2",  # 2계약 × ctVal 0.01 = 0.02 BTC
        "posSide": "long",
        "avgPx": "100.0",
        "lever": "10",
        "margin": "0.1",
        "liqPx": "90.36",
        "markPx": "101.0",
        "upl": "0.01",
    },
    {
        "instId": "ETH-USDT-SWAP",
        "pos": "5",  # 5계약 × ctVal 0.1 = 0.5 ETH
        "posSide": "long",
        "avgPx": "3000.0",
        "lever": "5",
        "margin": "1.0",
        "liqPx": "2700.0",
        "markPx": "3010.0",
        "upl": "0.5",
    },
    {  # 0계약 — 반환에서 제외돼야
        "instId": "SOL-USDT-SWAP",
        "pos": "0",
        "posSide": "long",
        "avgPx": "0",
        "lever": "5",
        "margin": "0",
        "liqPx": "0",
        "markPx": "0",
        "upl": "0",
    },
]


class MockOKXApi:
    """Stateful httpx.MockTransport handler emulating OKX v5 REST."""

    def __init__(self):
        self.order_posts: list[dict] = []
        self.order_post_attempts = 0
        self.order_429_remaining = 0
        self.cancels: list[dict] = []
        self.leverage_calls: list[dict] = []
        # 계정 포지션 모드 — 'net_mode'(단방향, posSide 불필요) 기본.
        self.pos_mode = "net_mode"
        # 서명/헤더 감사 기록.
        self.sign_checks: list[bool] = []
        self.api_keys_seen: list[str] = []
        self.passphrases_seen: list[str] = []
        self.timestamps_seen: list[str] = []
        self.demo_flags: list[str | None] = []
        # 응답 제어 플래그.
        self.order_scode = "0"  # 모든 주문에 적용되는 sCode (거부 시뮬)
        self.post_only_scode = "0"  # post_only 주문 전용 sCode (즉시 크로스 거부)
        self.query_state = "live"  # GET /trade/order 상태
        self.public_error: tuple[str, str] | None = None  # ticker code!='0'
        self.positions = [dict(p) for p in DEFAULT_POSITIONS]

    # -- 서명/헤더 검증 --------------------------------------------------------
    def _verify_signed(self, request: httpx.Request) -> None:
        ts = request.headers.get("OK-ACCESS-TIMESTAMP", "")
        path = request.url.path
        query = request.url.query.decode()
        request_path = f"{path}?{query}" if query else path
        body = request.content.decode()
        prehash = f"{ts}{request.method.upper()}{request_path}{body}"
        expected = base64.b64encode(
            hmac.new(API_SECRET.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        self.sign_checks.append(request.headers.get("OK-ACCESS-SIGN") == expected)
        self.api_keys_seen.append(request.headers.get("OK-ACCESS-KEY", ""))
        self.passphrases_seen.append(request.headers.get("OK-ACCESS-PASSPHRASE", ""))
        self.timestamps_seen.append(ts)
        self.demo_flags.append(request.headers.get("x-simulated-trading"))

    @staticmethod
    def _ok(data) -> httpx.Response:
        return httpx.Response(200, json={"code": "0", "msg": "", "data": data})

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if path == "/api/v5/market/ticker":
            self.demo_flags.append(request.headers.get("x-simulated-trading"))
            if self.public_error is not None:
                code, msg = self.public_error
                return httpx.Response(200, json={"code": code, "msg": msg, "data": []})
            return self._ok([{"instId": "BTC-USDT-SWAP", "last": str(PRICE)}])

        if path == "/api/v5/account/balance" and method == "GET":
            self._verify_signed(request)
            return self._ok(
                [
                    {
                        "totalEq": "10000.0",
                        "imr": "1000.0",
                        "upl": "50.0",
                        "details": [
                            {
                                "ccy": "USDT",
                                "eq": "10000.0",
                                "availEq": "9000.0",
                                "frozenBal": "1000.0",
                                "upl": "50.0",
                            }
                        ],
                    }
                ]
            )

        if path == "/api/v5/account/positions" and method == "GET":
            self._verify_signed(request)
            return self._ok(self.positions)

        if path == "/api/v5/trade/order" and method == "POST":
            self.order_post_attempts += 1
            if self.order_429_remaining > 0:
                self.order_429_remaining -= 1
                return httpx.Response(429, json={"code": "50011", "msg": "rate limit"})
            self._verify_signed(request)
            body = json.loads(request.content)
            self.order_posts.append(body)
            ordtype = body.get("ordType", "")
            scode = self.order_scode
            if ordtype == "post_only" and self.post_only_scode != "0":
                scode = self.post_only_scode
            state = "filled" if ordtype in ("ioc", "limit") else "live"
            return self._ok(
                [
                    {
                        "ordId": str(1000 + len(self.order_posts)),
                        "clOrdId": body.get("clOrdId", ""),
                        "sCode": scode,
                        "sMsg": "" if scode == "0" else "order rejected",
                        "state": state,
                        "accFillSz": body["sz"] if state == "filled" else "0",
                        "avgPx": body["px"] if state == "filled" else "",
                    }
                ]
            )

        if path == "/api/v5/trade/order" and method == "GET":
            self._verify_signed(request)
            params = dict(request.url.params)
            filled = self.query_state == "filled"
            return self._ok(
                [
                    {
                        "instId": params.get("instId", ""),
                        "ordId": "999",
                        "clOrdId": params.get("clOrdId", ""),
                        "state": self.query_state,
                        "side": "buy",
                        "px": str(PRICE),
                        "sz": "2",
                        "accFillSz": "2" if filled else "0",
                        "avgPx": str(PRICE) if filled else "",
                        "reduceOnly": "false",
                    }
                ]
            )

        if path == "/api/v5/trade/cancel-order" and method == "POST":
            self._verify_signed(request)
            body = json.loads(request.content)
            self.cancels.append(body)
            return self._ok(
                [{"ordId": "999", "clOrdId": body.get("clOrdId", ""), "sCode": "0"}]
            )

        if path == "/api/v5/trade/orders-pending" and method == "GET":
            self._verify_signed(request)
            return self._ok([])

        if path == "/api/v5/account/config" and method == "GET":
            self._verify_signed(request)
            return self._ok([{"posMode": self.pos_mode, "acctLv": "2"}])

        if path == "/api/v5/account/set-leverage" and method == "POST":
            self._verify_signed(request)
            self.leverage_calls.append(json.loads(request.content))
            return self._ok([{"lever": "10"}])

        return httpx.Response(404, json={"code": "404", "msg": f"unknown {path}"})


@pytest.fixture
def api() -> MockOKXApi:
    return MockOKXApi()


def make_broker(api, settings=None, **kwargs) -> OKXBroker:
    return OKXBroker(
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
    # 0.02 BTC × 60,000 = 1,200 USDT. ctVal 0.01 → sz 2계약.
    kwargs = dict(
        symbol="BTCUSDT", side="buy", qty=0.02, limit_price=60_000.0,
        plan_id=1, client_order_id="1-entry-0-0",
    )
    kwargs.update(overrides)
    return OrderRequest(**kwargs)


# -- config guard (live 키 3종 없이 기동 거부) ------------------------------------------


def test_missing_keys_raise_config_error():
    with pytest.raises(OKXConfigError):
        OKXBroker(make_settings(okx_api_key=""))
    with pytest.raises(OKXConfigError):
        OKXBroker(make_settings(okx_api_secret=""))
    with pytest.raises(OKXConfigError):
        OKXBroker(make_settings(okx_api_passphrase=""))  # 패스프레이즈도 필수


def test_default_base_url(api):
    b = make_broker(api)
    assert b.base_url == BASE_URL


# -- signing / headers ------------------------------------------------------------------


async def test_base64_hmac_signature_and_auth_headers(broker, api):
    order = await broker.place_order(entry_req())
    assert order.status == "open"
    assert api.sign_checks and all(api.sign_checks)  # base64 HMAC 서명 일치
    assert api.api_keys_seen[-1] == API_KEY
    assert api.passphrases_seen[-1] == API_PASSPHRASE
    assert TS_RE.match(api.timestamps_seen[-1])  # ISO8601 ms UTC


async def test_demo_header_present_only_when_okx_demo(api):
    b = make_broker(
        api, make_settings(okx_demo=True), plan_lookup=lambda plan_id: "approved"
    )
    try:
        await b.place_order(entry_req())
        assert api.demo_flags and all(f == "1" for f in api.demo_flags)
    finally:
        await b.aclose()


async def test_demo_header_absent_by_default(broker, api):
    await broker.place_order(entry_req())
    assert api.demo_flags and all(f is None for f in api.demo_flags)


# -- symbol / timeframe mapping ---------------------------------------------------------


def test_symbol_mapping_round_trip():
    assert to_okx_symbol("BTCUSDT") == "BTC-USDT-SWAP"
    assert to_okx_symbol("DOGEUSDT") == "DOGE-USDT-SWAP"
    assert from_okx_symbol("BTC-USDT-SWAP") == "BTCUSDT"
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"):
        assert from_okx_symbol(to_okx_symbol(sym)) == sym


def test_timeframe_mapping():
    assert to_okx_bar("15m") == "15m"
    assert to_okx_bar("4h") == "4H"
    assert to_okx_bar("1d") == "1Dutc"
    with pytest.raises(ValueError):
        to_okx_bar("3m")


# -- ctVal 계약 환산 (양방향) ------------------------------------------------------------


@pytest.mark.parametrize(
    "inst_id, ct",
    [
        ("BTC-USDT-SWAP", 0.01),
        ("ETH-USDT-SWAP", 0.1),
        ("SOL-USDT-SWAP", 1.0),
        ("XRP-USDT-SWAP", 100.0),
        ("DOGE-USDT-SWAP", 1000.0),
        ("ADA-USDT-SWAP", 100.0),
        ("LTC-USDT-SWAP", 1.0),
    ],
)
def test_contract_conversion_both_directions(inst_id, ct):
    assert OKX_CT_VAL[inst_id] == ct
    # 코인 → 계약 (내림), 계약 → 코인.
    assert OKXBroker._to_contracts(inst_id, 3 * ct) == 3
    assert OKXBroker._from_contracts(inst_id, 3) == pytest.approx(3 * ct)
    # 1계약 미만은 0계약으로 내림.
    assert OKXBroker._to_contracts(inst_id, ct * 0.5) == 0


async def test_place_order_qty_coins_to_contracts(broker, api):
    await broker.place_order(entry_req())  # 0.02 BTC / 0.01 = 2계약
    assert api.order_posts[0]["sz"] == "2"
    assert api.order_posts[0]["instId"] == "BTC-USDT-SWAP"


async def test_place_order_contract_conversion_per_symbol(broker, api):
    cases = [
        ("ETHUSDT", 0.5, 3_000.0, "5"),  # 0.5 / 0.1
        ("SOLUSDT", 3.0, 150.0, "3"),  # 3 / 1
        ("XRPUSDT", 300.0, 0.5, "3"),  # 300 / 100
        ("DOGEUSDT", 5000.0, 0.1, "5"),  # 5000 / 1000
    ]
    for i, (sym, qty, px, sz) in enumerate(cases):
        await broker.place_order(
            entry_req(symbol=sym, qty=qty, limit_price=px, client_order_id=f"1-e-{i}-0")
        )
        assert api.order_posts[-1]["sz"] == sz


async def test_get_positions_contracts_to_coins(broker):
    positions = await broker.get_positions()
    assert len(positions) == 2  # 0계약 SOL 제외
    by_symbol = {p.symbol: p for p in positions}
    assert by_symbol["BTCUSDT"].qty == pytest.approx(0.02)  # 2 × 0.01
    assert by_symbol["ETHUSDT"].qty == pytest.approx(0.5)  # 5 × 0.1
    assert by_symbol["BTCUSDT"].side == "long"
    assert by_symbol["BTCUSDT"].liq_price == 90.36


async def test_sub_contract_qty_rejected(broker, api):
    # 0.005 BTC < 1계약(ctVal 0.01) → 전송 없이 거부.
    order = await broker.place_order(entry_req(qty=0.005))
    assert order.status == "rejected"
    assert "계약" in order.reason
    assert api.order_post_attempts == 0


# -- order body shape (posSide / tdMode / ordType) --------------------------------------


async def test_passive_entry_is_post_only_isolated_long(broker, api):
    await broker.place_order(entry_req())
    body = api.order_posts[0]
    assert body["ordType"] == "post_only"  # 진입 = 패시브 maker
    assert body["tdMode"] == "isolated"
    assert body["posSide"] == "long"  # buy 진입 → long
    assert body["reduceOnly"] is False


async def test_aggressive_reduce_only_is_crossing_limit(broker, api):
    order = await broker.place_order(
        entry_req(side="sell", reduce_only=True, aggressive=True,
                  client_order_id="1-stop-0-0")
    )
    assert order.status == "filled"  # mock: limit(taker) → filled
    body = api.order_posts[0]
    assert body["ordType"] == "limit"
    assert body["reduceOnly"] is True
    assert body["posSide"] == "long"  # sell로 롱 청산 → posSide long


# -- plan gate (ABC 공통) ----------------------------------------------------------------


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


# -- safety guards -----------------------------------------------------------------------


async def test_max_order_notional_guard(api):
    b = make_broker(
        api, make_settings(live_max_order_usdt=100.0),
        plan_lookup=lambda plan_id: "approved",
    )
    try:
        # 0.02 × 60,000 = 1,200 USDT > 100 → 거부.
        order = await b.place_order(entry_req())
        assert order.status == "rejected"
        assert "live_max_order_usdt" in order.reason
        assert api.order_post_attempts == 0
    finally:
        await b.aclose()


async def test_rolling_24h_order_limit(api):
    b = make_broker(
        api, make_settings(live_daily_order_limit=2),
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
        # 24h 경과 시뮬 → 창에서 밀려나 다시 허용.
        b._order_times[0] -= 90_000
        b._order_times[1] -= 90_000
        fourth = await b.place_order(entry_req(client_order_id="1-entry-3-0"))
        assert fourth.status == "open"
    finally:
        await b.aclose()


async def test_daily_loss_kill_switch_reduce_only_mode(broker, api):
    # seed 10,000 × live_max_loss_pct 5% = 500 USDT.
    assert broker.check_daily_loss(-499.0) is False
    assert broker.check_daily_loss(-500.0) is True
    assert broker.kill_switch
    entry = await broker.place_order(entry_req())
    assert entry.status == "rejected"
    assert "킬스위치" in entry.reason
    # reduce-only 청산 주문은 계속 허용.
    exit_order = await broker.place_order(
        entry_req(side="sell", reduce_only=True, aggressive=True,
                  client_order_id="1-stop-0-1")
    )
    assert exit_order.status == "filled"


async def test_leverage_caps_on_reconcile(broker, api):
    result = await broker.reconcile()
    settings = broker.settings
    by_symbol = {c["instId"]: int(c["lever"]) for c in api.leverage_calls}
    assert by_symbol["BTC-USDT-SWAP"] == settings.btc_max_leverage  # 10
    assert by_symbol["ETH-USDT-SWAP"] == settings.alt_max_leverage  # 5
    assert by_symbol["SOL-USDT-SWAP"] == settings.alt_max_leverage
    assert all(c["mgnMode"] == "isolated" for c in api.leverage_calls)
    assert result["open_orders"] == []
    assert len(result["positions"]) == 2  # 0계약 제외


# -- isolated margin / leverage endpoints -----------------------------------------------


async def test_set_leverage_hits_isolated_endpoint(broker, api):
    await broker.set_leverage("BTCUSDT", 7)
    call = api.leverage_calls[-1]
    assert call["instId"] == "BTC-USDT-SWAP"
    assert call["lever"] == "7"
    assert call["mgnMode"] == "isolated"
    assert "posSide" not in call  # net_mode(단방향)에서는 posSide 없이 1회


async def test_set_leverage_long_short_mode_sets_both_sides(broker, api):
    # 양방향(long_short_mode) 계정: 격리 레버리지는 posSide 필수 — 방향별 2회
    # (실계정 400 재현 회귀, 2026-07-20).
    api.pos_mode = "long_short_mode"
    await broker.set_leverage("BTCUSDT", 7)
    calls = api.leverage_calls[-2:]
    assert {c["posSide"] for c in calls} == {"long", "short"}
    assert all(
        c["instId"] == "BTC-USDT-SWAP"
        and c["lever"] == "7"
        and c["mgnMode"] == "isolated"
        for c in calls
    )


async def test_set_margin_mode_isolated_is_noop_and_cross_rejected(broker, api):
    await broker.set_margin_mode("BTCUSDT", "isolated")  # 엔드포인트 호출 없음
    assert api.leverage_calls == []
    with pytest.raises(ValueError):
        await broker.set_margin_mode("BTCUSDT", "cross")


# -- backoff ------------------------------------------------------------------------------


async def test_429_backoff_then_success(broker, api):
    api.order_429_remaining = 1
    order = await broker.place_order(entry_req())
    assert order.status == "open"
    assert api.order_post_attempts == 2


async def test_429_exhausted_rejects_order(broker, api):
    api.order_429_remaining = 10
    order = await broker.place_order(entry_req())
    assert order.status == "rejected"
    assert api.order_post_attempts == MAX_RETRIES + 1


# -- balance parsing ----------------------------------------------------------------------


async def test_balance_parsing(broker):
    bal = await broker.get_balance()
    assert bal.wallet_balance == 10_000.0
    assert bal.available == 9_000.0
    assert bal.margin_used == 1_000.0
    assert bal.unrealized_pnl == 50.0


# -- OKX 에러 봉투 (code != '0') ---------------------------------------------------------


async def test_error_envelope_raises_with_message(broker, api):
    api.public_error = ("51001", "Instrument ID does not exist")
    with pytest.raises(OKXError) as exc:
        await broker.get_quote("BTCUSDT")
    assert "51001" in str(exc.value)
    assert "Instrument" in str(exc.value)


async def test_order_scode_rejection(broker, api):
    # 봉투 code '0'이지만 per-order sCode != '0' → 거부 (미접수).
    api.order_scode = "51008"  # 잔고 부족
    order = await broker.place_order(entry_req())
    assert order.status == "rejected"
    assert "sCode" in order.reason and "51008" in order.reason


# -- live order mirror + settle (db 주입 시) ---------------------------------------------


@pytest.fixture
def live_db():
    db = Database(":memory:")
    db.execute(
        "INSERT INTO trade_plans (symbol, side, plan_json, status) "
        "VALUES ('BTCUSDT', 'long', '{}', 'approved')"
    )
    yield db
    db.close()


def make_db_broker(api, db, settings=None, **kwargs) -> OKXBroker:
    return OKXBroker(
        settings or make_settings(),
        transport=httpx.MockTransport(api.handler),
        retry_base_delay=0.0,
        db=db,
        **kwargs,
    )


async def test_live_order_is_mirrored_into_paper_orders(api, live_db):
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
        assert order.id == str(r["id"])  # 반환 id = 로컬 미러 행 id
    finally:
        await broker.aclose()


async def test_live_settle_reconciles_fill_from_exchange(api, live_db):
    broker = make_db_broker(api, live_db)
    try:
        await broker.place_order(entry_req())
        api.query_state = "filled"  # 거래소가 이제 체결 보고
        changed = await asyncio.to_thread(broker.settle, None)
        assert len(changed) == 1
        assert changed[0].status == "filled"
        assert changed[0].filled_qty == pytest.approx(0.02)  # 2계약 × 0.01
        row = live_db.execute(
            "SELECT status FROM paper_orders WHERE client_order_id = '1-entry-0-0'"
        )[0]
        assert row["status"] == "filled"
    finally:
        await broker.aclose()


async def test_live_cancel_by_mirror_row_id_maps_to_client_order_id(api, live_db):
    broker = make_db_broker(api, live_db)
    try:
        await broker.place_order(entry_req())
        row_id = live_db.execute(
            "SELECT id FROM paper_orders WHERE client_order_id = '1-entry-0-0'"
        )[0]["id"]
        res = await broker.cancel_order(str(row_id), "BTCUSDT")
        assert res.status == "cancelled"
        # OKX clOrdId는 영숫자만 — 대시 제거된 정제본으로 취소.
        assert api.cancels[-1].get("clOrdId") == "1entry00"
        assert "ordId" not in api.cancels[-1]
        row = live_db.execute(
            "SELECT status FROM paper_orders WHERE client_order_id = '1-entry-0-0'"
        )[0]
        assert row["status"] == "cancelled"
    finally:
        await broker.aclose()


async def test_live_snapshot_writes_portfolio_snapshot_row(api, live_db):
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


async def test_live_kill_switch_and_counter_persist_across_restart(api, live_db):
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


# -- stop-exit chase ---------------------------------------------------------------------


async def test_stop_exit_chase_cancel_replace_then_ioc_fallback(broker, api):
    api.query_state = "live"  # 패시브 reduce-only 리밋은 계속 미체결
    order = await broker.stop_exit_chase(
        "BTCUSDT", "sell", 0.02, plan_id=7, attempts=2, wait_seconds=0.0
    )
    # post_only 체이스 2회 후 IOC 폴백.
    assert [p["ordType"] for p in api.order_posts] == ["post_only", "post_only", "ioc"]
    assert all(p["reduceOnly"] is True for p in api.order_posts)
    assert len(api.cancels) == 2
    assert order.status == "filled"  # IOC 체결


async def test_stop_exit_chase_returns_early_when_filled(broker, api):
    api.query_state = "filled"  # 첫 대기 후 체결 확인
    order = await broker.stop_exit_chase(
        "BTCUSDT", "sell", 0.02, plan_id=7, attempts=3, wait_seconds=0.0
    )
    assert order.status == "filled"
    assert len(api.order_posts) == 1  # 재발주 없음
    assert not api.cancels


async def test_stop_exit_chase_skips_wait_on_immediate_reject(broker, api):
    """스펙 §5 회귀: 크래시장에서 post_only reduce-only가 즉시 크로스로 거부되면
    (OKX sCode) wait_seconds를 태우지 않고 곧장 다음 시도/IOC 폴백으로 넘어간다
    (버그였다면 999초 sleep에 걸려 이 테스트가 멈춘다)."""
    api.post_only_scode = "51022"  # post_only가 유동성을 가져가 거부
    order = await broker.stop_exit_chase(
        "BTCUSDT", "sell", 0.02, plan_id=7, attempts=2, wait_seconds=999.0
    )
    assert [p["ordType"] for p in api.order_posts] == ["post_only", "post_only", "ioc"]
    assert not api.cancels  # 레스팅 주문이 없었으니 취소도 없음
    assert order.status == "filled"  # IOC 체결


# -- DataLoader OKX 소스 선택 + 캐시 shape ------------------------------------------------

_OKX_TF_MS = {"15m": 900_000}
COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume"]


def okx_candles_rows(df: pd.DataFrame, tf: str) -> list[list]:
    """OHLCV 프레임 → OKX 캔들 배열 (최신순, [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm])."""
    rows = []
    for idx, r in df.iterrows():
        open_ms = ts_ms(idx)
        rows.append(
            [
                str(open_ms),
                f"{r['open']:.8f}",
                f"{r['high']:.8f}",
                f"{r['low']:.8f}",
                f"{r['close']:.8f}",
                f"{r['volume']:.8f}",  # vol (계약수) — 미사용
                f"{r['volume']:.8f}",  # volCcy = 기초자산 수량 → volume
                f"{r.get('quote_volume', 0.0):.8f}",  # volCcyQuote
                "1",
            ]
        )
    return list(reversed(rows))  # OKX 응답은 최신순


def okx_candles_transport(df: pd.DataFrame, tf: str, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        assert request.url.path in (
            "/api/v5/market/candles",
            "/api/v5/market/history-candles",
        )
        return httpx.Response(200, json={"code": "0", "data": okx_candles_rows(df, tf)})

    return httpx.MockTransport(handler)


def test_loader_selects_okx_source_and_cache_shape(db, monkeypatch):
    raw = make_perp_ohlcv(n=192, seed=7, freq="15min", base_price=60_000.0)
    settings = make_settings(exchange="okx")
    calls: list = []
    loader = DataLoader(
        db,
        transport=okx_candles_transport(raw, "15m", calls),
        settings=settings,
    )
    # OKX 소스 선택 + base_url이 OKX로 바인딩.
    assert isinstance(loader._source, OKXSource)
    assert loader._base_url == OKX_BASE

    now = ts_ms(raw.index.max()) + _OKX_TF_MS["15m"]
    monkeypatch.setattr(DataLoader, "_now_ms", lambda self: now)
    df = loader.get_ohlcv("BTCUSDT", "15m")

    assert calls and calls[0] == "/api/v5/market/candles"  # OKX 경로로 위임
    # 캐시 write shape는 Binance 경로와 동일 — 같은 컬럼/타입.
    assert list(df.columns) == COLUMNS
    assert (df.dtypes == float).all()
    assert isinstance(df.index, pd.DatetimeIndex)
    assert len(df) == len(raw)
    pd.testing.assert_series_equal(
        df["close"], raw["close"],
        check_freq=False, check_exact=False, check_index_type=False,
    )
    # ohlcv_cache 행이 Binance 경로와 같은 스키마로 기록됨.
    cached = db.execute(
        "SELECT symbol, timeframe, ts, open, high, low, close, volume, quote_volume "
        "FROM ohlcv_cache WHERE symbol = 'BTCUSDT' ORDER BY ts"
    )
    assert len(cached) == len(raw)
    assert cached[0]["timeframe"] == "15m"
