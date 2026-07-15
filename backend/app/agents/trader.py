"""Trader 태오 (Teo): builds TradePlans from the champion spec, passes them
through the RiskEngine with a live MarketState, places the laddered passive
limit entries via the broker provider, reconciles fills and keeps
``trade_plans.filled_fraction`` / reduce-only TP orders up to date.

플랜 상태머신 (스펙 §2): draft → approved → active(부분/전량 체결) →
closed|stopped|abandoned. 손절 판정·TTL·펀딩·청산 경고는 PositionMonitor
소관 — 태오는 trade 사이클 한 패스(정산 → 리컨실 → 신규 플랜 발주)만 진다.

멱등성: client_order_id = ``{plan_id}-{leg_kind}-{leg_index}-{attempt}``
결정론 생성 — 재기동/재실행 후에도 중복 주문 0건. 심볼에 이미 오픈 플랜이나
포지션이 있으면 신규 진입을 건너뛴다 (중복 진입 방지).
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import time

import pandas as pd

from ..broker.base import Broker, Order, OrderRequest
from ..config import Settings
from ..db import Database
from ..risk.engine import MarketState, RiskEngine
from ..risk.plan import TradePlan
from ..strategies.base import StrategySpec, generate_plan
from .base import AgentBase

#: 오픈 플랜으로 간주하는 상태 (신규 진입 스킵 + 마진 예산 합산 대상).
OPEN_PLAN_STATUSES = ("approved", "active")

#: TP 수량 재계산 허용 오차 — 이보다 크게 어긋나면 취소 후 재발주.
_QTY_TOL = 1e-9


def client_order_id(plan_id: int, leg_kind: str, leg_index: int, attempt: int) -> str:
    """결정론 client_order_id (스펙 §2 멱등성)."""
    return f"{plan_id}-{leg_kind}-{leg_index}-{attempt}"


def parse_client_order_id(coid: str | None) -> tuple[int, str, int, int] | None:
    """``{plan_id}-{leg_kind}-{leg_index}-{attempt}`` → tuple, else None."""
    if not coid:
        return None
    parts = coid.split("-")
    if len(parts) < 4:
        return None
    try:
        return int(parts[0]), "-".join(parts[1:-2]), int(parts[-2]), int(parts[-1])
    except ValueError:
        return None


def _now_ms(broker: Broker, now_ms: int | None = None) -> int:
    if now_ms is not None:
        return now_ms
    clock = getattr(broker, "clock", None)
    if callable(clock):
        return int(clock())
    return int(time.time() * 1000)


def open_plans(db: Database) -> list[dict]:
    return db.execute(
        "SELECT * FROM trade_plans WHERE status IN ('approved', 'active') "
        "ORDER BY id"
    )


def open_plan_margin(db: Database) -> float:
    """Σ 오픈(approved|active) 플랜 마진 — 복리 금지 게이트 입력 (스펙 §2)."""
    total = 0.0
    for row in open_plans(db):
        try:
            total += float(json.loads(row["plan_json"]).get("margin_usdt") or 0.0)
        except (ValueError, TypeError):
            continue
    return total


def blackout_windows(
    db: Database, settings: Settings, now_ms: int
) -> tuple[tuple[int, int], ...]:
    """econ_events ±blackout_hours 블랙아웃 윈도 (규칙 §2)."""
    half = int(settings.blackout_hours * 3_600_000)
    windows: list[tuple[int, int]] = []
    for row in db.execute("SELECT ts FROM econ_events"):
        try:
            ts = dt.datetime.fromisoformat(str(row["ts"]))
        except (ValueError, TypeError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        event_ms = int(ts.timestamp() * 1000)
        if abs(event_ms - now_ms) <= half + 86_400_000:  # 근접 이벤트만
            windows.append((event_ms - half, event_ms + half))
    return tuple(windows)


class Trader(AgentBase):
    id = "trader"
    name = "태오"
    role = "Trader"

    # -- settlement --------------------------------------------------------------
    async def settle(self, broker: Broker, now_ms: int | None = None) -> list[Order]:
        """Run the paper broker's bar-close settlement (fills/funding/청산/TTL),
        skim the compounding-forbidden withdrawal, and log every change.
        (live 브로커는 거래소가 정산하므로 no-op.)"""
        settle_fn = getattr(broker, "settle", None)
        if settle_fn is None:
            return []
        await self.set_state("working", "페이퍼 정산 — 마감 봉 체결/펀딩/청산")
        changed: list[Order] = await asyncio.to_thread(settle_fn, now_ms)
        for order in changed:
            await self._log_order(order)
        skim = getattr(broker, "skim_withdrawal", None)
        if skim is not None:
            amount = await asyncio.to_thread(skim, now_ms)
            if amount > 0:
                await self.log(
                    f"시드 초과 수익 {amount:,.2f} USDT 출금 원장 분리 — "
                    f"복리 금지 (시드 고정)",
                    amount=amount,
                )
        if changed and hasattr(broker, "snapshot"):
            await asyncio.to_thread(broker.snapshot)
        await self.set_state("idle")
        return changed

    # -- fill reconciliation ------------------------------------------------------
    async def reconcile(
        self, db: Database, broker: Broker, settings: Settings
    ) -> None:
        """Reconcile fills into the plan state machine: update
        ``filled_fraction`` from filled entry legs, promote approved → active,
        and (re)size the reduce-only TP ladder to the actually filled qty
        (TP 수량은 매 체결 이벤트마다 실제 체결 수량 기준 재계산, 스펙 §2)."""
        positions = {p.symbol: p for p in await broker.get_positions()}
        for row in open_plans(db):
            plan_id = int(row["id"])
            plan = TradePlan.from_json(row["plan_json"])
            orders = db.execute(
                "SELECT * FROM paper_orders WHERE plan_id = ? ORDER BY id",
                (plan_id,),
            )
            filled_fraction = 0.0
            filled_qty = 0.0
            filled_legs: set[int] = set()
            for o in orders:
                leg = parse_client_order_id(o["client_order_id"])
                if leg is None or leg[1] != "entry" or o["status"] != "filled":
                    continue
                _, _, leg_index, _ = leg
                if leg_index in filled_legs or leg_index >= len(plan.entries):
                    continue
                filled_legs.add(leg_index)
                filled_fraction += plan.entries[leg_index].fraction
                filled_qty += float(o["filled_qty"] or 0.0)
            if abs(filled_fraction - float(row["filled_fraction"] or 0.0)) > _QTY_TOL:
                db.execute(
                    "UPDATE trade_plans SET filled_fraction = ? WHERE id = ?",
                    (filled_fraction, plan_id),
                )
            if filled_fraction > 0 and row["status"] == "approved":
                db.execute(
                    "UPDATE trade_plans SET status = 'active' WHERE id = ?",
                    (plan_id,),
                )
                await self.log(
                    f"{plan.symbol} 분할 진입 {len(filled_legs)}/"
                    f"{len(plan.entries)} 체결 — 플랜 #{plan_id} active "
                    f"(체결 비중 {filled_fraction:.0%})",
                    plan_id=plan_id,
                    symbol=plan.symbol,
                    filled_fraction=filled_fraction,
                )
            if filled_qty > 0 and plan.symbol in positions:
                await self._maintain_tp_orders(
                    db, broker, plan_id, plan, filled_qty, orders
                )

    async def _maintain_tp_orders(
        self,
        db: Database,
        broker: Broker,
        plan_id: int,
        plan: TradePlan,
        filled_qty: float,
        orders: list[dict],
    ) -> None:
        """분할 익절 reduce-only 레그를 실제 체결 수량 기준으로 유지한다.

        마지막 레그는 백테스트의 final=True와 동일하게 **잔량 전량**으로
        사이징한다 — 앞 레그들의 stepSize 절사로 생기는 더스트가 최종
        익절 뒤에도 남아 플랜이 영구 active로 고이는 것을 방지 (규칙 §3
        '2차 저항에서 잔량 전량').
        """
        exit_side = "sell" if plan.side == "long" else "buy"
        by_leg: dict[int, list[dict]] = {}
        for o in orders:
            parsed = parse_client_order_id(o["client_order_id"])
            if parsed and parsed[1] == "tp":
                by_leg.setdefault(parsed[2], []).append(o)
        tp_filled = sum(
            float(o["filled_qty"] or 0.0)
            for legs in by_leg.values()
            for o in legs
            if o["status"] == "filled"
        )
        remaining = max(0.0, filled_qty - tp_filled)
        reserved = 0.0  # 앞선 레그의 오픈 주문이 점유 중인 수량
        for i, leg in enumerate(plan.tps):
            is_final = i == len(plan.tps) - 1
            desired = (
                max(0.0, remaining - reserved)
                if is_final
                else filled_qty * leg.fraction
            )
            leg_orders = by_leg.get(i, [])
            open_order = next(
                (o for o in leg_orders if o["status"] == "open"), None
            )
            if any(o["status"] == "filled" for o in leg_orders):
                continue  # 이 레그는 이미 익절 완료
            if desired <= _QTY_TOL:
                continue
            if open_order is not None:
                if abs(float(open_order["qty"]) - desired) <= max(
                    _QTY_TOL, desired * 1e-6
                ):
                    if not is_final:
                        reserved += float(open_order["qty"])
                    continue
                # 체결 수량이 늘었다 → 기존 TP 취소 후 재계산 수량으로 재발주.
                await broker.cancel_order(str(open_order["id"]), plan.symbol)
                await self.log(
                    f"{plan.symbol} 분할 익절 레그 {i + 1} 수량 재계산 — "
                    f"{float(open_order['qty']):g} → {desired:g}",
                    plan_id=plan_id,
                )
            attempt = len(leg_orders)
            order = await broker.place_order(
                OrderRequest(
                    symbol=plan.symbol,
                    side=exit_side,
                    qty=desired,
                    limit_price=leg.price,
                    reduce_only=True,
                    leverage=plan.leverage,
                    plan_id=plan_id,
                    client_order_id=client_order_id(plan_id, "tp", i, attempt),
                )
            )
            await self._log_order(order)
            if not is_final and order.status == "open":
                reserved += float(order.qty)

    # -- plan building + order placement -------------------------------------------
    async def execute(
        self,
        spec: StrategySpec,
        data: dict[str, dict[str, pd.DataFrame]],
        db: Database,
        broker: Broker,
        settings: Settings,
        regime: str,
        *,
        risk_agent=None,
        now_ms: int | None = None,
    ) -> list[Order]:
        """One trade pass: generate the champion's TradePlan per symbol,
        risk-review it against the live MarketState, persist the plan row and
        place the passive limit entry ladder. Returns the placed orders."""
        await self.set_state("working", f"챔피언 {spec.id_key()} 플랜 산출")
        now = _now_ms(broker, now_ms)
        positions = {p.symbol: p for p in await broker.get_positions()}
        balance = await broker.get_balance()
        wallet = float(balance.wallet_balance)
        seed = settings.initial_seed_usdt
        # 복리 금지 사이징: effective_capital = min(지갑, 시드) (스펙 §2).
        margin_budget = min(wallet, seed) / max(1, settings.max_concurrent_positions)
        plan_margin = open_plan_margin(db)
        open_symbols = {row["symbol"] for row in open_plans(db)}
        blackouts = blackout_windows(db, settings, now)
        daily_pnl = self._daily_realized_pnl(db, wallet)

        orders: list[Order] = []
        for symbol, frames in data.items():
            if symbol in open_symbols:
                await self.log(
                    f"{symbol} 오픈 플랜 유지 — 중복 진입 방지", symbol=symbol
                )
                continue
            if symbol in positions:
                continue  # 포지션 보유 중 — 플랜 종료 전 신규 진입 없음
            plan = await asyncio.to_thread(
                self._build_plan, spec, frames, regime, symbol
            )
            if plan is None:
                continue
            plan = dataclasses.replace(plan, margin_usdt=margin_budget)
            try:
                quote = await broker.get_quote(symbol)
                mark = float(quote.price)
            except Exception as exc:  # noqa: BLE001
                await self.log(
                    f"{symbol} 시세 조회 실패 — 플랜 스킵: {exc}",
                    level="error",
                    symbol=symbol,
                )
                continue
            state = MarketState(
                as_of_ts=now,
                mark_price=mark,
                open_positions=len(positions),
                daily_realized_pnl=daily_pnl,
                blackout_windows=blackouts,
                open_plan_margin=plan_margin,
                wallet_balance=wallet,
            )
            if risk_agent is not None:
                verdict = await risk_agent.review_plan(plan, settings, state)
            else:
                verdict = RiskEngine.review(plan, settings, state)
            if not verdict.approved:
                db.execute(
                    "INSERT INTO trade_plans (symbol, side, plan_json, status, "
                    "reject_reason) VALUES (?, ?, ?, 'rejected', ?)",
                    (plan.symbol, plan.side, plan.to_json(), verdict.reason),
                )
                await self.log(
                    f"{symbol} 플랜 거부 — {verdict.reason}",
                    level="warning",
                    symbol=symbol,
                    reason=verdict.reason,
                )
                continue
            placed = await self._place_plan(db, broker, plan)
            if placed:
                plan_margin += plan.margin_usdt
                open_symbols.add(symbol)
                orders.extend(placed)

        if not orders:
            await self.log("이번 사이클 신규 진입 없음", strategy=spec.id_key())
        if hasattr(broker, "snapshot"):
            await asyncio.to_thread(broker.snapshot)
        await self.set_state("idle")
        return orders

    @staticmethod
    def _build_plan(
        spec: StrategySpec,
        frames: dict[str, pd.DataFrame],
        regime: str,
        symbol: str,
    ) -> TradePlan | None:
        try:
            return generate_plan(spec, frames, regime, symbol=symbol)
        except Exception:  # noqa: BLE001 — a bad symbol must not kill the pass
            return None

    async def _place_plan(
        self, db: Database, broker: Broker, plan: TradePlan
    ) -> list[Order]:
        """플랜 행(approved) 기록 후 분할 진입 패시브 래더 발주."""
        rows = db.execute(
            "INSERT INTO trade_plans (symbol, side, plan_json, status) "
            "VALUES (?, ?, ?, 'approved')",
            (plan.symbol, plan.side, plan.to_json()),
        )
        plan_id = int(rows[0]["id"])
        await self.log(
            f"{plan.symbol} {plan.side} 플랜 #{plan_id} 승인 — 분할 진입 "
            f"{len(plan.entries)}레그, RR 1:{plan.rr:.2f}, x{plan.leverage}, "
            f"근거: {' / '.join(plan.evidence[:2])}",
            plan_id=plan_id,
            symbol=plan.symbol,
            side=plan.side,
            evidence=plan.evidence,
        )
        try:
            await broker.set_margin_mode(plan.symbol, "isolated")
            await broker.set_leverage(plan.symbol, plan.leverage)
        except Exception as exc:  # noqa: BLE001
            await self.log(
                f"{plan.symbol} 레버리지/마진 설정 실패: {exc}",
                level="warning",
                symbol=plan.symbol,
            )
        entry_side = "buy" if plan.side == "long" else "sell"
        placed: list[Order] = []
        for i, leg in enumerate(plan.entries):
            qty = plan.margin_usdt * plan.leverage * leg.fraction / leg.price
            order = await broker.place_order(
                OrderRequest(
                    symbol=plan.symbol,
                    side=entry_side,
                    qty=qty,
                    limit_price=leg.price,
                    leverage=plan.leverage,
                    plan_id=plan_id,
                    client_order_id=client_order_id(plan_id, "entry", i, 0),
                )
            )
            await self._log_order(order, prefix="분할 진입 레그 발주 — ")
            if order.status in ("open", "filled"):
                placed.append(order)
        if not placed:
            # 래더 전 레그 거부 → 시나리오 실행 불가, 플랜 abandoned.
            db.execute(
                "UPDATE trade_plans SET status = 'abandoned', "
                "reject_reason = '전 레그 발주 거부' WHERE id = ?",
                (plan_id,),
            )
            await self.log(
                f"{plan.symbol} 플랜 #{plan_id} 전 레그 발주 거부 — abandoned",
                level="warning",
                plan_id=plan_id,
            )
        return placed

    @staticmethod
    def _daily_realized_pnl(db: Database, wallet: float) -> float:
        """오늘(UTC) 실현 손익 (일손실 서킷브레이커 입력).

        기준선 = **어제까지의 마지막** 지갑 스냅샷 — 오늘 첫 스냅샷을 쓰면
        자정~첫 스냅샷 사이에 실현된 손실(모니터의 손절/청산)이 영구히
        누락된다. 출금 스윕은 손실이 아니므로 오늘 출금액을 되더한다.
        (substr 비교: 스냅샷/원장 ts가 'T'·공백 어느 형식이어도 동일 동작.)
        """
        rows = db.execute(
            "SELECT wallet_balance FROM portfolio_snapshots "
            "WHERE substr(ts, 1, 10) < date('now') ORDER BY id DESC LIMIT 1"
        )
        if rows:
            baseline = float(rows[0]["wallet_balance"])
        else:
            first_today = db.execute(
                "SELECT wallet_balance FROM portfolio_snapshots "
                "WHERE substr(ts, 1, 10) = date('now') ORDER BY id LIMIT 1"
            )
            if not first_today:
                return 0.0
            baseline = float(first_today[0]["wallet_balance"])
        skimmed = db.execute(
            "SELECT COALESCE(SUM(amount), 0.0) AS s FROM withdrawal_ledger "
            "WHERE substr(ts, 1, 10) = date('now')"
        )[0]["s"]
        return wallet - baseline + float(skimmed or 0.0)

    async def _log_order(self, order: Order, prefix: str = "") -> None:
        side = "매수" if order.side == "buy" else "매도"
        price = order.limit_price
        price_txt = f"{price:,.4f}" if price is not None else "–"
        base = f"{order.symbol} {side} {order.qty:g} @ {price_txt}"
        data = {
            "order_id": order.id,
            "symbol": order.symbol,
            "side": order.side,
            "qty": order.qty,
            "plan_id": order.plan_id,
            "status": order.status,
        }
        if order.status == "filled":
            fill = order.avg_fill_price
            fill_txt = f"{fill:,.4f}" if fill is not None else price_txt
            await self.log(
                f"{prefix}{base} 체결 (체결가 {fill_txt}) {order.reason}".strip(),
                **data,
            )
        elif order.status == "open":
            await self.log(
                f"{prefix}{base} 지정가 접수 — 관통 체결 대기 (post-only)",
                **data,
            )
        elif order.status in ("cancelled", "expired"):
            await self.log(f"{base} {order.status} — {order.reason}", **data)
        else:
            await self.log(
                f"{prefix}{base} 거부 — {order.reason}",
                level="warning",
                reason=order.reason,
                **data,
            )
