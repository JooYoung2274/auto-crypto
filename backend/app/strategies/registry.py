"""Strategy template registry: parameter grids (스펙 §3.2), random candidate
generation, and ±20% mutation with range clamping."""
from __future__ import annotations

import random

from . import (
    box_range,
    candle_breakout,
    ma_confluence,
    topdown_pullback,
    vwma_support,
)
from .base import ParamGrid, ParamRange, StrategySpec

# Every template carries a ``leverage`` parameter explored alongside the
# signal params (스펙 §3.2 — 레버리지도 탐색 대상). The grid spans the global
# 3–10x range; the per-symbol cap (BTC 10x / 알트 5x, 규칙 §1) is clamped at
# plan-build time by ``base.clamp_leverage`` and re-verified by the RiskEngine.
LEVERAGE_RANGE = ParamRange(3, 10)

# R-multiple take-profit ranges: the weighted RR is 0.5·(tp_r1 + tp_r2), so
# the minimum reachable RR is 0.5·(2.2 + 4.0) = 3.1 ≥ 규칙 §1의 알트 하한 1:3.
TP_R1_RANGE = ParamRange(2.2, 4.0, is_int=False)
TP_R2_RANGE = ParamRange(4.0, 6.5, is_int=False)

TEMPLATES: dict[str, ParamGrid] = {
    "topdown_pullback": {
        "fast": ParamRange(30, 70),
        "slow": ParamRange(120, 240),
        "pull_band": ParamRange(0.01, 0.05, is_int=False),
        "vol_mult": ParamRange(1.0, 2.5, is_int=False),
        "tp_r1": TP_R1_RANGE,
        "tp_r2": TP_R2_RANGE,
        "leverage": LEVERAGE_RANGE,
    },
    "ma_confluence": {
        "tol": ParamRange(0.004, 0.03, is_int=False),
        "band": ParamRange(0.01, 0.05, is_int=False),
        "stop_pad": ParamRange(0.005, 0.03, is_int=False),
        "panic_drop": ParamRange(0.08, 0.30, is_int=False),
        "tp_r1": TP_R1_RANGE,
        "tp_r2": TP_R2_RANGE,
        "leverage": LEVERAGE_RANGE,
    },
    "box_range": {
        "pivot_k": ParamRange(2, 5),  # 피벗 확정 우측 봉수 (스펙 §3.2)
        "entry_q": ParamRange(0.15, 0.30, is_int=False),  # 박스 분위 임계
        "stop_buf": ParamRange(0.02, 0.10, is_int=False),  # 높이 대비 손절 버퍼
        "tp1_frac": ParamRange(0.30, 0.45, is_int=False),  # 1차 익절 (< 미드포인트)
        "leverage": LEVERAGE_RANGE,
    },
    "vwma_support": {
        "window": ParamRange(60, 150),  # VWMA 창 (기본기 100선)
        "band": ParamRange(0.005, 0.03, is_int=False),
        "vp_window": ParamRange(80, 200),  # VPVR 윈도우
        "stop_pad": ParamRange(0.005, 0.03, is_int=False),
        "tp_r1": TP_R1_RANGE,
        "tp_r2": TP_R2_RANGE,
        "leverage": LEVERAGE_RANGE,
    },
    "candle_breakout": {
        "body_mult": ParamRange(1.5, 3.0, is_int=False),
        "vol_mult": ParamRange(1.2, 3.0, is_int=False),
        "lookback": ParamRange(3, 12),
        "retest_band": ParamRange(0.005, 0.03, is_int=False),
        "tp_r1": TP_R1_RANGE,
        "tp_r2": TP_R2_RANGE,
        "leverage": LEVERAGE_RANGE,
    },
}

PLAN_FUNCS = {
    "topdown_pullback": topdown_pullback.plan,
    "ma_confluence": ma_confluence.plan,
    "box_range": box_range.plan,
    "vwma_support": vwma_support.plan,
    "candle_breakout": candle_breakout.plan,
}


def _fix_constraints(template: str, params: dict[str, float]) -> dict[str, float]:
    """Enforce cross-parameter constraints in place (fast < slow etc.)."""
    if template == "topdown_pullback" and params["fast"] >= params["slow"]:
        grid = TEMPLATES[template]
        params["fast"] = grid["fast"].clamp(params["slow"] - 1)
        if params["fast"] >= params["slow"]:  # slow at its minimum
            params["slow"] = grid["slow"].clamp(params["fast"] + 1)
    return params


def _sample(pr: ParamRange, rng: random.Random) -> float:
    if pr.is_int:
        return rng.randint(int(pr.low), int(pr.high))
    return rng.uniform(pr.low, pr.high)


def random_candidates(n: int, rng: random.Random) -> list[StrategySpec]:
    names = list(TEMPLATES)
    specs: list[StrategySpec] = []
    for _ in range(n):
        template = names[rng.randrange(len(names))]
        params = {k: _sample(pr, rng) for k, pr in TEMPLATES[template].items()}
        specs.append(StrategySpec(template, _fix_constraints(template, params)))
    return specs


def mutate(spec: StrategySpec, rng: random.Random) -> StrategySpec:
    """Return a new spec with each parameter perturbed by ±20%, clamped to
    its template range. Parameters the parent predates (e.g. ``leverage`` on
    champions from before it became searchable) are sampled fresh so new
    dimensions enter the champion lineage instead of being lost forever."""
    grid = TEMPLATES[spec.template]
    params: dict[str, float] = {}
    for key, pr in grid.items():
        if key in spec.params:
            params[key] = pr.clamp(spec.params[key] * rng.uniform(0.8, 1.2))
        else:
            params[key] = _sample(pr, rng)
    return StrategySpec(spec.template, _fix_constraints(spec.template, params))
