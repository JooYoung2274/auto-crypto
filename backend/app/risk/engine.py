"""RiskEngine — 순수 함수 주문 전 게이트 (규칙 §1·§2·§3, 스펙 §2).

``review(plan, settings, market_state)``는 DB/시계 접근 없이 명시적
:class:`MarketState` 입력만으로 판정한다. 백테스트는 시뮬레이션 봉마다,
트레이더는 라이브 상태로 market_state를 구성해 **동일한 게이트**를 통과시킨다.

게이트 2계층:
- 정적 플랜 게이트: 화이트리스트, 레버리지 캡(BTC 10 / 그 외 5, 최소 3),
  근거≥2, 분할 구조(진입≥2·합1.0, TP≥2·합1.0), 기하 검증,
  RR(BTC·ETH ≥2, 알트 ≥3), 패시브 사이드, 청산 버퍼.
- 런타임 포트폴리오 게이트: 최대 동시 포지션, 일손실 서킷브레이커,
  이벤트 블랙아웃, 사이징(복리 금지: Σ오픈 플랜 마진 + 신규 마진 ≤ min(잔고, 시드)).

거부 사유는 한국어 문자열로 리포트/로그에 그대로 실린다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from ..config import Settings
from .plan import FRACTION_TOL, Side, TradePlan

#: RR 하한이 rr_min_major(≥2)로 적용되는 메이저 심볼 (규칙 §1).
MAJOR_SYMBOLS = ("BTCUSDT", "ETHUSDT")

#: 마진 예산 비교 허용 오차 (부동소수점).
_BUDGET_TOL = 1e-9


@dataclass(frozen=True)
class MarketState:
    """RiskEngine 입력 상태 — DB/시계 대신 호출자가 명시적으로 구성 (스펙 §2).

    - as_of_ts: 판정 기준 시각 (epoch ms)
    - mark_price: 심볼 markPrice (패시브 사이드 게이트 기준)
    - open_positions: 현재 열린 포지션 수
    - daily_realized_pnl: 오늘(UTC) 실현 손익 합 (USDT)
    - blackout_windows: 신규 진입 금지 (start_ms, end_ms) 구간들
      (econ_events ±blackout_hours 는 호출자가 전개해서 넣는다)
    - open_plan_margin: 오픈(approved|active) 플랜 마진 합 (USDT)
    - wallet_balance: 실현 지갑 잔고 (None → 시드로 간주)
    """

    as_of_ts: int
    mark_price: float
    open_positions: int = 0
    daily_realized_pnl: float = 0.0
    blackout_windows: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    open_plan_margin: float = 0.0
    wallet_balance: float | None = None


@dataclass(frozen=True)
class Approval:
    approved: bool = True
    reason: str = ""


@dataclass(frozen=True)
class Rejection:
    reason: str
    approved: bool = False


ReviewResult = Union[Approval, Rejection]


class RiskEngine:
    """순수 게이트 모음 — 상태 없음, 전부 정적 메서드."""

    # -- caps ------------------------------------------------------------------
    @staticmethod
    def leverage_cap(symbol: str, settings: Settings) -> int:
        """BTC 10배, 그 외(ETH 포함) 5배 (규칙 §1)."""
        if symbol == "BTCUSDT":
            return settings.btc_max_leverage
        return settings.alt_max_leverage

    @staticmethod
    def min_rr(symbol: str, settings: Settings) -> float:
        """BTC·ETH ≥ 1:2, 알트 ≥ 1:3 (규칙 §1)."""
        if symbol in MAJOR_SYMBOLS:
            return settings.rr_min_major
        return settings.rr_min_alt

    # -- main review -----------------------------------------------------------
    @staticmethod
    def review(
        plan: TradePlan, settings: Settings, market_state: MarketState
    ) -> ReviewResult:
        static = RiskEngine._review_static(plan, settings, market_state)
        if not static.approved:
            return static
        runtime = RiskEngine._review_runtime(plan, settings, market_state)
        if not runtime.approved:
            return runtime
        return Approval()

    # -- static plan gates (백테스트·트레이더 동일 적용) -------------------------
    @staticmethod
    def _review_static(
        plan: TradePlan, settings: Settings, state: MarketState
    ) -> ReviewResult:
        # 화이트리스트 = 저시총 금지 (규칙 §1).
        if plan.symbol not in settings.universe:
            return Rejection(f"화이트리스트 외 심볼 거부: {plan.symbol} (저시총 금지)")

        # 레버리지 캡 — BTC 10배 / 그 외 5배, 최소 3배.
        cap = RiskEngine.leverage_cap(plan.symbol, settings)
        if plan.leverage > cap:
            return Rejection(
                f"레버리지 {plan.leverage}배 > {plan.symbol} 한도 {cap}배"
            )
        if plan.leverage < settings.min_leverage:
            return Rejection(
                f"레버리지 {plan.leverage}배 < 최소 {settings.min_leverage}배"
            )

        # 근거 2개 이상 (규칙 §2).
        if len(plan.evidence) < 2:
            return Rejection(f"근거 {len(plan.evidence)}개 < 2개 — 관망")

        # 마진 양수.
        if plan.margin_usdt <= 0:
            return Rejection(f"마진 {plan.margin_usdt} USDT ≤ 0 — 플랜 무효")

        # 분할 구조 (규칙 §1 몰빵 금지): 진입 ≥2레그 합 1.0, TP ≥2레그 합 1.0.
        if len(plan.entries) < 2:
            return Rejection(f"분할 진입 레그 {len(plan.entries)}개 < 2개 (몰빵 금지)")
        if abs(plan.entries_fraction_sum - 1.0) > FRACTION_TOL:
            return Rejection(
                f"분할 진입 비중 합 {plan.entries_fraction_sum:.4f} ≠ 1.0"
            )
        if len(plan.tps) < 2:
            return Rejection(f"분할 익절 레그 {len(plan.tps)}개 < 2개")
        if abs(plan.tps_fraction_sum - 1.0) > FRACTION_TOL:
            return Rejection(f"분할 익절 비중 합 {plan.tps_fraction_sum:.4f} ≠ 1.0")

        # 기하 검증 — long: stop < entries < tps, short 역순.
        if not plan.geometry_ok():
            order_txt = (
                "손절 < 진입 < 익절" if plan.side == "long" else "익절 < 진입 < 손절"
            )
            return Rejection(f"기하 검증 실패: {plan.side}는 {order_txt} 순서여야 함")

        # 최소 손절 거리 — 진입가와 손절선이 붙으면 RR이 뻥튀기되고 진입 즉시
        # 무효화된다. 가중 진입가 대비 손절 거리가 하한 미만이면 거부.
        w_entry = plan.weighted_entry
        stop_dist = abs(w_entry - plan.stop.price)
        if w_entry > 0 and stop_dist / w_entry < settings.min_stop_distance_pct:
            return Rejection(
                f"손절 거리 {stop_dist / w_entry:.2%} < 최소 "
                f"{settings.min_stop_distance_pct:.2%} — 진입가·손절선 근접 거부"
            )

        # 손익비 게이트 (규칙 §1).
        min_rr = RiskEngine.min_rr(plan.symbol, settings)
        rr = plan.rr
        if rr < min_rr:
            return Rejection(f"손익비 1:{rr:.2f} < 최소 1:{min_rr:g} — 진입 거부")

        # 패시브 사이드 강제 — long 진입가 > mark 거부 (돌파는 눌림 리테스트로만).
        if state.mark_price > 0:
            if plan.side == "long" and any(
                leg.price > state.mark_price for leg in plan.entries
            ):
                return Rejection(
                    "패시브 사이드 위반: long 진입가가 markPrice 위 — 눌림 리테스트로만 진입"
                )
            if plan.side == "short" and any(
                leg.price < state.mark_price for leg in plan.entries
            ):
                return Rejection(
                    "패시브 사이드 위반: short 진입가가 markPrice 아래 — 되돌림 리테스트로만 진입"
                )

        # 청산 버퍼 — 손절가는 청산가보다 markPrice 쪽으로 (진입−청산 거리의)
        # liq_buffer_pct 이상 여유가 있어야 한다 (스펙 §2). 부분 체결 래더가
        # 승인된 손절선 위에서 청산되지 않도록 **가장 불리한 레그 단독 체결**
        # (worst-case) 청산가 기준으로 심사한다.
        liq = plan.worst_case_liq_price()
        entry_prices = [leg.price for leg in plan.entries]
        ref_entry = (
            max(entry_prices) if plan.side == "long" else min(entry_prices)
        )
        gap = abs(ref_entry - liq)
        buffer_needed = settings.liq_buffer_pct * gap
        if plan.side == "long":
            if plan.stop.price < liq + buffer_needed:
                return Rejection(
                    f"청산 버퍼 부족: 손절가 {plan.stop.price:.4f} — "
                    f"청산가 {liq:.4f} 대비 여유 {settings.liq_buffer_pct:.0%} 미만"
                )
        else:
            if plan.stop.price > liq - buffer_needed:
                return Rejection(
                    f"청산 버퍼 부족: 손절가 {plan.stop.price:.4f} — "
                    f"청산가 {liq:.4f} 대비 여유 {settings.liq_buffer_pct:.0%} 미만"
                )

        return Approval()

    # -- runtime portfolio gates (트레이더/모니터 전용) --------------------------
    @staticmethod
    def _review_runtime(
        plan: TradePlan, settings: Settings, state: MarketState
    ) -> ReviewResult:
        # 최대 동시 포지션.
        if state.open_positions >= settings.max_concurrent_positions:
            return Rejection(
                f"최대 동시 포지션 {settings.max_concurrent_positions}개 초과 — 신규 진입 거부"
            )

        # 일손실 서킷브레이커 (시드 기준).
        loss_limit = settings.daily_max_loss_pct * settings.initial_seed_usdt
        if state.daily_realized_pnl <= -loss_limit:
            return Rejection(
                f"일손실 {state.daily_realized_pnl:.2f} USDT ≤ 한도 -{loss_limit:.2f} USDT"
                " — 서킷브레이커 발동"
            )

        # 이벤트 블랙아웃 (CPI·FOMC ±blackout_hours, 규칙 §2).
        for start_ms, end_ms in state.blackout_windows:
            if start_ms <= state.as_of_ts <= end_ms:
                return Rejection("이벤트 블랙아웃 — 신규 진입 금지 (지표 발표 전후)")

        # 복리 금지 (규칙 §1): Σ오픈 플랜 마진 + 신규 마진 ≤ min(지갑, 시드).
        seed = settings.initial_seed_usdt
        wallet = state.wallet_balance if state.wallet_balance is not None else seed
        effective_capital = min(wallet, seed)
        if state.open_plan_margin + plan.margin_usdt > effective_capital + _BUDGET_TOL:
            return Rejection(
                f"마진 예산 초과: 오픈 {state.open_plan_margin:.2f} + 신규 "
                f"{plan.margin_usdt:.2f} > 가용 {effective_capital:.2f} USDT"
                " — 복리 금지 (시드 고정)"
            )

        return Approval()

    # -- stop modification (규칙 §3 '계획 라인 무조건 준수') ----------------------
    @staticmethod
    def review_stop_update(
        side: Side, current_stop: float, new_stop: float
    ) -> ReviewResult:
        """손절선 수정은 '유리한 방향'(타이트닝)만 허용.

        long: 손절선을 올리는 것만 허용, short: 내리는 것만 허용.
        불리하게 넓히는 수정(손절 미루기)은 거부.
        """
        favorable = new_stop >= current_stop if side == "long" else new_stop <= current_stop
        if favorable:
            return Approval()
        return Rejection(
            f"손절선 불리한 수정 거부: {current_stop} → {new_stop} ({side})"
            " — 계획 라인 무조건 준수, 손절 미루기 금지"
        )
