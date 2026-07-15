"""Technical indicators for the coin agents (pure pandas/numpy, all causal).

- SMA / EMA / VWMA / RSI(Wilder) / ATR(Wilder)
- volume profile (VPVR) over the ``[t-W, t-1]`` window of *closed* bars
- swing pivots that are only confirmed after ``k`` right bars (each pivot
  carries its confirmation timestamp — 스펙 §3.2 룩어헤드 차단)
- box/zone builder from confirmed pivots only
- ``resample_shift_align``: higher-TF ``shift(1)`` + ffill onto a lower-TF
  index (스펙 §1.2 교차 TF 룩어헤드 금지)

Every function here must be *causal*: corrupting the tail of the input
series never changes earlier output values (look-ahead poison tests).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

__all__ = [
    "sma",
    "ema",
    "vwma",
    "rsi",
    "atr",
    "volume_profile",
    "poc_price",
    "swing_pivots",
    "Box",
    "build_box",
    "resample_shift_align",
]

PIVOT_COLUMNS = ["ts", "kind", "price", "confirm_ts"]


# -- moving averages ----------------------------------------------------------
def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average (NaN during warm-up)."""
    return series.rolling(window, min_periods=window).mean()


def ema(series: pd.Series, window: int) -> pd.Series:
    """Exponential moving average (span=window, causal recursion)."""
    return series.ewm(span=window, adjust=False, min_periods=window).mean()


def vwma(close: pd.Series, volume: pd.Series, window: int) -> pd.Series:
    """Volume-weighted moving average: Σ(close·vol) / Σ(vol)."""
    pv = (close * volume).rolling(window, min_periods=window).sum()
    v = volume.rolling(window, min_periods=window).sum()
    return pv / v


# -- oscillators / volatility -------------------------------------------------
def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """Wilder's RSI in [0, 100] (loss-free streak → 100)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(avg_loss > 0, 100.0)
    # keep warm-up NaN even where the where() above filled a value
    out[avg_gain.isna() | avg_loss.isna()] = np.nan
    return out


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """Average True Range with Wilder smoothing. Needs high/low/close."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / window, adjust=False, min_periods=window).mean()


# -- volume profile (VPVR) ----------------------------------------------------
def volume_profile(
    df: pd.DataFrame, window: int = 120, bins: int = 24, end: int | None = None
) -> pd.Series:
    """VPVR over the closed-bar window ``[end-window, end-1]`` (bar ``end``
    itself is excluded — at bar t only ``[t-W, t-1]`` bars are usable).

    Each bar's volume is assigned to the price bin containing its typical
    price (H+L+C)/3. Returns volume indexed by bin mid-price.
    """
    if end is None:
        end = len(df)
    sub = df.iloc[max(0, end - window) : end]
    if sub.empty:
        return pd.Series(dtype=float, name="volume")
    lo = float(sub["low"].min())
    hi = float(sub["high"].max())
    if hi <= lo:
        return pd.Series(
            [float(sub["volume"].sum())], index=[lo], name="volume"
        )
    edges = np.linspace(lo, hi, bins + 1)
    typical = ((sub["high"] + sub["low"] + sub["close"]) / 3.0).to_numpy()
    which = np.clip(np.digitize(typical, edges) - 1, 0, bins - 1)
    vol = np.zeros(bins)
    np.add.at(vol, which, sub["volume"].to_numpy(dtype=float))
    mids = (edges[:-1] + edges[1:]) / 2.0
    return pd.Series(vol, index=mids, name="volume")


def poc_price(profile: pd.Series) -> float | None:
    """Point of control: the price level with the most traded volume."""
    if profile.empty:
        return None
    return float(profile.idxmax())


# -- swing pivots / boxes -----------------------------------------------------
def swing_pivots(df: pd.DataFrame, k: int = 3) -> pd.DataFrame:
    """Swing pivot highs/lows confirmed only after ``k`` right bars close.

    A pivot high at bar i requires high[i] strictly above the highs of the
    k bars on each side (lows symmetric). Its confirmation timestamp is the
    index of bar ``i + k`` — the pivot may only be used by bars *after* it.

    Returns a DataFrame with columns ``ts, kind('high'|'low'), price,
    confirm_ts`` sorted by ``ts``.
    """
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    n = len(df)
    records: list[tuple] = []
    for i in range(k, n - k):
        h_left, h_right = high[i - k : i], high[i + 1 : i + k + 1]
        if high[i] > h_left.max() and high[i] > h_right.max():
            records.append((df.index[i], "high", high[i], df.index[i + k]))
        l_left, l_right = low[i - k : i], low[i + 1 : i + k + 1]
        if low[i] < l_left.min() and low[i] < l_right.min():
            records.append((df.index[i], "low", low[i], df.index[i + k]))
    out = pd.DataFrame(records, columns=PIVOT_COLUMNS)
    return out.sort_values("ts").reset_index(drop=True)


@dataclass(frozen=True)
class Box:
    """Price box/zone built from confirmed swing pivots."""

    top: float
    bottom: float
    formed_ts: pd.Timestamp  # confirmation ts of the latest pivot used

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def height(self) -> float:
        return self.top - self.bottom


def build_box(
    pivots: pd.DataFrame, as_of: pd.Timestamp | None = None, recent: int = 2
) -> Box | None:
    """Build a box from the most recent confirmed pivots.

    Only pivots with ``confirm_ts < as_of`` are usable at bar ``as_of``
    (스펙 §3.2: 봉 t에서는 t−1 이전 확정 피벗만). Uses the last ``recent``
    confirmed pivot highs/lows: top = max(highs), bottom = min(lows).
    Returns None when either side is missing or the box is degenerate.
    """
    if pivots.empty:
        return None
    usable = pivots if as_of is None else pivots[pivots["confirm_ts"] < as_of]
    highs = usable[usable["kind"] == "high"].tail(recent)
    lows = usable[usable["kind"] == "low"].tail(recent)
    if highs.empty or lows.empty:
        return None
    top = float(highs["price"].max())
    bottom = float(lows["price"].min())
    if top <= bottom:
        return None
    formed_ts = max(highs["confirm_ts"].max(), lows["confirm_ts"].max())
    return Box(top=top, bottom=bottom, formed_ts=formed_ts)


# -- cross-timeframe alignment ------------------------------------------------
def resample_shift_align(
    higher: pd.Series | pd.DataFrame, lower_index: pd.DatetimeIndex
):
    """Align a higher-TF series/frame onto a lower-TF index without lookahead.

    스펙 §1.2: 상위 TF에서 ``shift(1)`` 후 ffill — a lower-TF bar opening at
    time t only ever sees higher-TF bars that *closed* at or before t.
    Lower bars before the first completed higher bar get NaN.
    """
    shifted = higher.shift(1)
    union = shifted.index.union(lower_index)
    return shifted.reindex(union).ffill().reindex(lower_index)
