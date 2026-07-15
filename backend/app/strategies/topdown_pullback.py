"""톱다운 타점 매매 (기본기, 규칙 §4-1 / 스펙 §3.2).

일봉 200선 위 확인 → 4h 골든크로스(fast↑slow) 유지 → 가격이 4h fast 이평
눌림목에 리테스트 → 15m 거래량 확인 → 확정 15m 스윙 저점(정밀 손절선)
아래를 시나리오 붕괴 지점으로 잡고 패시브 래더 롱. "큰 흐름 읽고 작게
들어간다."
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data.indicators import sma, swing_pivots
from ..risk.plan import TradePlan
from .base import build_plan, mark_price

#: 일봉 추세 필터 창 (규칙 §4-1: 일봉 200선) — 규범값이라 탐색하지 않는다.
DAILY_TREND_WINDOW = 200
#: 15m 정밀 손절선용 스윙 피벗 확정 봉수 / 탐색 꼬리 길이.
PIVOT_K = 3
PIVOT_TAIL = 200
#: 15m 거래량 확인 기준 봉수.
VOLUME_BASELINE = 20


def plan(
    frames: dict[str, pd.DataFrame],
    symbol: str,
    *,
    fast: float = 50,
    slow: float = 200,
    pull_band: float = 0.03,
    vol_mult: float = 1.5,
    tp_r1: float = 3.0,
    tp_r2: float = 5.0,
    leverage: float = 5,
) -> TradePlan | None:
    d1 = frames.get("1d")
    h4 = frames.get("4h")
    m15 = frames.get("15m")
    if d1 is None or h4 is None or m15 is None:
        return None
    fast, slow = int(fast), int(slow)
    if len(d1) < DAILY_TREND_WINDOW or len(h4) < slow or len(m15) < VOLUME_BASELINE + 2:
        return None

    # 1) 일봉 200선 위 — 상위 추세 확인.
    daily_ma = float(sma(d1["close"], DAILY_TREND_WINDOW).iloc[-1])
    if not np.isfinite(daily_ma) or float(d1["close"].iloc[-1]) <= daily_ma:
        return None

    # 2) 4h 골든크로스(fast > slow) 유지.
    f4 = float(sma(h4["close"], fast).iloc[-1])
    s4 = float(sma(h4["close"], slow).iloc[-1])
    if not (np.isfinite(f4) and np.isfinite(s4)) or f4 <= s4:
        return None

    # 3) 눌림목: 현재가가 4h fast 이평 근처 (약간의 언더슛 허용 — 지지 테스트).
    mark = mark_price(frames)
    if mark is None:
        return None
    if not (f4 * (1.0 - 0.3 * pull_band) <= mark <= f4 * (1.0 + pull_band)):
        return None

    # 4) 15m 거래량 확인.
    vol = m15["volume"]
    base_vol = float(vol.iloc[-(VOLUME_BASELINE + 1) : -1].mean())
    if base_vol <= 0 or float(vol.iloc[-1]) < vol_mult * base_vol:
        return None

    # 손절선 = 시나리오 붕괴: 확정 15m 스윙 저점(정밀 손절선)과 4h 눌림 지지
    # 이탈선 중 낮은 쪽.
    pivots = swing_pivots(m15.tail(PIVOT_TAIL), k=PIVOT_K)
    lows = pivots[pivots["kind"] == "low"]
    pivot_low = float(lows["price"].iloc[-1]) if len(lows) else f4 * (1.0 - 2.0 * pull_band)
    stop = min(pivot_low, f4 * (1.0 - 0.5 * pull_band))

    evidence = [
        "1d 200선 위 — 상위 추세 상방",
        f"4h 골든크로스({fast}/{slow}) 유지",
        "4h 이평 눌림목 지지 리테스트",
        "15m 거래량 확인 — 분할 진입 대기",
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
