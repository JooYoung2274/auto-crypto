"""박스권 매매 (규칙 §4-5·§3 / 스펙 §3.2).

4h 확정 스윙 피벗(우측 pivot_k봉 마감 후에만 확정)으로 박스를 만들고:
- 하단 entry_q 분위 안 → 지지 롱 (손절 = 박스 하단 - stop_buf×높이)
- 상단 entry_q 분위 안 → 저항 숏 (손절 = 박스 상단 + stop_buf×높이)
- 중간(홀짝 자리, 손익비 ≈ 1:1) → 관망 (규칙 §2)
**최종 익절 레그 = 박스 미드포인트** — 반대편 끝까지 기다리지 않는다 (규칙 §3).
"""
from __future__ import annotations

import pandas as pd

from ..data.indicators import build_box, swing_pivots
from ..risk.plan import TradePlan
from .base import build_plan, mark_price

#: 박스 구성에 쓰는 최근 확정 피벗 수 (양쪽 각각).
RECENT_PIVOTS = 2
#: 최소 4h 봉 수.
MIN_BARS = 40


def plan(
    frames: dict[str, pd.DataFrame],
    symbol: str,
    *,
    pivot_k: float = 3,
    entry_q: float = 0.25,
    stop_buf: float = 0.05,
    tp1_frac: float = 0.35,
    leverage: float = 5,
) -> TradePlan | None:
    h4 = frames.get("4h")
    if h4 is None or len(h4) < MIN_BARS:
        return None
    mark = mark_price(frames)
    if mark is None:
        return None

    # 확정 피벗만으로 박스 구성 (스펙 §3.2 룩어헤드 차단 — swing_pivots는
    # 우측 k봉이 마감된 피벗만 반환한다).
    pivots = swing_pivots(h4, k=int(pivot_k))
    box = build_box(pivots, as_of=None, recent=RECENT_PIVOTS)
    if box is None or box.height <= 0:
        return None

    q = (mark - box.bottom) / box.height
    if q <= entry_q:
        # 박스 하단 지지 롱 — 최종 TP는 미드포인트.
        tp1 = box.bottom + tp1_frac * box.height
        if mark >= tp1:
            return None
        evidence = [
            f"4h 확정 피벗 박스 {box.bottom:.6g}~{box.top:.6g} — 하단 {q:.0%} 지지 구간",
            "박스 하단 분할 진입 / 박스 미드포인트 익절 시나리오",
            "박스 하단 이탈 = 시나리오 붕괴 손절",
        ]
        return build_plan(
            symbol=symbol,
            side="long",
            mark=mark,
            stop=box.bottom - stop_buf * box.height,
            evidence=evidence,
            leverage=leverage,
            tps=[(tp1, 0.5), (box.midpoint, 0.5)],
        )
    if q >= 1.0 - entry_q:
        # 박스 상단 저항 숏 — 최종 TP는 미드포인트.
        tp1 = box.top - tp1_frac * box.height
        if mark <= tp1:
            return None
        evidence = [
            f"4h 확정 피벗 박스 {box.bottom:.6g}~{box.top:.6g} — 상단 {1 - q:.0%} 저항 구간",
            "박스 상단 분할 진입 / 박스 미드포인트 익절 시나리오",
            "박스 상단 이탈 = 시나리오 붕괴 손절",
        ]
        return build_plan(
            symbol=symbol,
            side="short",
            mark=mark,
            stop=box.top + stop_buf * box.height,
            evidence=evidence,
            leverage=leverage,
            tps=[(tp1, 0.5), (box.midpoint, 0.5)],
        )
    return None  # 박스 중간 = 홀짝 자리 — 관망 (규칙 §2)
