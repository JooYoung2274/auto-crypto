"""TradePlan — 시나리오 자료구조 (규칙 §2 '시나리오 필수', 스펙 §2).

진입 전 TradePlan(근거 + 손절선 + 분할 익절선 + 분할 진입 비중)이 완성돼야
주문 가능하다. 손익비(RR)는 side-aware 정규화 공식으로 계산하고, 기하 검증
(long: stop < entries < tps, short은 역순)을 헬퍼로 제공한다.

청산 정확식(스펙 §4)도 여기 두어 RiskEngine과 PaperBroker가 공유한다:
liq_long = avg_entry×(1−1/L)/(1−MMR), liq_short = avg_entry×(1+1/L)/(1+MMR).
MMR은 노셔널 구간 테이블 기준.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Literal

LegKind = Literal["entry", "tp", "stop"]
Side = Literal["long", "short"]

#: 분할 비중 합 == 1.0 판정 허용 오차.
FRACTION_TOL = 1e-6

#: 유지증거금률(MMR) 노셔널 구간 테이블 — Binance USDT-M tier 근사.
#: (노셔널 상한 USDT, MMR). tier-1이면 충분하나 스펙 §4대로 테이블로 둔다.
MMR_TIERS: tuple[tuple[float, float], ...] = (
    (50_000.0, 0.004),
    (250_000.0, 0.005),
    (3_000_000.0, 0.01),
    (20_000_000.0, 0.025),
    (float("inf"), 0.05),
)


def maintenance_margin_rate(notional: float) -> float:
    """포지션 노셔널(USDT)에 해당하는 유지증거금률."""
    for cap, mmr in MMR_TIERS:
        if notional <= cap:
            return mmr
    return MMR_TIERS[-1][1]


def liquidation_price(
    avg_entry: float, side: str, leverage: int, notional: float
) -> float:
    """격리마진 청산가 정확식 (스펙 §4).

    liq_long = avg_entry×(1−1/L)/(1−MMR), liq_short = avg_entry×(1+1/L)/(1+MMR).
    """
    if leverage <= 0:
        raise ValueError(f"leverage must be positive, got {leverage}")
    mmr = maintenance_margin_rate(notional)
    if side == "long":
        return avg_entry * (1.0 - 1.0 / leverage) / (1.0 - mmr)
    if side == "short":
        return avg_entry * (1.0 + 1.0 / leverage) / (1.0 + mmr)
    raise ValueError(f"invalid side: {side}")


@dataclass(frozen=True)
class PlanLeg:
    """플랜 레그 — kind(entry/tp/stop), 가격, 비중."""

    kind: LegKind
    price: float
    fraction: float


@dataclass(frozen=True)
class TradePlan:
    """진입 시나리오 (스펙 §2).

    - evidence: 독립 근거 목록 (≥2, 규칙 §2)
    - entries: 분할 진입 레그 (len≥2, fraction 합 1.0, 기본 50/25/25)
    - stop: 손절선 = 시나리오 붕괴 지점 (4h 종가 판정)
    - tps: 분할 익절 레그 (len≥2, fraction 합 1.0, 마지막 레그 = 잔량 전량)
    """

    symbol: str
    side: Side
    evidence: list[str]
    entries: list[PlanLeg]
    stop: PlanLeg
    tps: list[PlanLeg]
    leverage: int
    margin_usdt: float = field(default=0.0)

    # -- geometry helpers -----------------------------------------------------
    @property
    def entries_fraction_sum(self) -> float:
        return sum(leg.fraction for leg in self.entries)

    @property
    def tps_fraction_sum(self) -> float:
        return sum(leg.fraction for leg in self.tps)

    @property
    def weighted_entry(self) -> float:
        """wEntry = Σ(pᵢfᵢ) — fraction 합이 1.0일 때 정규화 평균 진입가."""
        return sum(leg.price * leg.fraction for leg in self.entries)

    @property
    def weighted_tp(self) -> float:
        """wTP = Σ(pⱼfⱼ) — 정규화 평균 익절가."""
        return sum(leg.price * leg.fraction for leg in self.tps)

    @property
    def notional_usdt(self) -> float:
        return self.margin_usdt * self.leverage

    @property
    def rr(self) -> float:
        """Side-aware 정규화 손익비 (스펙 §2).

        long:  rr = (wTP − wEntry) / (wEntry − stop)
        short: rr = (wEntry − wTP) / (stop − wEntry)
        분모(리스크)가 0 이하이면 기하가 무너진 플랜 → 0.0 (게이트에서 거부).
        """
        w_entry = self.weighted_entry
        w_tp = self.weighted_tp
        if self.side == "long":
            risk = w_entry - self.stop.price
            reward = w_tp - w_entry
        else:
            risk = self.stop.price - w_entry
            reward = w_entry - w_tp
        if risk <= 0:
            return 0.0
        return reward / risk

    def geometry_ok(self) -> bool:
        """기하 검증 — long: stop < 모든 진입가 < 모든 익절가, short은 역순."""
        entry_prices = [leg.price for leg in self.entries]
        tp_prices = [leg.price for leg in self.tps]
        if not entry_prices or not tp_prices:
            return False
        if any(p <= 0 for p in entry_prices + tp_prices) or self.stop.price <= 0:
            return False
        if self.side == "long":
            return self.stop.price < min(entry_prices) and max(entry_prices) < min(
                tp_prices
            )
        return self.stop.price > max(entry_prices) and min(entry_prices) > max(
            tp_prices
        )

    def estimated_liq_price(self) -> float:
        """가중 평균 진입가 기준 예상 청산가 (전 레그 체결 가정)."""
        return liquidation_price(
            self.weighted_entry, self.side, self.leverage, self.notional_usdt
        )

    def worst_case_liq_price(self) -> float:
        """부분 체결 최악 케이스 청산가 — 가장 불리한 진입 레그 단독 체결.

        long은 가장 높은 레그만 체결됐을 때 청산가가 마크에 가장 가깝다.
        청산 버퍼 게이트는 이 값으로 심사해야, TTL 등으로 하위 레그가
        비어 있는 부분 체결 래더가 승인된 손절선 위에서 청산되지 않는다.
        """
        worst = (
            max(leg.price for leg in self.entries)
            if self.side == "long"
            else min(leg.price for leg in self.entries)
        )
        fraction = next(
            leg.fraction for leg in self.entries if leg.price == worst
        )
        notional = self.margin_usdt * self.leverage * fraction
        return liquidation_price(worst, self.side, self.leverage, notional)

    # -- (de)serialization for trade_plans.plan_json ---------------------------
    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "evidence": list(self.evidence),
            "entries": [asdict(leg) for leg in self.entries],
            "stop": asdict(self.stop),
            "tps": [asdict(leg) for leg in self.tps],
            "leverage": self.leverage,
            "margin_usdt": self.margin_usdt,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, payload: dict) -> "TradePlan":
        return cls(
            symbol=str(payload["symbol"]),
            side=payload["side"],
            evidence=[str(e) for e in payload.get("evidence", [])],
            entries=[PlanLeg(**leg) for leg in payload.get("entries", [])],
            stop=PlanLeg(**payload["stop"]),
            tps=[PlanLeg(**leg) for leg in payload.get("tps", [])],
            leverage=int(payload["leverage"]),
            margin_usdt=float(payload.get("margin_usdt", 0.0)),
        )

    @classmethod
    def from_json(cls, raw: str) -> "TradePlan":
        return cls.from_dict(json.loads(raw))
