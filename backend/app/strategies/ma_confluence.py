"""이평선 황금타점 (규칙 §4-2 / 스펙 §3.2).

4h 50/100/200 이평선이 한 지점에 수렴(스프레드 ≤ tol)하는 자리:
- 가격이 수렴 구간 위에서 근접 → 눌림목 지지 롱 (이탈 시 칼손절 = 수렴
  하단 아래 손절선).
- 가격이 수렴 구간 아래에서 근접 → 이탈 숏 (수렴 상단 위 손절선).
- 400선은 패닉셀 급락(고점 대비 panic_drop 이상) 시 분할 매수 기준.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data.indicators import sma
from ..risk.plan import TradePlan
from .base import build_plan, mark_price

#: 수렴 판정에 쓰는 4h 이평 3종 (규칙 §4-2 규범값).
CONFLUENCE_WINDOWS = (50, 100, 200)
#: 패닉 분할 매수 기준선 (규칙 §4-2: 400선).
PANIC_WINDOW = 400
#: 패닉 낙폭 측정용 최근 고점 창 (4h 봉수).
PANIC_LOOKBACK = 60


def plan(
    frames: dict[str, pd.DataFrame],
    symbol: str,
    *,
    tol: float = 0.01,
    band: float = 0.03,
    stop_pad: float = 0.02,
    panic_drop: float = 0.15,
    tp_r1: float = 3.0,
    tp_r2: float = 5.0,
    leverage: float = 5,
) -> TradePlan | None:
    h4 = frames.get("4h")
    if h4 is None or len(h4) < max(CONFLUENCE_WINDOWS):
        return None
    mark = mark_price(frames)
    if mark is None:
        return None

    close = h4["close"]
    smas = [float(sma(close, w).iloc[-1]) for w in CONFLUENCE_WINDOWS]
    if not all(np.isfinite(v) for v in smas):
        return None
    top, bottom = max(smas), min(smas)
    mid = sum(smas) / len(smas)
    spread = (top - bottom) / mid if mid > 0 else float("inf")

    if spread <= tol:
        # 수렴 지지 → 눌림목 롱.
        if top <= mark <= mid * (1.0 + band):
            evidence = [
                f"4h 50/100/200 이평 수렴 (스프레드 {spread:.2%} ≤ {tol:.2%})",
                "수렴 구간 위 눌림목 지지 리테스트",
                "수렴 하단 이탈 시 칼손절 시나리오",
            ]
            return build_plan(
                symbol=symbol,
                side="long",
                mark=mark,
                stop=bottom * (1.0 - stop_pad),
                evidence=evidence,
                leverage=leverage,
                tp_r1=float(tp_r1),
                tp_r2=float(tp_r2),
            )
        # 수렴 이탈 → 되돌림 저항 숏.
        if mid * (1.0 - band) <= mark <= bottom:
            evidence = [
                f"4h 50/100/200 이평 수렴 (스프레드 {spread:.2%} ≤ {tol:.2%})",
                "수렴 구간 하향 이탈 — 되돌림 저항 숏",
                "수렴 상단 회복 시 시나리오 붕괴 손절",
            ]
            return build_plan(
                symbol=symbol,
                side="short",
                mark=mark,
                stop=top * (1.0 + stop_pad),
                evidence=evidence,
                leverage=leverage,
                tp_r1=float(tp_r1),
                tp_r2=float(tp_r2),
            )
        return None

    # 400선 패닉셀 분할 매수 (규칙 §4-2).
    if len(h4) >= PANIC_WINDOW:
        s400 = float(sma(close, PANIC_WINDOW).iloc[-1])
        recent_max = float(close.iloc[-PANIC_LOOKBACK:].max())
        if np.isfinite(s400) and recent_max > 0:
            drop = 1.0 - mark / recent_max
            if drop >= panic_drop and s400 * (1.0 - band) <= mark <= s400 * (1.0 + band):
                evidence = [
                    f"패닉셀 급락 — 최근 고점 대비 {drop:.0%} 하락",
                    "4h 400선 지지 구간 도달 — 분할 매수 기준",
                    "400선 이탈 시 시나리오 붕괴 손절",
                ]
                return build_plan(
                    symbol=symbol,
                    side="long",
                    mark=mark,
                    stop=s400 * (1.0 - 2.0 * stop_pad),
                    evidence=evidence,
                    leverage=leverage,
                    tp_r1=float(tp_r1),
                    tp_r2=float(tp_r2),
                )
    return None
