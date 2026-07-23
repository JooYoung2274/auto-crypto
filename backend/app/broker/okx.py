"""OKX USDT 무기한(SWAP) 라이브 브로커 — base64 HMAC 서명 httpx 클라이언트.

Binance가 주(primary) 거래소이고 OKX는 추가 옵션 (``CA_EXCHANGE=okx``).
:class:`BinanceBroker`와 동일한 ABC 표면·안전장치·라이브 미러를 갖되 OKX 규약에
맞춘다:

- 키 3종(key/secret/**passphrase**) 없이 기동 거부 (:class:`OKXConfigError`).
- 서명: ``base64(hmac_sha256(secret, timestamp+method+requestPath+body))``,
  헤더 OK-ACCESS-KEY / OK-ACCESS-SIGN / OK-ACCESS-TIMESTAMP(ISO ms) /
  OK-ACCESS-PASSPHRASE. ``okx_demo=True``면 ``x-simulated-trading: 1``.
- 심볼 매핑 ``BTCUSDT`` ↔ ``BTC-USDT-SWAP``, 주문 수량은 **계약 수(sz)** — 코인
  수량을 ctVal로 나눠 계약으로, 포지션은 계약을 ctVal로 곱해 코인으로 환산.
- 격리마진 ``tdMode='isolated'``, posSide long/short(헤지 모드).
- 안전장치는 Binance와 동일: 노셔널 상한, rolling-24h 주문 수, 레버리지 캡,
  일손실 서킷브레이커 → reduce-only 킬스위치(paper_state 지속), 플랜 게이트.
- 라이브 미러: 발주/취소/정산 주문을 범용 paper_orders에 미러링 →
  PositionMonitor/Trader가 paper와 동일 경로로 관리 (스펙 §5). db 없으면 no-op.
- 429 백오프, OKX 에러 봉투(code!='0' 또는 주문 sCode!='0') → 사유와 함께 거부.

진입·TP 레그는 post_only(maker), aggressive exit는 크로싱 limit(taker),
스탑엑싯 폴백은 reduce-only IOC.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import math
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from ..config import Settings
from .base import (
    Balance,
    Broker,
    Order,
    OrderRequest,
    Position,
    Quote,
    ledger_only_skim,
)

BASE_URL = "https://www.okx.com"

#: 429(레이트리밋) 재시도 횟수 (첫 시도 제외).
MAX_RETRIES = 2

#: 스탑엑싯 체이스 기본 cancel-replace 횟수 (그 후 reduce-only IOC 폴백).
STOP_CHASE_ATTEMPTS = 3

#: 모호한 발주(타임아웃/5xx) + 채택 조회 실패 시 미러 사유 마커 — settle이
#: clOrdId로 최종 해소한다 (finding #7). 절대 rejected로 단정하지 않는다.
_PENDING_UNKNOWN = "발주 확인 불가"

#: paper_state 지속 키 (재기동 복원, 스펙 §5) — Binance와 공유 (동시 활성 브로커 1개).
_KILL_KEY = "live_kill_switch"
_ORDER_TIMES_KEY = "live_order_times_json"

#: OKX instId별 계약 크기 (ctVal = 1계약당 코인 수, OKX 문서값 하드코딩).
OKX_CT_VAL: dict[str, float] = {
    "BTC-USDT-SWAP": 0.01,
    "ETH-USDT-SWAP": 0.1,
    "SOL-USDT-SWAP": 1.0,
    "XRP-USDT-SWAP": 100.0,
    "DOGE-USDT-SWAP": 1000.0,
    # 2026-07-20 사용자 요청 추가 (OKX instruments 실측값)
    "ADA-USDT-SWAP": 100.0,
    "LTC-USDT-SWAP": 1.0,
}

#: OKX 주문 상태 → ABC OrderStatus.
_STATE_MAP = {
    "live": "open",
    "partially_filled": "open",
    "filled": "filled",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "mmp_canceled": "cancelled",
}


class OKXConfigError(RuntimeError):
    """OKX API 키/시크릿/패스프레이즈 부재 — live 기동 거부."""


def to_okx_symbol(symbol: str) -> str:
    """``BTCUSDT`` → ``BTC-USDT-SWAP``."""
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}-USDT-SWAP"
    if symbol.endswith("USD"):
        return f"{symbol[:-3]}-USD-SWAP"
    return symbol


def from_okx_symbol(inst_id: str) -> str:
    """``BTC-USDT-SWAP`` → ``BTCUSDT``."""
    parts = inst_id.split("-")
    if len(parts) >= 2:
        return f"{parts[0]}{parts[1]}"
    return inst_id


def _ct_val(inst_id: str) -> float:
    return OKX_CT_VAL.get(inst_id, 1.0)


def _okx_clordid(coid: str | None) -> str | None:
    """OKX clOrdId는 영숫자만 허용(≤32자) — 대시 등 특수문자 제거."""
    if not coid:
        return None
    cleaned = "".join(ch for ch in coid if ch.isalnum())
    return cleaned[:32] or None


def _map_state(raw: str) -> str:
    return _STATE_MAP.get(str(raw).lower(), "rejected")


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def _leg_fields(coid: str | None) -> tuple[str | None, int | None]:
    """client_order_id ``{plan_id}-{leg_kind}-{leg_index}-{attempt}`` →
    (leg_kind, leg_index). 스킴을 안 따르면 (None, None) — 미러 컬럼용."""
    if not coid:
        return None, None
    parts = coid.split("-")
    if len(parts) < 4:
        return None, None
    try:
        int(parts[0])
        leg_index = int(parts[-2])
        int(parts[-1])
    except ValueError:
        return None, None
    return "-".join(parts[1:-2]), leg_index


class OKXBroker(Broker):
    """OKX USDT 무기한 라이브 브로커 (httpx, 주입 가능한 transport)."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str | None = None,
        db=None,
        plan_lookup: Callable[[int], str | None] | None = None,
        retry_base_delay: float = 0.5,
    ):
        if not (
            settings.okx_api_key
            and settings.okx_api_secret
            and settings.okx_api_passphrase
        ):
            raise OKXConfigError(
                "OKXBroker requires CA_OKX_API_KEY, CA_OKX_API_SECRET and "
                "CA_OKX_API_PASSPHRASE — 키 없이 live 기동 거부 (스펙 §0)"
            )
        if plan_lookup is None and db is not None:
            def plan_lookup(plan_id: int) -> str | None:  # noqa: F811
                rows = db.execute(
                    "SELECT status FROM trade_plans WHERE id = ?", (plan_id,)
                )
                return rows[0]["status"] if rows else None

        super().__init__(plan_lookup=plan_lookup)
        self.settings = settings
        self.db = db
        self.base_url = base_url or BASE_URL
        self._transport = transport
        self._client = httpx.AsyncClient(
            base_url=self.base_url, transport=transport, timeout=10.0
        )
        self._retry_base_delay = retry_base_delay
        #: rolling-24h 주문 타임스탬프 (epoch s).
        self._order_times: deque[float] = deque()
        self._pos_mode: str | None = None  # 계정 포지션 모드 캐시 (reconcile 시 조회)
        #: 일손실 서킷브레이커 — True면 reduce-only 주문만 허용.
        self.kill_switch: bool = False
        self._order_symbols: dict[str, str] = {}  # order id → symbol (cancel용)
        self._load_persisted()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- HTTP plumbing -------------------------------------------------------------
    def _demo_header(self) -> dict[str, str]:
        return {"x-simulated-trading": "1"} if self.settings.okx_demo else {}

    def _timestamp(self) -> str:
        """OKX ISO8601 밀리초 UTC 타임스탬프 (예: 2020-12-08T09:08:57.715Z)."""
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        prehash = f"{timestamp}{method.upper()}{request_path}{body}"
        mac = hmac.new(
            self.settings.okx_api_secret.encode(), prehash.encode(), hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode()

    async def _request_with_backoff(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        content: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> httpx.Response:
        """429(레이트리밋) 지수 백오프 재시도."""
        c = client or self._client
        resp: httpx.Response | None = None
        for attempt in range(MAX_RETRIES + 1):
            resp = await c.request(method, url, headers=headers, content=content)
            if resp.status_code == 429 and attempt < MAX_RETRIES:
                await asyncio.sleep(self._retry_base_delay * (2**attempt))
                continue
            break
        assert resp is not None
        resp.raise_for_status()
        return resp

    async def _public(
        self,
        path: str,
        params: dict | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> Any:
        request_path = path
        if params:
            request_path = f"{path}?{httpx.QueryParams(params)}"
        resp = await self._request_with_backoff(
            "GET", request_path, self._demo_header(), client=client
        )
        return _envelope_data(resp.json())

    async def _signed(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        body: dict | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> Any:
        request_path = path
        content: str | None = None
        if method.upper() == "GET" and params:
            request_path = f"{path}?{httpx.QueryParams(params)}"
        if body is not None:
            content = json.dumps(body)
        timestamp = self._timestamp()
        signature = self._sign(timestamp, method, request_path, content or "")
        headers = {
            "OK-ACCESS-KEY": self.settings.okx_api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.settings.okx_api_passphrase,
            "Content-Type": "application/json",
            **self._demo_header(),
        }
        resp = await self._request_with_backoff(
            method, request_path, headers, content=content, client=client
        )
        return _envelope_data(resp.json())

    def _temp_client(self) -> httpx.AsyncClient:
        """정산/스냅샷용 임시 AsyncClient — asyncio.to_thread(워커 스레드)에서
        호출되므로 공유 self._client(메인 루프 바인딩)를 재사용하지 못한다."""
        return httpx.AsyncClient(
            base_url=self.base_url, transport=self._transport, timeout=10.0
        )

    def _blocking(self, coro_factory) -> Any:
        return asyncio.run(coro_factory())

    # -- persistence (재기동 복원, 스펙 §5) --------------------------------------------
    def _load_persisted(self) -> None:
        if self.db is None:
            return
        rows = self.db.execute(
            "SELECT value FROM paper_state WHERE key = ?", (_KILL_KEY,)
        )
        if rows:
            self.kill_switch = str(rows[0]["value"]) == "1"
        rows = self.db.execute(
            "SELECT value FROM paper_state WHERE key = ?", (_ORDER_TIMES_KEY,)
        )
        if rows:
            try:
                cutoff = time.time() - 86_400.0
                times = [float(t) for t in json.loads(rows[0]["value"])]
                self._order_times = deque(t for t in times if t > cutoff)
            except (ValueError, TypeError):
                pass

    def _persist_kill_switch(self) -> None:
        if self.db is None:
            return
        self.db.execute(
            "INSERT OR REPLACE INTO paper_state (key, value) VALUES (?, ?)",
            (_KILL_KEY, "1" if self.kill_switch else "0"),
        )

    def _persist_order_times(self) -> None:
        if self.db is None:
            return
        self.db.execute(
            "INSERT OR REPLACE INTO paper_state (key, value) VALUES (?, ?)",
            (_ORDER_TIMES_KEY, json.dumps(list(self._order_times))),
        )

    def _record_order_time(self) -> None:
        self._order_times.append(time.time())
        self._persist_order_times()

    # -- reconcile / kill switch -------------------------------------------------------
    async def reconcile(self) -> dict:
        """부팅 리컨실 (스펙 §2): 심볼별 격리마진 + 레버리지 캡 설정 →
        오픈 주문/포지션 조회 반환. OKX는 격리모드를 주문 tdMode로 지정하므로
        set_margin_mode는 검증만 하고, set-leverage(mgnMode='isolated')로 캡을 건다."""
        for symbol in self.settings.universe:
            await self.set_margin_mode(symbol, "isolated")
            cap = (
                self.settings.btc_max_leverage
                if symbol == "BTCUSDT"
                else self.settings.alt_max_leverage
            )
            await self.set_leverage(symbol, cap)
        open_orders = await self.get_open_orders()
        positions = await self.get_positions()
        return {"open_orders": open_orders, "positions": positions}

    async def skim_withdrawal(self, now_ms: int) -> float:
        """장부 전용 출금 스윕 (실이체 없음) — 시드 초과 실현 수익을
        withdrawal_ledger에 격리한다 (규칙 §1). 거래소 잔고는 건드리지
        않는다. trader.settle이 UTC 일 1회 호출."""
        balance = await self.get_balance()
        return await asyncio.to_thread(
            ledger_only_skim, self.db, self.settings, balance, now_ms
        )

    def check_daily_loss(self, daily_realized_pnl: float) -> bool:
        """일손실 서킷브레이커 — 임계 초과 시 reduce-only 킬스위치 모드 진입
        (paper_state 지속, 재기동 복원)."""
        limit = self.settings.live_max_loss_pct * self.settings.initial_seed_usdt
        if daily_realized_pnl <= -limit and not self.kill_switch:
            self.kill_switch = True
            self._persist_kill_switch()
        return self.kill_switch

    def _rolling_order_count(self, now_s: float) -> int:
        while self._order_times and self._order_times[0] <= now_s - 86_400.0:
            self._order_times.popleft()
        return len(self._order_times)

    # -- contract-size 환산 --------------------------------------------------------------
    @staticmethod
    def _to_contracts(inst_id: str, qty_coins: float) -> float:
        """코인 수량 → 계약 수(sz). 정수 계약으로 내림 (초과 주문 방지)."""
        ct = _ct_val(inst_id)
        return math.floor(qty_coins / ct + 1e-9)

    @staticmethod
    def _from_contracts(inst_id: str, contracts: float) -> float:
        """계약 수 → 코인 수량."""
        return contracts * _ct_val(inst_id)

    # -- Broker interface -----------------------------------------------------------------
    async def get_quote(self, symbol: str) -> Quote:
        inst_id = to_okx_symbol(symbol)
        data = await self._public("/api/v5/market/ticker", {"instId": inst_id})
        row = data[0] if data else {}
        return Quote(
            symbol=symbol,
            price=float(row.get("last", 0.0)),
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    async def get_balance(self) -> Balance:
        data = await self._signed("GET", "/api/v5/account/balance")
        acct = data[0] if data else {}
        detail = None
        for d in acct.get("details", []):
            if d.get("ccy") == "USDT":
                detail = d
                break
        detail = detail or {}
        return Balance(
            wallet_balance=_f(detail.get("eq")) or _f(acct.get("totalEq")),
            available=_f(detail.get("availEq")) or _f(detail.get("availBal")),
            margin_used=_f(detail.get("frozenBal")) or _f(acct.get("imr")),
            unrealized_pnl=_f(detail.get("upl")) or _f(acct.get("upl")),
        )

    async def get_positions(self) -> list[Position]:
        data = await self._signed(
            "GET", "/api/v5/account/positions", {"instType": "SWAP"}
        )
        positions: list[Position] = []
        for p in data:
            contracts = _f(p.get("pos"))
            if contracts == 0.0:
                continue
            inst_id = str(p.get("instId", ""))
            pos_side = str(p.get("posSide", "")).lower()
            if pos_side not in ("long", "short"):
                pos_side = "long" if contracts > 0 else "short"
            positions.append(
                Position(
                    symbol=from_okx_symbol(inst_id),
                    side=pos_side,  # type: ignore[arg-type]
                    qty=self._from_contracts(inst_id, abs(contracts)),
                    avg_entry=_f(p.get("avgPx")),
                    leverage=int(_f(p.get("lever")) or 0),
                    isolated_margin=_f(p.get("margin")) or _f(p.get("imr")),
                    liq_price=_f(p.get("liqPx")),
                    mark_price=_f(p.get("markPx")),
                    unrealized_pnl=_f(p.get("upl")),
                )
            )
        return positions

    def _rejected(self, request: OrderRequest, reason: str) -> Order:
        return Order(
            id=f"rejected-{uuid.uuid4().hex[:8]}",
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            limit_price=request.limit_price,
            status="rejected",
            reduce_only=request.reduce_only,
            aggressive=request.aggressive,
            plan_id=request.plan_id,
            client_order_id=request.client_order_id,
            reason=reason,
        )

    def _pos_side(self, request: OrderRequest) -> str:
        """헤지 모드 posSide — 포지션 방향 기준. 진입은 주문 방향, 청산은 반대."""
        if request.reduce_only:
            return "long" if request.side == "sell" else "short"
        return "long" if request.side == "buy" else "short"

    def _order_from_payload(
        self, data: dict, request: OrderRequest, inst_id: str
    ) -> Order:
        # 미러/모니터가 파싱할 수 있게 원본(대시 포함) client_order_id를 유지한다
        # — OKX 응답 clOrdId는 영숫자만 남은 정제본이라 leg 파싱이 깨진다.
        coid = request.client_order_id or (
            str(data.get("clOrdId", "") or "") or None
        )
        order = Order(
            id=str(data.get("ordId", "")),
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            limit_price=request.limit_price,
            status=_map_state(data.get("state", "live")),  # type: ignore[arg-type]
            filled_qty=self._from_contracts(inst_id, _f(data.get("accFillSz"))),
            avg_fill_price=(_f(data.get("avgPx")) or None),
            reduce_only=request.reduce_only,
            aggressive=request.aggressive,
            plan_id=request.plan_id,
            client_order_id=coid,
        )
        if order.id:
            self._order_symbols[order.id] = request.symbol
        return order

    # -- order mirror (paper_orders) --------------------------------------------------
    def _mirror_row_to_order(self, r: dict) -> Order:
        return Order(
            id=str(r["id"]),
            symbol=r["symbol"],
            side=r["side"],
            qty=float(r["qty"]),
            limit_price=(
                None if r["limit_price"] is None else float(r["limit_price"])
            ),
            status=r["status"],
            filled_qty=float(r["filled_qty"] or 0.0),
            avg_fill_price=(
                None if r["avg_fill_price"] is None else float(r["avg_fill_price"])
            ),
            reduce_only=bool(r["reduce_only"]),
            aggressive=bool(r["aggressive"]),
            plan_id=r["plan_id"],
            client_order_id=r["client_order_id"],
            reason=r["reason"] or "",
        )

    def _mirror(self, order: Order, request: OrderRequest) -> Order:
        """거래소 응답/거부를 범용 paper_orders에 미러링. db 없으면 no-op."""
        if self.db is None:
            return order
        leg_kind, leg_index = _leg_fields(order.client_order_id)
        self.db.execute(
            "INSERT OR IGNORE INTO paper_orders (ts, symbol, side, qty, order_type, "
            "limit_price, filled_qty, avg_fill_price, reduce_only, aggressive, "
            "leverage, plan_id, leg_kind, leg_index, client_order_id, status, reason) "
            "VALUES (?, ?, ?, ?, 'limit', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(int(time.time() * 1000)),
                order.symbol,
                order.side,
                order.qty,
                order.limit_price,
                order.filled_qty,
                order.avg_fill_price,
                int(order.reduce_only),
                int(order.aggressive),
                request.leverage,
                order.plan_id,
                leg_kind,
                leg_index,
                order.client_order_id,
                order.status,
                order.reason,
            ),
        )
        if order.client_order_id:
            got = self.db.execute(
                "SELECT * FROM paper_orders WHERE client_order_id = ?",
                (order.client_order_id,),
            )
        else:
            got = self.db.execute(
                "SELECT * FROM paper_orders ORDER BY id DESC LIMIT 1"
            )
        return self._mirror_row_to_order(got[0]) if got else order

    async def place_order(self, request: OrderRequest) -> Order:
        # 멱등성 (미러 기반): 같은 client_order_id 재제출 → 기존 미러 주문 반환.
        if self.db is not None and request.client_order_id:
            rows = self.db.execute(
                "SELECT * FROM paper_orders WHERE client_order_id = ?",
                (request.client_order_id,),
            )
            if rows:
                return self._mirror_row_to_order(rows[0])

        request, reason = self.validate_order(request)
        if reason:
            return self._mirror(self._rejected(request, reason), request)

        # -- 안전장치 (신규 리스크를 여는 주문에만; exit는 절대 막지 않는다) -----
        if not request.reduce_only:
            if self.kill_switch:
                return self._mirror(
                    self._rejected(
                        request, "일손실 서킷브레이커 — reduce-only 킬스위치 모드"
                    ),
                    request,
                )
            notional = request.qty * request.limit_price
            if notional > self.settings.live_max_order_usdt:
                return self._mirror(
                    self._rejected(
                        request,
                        f"주문 노셔널 {notional:.2f} USDT > live_max_order_usdt "
                        f"{self.settings.live_max_order_usdt:g}",
                    ),
                    request,
                )
            now_s = time.time()
            if self._rolling_order_count(now_s) >= self.settings.live_daily_order_limit:
                return self._mirror(
                    self._rejected(
                        request,
                        f"rolling-24h 주문 한도 초과 ({self.settings.live_daily_order_limit}건)",
                    ),
                    request,
                )

        inst_id = to_okx_symbol(request.symbol)
        sz = self._to_contracts(inst_id, request.qty)
        if sz <= 0:
            return self._mirror(
                self._rejected(
                    request,
                    f"수량 {request.qty} < 1계약(ctVal {_ct_val(inst_id):g}) — 주문 불가",
                ),
                request,
            )
        body: dict[str, Any] = {
            "instId": inst_id,
            "tdMode": "isolated",
            "side": request.side,
            "posSide": self._pos_side(request),
            # 진입·TP = post_only(maker). aggressive exit = 크로싱 limit(taker).
            "ordType": "limit" if request.aggressive else "post_only",
            "px": _fmt(request.limit_price),
            "sz": _fmt(sz),
            "reduceOnly": request.reduce_only,
        }
        coid = _okx_clordid(request.client_order_id)
        if coid:
            body["clOrdId"] = coid
        try:
            data = await self._signed("POST", "/api/v5/trade/order", body=body)
        except OKXError as exc:
            return self._mirror(self._rejected(request, f"주문 실패: {exc}"), request)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code >= 500:
                return self._mirror(
                    await self._adopt_or_reject(request, f"주문 실패: HTTP {code}"),
                    request,
                )
            return self._mirror(
                self._rejected(request, f"주문 실패: HTTP {code}"), request
            )
        except httpx.HTTPError as exc:
            return self._mirror(
                await self._adopt_or_reject(request, f"주문 실패: {exc}"), request
            )
        row = data[0] if data else {}
        s_code = str(row.get("sCode", "0"))
        if s_code not in ("0", ""):
            return self._mirror(
                self._rejected(
                    request, f"주문 거부: OKX sCode {s_code} {row.get('sMsg', '')}"
                ),
                request,
            )
        self._record_order_time()
        return self._mirror(self._order_from_payload(row, request, inst_id), request)

    def _pending_unknown(self, request: OrderRequest, reason: str) -> Order:
        """발주 결과 미상 — 'open'으로 미러해 settle이 clOrdId로 해소하게 한다
        (finding #7). 거래소에 주문이 남아 있을 수 있어 rejected로 단정 금지."""
        return Order(
            id=f"pending-{uuid.uuid4().hex[:8]}",
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            limit_price=request.limit_price,
            status="open",
            reduce_only=request.reduce_only,
            aggressive=request.aggressive,
            plan_id=request.plan_id,
            client_order_id=request.client_order_id,
            reason=f"{_PENDING_UNKNOWN} — {reason} (정산에서 clOrdId로 해소)",
        )

    async def _adopt_or_reject(self, request: OrderRequest, reason: str) -> Order:
        """모호한 타임아웃/5xx 뒤: clOrdId로 조회해 실제로 접수됐으면 채택
        (중복 재제출 방지, 스펙 §2). 조회가 명확히 '미접수'(None)면 거부하되,
        조회 자체가 실패하면(같은 네트워크 장애) 주문이 남아 있을 수 있으므로
        pending-unknown(open)으로 미러 — settle이 해소한다 (finding #7)."""
        if request.client_order_id:
            try:
                live = await self.query_order(
                    request.symbol, request.client_order_id
                )
            except (httpx.HTTPError, OKXError):
                return self._pending_unknown(request, reason)
            if live is not None:
                self._record_order_time()
                return live
        return self._rejected(request, reason)

    async def query_order(
        self,
        symbol: str,
        client_order_id: str,
        client: httpx.AsyncClient | None = None,
    ) -> Order | None:
        """client_order_id로 주문 조회 — 멱등 재제출/정산 확인용 (스펙 §2)."""
        inst_id = to_okx_symbol(symbol)
        cl = _okx_clordid(client_order_id)
        try:
            data = await self._signed(
                "GET",
                "/api/v5/trade/order",
                {"instId": inst_id, "clOrdId": cl},
                client=client,
            )
        except OKXError:
            return None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 404):
                return None
            raise
        row = data[0] if data else None
        if not row:
            return None
        side = "buy" if str(row.get("side", "")).lower() == "buy" else "sell"
        req = OrderRequest(
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            qty=self._from_contracts(inst_id, _f(row.get("sz"))),
            limit_price=_f(row.get("px")),
            reduce_only=str(row.get("reduceOnly", "false")).lower() == "true",
            client_order_id=client_order_id,
        )
        return self._order_from_payload(row, req, inst_id)

    async def cancel_order(self, order_id: str, symbol: str | None = None) -> Order:
        # 모니터/트레이더는 미러 행의 로컬 id를 넘긴다 → coid로 매핑해 취소하고
        # 미러 행을 갱신한다. 체이스는 coid를 직접 넘긴다.
        row: dict | None = None
        coid: str | None = None
        if self.db is not None:
            if order_id.isdigit():
                rows = self.db.execute(
                    "SELECT * FROM paper_orders WHERE id = ?", (int(order_id),)
                )
            else:
                rows = self.db.execute(
                    "SELECT * FROM paper_orders WHERE client_order_id = ?",
                    (order_id,),
                )
            if rows:
                row = rows[0]
                coid = row["client_order_id"]
                symbol = symbol or row["symbol"]
        symbol = symbol or self._order_symbols.get(order_id)
        if symbol is None:
            raise ValueError(
                f"cancel_order requires symbol for unknown order {order_id}"
            )
        inst_id = to_okx_symbol(symbol)
        body: dict[str, Any] = {"instId": inst_id}
        if coid:
            body["clOrdId"] = _okx_clordid(coid)
        elif order_id.isdigit():
            body["ordId"] = order_id
        else:
            body["clOrdId"] = _okx_clordid(order_id)
        data = await self._signed("POST", "/api/v5/trade/cancel-order", body=body)
        cancel_row = data[0] if data else {}
        if row is not None and self.db is not None:
            self.db.execute(
                "UPDATE paper_orders SET status = 'cancelled', reason = ? WHERE id = ?",
                ("취소", row["id"]),
            )
        return Order(
            id=str(cancel_row.get("ordId", order_id) or order_id),
            symbol=symbol,
            side="buy",
            qty=0.0,
            limit_price=None,
            status="cancelled",
            client_order_id=str(cancel_row.get("clOrdId", "") or "") or coid,
        )

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        params: dict[str, Any] = {"instType": "SWAP"}
        if symbol:
            params["instId"] = to_okx_symbol(symbol)
        data = await self._signed("GET", "/api/v5/trade/orders-pending", params)
        orders: list[Order] = []
        for o in data:
            inst_id = str(o.get("instId", ""))
            side = "buy" if str(o.get("side", "")).lower() == "buy" else "sell"
            req = OrderRequest(
                symbol=from_okx_symbol(inst_id),
                side=side,  # type: ignore[arg-type]
                qty=self._from_contracts(inst_id, _f(o.get("sz"))),
                limit_price=_f(o.get("px")),
                reduce_only=str(o.get("reduceOnly", "false")).lower() == "true",
            )
            orders.append(self._order_from_payload(o, req, inst_id))
        return orders

    async def _position_mode(self) -> str:
        """계정 포지션 모드 조회 (캐시) — 'long_short_mode' | 'net_mode'.

        격리마진 set-leverage는 양방향(long_short_mode) 계정에서 posSide가
        필수라(없으면 400) 모드에 따라 호출 형태가 달라진다."""
        if self._pos_mode is None:
            cfg = await self._signed("GET", "/api/v5/account/config")
            self._pos_mode = str((cfg[0] if cfg else {}).get("posMode", "net_mode"))
        return self._pos_mode

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        base = {
            "instId": to_okx_symbol(symbol),
            "lever": str(int(leverage)),
            "mgnMode": "isolated",
        }
        if await self._position_mode() == "long_short_mode":
            # 양방향 모드: 방향별로 각각 설정해야 한다.
            for pos_side in ("long", "short"):
                await self._signed(
                    "POST",
                    "/api/v5/account/set-leverage",
                    body={**base, "posSide": pos_side},
                )
        else:
            await self._signed("POST", "/api/v5/account/set-leverage", body=base)

    async def set_margin_mode(self, symbol: str, mode: str = "isolated") -> None:
        # OKX는 격리마진을 주문 tdMode/레버리지 mgnMode로 지정한다 — 심볼별 별도
        # 토글 엔드포인트가 없으므로 여기서는 모드만 검증한다 (규칙 §1 격리 고정).
        if mode != "isolated":
            raise ValueError("마진 모드는 격리(isolated) 고정 (규칙 §1)")

    # -- settlement / snapshot (모니터/트레이더 훅) -------------------------------------
    def settle(self, now_ms: int | None = None) -> list[Order]:
        """오픈 미러 주문을 거래소 상태로 리컨실 (모니터 훅). db 없으면 no-op.
        asyncio.to_thread(워커 스레드)에서 호출 → 새 루프+임시 클라이언트."""
        if self.db is None:
            return []
        return self._blocking(lambda: self._settle_async(now_ms))

    async def _settle_async(self, now_ms: int | None) -> list[Order]:
        rows = self.db.execute(
            "SELECT * FROM paper_orders WHERE status = 'open' ORDER BY id"
        )
        changed: list[Order] = []
        async with self._temp_client() as client:
            for r in rows:
                coid = r["client_order_id"]
                if not coid:
                    continue
                try:
                    live = await self.query_order(r["symbol"], coid, client=client)
                except (httpx.HTTPError, OKXError):
                    continue
                if live is None:
                    # pending-unknown 행인데 거래소가 '없음'으로 답하면 미접수
                    # 확정 → rejected (finding #7). 일반 오픈 행은 그대로 둔다.
                    if _PENDING_UNKNOWN in (r["reason"] or ""):
                        self.db.execute(
                            "UPDATE paper_orders SET status = 'rejected', "
                            "reason = ? WHERE id = ?",
                            (f"{_PENDING_UNKNOWN} — 거래소 미접수 확정", r["id"]),
                        )
                        got = self.db.execute(
                            "SELECT * FROM paper_orders WHERE id = ?", (r["id"],)
                        )[0]
                        changed.append(self._mirror_row_to_order(got))
                    continue
                if live.status == r["status"]:
                    # 상태 불변이라도 부분 체결량이 늘었으면 미러에 반영한다
                    # (finding #10): 라이브 partially_filled는 'open'으로 남아
                    # filled_qty가 0으로 고이면 실제 포지션을 든 플랜을 TTL이
                    # abandon할 수 있다.
                    if live.filled_qty > float(r["filled_qty"] or 0.0):
                        self.db.execute(
                            "UPDATE paper_orders SET filled_qty = ?, "
                            "avg_fill_price = ? WHERE id = ?",
                            (live.filled_qty, live.avg_fill_price, r["id"]),
                        )
                    continue
                self.db.execute(
                    "UPDATE paper_orders SET status = ?, filled_qty = ?, "
                    "avg_fill_price = ?, reason = ? WHERE id = ?",
                    (
                        live.status,
                        live.filled_qty,
                        live.avg_fill_price,
                        live.reason or r["reason"] or "",
                        r["id"],
                    ),
                )
                got = self.db.execute(
                    "SELECT * FROM paper_orders WHERE id = ?", (r["id"],)
                )[0]
                changed.append(self._mirror_row_to_order(got))
        return changed

    def snapshot(self) -> dict:
        """계정 잔고 → portfolio_snapshots 기록 (PaperBroker.snapshot과 동일 shape).
        db 없으면 no-op. asyncio.to_thread(트레이더)로 호출 → 새 루프+임시 클라이언트."""
        if self.db is None:
            return {}
        return self._blocking(self._snapshot_async)

    async def _snapshot_async(self) -> dict:
        async with self._temp_client() as client:
            data = await self._signed(
                "GET", "/api/v5/account/balance", client=client
            )
        acct = data[0] if data else {}
        detail = None
        for d in acct.get("details", []):
            if d.get("ccy") == "USDT":
                detail = d
                break
        detail = detail or {}
        wallet = _f(detail.get("eq")) or _f(acct.get("totalEq"))
        available = _f(detail.get("availEq")) or _f(detail.get("availBal"))
        margin_used = _f(detail.get("frozenBal")) or _f(acct.get("imr"))
        upnl = _f(detail.get("upl")) or _f(acct.get("upl"))
        total = wallet + upnl
        self.db.execute(
            "INSERT INTO portfolio_snapshots (wallet_balance, available, margin_used, "
            "unrealized_pnl, funding_cum, total_value) VALUES (?, ?, ?, ?, ?, ?)",
            (wallet, available, margin_used, upnl, 0.0, total),
        )
        return {
            "wallet_balance": wallet,
            "available": available,
            "margin_used": margin_used,
            "unrealized_pnl": upnl,
            "funding_cum": 0.0,
            "total_value": total,
        }

    # -- stop-exit chase (스펙 §5) --------------------------------------------------------
    def _stop_exit_base(self, plan_id: int | None) -> int:
        """이 플랜의 기존 스탑엑싯 미러 행 수 = 다음 체이스 세대(generation).
        재호출마다 여기서 시작해 신선한 clOrdId를 쓴다 (finding #11/#16)."""
        if self.db is None or plan_id is None:
            return 0
        rows = self.db.execute(
            "SELECT COUNT(*) AS n FROM paper_orders WHERE plan_id = ? "
            "AND client_order_id LIKE '%-stop-exit-%'",
            (plan_id,),
        )
        return int(rows[0]["n"])

    async def stop_exit_chase(
        self,
        symbol: str,
        side: str,
        qty: float,
        *,
        plan_id: int | None = None,
        attempts: int = STOP_CHASE_ATTEMPTS,
        wait_seconds: float = 60.0,
        start_attempt: int | None = None,
    ) -> Order:
        """손절/청산회피 exit: reduce-only post_only 리밋 체이스 K회 → reduce-only
        IOC 크로싱 리밋 폴백 (한정 taker). 체결 성공/최종 폴백 주문을 반환.

        coid는 파싱 가능한 정규형 ``{plan_id}-stop-exit-0-{attempt}``이며 attempt는
        기존 스탑엑싯 행 수(세대)에서 시작한다 — 재호출 시 clOrdId 재사용/무한
        루프/중복 POST를 원천 차단한다 (finding #11/#12/#16)."""
        if plan_id is not None:
            base = (
                start_attempt
                if start_attempt is not None
                else self._stop_exit_base(plan_id)
            )

            def _coid(i: int) -> str:
                return f"{plan_id}-stop-exit-0-{base + i}"
        else:
            tag = uuid.uuid4().hex[:8]

            def _coid(i: int) -> str:
                return f"chase-{tag}-{i}"

        for i in range(attempts):
            quote = await self.get_quote(symbol)
            coid = _coid(i)
            order = await self.place_order(
                OrderRequest(
                    symbol=symbol,
                    side=side,  # type: ignore[arg-type]
                    qty=qty,
                    limit_price=quote.price,
                    reduce_only=True,
                    plan_id=plan_id,
                    client_order_id=coid,
                )
            )
            if order.status == "filled":
                return order
            # 즉시 종결 — 거부/소멸, 또는 stale 멱등 반환('cancelled') — 이면
            # 60초 대기를 태우지 않고 곧장 다음 시도/폴백으로 (finding #16).
            if order.status in ("rejected", "expired", "cancelled"):
                continue
            await asyncio.sleep(wait_seconds)
            current = await self.query_order(symbol, coid)
            if current is not None and current.status == "filled":
                return current
            with contextlib.suppress(httpx.HTTPError, OKXError, ValueError):
                await self.cancel_order(coid, symbol)
        # 폴백: reduce-only IOC 크로싱 리밋 (신선한 coid). 실패/거부는 rejected로
        # 미러해 모니터의 가드가 볼 수 있게 하고, OKXError를 밖으로 던지지 않는다.
        quote = await self.get_quote(symbol)
        inst_id = to_okx_symbol(symbol)
        sz = self._to_contracts(inst_id, qty)
        ioc_coid = _coid(attempts)
        req = OrderRequest(
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            qty=qty,
            limit_price=quote.price,
            reduce_only=True,
            aggressive=True,
            plan_id=plan_id,
            client_order_id=ioc_coid,
        )
        if sz <= 0:
            return self._mirror(
                self._rejected(req, f"수량 {qty} < 1계약 — IOC 폴백 불가"), req
            )
        body: dict[str, Any] = {
            "instId": inst_id,
            "tdMode": "isolated",
            "side": side,
            "posSide": self._pos_side(req),
            "ordType": "ioc",
            "px": _fmt(quote.price),
            "sz": _fmt(sz),
            "reduceOnly": True,
            "clOrdId": _okx_clordid(ioc_coid),
        }
        try:
            data = await self._signed("POST", "/api/v5/trade/order", body=body)
        except (OKXError, httpx.HTTPError) as exc:
            return self._mirror(self._rejected(req, f"IOC 폴백 실패: {exc}"), req)
        row = data[0] if data else {}
        s_code = str(row.get("sCode", "0"))
        if s_code not in ("0", ""):
            return self._mirror(
                self._rejected(
                    req, f"IOC 거부: OKX sCode {s_code} {row.get('sMsg', '')}"
                ),
                req,
            )
        self._record_order_time()
        return self._mirror(self._order_from_payload(row, req, inst_id), req)


class OKXError(RuntimeError):
    """OKX 에러 봉투 (code != '0')."""


def _envelope_data(payload: Any) -> Any:
    """OKX 응답 봉투 해제 — code != '0'이면 OKXError, 아니면 data 반환."""
    if isinstance(payload, dict) and "code" in payload:
        if str(payload.get("code")) != "0":
            raise OKXError(f"{payload.get('code')} {payload.get('msg', '')}")
        return payload.get("data", [])
    return payload


def _f(value: Any) -> float:
    """빈 문자열/None에 관대한 float 변환."""
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fmt(value: float) -> str:
    """OKX 수치 문자열 — 불필요한 소수 0 제거."""
    return f"{value:.12f}".rstrip("0").rstrip(".") or "0"
