"""Broker abstraction — 선물(perp) 지정가 전용 인터페이스 (스펙 §5).

PaperBroker(시뮬)와 BinanceBroker(라이브)가 동일한 async 인터페이스를
구현한다. 주문 유형은 **지정가(limit)만** — market 주문 메서드 자체가 없다
(규칙 §1). ABC 공통 검증:

- **플랜 강제**: reduce_only가 아닌 모든 주문은 status가 approved|active인
  trade_plans 행(plan_id)을 참조해야 한다 — "시나리오 없는 주문은 브로커가
  거부"(규칙 §2)를 어느 코드 경로도 우회할 수 없다.
- **심볼 필터**: tickSize/stepSize/minNotional 반올림·검증 (Binance 필터 모사,
  유니버스 심볼 하드코딩 테이블).
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Callable, Literal

OrderStatus = Literal["open", "filled", "cancelled", "expired", "rejected"]
OrderSide = Literal["buy", "sell"]


@dataclass
class Quote:
    symbol: str
    price: float
    ts: str


@dataclass
class Position:
    """격리마진 선물 포지션."""

    symbol: str
    side: Literal["long", "short"]
    qty: float
    avg_entry: float
    leverage: int
    isolated_margin: float
    liq_price: float
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0


@dataclass
class Balance:
    """선물 지갑 — wallet_balance는 실현 잔고(격리마진 포함), available은
    신규 주문에 쓸 수 있는 잔여분."""

    wallet_balance: float
    available: float
    margin_used: float
    unrealized_pnl: float


@dataclass
class OrderRequest:
    """지정가 주문 요청 — limit_price 필수 (market 주문 없음, 규칙 §1).

    - reduce_only: 청산(exit) 전용 — 포지션을 늘릴 수 없다.
    - aggressive: 손절/청산회피 exit 전용 크로싱 리밋 (taker). 진입·TP 레그는
      항상 패시브 post-only.
    - client_order_id: 멱등 재제출 키 (``{plan_id}-{leg_kind}-{leg_index}-{attempt}``).
    """

    symbol: str
    side: OrderSide
    qty: float
    limit_price: float
    reduce_only: bool = False
    aggressive: bool = False
    leverage: int | None = None
    client_order_id: str | None = None
    plan_id: int | None = None


@dataclass
class Order:
    id: str
    symbol: str
    side: OrderSide
    qty: float
    limit_price: float | None
    status: OrderStatus
    filled_qty: float = 0.0
    avg_fill_price: float | None = None
    reduce_only: bool = False
    aggressive: bool = False
    plan_id: int | None = None
    client_order_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class SymbolFilter:
    tick_size: float
    step_size: float
    min_notional: float


#: 유니버스 심볼별 거래 필터 (Binance USDT-M 필터 모사, 하드코딩).
SYMBOL_FILTERS: dict[str, SymbolFilter] = {
    "BTCUSDT": SymbolFilter(tick_size=0.1, step_size=0.001, min_notional=100.0),
    "ETHUSDT": SymbolFilter(tick_size=0.01, step_size=0.001, min_notional=20.0),
    "SOLUSDT": SymbolFilter(tick_size=0.01, step_size=1.0, min_notional=5.0),
    "XRPUSDT": SymbolFilter(tick_size=0.0001, step_size=0.1, min_notional=5.0),
    "DOGEUSDT": SymbolFilter(tick_size=0.00001, step_size=1.0, min_notional=5.0),
    # 2026-07-20 사용자 요청 추가 (Binance exchangeInfo 실측값)
    "ADAUSDT": SymbolFilter(tick_size=0.0001, step_size=1.0, min_notional=5.0),
    "LTCUSDT": SymbolFilter(tick_size=0.01, step_size=0.001, min_notional=20.0),
}

#: 플랜 게이트를 통과하는 trade_plans.status 값.
ORDERABLE_PLAN_STATUSES = ("approved", "active")


def round_to_tick(price: float, tick: float) -> float:
    """가격을 tickSize 격자에 반올림."""
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, 12)


def floor_to_step(qty: float, step: float) -> float:
    """수량을 stepSize 격자로 내림 (초과 주문 방지)."""
    if step <= 0:
        return qty
    return round(math.floor(qty / step + 1e-9) * step, 12)


class Broker(ABC):
    """공통 검증을 가진 브로커 ABC.

    ``plan_lookup``: plan_id → trade_plans.status 조회 콜백. PaperBroker는
    자체 DB 조회를 물리고, BinanceBroker는 주입받는다(없으면 비-reduce_only
    주문 전부 거부 — 안전 기본값).
    """

    def __init__(self, plan_lookup: Callable[[int], str | None] | None = None):
        self._plan_lookup = plan_lookup

    # -- shared validation -------------------------------------------------------
    def _plan_status(self, plan_id: int) -> str | None:
        if self._plan_lookup is None:
            return None
        return self._plan_lookup(plan_id)

    def validate_order(self, request: OrderRequest) -> tuple[OrderRequest, str | None]:
        """공통 주문 검증 — (정규화된 요청, 거부 사유|None)을 반환.

        플랜 게이트(규칙 §2) + 심볼 필터 반올림/검증. 거부 사유가 있으면
        주문을 절대 전송/기록하면 안 된다.
        """
        # 플랜 강제: 비-reduce_only 주문은 approved|active 플랜 필수.
        if not request.reduce_only:
            if request.plan_id is None:
                return request, "시나리오 없는 주문 거부 — TradePlan 필수 (규칙 §2)"
            status = self._plan_status(request.plan_id)
            if status not in ORDERABLE_PLAN_STATUSES:
                return (
                    request,
                    f"플랜 {request.plan_id} 상태 '{status}' — approved|active 아님, 주문 거부",
                )

        if request.limit_price is None or request.limit_price <= 0:
            return request, "지정가(limit_price) 필수 — market 주문 없음 (규칙 §1)"
        if request.qty <= 0:
            return request, f"주문 수량 {request.qty} ≤ 0 — 무효"
        # aggressive는 손절/청산회피 exit 전용 (스펙 §0) — 진입에 taker 경로 없음.
        if request.aggressive and not request.reduce_only:
            return request, "aggressive 주문은 reduce_only(손절/청산회피 exit) 전용"

        flt = SYMBOL_FILTERS.get(request.symbol)
        if flt is None:
            return request, None  # 필터 미정의 심볼 — 화이트리스트는 RiskEngine 소관

        price = round_to_tick(request.limit_price, flt.tick_size)
        qty = floor_to_step(request.qty, flt.step_size)
        if qty <= 0:
            return request, f"수량 {request.qty} < stepSize {flt.step_size} — 주문 불가"
        # minNotional은 신규 리스크를 여는 주문에만 — reduce_only(청산)는 잔량이
        # 필터 미만이어도 반드시 닫을 수 있어야 한다 (Binance 선물 동작).
        if not request.reduce_only and qty * price < flt.min_notional:
            return (
                request,
                f"노셔널 {qty * price:.4f} USDT < minNotional {flt.min_notional:g} USDT",
            )
        if price != request.limit_price or qty != request.qty:
            request = replace(request, limit_price=price, qty=qty)
        return request, None

    # -- interface ----------------------------------------------------------------
    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    async def get_balance(self) -> Balance: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str | None = None) -> Order: ...

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[Order]: ...

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> None: ...

    @abstractmethod
    async def set_margin_mode(self, symbol: str, mode: str = "isolated") -> None: ...
