"""VWMA·VPVR 보조 판단 (규칙 §4-6 / 스펙 §3.2).

- 4h VWMA(window) 위 지지 = 상승 유효: 가격이 VWMA 근처로 눌리면 지지
  리테스트 롱 (손절 = VWMA 이탈선). VPVR 매물대(POC)가 아래에서 받치면
  근거에 추가.
- VWMA + VPVR 매물대(POC) **동시 하향 이탈** = 숏 (손절 = 이탈한 레벨 위).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data.indicators import poc_price, volume_profile, vwma
from ..risk.plan import TradePlan
from .base import build_plan, mark_price

#: 최소 여유 봉 수 (VWMA 워밍업 이후).
MIN_EXTRA_BARS = 5


def plan(
    frames: dict[str, pd.DataFrame],
    symbol: str,
    *,
    window: float = 100,
    band: float = 0.015,
    vp_window: float = 120,
    stop_pad: float = 0.015,
    tp_r1: float = 3.0,
    tp_r2: float = 5.0,
    leverage: float = 5,
) -> TradePlan | None:
    h4 = frames.get("4h")
    window = int(window)
    if h4 is None or len(h4) < window + MIN_EXTRA_BARS:
        return None
    mark = mark_price(frames)
    if mark is None:
        return None

    vw = float(vwma(h4["close"], h4["volume"], window).iloc[-1])
    if not np.isfinite(vw) or vw <= 0:
        return None
    profile = volume_profile(h4, window=int(vp_window))
    poc = poc_price(profile)

    # VWMA 지지 눌림 리테스트 롱 (약간의 언더슛 허용 — 지지 테스트).
    if vw * (1.0 - 0.3 * band) <= mark <= vw * (1.0 + band):
        evidence = [
            f"4h VWMA{window} 위 지지 유효 — 상승 구조 유지",
            "VWMA 눌림 리테스트 분할 매수 대기",
        ]
        if poc is not None and poc <= mark:
            evidence.append("VPVR 매물대(POC) 하방 지지 확인")
        return build_plan(
            symbol=symbol,
            side="long",
            mark=mark,
            stop=vw * (1.0 - 2.0 * stop_pad),
            evidence=evidence,
            leverage=leverage,
            tp_r1=float(tp_r1),
            tp_r2=float(tp_r2),
        )

    # VWMA + VPVR 동시 하향 이탈 숏.
    if poc is not None and mark < vw and mark < poc:
        evidence = [
            f"4h VWMA{window} 하향 이탈 — 상승 유효 구조 붕괴",
            "VPVR 매물대(POC) 하향 이탈 — 동시 이탈 숏",
        ]
        return build_plan(
            symbol=symbol,
            side="short",
            mark=mark,
            stop=max(vw, poc) * (1.0 + stop_pad),
            evidence=evidence,
            leverage=leverage,
            tp_r1=float(tp_r1),
            tp_r2=float(tp_r2),
        )
    return None
