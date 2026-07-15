"""Strategy primitives (crypto perp, plan-driven — 스펙 §3).

A strategy template is a pure function ``params × frames × regime →
TradePlan | None``. Templates emit **full TradePlans**: 근거(evidence) 2개
이상, 분할 진입 50/25/25 패시브 래더, 손절선 = 시나리오 붕괴 지점, 분할
익절 2레그 이상(비중 합 1.0). The backtest engine / trader only ever consume
plans — ``{-1, 0, +1}`` direction is an internal intent, never an API.

Regime gating (스펙 §3.1): ``cash`` → 관망(None). Long plans only in
``long_alt`` (any whitelisted symbol) or ``long_btc`` (BTC only). Short
plans only in ``short``.

Cross-TF look-ahead (스펙 §1.2): ``generate_plan`` clips every frame to
bars fully *closed* by ``as_of`` (default: the finest frame's last close),
so a template can never read a bar that had not closed at decision time.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..risk.plan import PlanLeg, TradePlan, liquidation_price


@dataclass(frozen=True)
class ParamRange:
    low: float
    high: float
    is_int: bool = True

    def clamp(self, value: float) -> float:
        v = min(max(value, self.low), self.high)
        return int(round(v)) if self.is_int else float(v)


ParamGrid = dict[str, ParamRange]


@dataclass
class StrategySpec:
    template: str
    params: dict[str, float] = field(default_factory=dict)

    def id_key(self) -> str:
        inner = ",".join(f"{k}={v:g}" for k, v in sorted(self.params.items()))
        return f"{self.template}({inner})"


# -- normative constants (규칙 §1, docs/trading-rules.md) -----------------------
BTC_SYMBOL = "BTCUSDT"
MAJOR_SYMBOLS = ("BTCUSDT", "ETHUSDT")
BTC_MAX_LEVERAGE = 10  # BTC 최대 10배
ALT_MAX_LEVERAGE = 5  # 알트(ETH 포함) 3~5배
MIN_LEVERAGE = 3
RR_MIN_MAJOR = 2.0  # BTC·ETH ≥ 1:2
RR_MIN_ALT = 3.0  # 알트 ≥ 1:3
LIQ_BUFFER_PCT = 0.10  # 손절선은 청산가 대비 (진입-청산 갭의) 10% 이상 여유

#: 분할 진입 비중 (몰빵 금지 — 기본 50/25/25).
ENTRY_FRACTIONS = (0.5, 0.25, 0.25)
#: 각 진입 레그의 mark→stop 구간 내 래더 위치 (0=mark, 1=stop). 전부 패시브 사이드.
ENTRY_LADDER_STEPS = (0.10, 0.25, 0.45)
#: R-배수 익절 시 분할 비중 (1차 50% / 잔량 전량).
TP_FRACTIONS = (0.5, 0.5)

#: 전략이 붙이는 기본 마진 (Trader가 사이징 시 dataclasses.replace로 교체).
DEFAULT_MARGIN_USDT = 1_000.0

LONG_REGIMES = ("long_alt", "long_btc")
TRADEABLE_REGIMES = ("long_alt", "long_btc", "short")

TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}


def clamp_leverage(symbol: str, leverage: float) -> int:
    """레버리지 캡 — BTC 10배 / 그 외 5배, 최소 3배 (규칙 §1)."""
    cap = BTC_MAX_LEVERAGE if symbol == BTC_SYMBOL else ALT_MAX_LEVERAGE
    return int(min(max(round(leverage), MIN_LEVERAGE), cap))


def min_rr(symbol: str) -> float:
    """손익비 하한 — BTC·ETH 1:2, 알트 1:3 (규칙 §1)."""
    return RR_MIN_MAJOR if symbol in MAJOR_SYMBOLS else RR_MIN_ALT


def regime_allows(side: str, regime: str, symbol: str) -> bool:
    """레짐 게이트 (스펙 §3.1): 롱은 롱장에서만, 숏은 숏장에서만, cash는 관망.

    ``long_btc``(시장↑ + 도미넌스↑)에서는 BTC 롱만 허용 — 알트는 관망.
    """
    if side == "long":
        if regime == "long_alt":
            return True
        return regime == "long_btc" and symbol == BTC_SYMBOL
    if side == "short":
        return regime == "short"
    return False


def _frame_close_ts(tf: str, df: pd.DataFrame) -> pd.Timestamp | None:
    if df is None or df.empty:
        return None
    return df.index[-1] + pd.Timedelta(minutes=TF_MINUTES.get(tf, 0))


def clip_frames(
    frames: dict[str, pd.DataFrame], as_of: pd.Timestamp | None = None
) -> dict[str, pd.DataFrame]:
    """Keep only bars fully closed by ``as_of`` in every frame (스펙 §1.2).

    ``as_of`` defaults to the finest present frame's last bar close — the
    decision time. Bars whose close time is after ``as_of`` (미완성/미래 봉)
    are dropped, so cross-TF look-ahead is structurally impossible even if a
    caller hands frames with poisoned future bars.
    """
    if as_of is None:
        finest = None
        for tf, df in frames.items():
            close_ts = _frame_close_ts(tf, df)
            if close_ts is None:
                continue
            minutes = TF_MINUTES.get(tf, 0)
            if finest is None or minutes < finest[0]:
                finest = (minutes, close_ts)
        if finest is None:
            return dict(frames)
        as_of = finest[1]
    clipped: dict[str, pd.DataFrame] = {}
    for tf, df in frames.items():
        if df is None or df.empty:
            clipped[tf] = df
            continue
        duration = pd.Timedelta(minutes=TF_MINUTES.get(tf, 0))
        clipped[tf] = df[(df.index + duration) <= as_of]
    return clipped


def mark_price(frames: dict[str, pd.DataFrame]) -> float | None:
    """현재가(mark) = 존재하는 가장 촘촘한 TF의 마지막 완결 봉 종가."""
    best: tuple[int, float] | None = None
    for tf, df in frames.items():
        if df is None or df.empty:
            continue
        minutes = TF_MINUTES.get(tf, 10**9)
        if best is None or minutes < best[0]:
            best = (minutes, float(df["close"].iloc[-1]))
    return None if best is None else best[1]


def build_plan(
    *,
    symbol: str,
    side: str,
    mark: float,
    stop: float,
    evidence: list[str],
    leverage: float,
    tps: list[tuple[float, float]] | None = None,
    tp_r1: float | None = None,
    tp_r2: float | None = None,
    margin_usdt: float = DEFAULT_MARGIN_USDT,
) -> TradePlan | None:
    """Assemble a gate-passing TradePlan or return None (관망).

    - 진입: mark→stop 구간 안의 50/25/25 패시브 래더 (long은 mark 아래,
      short은 mark 위 — RiskEngine 패시브 사이드 게이트와 일치).
    - 익절: 구조 레그(``tps=[(price, fraction), ...]``) 또는 R-배수
      (``tp_r1``/``tp_r2`` × 리스크, 50/50).
    - 자체 검증: 근거 ≥2, 기하, RR(심볼별 하한), 청산 버퍼 — 미달이면 None.
    """
    if len(evidence) < 2:
        return None
    if mark <= 0 or stop <= 0:
        return None
    if side == "long" and stop >= mark:
        return None
    if side == "short" and stop <= mark:
        return None

    lev = clamp_leverage(symbol, leverage)
    entries = [
        PlanLeg("entry", mark - step * (mark - stop), frac)
        for step, frac in zip(ENTRY_LADDER_STEPS, ENTRY_FRACTIONS)
    ]
    w_entry = sum(leg.price * leg.fraction for leg in entries)
    risk = (w_entry - stop) if side == "long" else (stop - w_entry)
    if risk <= 0:
        return None

    if tps is None:
        if tp_r1 is None or tp_r2 is None:
            return None
        sign = 1.0 if side == "long" else -1.0
        tp_prices = (w_entry + sign * tp_r1 * risk, w_entry + sign * tp_r2 * risk)
        if any(p <= 0 for p in tp_prices):
            return None
        tp_legs = [
            PlanLeg("tp", price, frac)
            for price, frac in zip(tp_prices, TP_FRACTIONS)
        ]
    else:
        tp_legs = [PlanLeg("tp", price, frac) for price, frac in tps]
        if len(tp_legs) < 2 or any(leg.price <= 0 for leg in tp_legs):
            return None

    plan = TradePlan(
        symbol=symbol,
        side=side,  # type: ignore[arg-type]
        evidence=list(evidence),
        entries=entries,
        stop=PlanLeg("stop", stop, 1.0),
        tps=tp_legs,
        leverage=lev,
        margin_usdt=margin_usdt,
    )
    if not plan.geometry_ok():
        return None
    if plan.rr + 1e-9 < min_rr(symbol):
        return None  # 손익비 미달 — 홀짝 자리 관망 (규칙 §2)
    liq = liquidation_price(w_entry, side, lev, margin_usdt * lev)
    buffer_needed = LIQ_BUFFER_PCT * abs(w_entry - liq)
    if side == "long" and stop < liq + buffer_needed:
        return None
    if side == "short" and stop > liq - buffer_needed:
        return None
    return plan


def generate_plan(
    spec: StrategySpec,
    frames: dict[str, pd.DataFrame],
    regime: str,
    symbol: str = BTC_SYMBOL,
    as_of: pd.Timestamp | None = None,
) -> TradePlan | None:
    """Dispatch to the template's plan function (스펙 §3).

    Returns a full TradePlan or None (관망). ``cash`` regime always returns
    None; the produced plan's side must be allowed by the regime.
    """
    from . import registry  # local import avoids a circular dependency

    try:
        fn = registry.PLAN_FUNCS[spec.template]
    except KeyError:
        raise ValueError(f"unknown strategy template: {spec.template!r}") from None
    if regime not in TRADEABLE_REGIMES:
        return None  # cash(관망) 또는 미지 레짐 — 진입 차단 (스펙 §3.1)
    clipped = clip_frames(frames, as_of)
    plan = fn(clipped, symbol, **spec.params)
    if plan is None:
        return None
    if not regime_allows(plan.side, regime, symbol):
        return None
    return plan


def generate_signal(spec: StrategySpec, df: pd.DataFrame):  # pragma: no cover
    """Deprecated stock-era signal API.

    Kept only so legacy importers (orchestrator/quant/trader, C2 에이전트
    웨이브에서 generate_plan으로 전환 예정) keep importing during the
    transform. Plan-driven strategies never emit signals.
    """
    raise NotImplementedError(
        "signal API removed — strategies emit TradePlans via generate_plan()"
    )
