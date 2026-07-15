"""Risk Manager 로건 (Rogan): metrics filters on backtested strategies +
RiskEngine static gates on champion trade plans.

Metrics filters (연구 사이클): reject when the margin-equity MDD exceeds
``max_mdd``, when metrics could not be computed, when **any liquidation
occurred** (청산 즉시 탈락, 스펙 §4), when funding drag ate most of the gross
profit, or when the strategy is too passive (연 환산 거래 수 <
``min_trades_per_year``). Strategies whose total trade count is below
``min_trades`` pass but stay tagged low-confidence (excluded from ranking).

Plan gates (trade 사이클): every champion TradePlan goes through the pure
:class:`RiskEngine` (정적 게이트 + 런타임 포트폴리오 게이트) — 로건 logs the
Korean verdicts that end up in reports.
"""
from __future__ import annotations

from ..config import Settings
from ..risk.engine import MarketState, ReviewResult, RiskEngine
from ..risk.plan import TradePlan
from .base import AgentBase

#: 데이터 스팬을 알 수 없을 때의 연 환산 폴백 (quant가 avg_metrics에
#: trades_per_year를 실어 보내므로 실제로는 거의 쓰이지 않는다).
LOOKBACK_YEARS = 1.0

#: 펀딩 드래그 한도 — 총이익(gross) 중 펀딩으로 유출된 비율이 이 값을
#: 넘으면 전략의 엣지가 펀딩에 잠식된 것으로 보고 탈락시킨다.
FUNDING_DRAG_MAX = 0.5


def trades_per_year(avg_metrics: dict) -> float:
    """연 환산 거래 수 — quant가 계산한 값을 우선, 없으면 폴백 스팬."""
    tpy = avg_metrics.get("trades_per_year")
    if tpy is not None:
        return float(tpy)
    return (avg_metrics.get("trade_count") or 0) / LOOKBACK_YEARS


class Risk(AgentBase):
    id = "risk"
    name = "로건"
    role = "Risk Manager"

    async def review(
        self, results: dict[int, dict], settings: Settings
    ) -> tuple[list[int], dict[int, str]]:
        """Return (passed strategy ids, rejected strategy id → reason)."""
        await self.set_state("working", f"전략 {len(results)}개 리스크 필터링")
        passed: list[int] = []
        rejected: dict[int, str] = {}
        low_conf = 0
        low_activity = 0
        for strategy_id, res in results.items():
            avg = res["avg_metrics"]
            reason = self._reject_reason(avg, settings)
            if reason is not None:
                rejected[strategy_id] = reason
                if "활동성" in reason:
                    low_activity += 1
                continue
            passed.append(strategy_id)
            if res["low_confidence"]:
                low_conf += 1
        await self.log(
            f"필터링 완료 — 통과 {len(passed)}개 / 탈락 {len(rejected)}개 "
            f"(저신뢰 태깅 {low_conf}개 / 활동성 부족 {low_activity}개)",
            passed=len(passed),
            rejected=len(rejected),
            low_confidence=low_conf,
            low_activity=low_activity,
        )
        await self.set_state("idle")
        return passed, rejected

    def _reject_reason(self, avg: dict, settings: Settings) -> str | None:
        mdd = avg.get("mdd")
        if mdd is None:
            return "지표 산출 불가 (데이터 부족)"
        liq = int(avg.get("liquidation_count") or 0)
        if liq > 0:
            # 강제 청산 발생 = 격리마진 전액 손실 이력 — 즉시 탈락 (스펙 §4).
            return f"강제 청산 {liq}회 발생 — 즉시 탈락"
        if mdd > settings.max_mdd:
            return f"MDD {mdd:.1%} > 한도 {settings.max_mdd:.0%}"
        funding = avg.get("funding_paid") or 0.0
        total_return = avg.get("total_return")
        if funding > 0 and total_return is not None:
            gross = total_return * settings.initial_seed_usdt + funding
            if gross > 0 and funding / gross > FUNDING_DRAG_MAX:
                return (
                    f"펀딩 드래그 {funding / gross:.0%} > 한도 "
                    f"{FUNDING_DRAG_MAX:.0%} — 수익이 펀딩비로 잠식"
                )
        tpy = trades_per_year(avg)
        if tpy < settings.min_trades_per_year:
            # 활동성 부족: 거의 매매하지 않는 초수동 전략을 걸러낸다.
            return (
                f"활동성 부족 (연 {tpy:.1f}회 < "
                f"{settings.min_trades_per_year:.0f}회)"
            )
        return None

    # -- plan gates (trade 사이클) --------------------------------------------
    async def review_plan(
        self, plan: TradePlan, settings: Settings, market_state: MarketState
    ) -> ReviewResult:
        """RiskEngine 정적+런타임 게이트를 챔피언 플랜에 적용하고 한국어
        판정을 로그로 남긴다 (규칙 §1·§2·§3)."""
        verdict = RiskEngine.review(plan, settings, market_state)
        if verdict.approved:
            await self.log(
                f"{plan.symbol} {plan.side} 플랜 승인 — RR 1:{plan.rr:.2f}, "
                f"레버리지 x{plan.leverage}, 마진 {plan.margin_usdt:,.2f} USDT",
                symbol=plan.symbol,
                side=plan.side,
                rr=plan.rr,
                leverage=plan.leverage,
            )
        else:
            await self.log(
                f"{plan.symbol} {plan.side} 플랜 거부 — {verdict.reason}",
                level="warning",
                symbol=plan.symbol,
                side=plan.side,
                reason=verdict.reason,
            )
        return verdict
