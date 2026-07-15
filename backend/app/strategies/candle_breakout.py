"""캔들 패턴 대응 — 돌파 후 눌림 리테스트 (규칙 §4-7 / 스펙 §3.2).

장대양봉(몸통 ≥ body_mult × 평균, 거래량 ≥ vol_mult × 평균)이 직전 구간
고점을 돌파한 뒤 **눌림 리테스트** 형태로만 진입한다 — 진입 레그는 항상
패시브 사이드 (돌파 추격 금지, 거래량 없는 돌파는 페이크아웃 의심).
장대음봉의 구간 저점 이탈 + 되돌림 리테스트는 대칭 숏.

손절선 = 돌파봉 저점(숏은 고점) 이탈 = 시나리오 붕괴 지점.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..risk.plan import TradePlan
from .base import build_plan, mark_price

#: 몸통/거래량 평균과 직전 고저점을 재는 기준 구간 (4h 봉수).
BASE_WINDOW = 20


def plan(
    frames: dict[str, pd.DataFrame],
    symbol: str,
    *,
    body_mult: float = 2.0,
    vol_mult: float = 1.5,
    lookback: float = 8,
    retest_band: float = 0.01,
    tp_r1: float = 3.0,
    tp_r2: float = 5.0,
    leverage: float = 5,
) -> TradePlan | None:
    h4 = frames.get("4h")
    lookback = int(lookback)
    if h4 is None or len(h4) < BASE_WINDOW + lookback + 2:
        return None
    mark = mark_price(frames)
    if mark is None:
        return None

    o = h4["open"].to_numpy(dtype=float)
    h = h4["high"].to_numpy(dtype=float)
    low = h4["low"].to_numpy(dtype=float)
    c = h4["close"].to_numpy(dtype=float)
    v = h4["volume"].to_numpy(dtype=float)
    n = len(h4)

    # 돌파봉은 마지막 봉 이전(리테스트가 진행돼야 하므로), 최근 lookback봉 안.
    for j in range(n - 2, max(BASE_WINDOW, n - 2 - lookback) - 1, -1):
        body = abs(c[j] - o[j])
        avg_body = float(np.mean(np.abs(c[j - BASE_WINDOW : j] - o[j - BASE_WINDOW : j])))
        avg_vol = float(np.mean(v[j - BASE_WINDOW : j]))
        if body <= 0 or body < body_mult * avg_body:
            continue
        if avg_vol <= 0 or v[j] < vol_mult * avg_vol:
            continue
        prior_high = float(np.max(h[j - BASE_WINDOW : j]))
        prior_low = float(np.min(low[j - BASE_WINDOW : j]))

        # 장대양봉 돌파 → 눌림 리테스트 롱.
        if c[j] > o[j] and c[j] > prior_high:
            level = prior_high
            if (
                level * (1.0 - 0.3 * retest_band) <= mark <= level * (1.0 + retest_band)
                and mark < c[j]
            ):
                stop = float(low[j])
                if stop >= mark:
                    continue
                evidence = [
                    f"4h 장대양봉 돌파 (몸통 {body_mult:g}배·거래량 {vol_mult:g}배 확인)",
                    "돌파 레벨 눌림 리테스트 — 패시브 분할 진입",
                    "돌파봉 저점 이탈 = 시나리오 붕괴 손절",
                ]
                return build_plan(
                    symbol=symbol,
                    side="long",
                    mark=mark,
                    stop=stop,
                    evidence=evidence,
                    leverage=leverage,
                    tp_r1=float(tp_r1),
                    tp_r2=float(tp_r2),
                )

        # 장대음봉 이탈 → 되돌림 리테스트 숏.
        if c[j] < o[j] and c[j] < prior_low:
            level = prior_low
            if (
                level * (1.0 - retest_band) <= mark <= level * (1.0 + 0.3 * retest_band)
                and mark > c[j]
            ):
                stop = float(h[j])
                if stop <= mark:
                    continue
                evidence = [
                    f"4h 장대음봉 이탈 (몸통 {body_mult:g}배·거래량 {vol_mult:g}배 확인)",
                    "이탈 레벨 되돌림 리테스트 — 패시브 분할 진입",
                    "이탈봉 고점 회복 = 시나리오 붕괴 손절",
                ]
                return build_plan(
                    symbol=symbol,
                    side="short",
                    mark=mark,
                    stop=stop,
                    evidence=evidence,
                    leverage=leverage,
                    tp_r1=float(tp_r1),
                    tp_r2=float(tp_r2),
                )
    return None
