"""PositionMonitor — 사이클 상태머신 밖의 상시 리스크 태스크 (스펙 §1.1).

리스크 크리티컬 동작 전담: 4h 종가 손절 판정(4h 마감 후 첫 틱), 주문 TTL
만료 취소/원가격 재큐, 플랜 TTL(abandoned), 펀딩 정산 트리거, 청산 경고
이벤트, 스탑엑싯 체이스 드라이버, 플랜 수명주기 강제(종료 시 자식 주문 전량
취소). research 사이클이 아무리 길어도 절대 블로킹되지 않는다.

trade 사이클과 **공유 asyncio.Lock**(orchestrator가 노출)으로 주문/포지션
변이를 직렬화한다 — 같은 심볼에 cancel-replace와 신규 진입이 인터리브 불가.
판단은 인메모리 상태(judged 4h bar, 경고 밴드)로 하고 결과만 DB에 기록한다.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Callable

from .agents.trader import (
    Trader,
    blackout_windows,
    client_order_id,
    open_plans,
    parse_client_order_id,
)
from .broker.base import Broker, OrderRequest
from .config import Settings
from .db import Database
from .events import Event, EventBus
from .risk.plan import TradePlan

logger = logging.getLogger(__name__)

_TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
_4H_MS = _TF_MS["4h"]

#: 스탑엑싯 체이스 — 재발주 대기(ms)와 최대 시도 횟수 (스펙 §5).
STOP_CHASE_WAIT_MS = 60_000
STOP_CHASE_ATTEMPTS = 3

#: 청산 경고 밴드 — markPrice가 청산가에서 이 비율 이내로 접근하면 경고.
LIQ_WARN_BAND = 0.10


def _parse_iso_ms(raw: str) -> int:
    ts = dt.datetime.fromisoformat(str(raw))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return int(ts.timestamp() * 1000)


class PositionMonitor:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        settings: Settings,
        broker_provider: Callable[[], Broker],
        trade_lock: asyncio.Lock,
        *,
        clock: Callable[[], int] | None = None,
        poll_seconds: float = 5.0,
    ):
        self.db = db
        self.bus = bus
        self.settings = settings
        self.broker_provider = broker_provider
        self.trade_lock = trade_lock
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.poll_seconds = poll_seconds
        # 인메모리 판단 상태 (스펙 §1.1) — 재기동 시 마지막 완결 4h봉을
        # 다시 판정해도 결과는 멱등 (이미 stopped/abandoned면 스킵).
        self._judged_4h: dict[int, int] = {}
        self._warned_liq: set[str] = set()
        # 고아 포지션 경고 1회성 플래그 — 최신 플랜이 종결 상태인데 포지션이
        # 남아 있는 심볼(수동/미러갭 고아)을 매 틱 재경고하지 않기 위함.
        self._warned_orphan: set[tuple[str, str]] = set()
        # 일손실 서킷브레이커 발동 경고 1회성 플래그 (라이브 브로커 한정).
        self._kill_announced = False
        # 재기동 시 펀딩 이력 전체를 activity_log/WS로 재방송하지 않도록
        # 현재 최대 id에서 시작한다.
        rows = db.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM funding_payments"
        )
        self._last_funding_id = int(rows[0]["m"])

    # -- lifecycle ----------------------------------------------------------------
    async def run(self) -> None:
        """상시 루프 — 틱 실패는 로그만 남기고 절대 죽지 않는다."""
        while True:
            await asyncio.sleep(self.poll_seconds)
            try:
                async with self.trade_lock:
                    await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the monitor must survive
                logger.exception("PositionMonitor tick failed")

    async def tick(self, now_ms: int | None = None) -> None:
        """한 틱: 정산 → 이벤트 발행 → 플랜 수명주기 → 손절 판정 → 경고."""
        now = now_ms if now_ms is not None else self.clock()
        broker = self.broker_provider()
        settle_fn = getattr(broker, "settle", None)
        changed = []
        if settle_fn is not None:
            changed = await asyncio.to_thread(settle_fn, now)
        await self._publish_order_events(changed)
        await self._check_circuit_breaker(broker)
        await self._publish_funding_events()
        await self._reconcile_fills()
        await self._requeue_expired(broker, now)
        await self._abandon_dead_ladders(broker)
        await self._enforce_plan_ttl(broker, now)
        await self._judge_4h_stops(broker, now)
        await self._drive_stop_exits(broker, now)
        await self._close_finished_plans(broker, now)
        await self._liquidation_warnings(broker)

    # -- event publication ----------------------------------------------------------
    async def _publish_order_events(self, changed: list) -> None:
        for order in changed:
            side = "매수" if order.side == "buy" else "매도"
            if order.status == "filled":
                price = order.avg_fill_price or order.limit_price or 0.0
                level = "warning" if "청산" in (order.reason or "") else "info"
                await self.bus.publish(
                    Event(
                        type="order_filled",
                        agent="trader",
                        level=level,
                        message=(
                            f"{order.symbol} {side} {order.qty:g} @ "
                            f"{price:,.4f} 체결 — {order.reason}"
                        ),
                        data={
                            "order_id": order.id,
                            "symbol": order.symbol,
                            "plan_id": order.plan_id,
                            "side": order.side,
                            "qty": order.qty,
                            "price": price,
                        },
                    )
                )
                await self.bus.publish(
                    Event(
                        type="position_update",
                        agent="trader",
                        data={"symbol": order.symbol},
                        persist=False,
                    )
                )
            elif order.status in ("cancelled", "expired"):
                await self.bus.publish(
                    Event(
                        type="order_cancelled",
                        agent="trader",
                        message=(
                            f"{order.symbol} {side} 주문 {order.status} — "
                            f"{order.reason}"
                        ),
                        data={
                            "order_id": order.id,
                            "symbol": order.symbol,
                            "plan_id": order.plan_id,
                        },
                    )
                )

    async def _check_circuit_breaker(self, broker: Broker) -> None:
        """일손실 서킷브레이커 (라이브 한정, 스펙 §5): 오늘 실현손익을 계산해
        브로커의 reduce-only 킬스위치를 발동시킨다. paper 브로커는
        ``check_daily_loss``가 없어 no-op. 발동 시 1회 경고 이벤트 발행."""
        check = getattr(broker, "check_daily_loss", None)
        if check is None:
            return
        balance = await broker.get_balance()
        wallet = float(balance.wallet_balance)
        daily_pnl = await asyncio.to_thread(
            Trader._daily_realized_pnl, self.db, wallet
        )
        tripped = check(daily_pnl)
        if tripped and not self._kill_announced:
            self._kill_announced = True
            await self.bus.publish(
                Event(
                    type="liquidation_warning",
                    agent="risk",
                    level="warning",
                    message=(
                        f"일손실 서킷브레이커 발동 — 오늘 실현손익 "
                        f"{daily_pnl:,.2f} USDT, reduce-only 킬스위치 진입 "
                        f"(신규 진입 전면 차단, 청산 주문만 허용)"
                    ),
                    data={"daily_realized_pnl": daily_pnl},
                )
            )

    async def _publish_funding_events(self) -> None:
        rows = self.db.execute(
            "SELECT * FROM funding_payments WHERE id > ? ORDER BY id",
            (self._last_funding_id,),
        )
        for r in rows:
            self._last_funding_id = int(r["id"])
            payment = float(r["payment"])
            await self.bus.publish(
                Event(
                    type="funding_payment",
                    agent="trader",
                    message=(
                        f"{r['symbol']} 펀딩 정산 {payment:+,.4f} USDT "
                        f"(rate {float(r['rate']):+.4%}, {r['side']})"
                    ),
                    data={
                        "symbol": r["symbol"],
                        "payment": payment,
                        "side": r["side"],
                        "rate": float(r["rate"]),
                    },
                )
            )

    # -- plan lifecycle ----------------------------------------------------------------
    async def _reconcile_fills(self) -> None:
        """filled_fraction 갱신 + approved → active 승격 (모니터 측 리컨실)."""
        for row in open_plans(self.db):
            plan_id = int(row["id"])
            plan = TradePlan.from_json(row["plan_json"])
            orders = self.db.execute(
                "SELECT client_order_id, status, filled_qty FROM paper_orders "
                "WHERE plan_id = ?",
                (plan_id,),
            )
            fraction = 0.0
            seen: set[int] = set()
            for o in orders:
                leg = parse_client_order_id(o["client_order_id"])
                # 부분 체결 가시화 (finding #17): 라이브 partially_filled는 미러
                # 상태가 'open'으로 남으므로, filled_qty > 0이면 상태가 아직
                # 'filled'가 아니어도 이 레그의 비중을 (보수적으로) 인정한다 —
                # 실제 포지션을 든 플랜을 TTL이 abandon하지 못하게.
                filled = o["status"] == "filled" or float(o["filled_qty"] or 0.0) > 0
                if (
                    leg is None
                    or leg[1] != "entry"
                    or not filled
                    or leg[2] in seen
                    or leg[2] >= len(plan.entries)
                ):
                    continue
                seen.add(leg[2])
                fraction += plan.entries[leg[2]].fraction
            if abs(fraction - float(row["filled_fraction"] or 0.0)) > 1e-9:
                self.db.execute(
                    "UPDATE trade_plans SET filled_fraction = ? WHERE id = ?",
                    (fraction, plan_id),
                )
            if fraction > 0 and row["status"] == "approved":
                self.db.execute(
                    "UPDATE trade_plans SET status = 'active' WHERE id = ?",
                    (plan_id,),
                )

    async def _requeue_expired(self, broker: Broker, now_ms: int) -> None:
        """TTL 만료된 진입 레그를 **원래 플랜 레그 가격 그대로** 재큐한다
        (가격 추격 금지 — RR 불변, 스펙 §2).

        블랙아웃 윈도 안에서는 재큐하지 않는다 — 재큐도 신규 진입 주문이다
        (규칙 §2). 마지막 시도가 rejected면 재큐를 멈춘다 (거부 무한 루프
        방지 — 플랜 TTL이 정리한다)."""
        windows = blackout_windows(self.db, self.settings, now_ms)
        if any(lo <= now_ms <= hi for lo, hi in windows):
            return
        for row in open_plans(self.db):
            plan_id = int(row["id"])
            plan = TradePlan.from_json(row["plan_json"])
            orders = self.db.execute(
                "SELECT * FROM paper_orders WHERE plan_id = ? ORDER BY id",
                (plan_id,),
            )
            by_leg: dict[int, list[dict]] = {}
            for o in orders:
                leg = parse_client_order_id(o["client_order_id"])
                if leg is None or leg[1] != "entry":
                    continue
                by_leg.setdefault(leg[2], []).append(o)
            entry_side = "buy" if plan.side == "long" else "sell"
            for leg_index, leg_orders in by_leg.items():
                if leg_index >= len(plan.entries):
                    continue
                statuses = {o["status"] for o in leg_orders}
                if "filled" in statuses or "open" in statuses:
                    continue
                # 마지막 시도가 expired일 때만 재큐 — rejected로 끝난 레그를
                # 틱마다 다시 발주하는 무한 루프 방지.
                if leg_orders[-1]["status"] != "expired":
                    continue
                leg = plan.entries[leg_index]
                attempt = len(leg_orders)
                qty = float(leg_orders[-1]["qty"])
                order = await broker.place_order(
                    OrderRequest(
                        symbol=plan.symbol,
                        side=entry_side,
                        qty=qty,
                        limit_price=leg.price,  # 원가격 그대로 (추격 금지)
                        leverage=plan.leverage,
                        plan_id=plan_id,
                        client_order_id=client_order_id(
                            plan_id, "entry", leg_index, attempt
                        ),
                    )
                )
                await self.bus.publish(
                    Event(
                        type="log",
                        agent="trader",
                        message=(
                            f"{plan.symbol} 진입 레그 {leg_index + 1} "
                            f"TTL 만료 — 원가격 재큐 @ {leg.price:,.4f} "
                            f"({attempt}회차, 상태 {order.status})"
                        ),
                        data={"plan_id": plan_id, "leg_index": leg_index},
                    )
                )

    async def _abandon_dead_ladders(self, broker: Broker) -> None:
        """래더 소멸 즉시 정리 — 진입 레그가 전부 죽었는데(취소/거부) 체결 0,
        포지션 없음이면 플랜을 abandoned로 전환한다.

        exchange가 post-only 크로싱 진입 주문을 취소하면(예: 진입가·손절선
        근접 플랜) 레그가 cancelled로 끝나는데, requeue는 expired만 재큐하고
        plan_ttl은 24h를 기다린다 — 그 사이 플랜이 'approved'로 고착돼 해당
        심볼 신규 진입이 막힌다 (XRP #49, 2026-07-22). 재큐 대상(expired)이나
        오픈 레그가 하나도 없고 래더가 전량 배치 완료된 경우에만 정리한다."""
        held = {(p.symbol, p.side) for p in await broker.get_positions()}
        for row in open_plans(self.db):
            if float(row["filled_fraction"] or 0.0) > 0:
                continue
            if (row["symbol"], row["side"]) in held:
                continue
            plan_id = int(row["id"])
            plan = TradePlan.from_json(row["plan_json"])
            by_leg: dict[int, list[dict]] = {}
            for o in self.db.execute(
                "SELECT status, client_order_id FROM paper_orders WHERE plan_id = ? "
                "ORDER BY id",
                (plan_id,),
            ):
                leg = parse_client_order_id(o["client_order_id"])
                if leg is not None and leg[1] == "entry":
                    by_leg.setdefault(leg[2], []).append(o)
            # 래더가 전량 배치됐고(모든 진입 레그 존재), 각 레그의 최종 상태가
            # cancelled/rejected(재큐·체결 불가)일 때만 소멸로 본다. open/
            # expired/filled가 하나라도 있으면 아직 살아 있다.
            if len(by_leg) < len(plan.entries):
                continue
            if any(
                legs[-1]["status"] not in ("cancelled", "rejected")
                for legs in by_leg.values()
            ):
                continue
            await self._cancel_plan_orders(
                broker, plan_id, row["symbol"], "래더 소멸 — abandoned"
            )
            self.db.execute(
                "UPDATE trade_plans SET status = 'abandoned', "
                "reject_reason = '래더 전 진입 레그 취소/거부' WHERE id = ?",
                (plan_id,),
            )
            await self.bus.publish(
                Event(
                    type="log",
                    agent="trader",
                    level="warning",
                    message=(
                        f"{row['symbol']} 플랜 #{plan_id} 래더 소멸 — 진입 레그 "
                        f"전량 취소/거부, abandoned (심볼 재진입 차단 해제)"
                    ),
                    data={"plan_id": plan_id},
                )
            )

    async def _enforce_plan_ttl(self, broker: Broker, now_ms: int) -> None:
        """진입 전 plan_ttl_bars 경과 → 래더 전량 취소 (abandoned)."""
        tf_ms = _TF_MS.get(self.settings.execution_timeframe, _TF_MS["15m"])
        ttl_ms = self.settings.plan_ttl_bars * tf_ms
        held = {(p.symbol, p.side) for p in await broker.get_positions()}
        for row in open_plans(self.db):
            if float(row["filled_fraction"] or 0.0) > 0:
                continue
            # 부분 체결이 미러에 아직 반영되지 않았어도(라이브 partially_filled)
            # 같은 방향 실제 포지션이 있으면 abandon 금지 — 고아 포지션 방지
            # (finding #17). 포지션은 스탑엑싯/종료 경로가 정리한다.
            if (row["symbol"], row["side"]) in held:
                continue
            created_ms = _parse_iso_ms(row["created_at"])
            if now_ms - created_ms < ttl_ms:
                continue
            plan_id = int(row["id"])
            await self._cancel_plan_orders(
                broker, plan_id, row["symbol"], "플랜 TTL 만료 — abandoned 취소"
            )
            self.db.execute(
                "UPDATE trade_plans SET status = 'abandoned', "
                "reject_reason = '플랜 TTL 만료' WHERE id = ?",
                (plan_id,),
            )
            await self.bus.publish(
                Event(
                    type="log",
                    agent="trader",
                    level="warning",
                    message=(
                        f"{row['symbol']} 플랜 #{plan_id} TTL 만료 — "
                        f"시장이 떠남, 래더 전량 취소 (abandoned)"
                    ),
                    data={"plan_id": plan_id},
                )
            )

    # -- 4h-close stop judgment ---------------------------------------------------------
    async def _judge_4h_stops(self, broker: Broker, now_ms: int) -> None:
        """완결된 4h봉마다 1회 손절 판정 (4h 마감 후 첫 틱에서 실행).

        포지션 보유 중 이탈 → 자식 주문 전량 취소 + 공격적 reduce-only
        스탑엑싯 (taker). 진입 전 이탈 → 플랜 무효화(abandoned).
        미완결 4h봉으로는 절대 판정하지 않는다."""
        # 헤지 모드(long_short_mode) 대응: (심볼, 방향)으로 키잉해 같은 심볼의
        # 반대 방향(수동) 포지션과 섞이지 않게 한다 (finding #5).
        positions = {(p.symbol, p.side): p for p in await broker.get_positions()}
        for row in open_plans(self.db):
            plan_id = int(row["id"])
            plan = TradePlan.from_json(row["plan_json"])
            bars = self.db.execute(
                "SELECT ts, close FROM ohlcv_cache "
                "WHERE symbol = ? AND timeframe = '4h' AND ts + ? <= ? "
                "ORDER BY ts DESC LIMIT 1",
                (plan.symbol, _4H_MS, now_ms),
            )
            if not bars:
                continue
            close_ms = int(bars[0]["ts"]) + _4H_MS
            created_ms = _parse_iso_ms(row["created_at"])
            watermark = self._judged_4h.get(plan_id, created_ms)
            if close_ms <= watermark:
                continue  # 이 4h봉은 이미 판정했다
            close = float(bars[0]["close"])
            breached = (
                close < plan.stop.price
                if plan.side == "long"
                else close > plan.stop.price
            )
            if not breached:
                self._judged_4h[plan_id] = close_ms
                continue
            pos = positions.get((plan.symbol, plan.side))
            if pos is not None:
                await self._cancel_plan_orders(
                    broker, plan_id, plan.symbol, "손절 판정 — 잔여 주문 취소"
                )
                await self._place_stop_exit(broker, plan_id, plan, pos.qty, 0)
                self.db.execute(
                    "UPDATE trade_plans SET status = 'stopped' WHERE id = ?",
                    (plan_id,),
                )
                await self.bus.publish(
                    Event(
                        type="log",
                        agent="trader",
                        level="warning",
                        message=(
                            f"{plan.symbol} 4h 종가 {close:,.4f} 손절선 "
                            f"{plan.stop.price:,.4f} 이탈 — 손절 청산 진행 "
                            f"(플랜 #{plan_id})"
                        ),
                        data={"plan_id": plan_id, "close": close},
                    )
                )
                # 판정 결과가 모두 실행된 뒤에만 워터마크 전진 — 취소/발주
                # 중 예외가 나면 다음 틱이 같은 4h봉을 재판정한다 (멱등).
                self._judged_4h[plan_id] = close_ms
            else:
                await self._cancel_plan_orders(
                    broker, plan_id, plan.symbol,
                    "4h 종가 손절선 이탈 — 플랜 무효화 취소",
                )
                self.db.execute(
                    "UPDATE trade_plans SET status = 'abandoned', "
                    "reject_reason = '4h 종가 손절선 이탈' WHERE id = ?",
                    (plan_id,),
                )
                await self.bus.publish(
                    Event(
                        type="log",
                        agent="trader",
                        level="warning",
                        message=(
                            f"{plan.symbol} 진입 전 4h 종가 손절선 이탈 — "
                            f"플랜 #{plan_id} 무효화, 래더 전량 취소"
                        ),
                        data={"plan_id": plan_id, "close": close},
                    )
                )
                self._judged_4h[plan_id] = close_ms

    async def _place_stop_exit(
        self, broker: Broker, plan_id: int, plan: TradePlan, qty: float, attempt: int
    ) -> None:
        """스탑엑싯: live는 reduce-only 리밋 체이스, paper는 공격적
        reduce-only 크로싱 리밋 (다음 1m 시가 taker 체결)."""
        exit_side = "sell" if plan.side == "long" else "buy"
        # 멱등 가드 — 같은 (plan, attempt) 스탑엑싯이 이미 존재하면 재발주
        # 금지 (부분 실패 후 재판정 시 중복 주문 방지).
        coid = client_order_id(plan_id, "stop-exit", 0, attempt)
        if self.db.execute(
            "SELECT id FROM paper_orders WHERE client_order_id = ?", (coid,)
        ):
            return
        chase = getattr(broker, "stop_exit_chase", None)
        if chase is not None:
            # attempt(=기존 스탑엑싯 미러 행 수)를 체이스 coid 세대(generation)로
            # 넘긴다 — 재발주마다 파싱 가능한 신선한 clOrdId를 쓰게 해
            # 재호출 시 clOrdId 재사용/무한 루프를 막는다 (finding #11/#12/#16).
            await chase(
                plan.symbol, exit_side, qty, plan_id=plan_id, start_attempt=attempt
            )
            return
        quote = await broker.get_quote(plan.symbol)
        await broker.place_order(
            OrderRequest(
                symbol=plan.symbol,
                side=exit_side,
                qty=qty,
                limit_price=float(quote.price),
                reduce_only=True,
                aggressive=True,
                leverage=plan.leverage,
                plan_id=plan_id,
                client_order_id=coid,
            )
        )

    async def _drive_stop_exits(self, broker: Broker, now_ms: int) -> None:
        """체이스 드라이버: 스탑엑싯 주문이 체결되지 않고 대기 시간이 지나면
        현재가로 cancel-replace (최대 STOP_CHASE_ATTEMPTS회).

        복구 패스가 먼저 돈다: stopped 플랜에 포지션이 남아 있는데 오픈
        스탑엑싯 주문이 하나도 없으면(취소~재발주 사이 크래시 등) 새
        스탑엑싯을 발주한다 — 손절 판정된 포지션은 반드시 청산된다."""
        # (심볼, 방향)으로 키잉 — 헤지 모드 반대 방향 포지션과 섞이지 않게 (finding #5).
        positions = {(p.symbol, p.side): p for p in await broker.get_positions()}
        for row in self.db.execute(
            "SELECT * FROM trade_plans WHERE status = 'stopped' ORDER BY id"
        ):
            plan_id = int(row["id"])
            pos = positions.get((row["symbol"], row["side"]))
            if pos is None or pos.qty <= 1e-12:
                continue
            # 포지션 소유권 가드 (2026-07-21 실계정 무한 재발주 사고 회귀 방지):
            # 이 심볼에 더 새로운 플랜이 하나라도 있으면(상태 불문 — closed/
            # abandoned/rejected 포함) 그 플랜이 생성됐다는 사실 자체가 옛
            # 포지션이 이미 정리됐음을 증명한다 → 지금 포지션은 이 옛 stopped
            # 플랜 소유가 아니다. 절대 복구 발주하지 않는다 (finding #1/#8).
            newest = self.db.execute(
                "SELECT id, status FROM trade_plans WHERE symbol = ? "
                "ORDER BY id DESC LIMIT 1",
                (row["symbol"],),
            )[0]
            if int(newest["id"]) != plan_id:
                # 최신 플랜이 종결 상태(approved/active/stopped 아님)인데도
                # 같은 방향 포지션이 남았다면 어떤 플랜도 관리하지 않는 고아/
                # 수동 포지션이다 — 발주 금지, 경고만 (심볼·방향당 1회).
                if newest["status"] not in ("approved", "active", "stopped"):
                    await self._warn_orphan_position(row["symbol"], row["side"], pos)
                continue
            if self.db.execute(
                "SELECT id FROM paper_orders WHERE plan_id = ? "
                "AND status = 'open' AND client_order_id LIKE '%-stop-exit-%'",
                (plan_id,),
            ):
                continue
            prior = self.db.execute(
                "SELECT COUNT(*) AS n FROM paper_orders WHERE plan_id = ? "
                "AND client_order_id LIKE '%-stop-exit-%'",
                (plan_id,),
            )[0]["n"]
            plan = TradePlan.from_json(row["plan_json"])
            await self._place_stop_exit(
                broker, plan_id, plan, float(pos.qty), int(prior)
            )
            await self.bus.publish(
                Event(
                    type="log",
                    agent="trader",
                    level="warning",
                    message=(
                        f"{row['symbol']} 스탑엑싯 복구 재발주 — stopped 플랜 "
                        f"#{plan_id}에 오픈 청산 주문 없음"
                    ),
                    data={"plan_id": plan_id},
                )
            )
        rows = self.db.execute(
            "SELECT * FROM paper_orders WHERE status = 'open' "
            "AND client_order_id LIKE '%-stop-exit-%' ORDER BY id"
        )
        for r in rows:
            leg = parse_client_order_id(r["client_order_id"])
            if leg is None:
                continue
            plan_id, _, _, attempt = leg
            placed_ms = _parse_iso_ms(r["ts"])
            if now_ms - placed_ms < STOP_CHASE_WAIT_MS:
                continue
            if attempt + 1 >= STOP_CHASE_ATTEMPTS:
                continue  # 마지막 시도 유지 (공격적 주문은 다음 봉에 체결)
            plan_rows = self.db.execute(
                "SELECT plan_json FROM trade_plans WHERE id = ?", (plan_id,)
            )
            if not plan_rows:
                continue
            plan = TradePlan.from_json(plan_rows[0]["plan_json"])
            await broker.cancel_order(str(r["id"]), r["symbol"])
            await self._place_stop_exit(
                broker, plan_id, plan, float(r["qty"]), attempt + 1
            )
            await self.bus.publish(
                Event(
                    type="log",
                    agent="trader",
                    message=(
                        f"{r['symbol']} 스탑엑싯 체이스 — 현재가 재발주 "
                        f"({attempt + 1}회차)"
                    ),
                    data={"plan_id": plan_id, "attempt": attempt + 1},
                )
            )

    async def _close_finished_plans(self, broker: Broker, now_ms: int) -> None:
        """포지션이 사라진 active 플랜 종료: 강제 청산이면 stopped(전액 손실),
        아니면 최종 익절 closed — 어느 쪽이든 자식 주문 전량 취소 (스펙 §2)."""
        # (심볼, 방향) 집합 — 헤지 모드 반대 방향 포지션이 플랜을 영구 active로
        # 고정하지 못하게 (finding #5).
        positions = {(p.symbol, p.side) for p in await broker.get_positions()}
        for row in self.db.execute(
            "SELECT * FROM trade_plans WHERE status IN ('active', 'stopped') "
            "ORDER BY id"
        ):
            plan_id = int(row["id"])
            symbol = row["symbol"]
            side = row["side"]
            if float(row["filled_fraction"] or 0.0) <= 0:
                continue
            if (symbol, side) in positions:
                continue
            open_orders = self.db.execute(
                "SELECT id FROM paper_orders WHERE plan_id = ? AND status = 'open'",
                (plan_id,),
            )
            # ts 형식 정규화 비교 — paper_orders.ts는 'T' 구분자(+00:00),
            # trade_plans.created_at은 SQLite datetime('now') 공백 구분자라
            # 바이트 비교가 어긋난다. 두 쪽 다 'YYYY-MM-DD HH:MM:SS'로
            # 잘라 비교하고, 청산 귀속은 다음 플랜 생성 시각으로 상한을 둔다.
            next_created = self.db.execute(
                "SELECT MIN(created_at) AS c FROM trade_plans "
                "WHERE symbol = ? AND id > ?",
                (symbol, plan_id),
            )[0]["c"]
            liq_params: list = [symbol, plan_id, row["created_at"]]
            upper_sql = ""
            if next_created is not None:
                upper_sql = (
                    "AND substr(replace(ts, 'T', ' '), 1, 19) < "
                    "substr(replace(?, 'T', ' '), 1, 19) "
                )
                liq_params.append(next_created)
            liq_rows = self.db.execute(
                "SELECT id, plan_id FROM paper_orders WHERE symbol = ? "
                "AND status = 'filled' AND reason LIKE '%강제 청산%' "
                "AND (plan_id = ? OR (plan_id IS NULL AND "
                "substr(replace(ts, 'T', ' '), 1, 19) >= "
                "substr(replace(?, 'T', ' '), 1, 19) " + upper_sql + ")) ORDER BY id",
                tuple(liq_params),
            )
            if row["status"] == "stopped":
                # 스탑엑싯 체결 완료 — 잔여 주문만 정리.
                if open_orders:
                    await self._cancel_plan_orders(
                        broker, plan_id, symbol, "손절 판정 — 잔여 주문 취소"
                    )
                continue
            if liq_rows:
                # 귀속한 plan_id-NULL 청산 행에 plan_id를 스탬핑 — 이후
                # trade-history가 심볼+윈도가 아니라 plan_id로만 청산을
                # 집계할 수 있게 (finding #3, 재진입 이중계상 방지).
                for lr in liq_rows:
                    if lr["plan_id"] is None:
                        self.db.execute(
                            "UPDATE paper_orders SET plan_id = ? WHERE id = ?",
                            (plan_id, lr["id"]),
                        )
                await self._cancel_plan_orders(
                    broker, plan_id, symbol, "청산으로 취소"
                )
                self.db.execute(
                    "UPDATE trade_plans SET status = 'stopped', "
                    "reject_reason = '강제 청산' WHERE id = ?",
                    (plan_id,),
                )
                await self.bus.publish(
                    Event(
                        type="log",
                        agent="trader",
                        level="warning",
                        message=(
                            f"{symbol} 강제 청산 감지 — 플랜 #{plan_id} 종료, "
                            f"격리마진 전액 손실"
                        ),
                        data={"plan_id": plan_id},
                    )
                )
                continue
            # 라이브 강제 청산 폴백 (finding #2/#10/#14/#19): 봇이 낸 reduce-only
            # 체결(익절/스탑엑싯)이 하나도 없이 포지션이 사라졌다면 거래소측
            # 강제 청산 서명이다 — '최종 익절'로 오분류하지 말고 청산 의심으로
            # stopped 처리 + 경고. (paper 청산은 위 liq_rows 경로로 잡힌다.)
            bot_exit = self.db.execute(
                "SELECT id FROM paper_orders WHERE plan_id = ? AND status = 'filled' "
                "AND reduce_only = 1 LIMIT 1",
                (plan_id,),
            )
            if not bot_exit:
                await self._cancel_plan_orders(
                    broker, plan_id, symbol, "강제 청산 의심 — 잔여 주문 취소"
                )
                self.db.execute(
                    "UPDATE trade_plans SET status = 'stopped', "
                    "reject_reason = '강제 청산 의심' WHERE id = ?",
                    (plan_id,),
                )
                await self.bus.publish(
                    Event(
                        type="log",
                        agent="trader",
                        level="warning",
                        message=(
                            f"{symbol} 포지션 소멸 — 플랜 #{plan_id} 청산 의심 "
                            f"(봇 청산 체결 없음), 손절 처리"
                        ),
                        data={"plan_id": plan_id},
                    )
                )
                continue
            await self._cancel_plan_orders(
                broker, plan_id, symbol, "최종 익절 — 잔여 주문 취소"
            )
            self.db.execute(
                "UPDATE trade_plans SET status = 'closed' WHERE id = ?",
                (plan_id,),
            )
            await self.bus.publish(
                Event(
                    type="log",
                    agent="trader",
                    message=(
                        f"{symbol} 포지션 종료 — 플랜 #{plan_id} closed, "
                        f"잔여 주문 전량 취소"
                    ),
                    data={"plan_id": plan_id},
                )
            )

    async def _liquidation_warnings(self, broker: Broker) -> None:
        """markPrice가 청산가 LIQ_WARN_BAND 이내로 접근하면 경고 이벤트."""
        positions = await broker.get_positions()
        seen: set[str] = set()
        for pos in positions:
            mark = pos.mark_price
            if not mark or pos.liq_price <= 0:
                continue
            gap = abs(mark - pos.liq_price) / mark
            if gap > LIQ_WARN_BAND:
                continue
            seen.add(pos.symbol)
            if pos.symbol in self._warned_liq:
                continue
            self._warned_liq.add(pos.symbol)
            await self.bus.publish(
                Event(
                    type="liquidation_warning",
                    agent="risk",
                    level="warning",
                    message=(
                        f"{pos.symbol} 청산 경고 — markPrice {mark:,.4f} / "
                        f"청산가 {pos.liq_price:,.4f} (여유 {gap:.1%})"
                    ),
                    data={
                        "symbol": pos.symbol,
                        "mark_price": mark,
                        "liq_price": pos.liq_price,
                    },
                )
            )
        # 밴드를 벗어난 심볼은 다시 경고할 수 있게 리셋.
        self._warned_liq &= seen

    # -- helpers -------------------------------------------------------------------
    async def _warn_orphan_position(self, symbol: str, side: str, pos) -> None:
        """최신 플랜이 종결 상태인데 같은 방향 포지션이 남아 있는 고아/수동
        포지션 경고 — 절대 주문하지 않고 운영자에게 알리기만 (심볼·방향당 1회)."""
        key = (symbol, side)
        if key in self._warned_orphan:
            return
        self._warned_orphan.add(key)
        await self.bus.publish(
            Event(
                type="liquidation_warning",
                agent="risk",
                level="warning",
                message=(
                    f"{symbol} {side} 고아 포지션 감지 — 최신 플랜이 종결 상태인데 "
                    f"포지션 {pos.qty:g} 잔존 (수동/미러갭 의심), 자동 청산 금지 — "
                    f"운영자 확인 필요"
                ),
                data={"symbol": symbol, "side": side, "qty": pos.qty},
            )
        )

    async def _cancel_plan_orders(
        self, broker: Broker, plan_id: int, symbol: str, reason: str
    ) -> None:
        rows = self.db.execute(
            "SELECT id FROM paper_orders WHERE plan_id = ? AND status = 'open'",
            (plan_id,),
        )
        cancel_sync = getattr(broker, "_cancel_order_sync", None)
        for r in rows:
            if cancel_sync is not None:
                await asyncio.to_thread(cancel_sync, str(r["id"]), reason)
            else:
                await broker.cancel_order(str(r["id"]), symbol)
            await self.bus.publish(
                Event(
                    type="order_cancelled",
                    agent="trader",
                    message=f"{symbol} 주문 취소 — {reason}",
                    data={
                        "order_id": str(r["id"]),
                        "plan_id": plan_id,
                        "symbol": symbol,
                    },
                )
            )
