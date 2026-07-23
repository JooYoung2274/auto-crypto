"""Binance USDT-M Perpetual (fapi) 라이브 브로커 — HMAC 서명 httpx 클라이언트.

스펙 §5:
- 키 없이 live 기동 거부 (:class:`BinanceConfigError`).
- ``CA_BINANCE_TESTNET=true`` → 테스트넷 base URL.
- 서명: query string 전체를 HMAC-SHA256, ``X-MBX-APIKEY`` 헤더.
- recvWindow + 서버시간 동기 (GET /fapi/v1/time 오프셋, TTL 갱신 + -1021 복구).
- 429/418 지수 백오프 (2회 재시도).
- 기동 리컨실: 심볼별 isolated 마진 + 레버리지 설정, 오픈 주문/포지션 조회.
- 안전장치: ``live_max_order_usdt`` 노셔널 상한, rolling-24h 주문 수 한도,
  일손실 서킷브레이커 → **reduce-only 킬스위치 모드** (신규 진입 전면 거부,
  청산 주문만 허용). 킬스위치·주문 카운터는 paper_state에 지속 (재기동 복원).
- 스탑엑싯 체이스: reduce-only 리밋 cancel-replace K회 → reduce-only IOC 폴백
  (손절/청산회피 한정 taker 허용).

라이브 주문 미러 (스펙 §5): 브로커가 발주/취소/정산하는 모든 주문을 범용
``paper_orders`` 테이블에 미러링한다 — PositionMonitor/Trader가 (paper와 동일
경로로) 체결 리컨실·TP 사이징·종료 시 자식 주문 전량 취소·TTL 재큐·스탑엑싯
복구를 관리할 수 있게 한다. ``db`` 없이 생성하면(단위 테스트) 미러는 no-op이고
거래소 응답을 그대로 돌려준다.

진입·TP 레그는 post-only(GTX), aggressive exit는 GTC 크로싱 리밋.
플랜 게이트(ABC 공통)는 ``plan_lookup`` 콜백 또는 ``db`` 주입으로 활성화 —
둘 다 없으면 비-reduce_only 주문은 전부 거부된다 (안전 기본값).
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode

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

BASE_URL = "https://fapi.binance.com"
TESTNET_URL = "https://testnet.binancefuture.com"

#: 서명 요청 recvWindow (ms).
RECV_WINDOW = 5_000

#: 429/418 재시도 횟수 (첫 시도 제외).
MAX_RETRIES = 2

#: 스탑엑싯 체이스 기본 cancel-replace 횟수 (그 후 reduce-only IOC 폴백).
STOP_CHASE_ATTEMPTS = 3

#: 모호한 발주(타임아웃/5xx) + 채택 조회 실패 시 미러 사유 마커 — settle이
#: origClientOrderId로 최종 해소한다 (finding #7). rejected로 단정하지 않는다.
_PENDING_UNKNOWN = "발주 확인 불가"

#: 서버시간 오프셋 캐시 TTL (초) — 이후 재조회해 클럭 스텝을 흡수한다.
TIME_SYNC_TTL_S = 1800.0

#: paper_state 지속 키 (재기동 복원, 스펙 §5).
_KILL_KEY = "live_kill_switch"
_ORDER_TIMES_KEY = "live_order_times_json"

_STATUS_MAP = {
    "NEW": "open",
    "PARTIALLY_FILLED": "open",
    "FILLED": "filled",
    "CANCELED": "cancelled",
    "CANCELLED": "cancelled",
    "EXPIRED": "expired",
    "REJECTED": "rejected",
}


class BinanceConfigError(RuntimeError):
    """Binance API 키 부재/무효 — live 기동 거부."""


def _map_status(raw: str) -> str:
    return _STATUS_MAP.get(str(raw).upper(), "rejected")


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


class BinanceBroker(Broker):
    """Binance USDT-M 선물 라이브 브로커 (httpx, 주입 가능한 transport)."""

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
        if not settings.binance_api_key or not settings.binance_api_secret:
            raise BinanceConfigError(
                "BinanceBroker requires CA_BINANCE_API_KEY and CA_BINANCE_API_SECRET "
                "— 키 없이 live 기동 거부 (스펙 §0)"
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
        self.base_url = base_url or (
            TESTNET_URL if settings.binance_testnet else BASE_URL
        )
        self._transport = transport
        self._client = httpx.AsyncClient(
            base_url=self.base_url, transport=transport, timeout=10.0
        )
        self._retry_base_delay = retry_base_delay
        self._time_offset_ms: int | None = None
        self._time_synced_at: float = 0.0
        self._time_lock = asyncio.Lock()
        #: rolling-24h 주문 타임스탬프 (epoch s).
        self._order_times: deque[float] = deque()
        #: 일손실 서킷브레이커 — True면 reduce-only 주문만 허용.
        self.kill_switch: bool = False
        self._order_symbols: dict[str, str] = {}  # order id → symbol (cancel용)
        self._load_persisted()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- HTTP plumbing -------------------------------------------------------------
    async def _request_with_backoff(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> httpx.Response:
        """429(레이트리밋)/418(IP 밴 경고) 지수 백오프 재시도."""
        c = client or self._client
        resp: httpx.Response | None = None
        for attempt in range(MAX_RETRIES + 1):
            resp = await c.request(method, url, headers=headers)
            if resp.status_code in (429, 418) and attempt < MAX_RETRIES:
                await asyncio.sleep(self._retry_base_delay * (2**attempt))
                continue
            break
        assert resp is not None
        resp.raise_for_status()
        return resp

    async def _public(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> Any:
        url = path if not params else f"{path}?{urlencode(params)}"
        resp = await self._request_with_backoff(method, url, client=client)
        return resp.json()

    async def _refresh_time(self, client: httpx.AsyncClient | None) -> None:
        payload = await self._public("GET", "/fapi/v1/time", client=client)
        server_ms = int(payload["serverTime"])
        self._time_offset_ms = server_ms - int(time.time() * 1000)
        self._time_synced_at = time.time()

    async def _ensure_time_sync(
        self, client: httpx.AsyncClient | None = None, force: bool = False
    ) -> int:
        """서버시간 오프셋(ms) — TTL 경과/강제 시 재조회 (클럭 스텝 흡수).

        ``client``이 주어지면(정산/스냅샷의 임시 루프) 크로스-루프 asyncio.Lock을
        피하려 락 없이 갱신한다 — 이 경로에서는 오프셋이 대개 이미 웜이다."""
        fresh = (
            self._time_offset_ms is not None
            and not force
            and (time.time() - self._time_synced_at) < TIME_SYNC_TTL_S
        )
        if fresh:
            return self._time_offset_ms  # type: ignore[return-value]
        if client is not None:
            await self._refresh_time(client)
            return self._time_offset_ms  # type: ignore[return-value]
        async with self._time_lock:
            if (
                force
                or self._time_offset_ms is None
                or (time.time() - self._time_synced_at) >= TIME_SYNC_TTL_S
            ):
                await self._refresh_time(None)
            return self._time_offset_ms  # type: ignore[return-value]

    def _sign(self, query: str) -> str:
        return hmac.new(
            self.settings.binance_api_secret.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _sign_url(
        self, path: str, params: dict | None, offset: int
    ) -> tuple[str, dict[str, str]]:
        payload = dict(params or {})
        payload["recvWindow"] = RECV_WINDOW
        payload["timestamp"] = int(time.time() * 1000) + offset
        query = urlencode(payload)
        signature = self._sign(query)
        url = f"{path}?{query}&signature={signature}"
        return url, {"X-MBX-APIKEY": self.settings.binance_api_key}

    async def _signed(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> Any:
        c = client or self._client
        offset = await self._ensure_time_sync(client=client)
        url, headers = self._sign_url(path, params, offset)
        try:
            resp = await self._request_with_backoff(
                method, url, headers=headers, client=c
            )
        except httpx.HTTPStatusError as exc:
            # -1021 'Timestamp outside recvWindow' — 클럭 스텝. 오프셋 재동기 후 1회 재시도.
            if "-1021" in (exc.response.text or ""):
                offset = await self._ensure_time_sync(client=client, force=True)
                url, headers = self._sign_url(path, params, offset)
                resp = await self._request_with_backoff(
                    method, url, headers=headers, client=c
                )
            else:
                raise
        return resp.json()

    def _temp_client(self) -> httpx.AsyncClient:
        """정산/스냅샷용 임시 AsyncClient — 이 메서드들은 asyncio.to_thread로
        워커 스레드(러닝 루프 없음)에서 호출되므로 공유 self._client(메인 루프
        바인딩)를 재사용하지 못한다. 주입 transport는 그대로 물린다."""
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
        """부팅 리컨실 (스펙 §2): 서버시간 동기 → 심볼별 isolated 마진 +
        레버리지 캡 설정 → 오픈 주문/포지션 조회 반환. plan_id/client_order_id
        재부착은 호출자(트레이더/모니터) 소관."""
        await self._ensure_time_sync()
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
        """일손실 서킷브레이커 — 임계 초과 시 reduce-only 킬스위치 모드 진입.

        임계: −live_max_loss_pct × 시드. 킬스위치는 수동 해제 전까지 유지하며
        paper_state에 지속된다 (재기동 복원)."""
        limit = self.settings.live_max_loss_pct * self.settings.initial_seed_usdt
        if daily_realized_pnl <= -limit and not self.kill_switch:
            self.kill_switch = True
            self._persist_kill_switch()
        return self.kill_switch

    def _rolling_order_count(self, now_s: float) -> int:
        while self._order_times and self._order_times[0] <= now_s - 86_400.0:
            self._order_times.popleft()
        return len(self._order_times)

    # -- Broker interface -----------------------------------------------------------------
    async def get_quote(self, symbol: str) -> Quote:
        data = await self._public(
            "GET", "/fapi/v1/ticker/price", {"symbol": symbol}
        )
        return Quote(
            symbol=str(data.get("symbol", symbol)),
            price=float(data["price"]),
            ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    async def get_balance(self) -> Balance:
        data = await self._signed("GET", "/fapi/v2/account")
        return Balance(
            wallet_balance=float(data.get("totalWalletBalance", 0.0)),
            available=float(data.get("availableBalance", 0.0)),
            margin_used=float(data.get("totalPositionInitialMargin", 0.0)),
            unrealized_pnl=float(data.get("totalUnrealizedProfit", 0.0)),
        )

    async def get_positions(self) -> list[Position]:
        data = await self._signed("GET", "/fapi/v2/positionRisk")
        positions: list[Position] = []
        for p in data:
            amt = float(p.get("positionAmt", 0.0))
            if amt == 0.0:
                continue
            positions.append(
                Position(
                    symbol=str(p["symbol"]),
                    side="long" if amt > 0 else "short",
                    qty=abs(amt),
                    avg_entry=float(p.get("entryPrice", 0.0)),
                    leverage=int(float(p.get("leverage", 0) or 0)),
                    isolated_margin=float(p.get("isolatedMargin", 0.0) or 0.0),
                    liq_price=float(p.get("liquidationPrice", 0.0) or 0.0),
                    mark_price=float(p.get("markPrice", 0.0) or 0.0),
                    unrealized_pnl=float(p.get("unRealizedProfit", 0.0) or 0.0),
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

    def _order_from_payload(self, data: dict, request: OrderRequest) -> Order:
        order = Order(
            id=str(data.get("orderId", "")),
            symbol=str(data.get("symbol", request.symbol)),
            side=request.side,
            qty=float(data.get("origQty", request.qty)),
            limit_price=float(data.get("price", request.limit_price)),
            status=_map_status(data.get("status", "NEW")),  # type: ignore[arg-type]
            filled_qty=float(data.get("executedQty", 0.0) or 0.0),
            avg_fill_price=(
                float(data["avgPrice"])
                if data.get("avgPrice") not in (None, "", "0", "0.0", "0.00")
                else None
            ),
            reduce_only=request.reduce_only,
            aggressive=request.aggressive,
            plan_id=request.plan_id,
            client_order_id=str(
                data.get("clientOrderId", request.client_order_id or "")
            )
            or None,
        )
        if order.id:
            self._order_symbols[order.id] = order.symbol
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
        """거래소 응답/거부를 범용 paper_orders에 미러링하고, 로컬 미러 행을
        재조회해 (모니터/트레이더가 쓰는) 로컬 id를 가진 Order로 돌려준다.

        db가 없으면(단위 테스트) no-op — 거래소 id를 가진 원본 Order를 그대로
        반환한다."""
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
        # 멱등성 (미러 기반): 같은 client_order_id 재제출 → 기존 미러 주문 반환
        # (결정론 coid = 이미 발주 = 거래소 중복 주문 0건).
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

        params: dict[str, Any] = {
            "symbol": request.symbol,
            "side": "BUY" if request.side == "buy" else "SELL",
            "type": "LIMIT",
            "quantity": request.qty,
            "price": request.limit_price,
            # 진입·TP = post-only(GTX, maker). aggressive exit = GTC 크로싱(taker).
            "timeInForce": "GTC" if request.aggressive else "GTX",
            "reduceOnly": "true" if request.reduce_only else "false",
        }
        if request.client_order_id:
            params["newClientOrderId"] = request.client_order_id
        try:
            data = await self._signed("POST", "/fapi/v1/order", params)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code >= 500:
                # 5xx = 모호(주문이 접수됐을 수도) → 조회 후 채택, 없으면 거부.
                return self._mirror(
                    await self._adopt_or_reject(
                        request, f"주문 실패: HTTP {code}"
                    ),
                    request,
                )
            return self._mirror(
                self._rejected(request, f"주문 실패: HTTP {code}"), request
            )
        except httpx.HTTPError as exc:
            # 타임아웃/전송오류 = 모호 → 조회 후 채택, 없으면 거부.
            return self._mirror(
                await self._adopt_or_reject(request, f"주문 실패: {exc}"), request
            )
        self._record_order_time()
        order = self._order_from_payload(data, request)
        if order.status == "expired" and order.filled_qty <= 0:
            # GTX(post-only)가 배치 즉시 크로싱으로 소멸하면 응답이 EXPIRED다.
            # 이를 'expired'로 미러하면 모니터의 원가격 재큐 가드가 무한 재발주로
            # 흘러간다 — 'rejected'로 미러해 재큐를 멈춘다 (finding #6). OKX는
            # sCode 거부로 이미 동일 동작.
            order = self._rejected(
                request,
                "post-only 크로싱 거부 — 원가격이 반대 호가 관통 (재큐 금지)",
            )
        return self._mirror(order, request)

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
        """모호한 타임아웃/5xx 뒤: origClientOrderId로 조회해 거래소에 실제로
        접수됐으면 그 주문을 채택(중복 재제출 방지, 스펙 §2). 조회가 명확히
        '미접수'(None)면 거부하되, 조회 자체가 실패하면(같은 네트워크 장애)
        주문이 남아 있을 수 있으므로 pending-unknown(open)으로 미러 —
        settle이 해소한다 (finding #7)."""
        if request.client_order_id:
            try:
                live = await self.query_order(
                    request.symbol, request.client_order_id
                )
            except httpx.HTTPError:
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
        """client_order_id로 주문 조회 — 멱등 재제출 전 확인용 (스펙 §2)."""
        try:
            data = await self._signed(
                "GET",
                "/fapi/v1/order",
                {"symbol": symbol, "origClientOrderId": client_order_id},
                client=client,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 404):
                return None
            raise
        side = "buy" if str(data.get("side", "")).upper() == "BUY" else "sell"
        req = OrderRequest(
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            qty=float(data.get("origQty", 0.0)),
            limit_price=float(data.get("price", 0.0)),
            reduce_only=str(data.get("reduceOnly", "false")).lower() == "true",
            client_order_id=client_order_id,
        )
        return self._order_from_payload(data, req)

    async def cancel_order(self, order_id: str, symbol: str | None = None) -> Order:
        # 모니터/트레이더는 미러 행의 로컬 id를 넘긴다 → coid로 매핑해 거래소에는
        # origClientOrderId로 취소하고 미러 행을 갱신한다. 체이스는 coid를 직접 넘긴다.
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
        params: dict[str, Any] = {"symbol": symbol}
        if coid:
            params["origClientOrderId"] = coid
        elif order_id.isdigit():
            params["orderId"] = order_id
        else:
            params["origClientOrderId"] = order_id
        try:
            data = await self._signed("DELETE", "/fapi/v1/order", params)
        except (httpx.HTTPStatusError, httpx.HTTPError) as exc:
            resolved = await self._confirm_cancel(symbol, coid or order_id, row)
            if resolved is not None:
                return resolved
            raise exc
        if row is not None and self.db is not None:
            self.db.execute(
                "UPDATE paper_orders SET status = 'cancelled', reason = ? WHERE id = ?",
                ("취소", row["id"]),
            )
        side = "buy" if str(data.get("side", "")).upper() == "BUY" else "sell"
        return Order(
            id=str(data.get("orderId", order_id)),
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            qty=float(data.get("origQty", 0.0)),
            limit_price=float(data.get("price", 0.0) or 0.0),
            status=_map_status(data.get("status", "CANCELED")),  # type: ignore[arg-type]
            filled_qty=float(data.get("executedQty", 0.0) or 0.0),
            client_order_id=str(data.get("clientOrderId", "")) or None,
        )

    async def _confirm_cancel(
        self, symbol: str, coid: str | None, row: dict | None
    ) -> Order | None:
        """모호한 취소 실패 뒤: 조회해 이미 취소/체결/소멸했으면 그 상태로
        확정(중복 취소·유령 주문 방지, 스펙 §2), 아니면 None(원 예외 재전파)."""
        if not coid or coid.isdigit():
            return None
        with contextlib.suppress(httpx.HTTPError):
            live = await self.query_order(symbol, coid)
            if live is None or live.status in ("cancelled", "filled", "expired"):
                final = live.status if live is not None else "cancelled"
                if row is not None and self.db is not None:
                    self.db.execute(
                        "UPDATE paper_orders SET status = ? WHERE id = ?",
                        (final, row["id"]),
                    )
                if live is not None:
                    return live
                return Order(
                    id=coid,
                    symbol=symbol,
                    side="buy",
                    qty=0.0,
                    limit_price=None,
                    status="cancelled",
                    client_order_id=coid,
                )
        return None

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        params = {"symbol": symbol} if symbol else {}
        data = await self._signed("GET", "/fapi/v1/openOrders", params)
        orders: list[Order] = []
        for o in data:
            side = "buy" if str(o.get("side", "")).upper() == "BUY" else "sell"
            req = OrderRequest(
                symbol=str(o.get("symbol", "")),
                side=side,  # type: ignore[arg-type]
                qty=float(o.get("origQty", 0.0)),
                limit_price=float(o.get("price", 0.0)),
                reduce_only=str(o.get("reduceOnly", "false")).lower() == "true",
            )
            orders.append(self._order_from_payload(o, req))
        return orders

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        await self._signed(
            "POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)}
        )

    async def set_margin_mode(self, symbol: str, mode: str = "isolated") -> None:
        if mode != "isolated":
            raise ValueError("마진 모드는 격리(isolated) 고정 (규칙 §1)")
        try:
            await self._signed(
                "POST",
                "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": "ISOLATED"},
            )
        except httpx.HTTPStatusError as exc:
            # -4046 'No need to change margin type' — 이미 isolated.
            if "-4046" in exc.response.text:
                return
            raise

    # -- settlement / snapshot (모니터/트레이더 훅) -------------------------------------
    def settle(self, now_ms: int | None = None) -> list[Order]:
        """오픈 미러 주문을 거래소 상태로 리컨실한다 (모니터 훅).

        각 오픈 주문을 origClientOrderId로 조회해 filled/cancelled/expired +
        체결량/평균가를 미러에 반영하고, 상태가 바뀐 Order 목록을 반환한다
        (PaperBroker.settle과 동일 계약). db 없으면 no-op. asyncio.to_thread로
        워커 스레드에서 호출되므로 새 루프+임시 클라이언트로 실행한다."""
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
                except httpx.HTTPError:
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
                    # 상태 불변이라도 부분 체결량이 늘었으면 미러에 반영 (finding
                    # #10): PARTIALLY_FILLED는 'open'으로 남아 filled_qty가 0으로
                    # 고이면 실제 포지션을 든 플랜을 TTL이 abandon할 수 있다.
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
        """GET /fapi/v2/account → portfolio_snapshots 기록 (PaperBroker.snapshot과
        동일 shape). 일손실 서킷브레이커 기준선/포트폴리오 히스토리 소스. db 없으면
        no-op. asyncio.to_thread(트레이더)로 호출 → 새 루프+임시 클라이언트."""
        if self.db is None:
            return {}
        return self._blocking(self._snapshot_async)

    async def _snapshot_async(self) -> dict:
        async with self._temp_client() as client:
            data = await self._signed("GET", "/fapi/v2/account", client=client)
        wallet = float(data.get("totalWalletBalance", 0.0))
        margin_used = float(data.get("totalPositionInitialMargin", 0.0))
        available = float(data.get("availableBalance", 0.0))
        upnl = float(data.get("totalUnrealizedProfit", 0.0))
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
        """손절/청산회피 exit: reduce-only 리밋 체이스.

        현재가에 reduce-only 지정가를 놓고 ``wait_seconds`` 후 미체결이면
        cancel-replace — ``attempts``회 반복 후 reduce-only IOC 크로싱 리밋
        폴백 (한정 taker 허용). 체결 성공/최종 폴백 주문을 반환.

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
            # wait_seconds를 태우지 말고 곧장 다음 시도/폴백으로 (finding #16).
            if order.status in ("rejected", "expired", "cancelled"):
                continue
            await asyncio.sleep(wait_seconds)
            current = await self.query_order(symbol, coid)
            if current is not None and current.status == "filled":
                return current
            with contextlib.suppress(httpx.HTTPError, ValueError):
                await self.cancel_order(coid, symbol)
        # 폴백: reduce-only IOC 크로싱 리밋 (신선한 coid). POST 실패는 rejected로
        # 미러해 모니터 가드가 볼 수 있게 하고, 예외를 밖으로 던지지 않는다.
        quote = await self.get_quote(symbol)
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
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": "BUY" if side == "buy" else "SELL",
            "type": "LIMIT",
            "quantity": qty,
            "price": quote.price,
            "timeInForce": "IOC",
            "reduceOnly": "true",
            "newClientOrderId": ioc_coid,
        }
        try:
            data = await self._signed("POST", "/fapi/v1/order", params)
        except httpx.HTTPError as exc:
            return self._mirror(self._rejected(req, f"IOC 폴백 실패: {exc}"), req)
        self._record_order_time()
        return self._mirror(self._order_from_payload(data, req), req)
