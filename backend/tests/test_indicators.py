"""Indicator tests: correctness + look-ahead poison (causality) for every
indicator, VPVR window exclusion, pivot confirmation delay, box building
from confirmed pivots only, and shift(1)+ffill cross-TF alignment."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.data.indicators import (
    Box,
    atr,
    build_box,
    ema,
    poc_price,
    resample_shift_align,
    rsi,
    sma,
    swing_pivots,
    volume_profile,
    vwma,
)
from tests.conftest import make_perp_ohlcv, resample_ohlcv


@pytest.fixture
def df() -> pd.DataFrame:
    return make_perp_ohlcv(n=400, seed=11, freq="15min")


class TestMovingAverages:
    def test_sma_hand_values(self):
        s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        out = sma(s, 3)
        assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])
        assert out.iloc[2] == pytest.approx(2.0)
        assert out.iloc[4] == pytest.approx(4.0)

    def test_ema_warmup_and_direction(self):
        s = pd.Series(np.arange(50, dtype=float))
        out = ema(s, 10)
        assert out.iloc[:9].isna().all()
        assert out.iloc[-1] < s.iloc[-1]  # EMA lags a rising series
        assert out.dropna().is_monotonic_increasing

    def test_vwma_equals_sma_with_constant_volume(self, df):
        vol = pd.Series(7.0, index=df.index)
        pd.testing.assert_series_equal(
            vwma(df["close"], vol, 20), sma(df["close"], 20), check_names=False
        )

    def test_vwma_weights_by_volume(self):
        close = pd.Series([100.0, 200.0])
        volume = pd.Series([1.0, 3.0])
        assert vwma(close, volume, 2).iloc[-1] == pytest.approx(175.0)


class TestOscillators:
    def test_rsi_bounds_and_extremes(self):
        up = pd.Series(np.arange(100, dtype=float) + 1.0)
        down = pd.Series(200.0 - np.arange(100, dtype=float))
        assert rsi(up, 14).iloc[-1] == pytest.approx(100.0)
        assert rsi(down, 14).iloc[-1] == pytest.approx(0.0)

    def test_rsi_in_range_and_warmup_nan(self, df):
        out = rsi(df["close"], 14)
        assert out.iloc[:14].isna().all()
        valid = out.dropna()
        assert ((valid >= 0) & (valid <= 100)).all()

    def test_atr_positive_after_warmup(self, df):
        out = atr(df, 14)
        # TR is defined from bar 0 (high-low) → first ATR lands at bar 13.
        assert out.iloc[:13].isna().all()
        assert (out.dropna() > 0).all()


class TestCausality:
    """Look-ahead poison: corrupting the tail never changes earlier values."""

    N_POISON = 5

    def poison(self, frame: pd.DataFrame) -> pd.DataFrame:
        bad = frame.copy()
        cols = [c for c in ("open", "high", "low", "close") if c in bad.columns]
        bad.iloc[-self.N_POISON :, [bad.columns.get_loc(c) for c in cols]] *= 10.0
        return bad

    @pytest.mark.parametrize(
        "func",
        [
            lambda d: sma(d["close"], 20),
            lambda d: ema(d["close"], 20),
            lambda d: vwma(d["close"], d["volume"], 20),
            lambda d: rsi(d["close"], 14),
            lambda d: atr(d, 14),
        ],
        ids=["sma", "ema", "vwma", "rsi", "atr"],
    )
    def test_indicator_head_unchanged(self, df, func):
        clean = func(df)
        poisoned = func(self.poison(df))
        cut = len(df) - self.N_POISON
        pd.testing.assert_series_equal(clean.iloc[:cut], poisoned.iloc[:cut])


class TestVolumeProfile:
    def make_two_cluster_df(self) -> pd.DataFrame:
        idx = pd.date_range("2024-01-01", periods=20, freq="15min")
        price = np.array([100.0] * 10 + [110.0] * 10)
        volume = np.array([10.0] * 10 + [1.0] * 10)
        return pd.DataFrame(
            {
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price,
                "volume": volume,
            },
            index=idx,
        )

    def test_poc_at_heavy_cluster(self):
        df = self.make_two_cluster_df()
        profile = volume_profile(df, window=20, bins=10)
        assert abs(poc_price(profile) - 100.0) < 2.0

    def test_total_volume_conserved(self, df):
        profile = volume_profile(df, window=120, bins=24)
        assert profile.sum() == pytest.approx(df["volume"].iloc[-120:].sum())

    def test_window_excludes_bar_t_and_tail(self, df):
        """VPVR at bar t uses [t-W, t-1] only → corrupting bars >= t is inert."""
        t = len(df) - 10
        clean = volume_profile(df, window=100, bins=24, end=t)
        bad = df.copy()
        bad.iloc[t:, :] *= 10.0  # corrupt bar t and everything after
        poisoned = volume_profile(bad, window=100, bins=24, end=t)
        pd.testing.assert_series_equal(clean, poisoned)

    def test_empty_and_degenerate(self):
        empty = volume_profile(pd.DataFrame(columns=["high", "low", "close", "volume"]))
        assert empty.empty
        assert poc_price(empty) is None


def zigzag_df(n: int = 40) -> pd.DataFrame:
    """Piecewise-linear zigzag: pivot highs at 10 and 30, pivot low at 20."""
    idx = pd.date_range("2024-01-01", periods=n, freq="15min")
    base = np.interp(np.arange(n), [0, 10, 20, 30, n - 1], [100, 110, 95, 112, 98])
    return pd.DataFrame(
        {
            "open": base,
            "high": base + 0.5,
            "low": base - 0.5,
            "close": base,
            "volume": np.full(n, 10.0),
        },
        index=idx,
    )


class TestSwingPivots:
    def test_pivots_and_confirmation_index(self):
        df = zigzag_df()
        pivots = swing_pivots(df, k=3)
        assert list(pivots["kind"]) == ["high", "low", "high"]
        assert list(pivots["ts"]) == [df.index[10], df.index[20], df.index[30]]
        # 확정은 우측 k봉 마감 후에만 — confirm_ts = pivot + k bars.
        assert list(pivots["confirm_ts"]) == [df.index[13], df.index[23], df.index[33]]
        assert pivots["price"].iloc[0] == pytest.approx(df["high"].iloc[10])
        assert pivots["price"].iloc[1] == pytest.approx(df["low"].iloc[20])

    def test_tail_poison_leaves_confirmed_pivots_unchanged(self):
        k = 3
        df = zigzag_df()
        clean = swing_pivots(df, k=k)
        bad = df.copy()
        bad.iloc[-(k + 1) :, :] = bad.iloc[-(k + 1) :, :] * 10.0
        poisoned = swing_pivots(bad, k=k)
        cutoff = df.index[len(df) - (k + 1)]
        pd.testing.assert_frame_equal(
            clean[clean["confirm_ts"] < cutoff].reset_index(drop=True),
            poisoned[poisoned["confirm_ts"] < cutoff].reset_index(drop=True),
        )

    def test_no_pivot_without_k_right_bars(self):
        df = zigzag_df(13)  # peak at 10 but only 2 right bars < k=3
        pivots = swing_pivots(df, k=3)
        assert not (pivots["ts"] == df.index[10]).any()


class TestBox:
    def test_box_uses_only_pivots_confirmed_before_as_of(self):
        df = zigzag_df()
        pivots = swing_pivots(df, k=3)

        box = build_box(pivots, as_of=df.index[25])
        assert isinstance(box, Box)
        assert box.top == pytest.approx(df["high"].iloc[10])
        assert box.bottom == pytest.approx(df["low"].iloc[20])
        assert box.midpoint == pytest.approx((box.top + box.bottom) / 2)

        # At bar 33 the pivot high@30 confirms only at index 33 → still
        # unusable (confirm_ts < as_of is required).
        box33 = build_box(pivots, as_of=df.index[33])
        assert box33.top == pytest.approx(df["high"].iloc[10])
        box34 = build_box(pivots, as_of=df.index[34])
        assert box34.top == pytest.approx(df["high"].iloc[30])

    def test_box_none_when_one_side_missing(self):
        df = zigzag_df()
        pivots = swing_pivots(df, k=3)
        # Before the low@20 is confirmed (idx 23) there is no bottom.
        assert build_box(pivots, as_of=df.index[20]) is None
        assert build_box(pd.DataFrame(columns=["ts", "kind", "price", "confirm_ts"])) is None


class TestResampleShiftAlign:
    def test_lower_bar_sees_previous_completed_higher_bar(self, multi_tf_frames):
        h4, m15 = multi_tf_frames["4h"], multi_tf_frames["15m"]
        aligned = resample_shift_align(h4["close"], m15.index)

        # A 15m bar opening exactly at a 4h open reads the 4h bar that
        # CLOSED at that instant (the previous row).
        t0 = h4.index[10]
        assert aligned.loc[t0] == pytest.approx(h4["close"].iloc[9])
        # Mid-window 15m bars keep reading the same completed 4h bar.
        t_mid = h4.index[10] + pd.Timedelta(minutes=45)
        assert aligned.loc[t_mid] == pytest.approx(h4["close"].iloc[9])
        # Bars inside the first 4h window have no completed higher bar.
        assert aligned.loc[m15.index[m15.index < h4.index[1]]].isna().all()

    def test_higher_tf_tail_poison_is_invisible(self, multi_tf_frames):
        h4, m15 = multi_tf_frames["4h"], multi_tf_frames["15m"]
        clean = resample_shift_align(h4["close"], m15.index)

        bad = h4.copy()
        bad.iloc[-1, bad.columns.get_loc("close")] *= 10.0
        poisoned = resample_shift_align(bad["close"], m15.index)
        # The last 4h bar only becomes visible after it closes — no 15m bar
        # in range can see it, so nothing may change.
        pd.testing.assert_series_equal(clean, poisoned)

        bad2 = h4.copy()
        bad2.iloc[-2, bad2.columns.get_loc("close")] *= 10.0
        poisoned2 = resample_shift_align(bad2["close"], m15.index)
        before_last_window = m15.index < h4.index[-1]
        pd.testing.assert_series_equal(
            clean.loc[before_last_window], poisoned2.loc[before_last_window]
        )
        assert (poisoned2.loc[~before_last_window] != clean.loc[~before_last_window]).all()


class TestFixtureCrossTfConsistency:
    def test_coarse_frames_derive_from_fine_base(self, multi_tf_frames):
        m15, h4 = multi_tf_frames["15m"], multi_tf_frames["4h"]
        rebuilt = resample_ohlcv(m15, "4h")
        pd.testing.assert_series_equal(rebuilt["close"], h4["close"])
        pd.testing.assert_series_equal(rebuilt["volume"], h4["volume"])
