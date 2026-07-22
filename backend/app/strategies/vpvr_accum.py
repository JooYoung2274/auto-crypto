"""VPVR 매집 시그널 — 상승 후 횡보 구간의 거래량 집중 (가이드 p118-119).

상승 이후의 횡보(조정) 구간에서 VPVR 거래량이 **이전 고점이 아니라 횡보
가격대에 집중**되기 시작하면 해당 가격대에서 매집이 진행 중이라고 보고,
전고점 재도전(2차 상승)을 기대하는 롱 시나리오를 낸다.

판정 (모두 마감된 4h봉 기준 — 진행 중 봉 배제):
1. 선행 상승: 횡보 직전 ``rise_bars``봉 동안 종가 상승률 ≥ ``rise_min``
2. 횡보: 최근 ``consol_bars``봉의 고저 범위가 마크 대비 ≤ ``consol_band``
   이고 횡보 상단이 전고점 아래 (고점 아래에서 쉬는 형태)
3. 매집: ``vp_window``봉 거래량 프로파일에서 횡보 밴드 안의 거래량 비중이
   ≥ ``conc_min`` 이고, 전고점 부근 밴드보다 커야 한다 ("이전 고점이 아닌
   횡보 구간에 집중")

손절 = 횡보 밴드 하단 이탈(× stop_pad 여유) = 매집 시나리오 붕괴 지점.
익절 = 표준 R-배수 2레그 (RR 게이트 보장). 롱 전용 — 레짐 게이트가 숏장
에서 자동 차단한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data.indicators import volume_profile
from ..risk.plan import TradePlan
from .base import build_plan, mark_price


def plan(
    frames: dict[str, pd.DataFrame],
    symbol: str,
    *,
    rise_bars: float = 40,
    rise_min: float = 0.08,
    consol_bars: float = 24,
    consol_band: float = 0.03,
    vp_window: float = 120,
    conc_min: float = 0.35,
    stop_pad: float = 0.01,
    tp_r1: float = 3.0,
    tp_r2: float = 5.0,
    leverage: float = 5,
) -> TradePlan | None:
    h4 = frames.get("4h")
    rise_bars = int(rise_bars)
    consol_bars = int(consol_bars)
    vp_window = int(vp_window)
    if h4 is None or len(h4) < rise_bars + consol_bars + 2:
        return None
    mark = mark_price(frames)
    if mark is None or mark <= 0:
        return None

    high = h4["high"].to_numpy(dtype=float)
    low = h4["low"].to_numpy(dtype=float)
    close = h4["close"].to_numpy(dtype=float)
    n = len(h4)

    # 1) 선행 상승 — 횡보 구간 직전 rise_bars봉의 종가 상승률.
    rise_start = close[n - consol_bars - rise_bars]
    rise_end = close[n - consol_bars - 1]
    if rise_start <= 0 or (rise_end - rise_start) / rise_start < rise_min:
        return None

    # 2) 횡보 밴드 — 최근 consol_bars 마감봉의 고저 범위.
    consol_high = float(np.max(high[n - consol_bars :]))
    consol_low = float(np.min(low[n - consol_bars :]))
    if consol_low <= 0 or (consol_high - consol_low) / mark > consol_band:
        return None
    # 전고점(상승 구간 고점) 아래에서 쉬는 형태여야 한다.
    prior_high = float(np.max(high[n - consol_bars - rise_bars : n - consol_bars]))
    if consol_high >= prior_high:
        return None
    # 마크가 횡보 밴드 안에 있어야 시나리오가 살아 있다.
    if not (consol_low <= mark <= consol_high * (1 + consol_band)):
        return None

    # 3) 매집 — 프로파일 질량이 전고점이 아닌 횡보 밴드에 집중.
    profile = volume_profile(h4, window=vp_window, bins=24)
    total = float(profile.sum())
    if total <= 0:
        return None
    prices = profile.index.to_numpy(dtype=float)
    consol_mass = float(profile[(prices >= consol_low) & (prices <= consol_high)].sum())
    band_half = consol_band * mark
    high_mass = float(
        profile[(prices >= prior_high - band_half) & (prices <= prior_high + band_half)].sum()
    )
    conc = consol_mass / total
    if conc < conc_min or consol_mass <= high_mass:
        return None

    stop = consol_low * (1 - stop_pad)
    return build_plan(
        symbol=symbol,
        side="long",
        mark=mark,
        stop=stop,
        evidence=[
            f"상승 후 횡보 구간 VPVR 매집 — 거래량 집중 {conc:.0%} (전고점 대비 우위)",
            f"횡보 밴드 {consol_low:.6g}~{consol_high:.6g} 지지, 전고점 {prior_high:.6g} 재도전 시나리오",
        ],
        leverage=leverage,
        tp_r1=tp_r1,
        tp_r2=tp_r2,
    )
