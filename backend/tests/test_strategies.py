"""Plan-driven strategy tests (스펙 §3): per-template plan shape (RiskEngine
static gates pass), regime gating, per-TF look-ahead poison + pivot/VPVR
poison, determinism, and the registry (grids, random candidates, mutation)."""
from __future__ import annotations

import random

import numpy as np
import pandas as pd
import pytest

from app.risk.engine import MarketState, RiskEngine
from app.risk.plan import TradePlan
from app.strategies.base import build_plan, explain_plan
from app.strategies import (
    TEMPLATES,
    StrategySpec,
    generate_plan,
    mutate,
    random_candidates,
)
from app.strategies.base import ENTRY_FRACTIONS
from tests.conftest import resample_ohlcv, ts_ms

ALT = "SOLUSDT"
BTC = "BTCUSDT"
TF_15M = pd.Timedelta(minutes=15)


# -- synthetic multi-TF scenario builders ---------------------------------------
def frames_from_close(
    close: np.ndarray,
    volume: np.ndarray | None = None,
    start: str = "2024-01-01",
    spread: float = 0.0005,
) -> dict[str, pd.DataFrame]:
    """15m OHLCV from a close path; 4h/1d resampled from the same base."""
    n = len(close)
    close = np.asarray(close, dtype=float)
    idx = pd.date_range(start=start, periods=n, freq="15min")
    open_ = np.empty(n)
    open_[0] = close[0]
    open_[1:] = close[:-1]
    high = np.maximum(open_, close) * (1.0 + spread)
    low = np.minimum(open_, close) * (1.0 - spread)
    if volume is None:
        volume = np.full(n, 1_000.0)
    df = pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": np.asarray(volume, dtype=float),
            "quote_volume": np.asarray(volume, dtype=float) * close,
        },
        index=idx,
    )
    return {"15m": df, "4h": resample_ohlcv(df, "4h"), "1d": resample_ohlcv(df, "1D")}


def topdown_frames() -> dict[str, pd.DataFrame]:
    """210일 상승(1d 200선 위 + 4h 골든) → 마지막 2일 -2.5% 눌림 + 15m 거래량."""
    n = 210 * 96
    close = np.linspace(50.0, 150.0, n)
    dip = 2 * 96
    close[-dip:] = close[-dip - 1] * np.linspace(1.0, 0.975, dip)
    volume = np.full(n, 1_000.0)
    volume[-1] = 20_000.0
    return frames_from_close(close, volume)


TOPDOWN_PARAMS = {
    "fast": 50, "slow": 200, "pull_band": 0.05, "vol_mult": 1.5,
    "tp_r1": 3.0, "tp_r2": 5.0, "leverage": 4,
}


def confluence_frames(direction: str) -> dict[str, pd.DataFrame]:
    """75일 횡보(4h 50/100/200 수렴) → 마지막 1일 위/아래로 이탈."""
    n = 75 * 96
    close = np.full(n, 100.0)
    target = 101.5 if direction == "long" else 98.5
    close[-96:] = np.linspace(100.0, target, 96)
    return frames_from_close(close)


CONFLUENCE_PARAMS = {
    "tol": 0.02, "band": 0.03, "stop_pad": 0.02, "panic_drop": 0.15,
    "tp_r1": 3.0, "tp_r2": 5.0, "leverage": 5,
}


def panic_frames() -> dict[str, pd.DataFrame]:
    """95일 상승(80→130) → 3일 패닉 급락으로 4h 400선 부근까지."""
    n_rise = 95 * 96
    rise = np.linspace(80.0, 130.0, n_rise)
    n_crash = 3 * 96
    crash = np.linspace(130.0, 112.0, n_crash)
    return frames_from_close(np.concatenate([rise, crash]))


PANIC_PARAMS = {
    "tol": 0.004, "band": 0.03, "stop_pad": 0.02, "panic_drop": 0.10,
    "tp_r1": 3.0, "tp_r2": 5.0, "leverage": 4,
}


def box_frames(position: str) -> dict[str, pd.DataFrame]:
    """5일 주기 사인파 박스(95~105). position: 마지막 위치 하단/상단/중간."""
    period = 480  # 15m bars per cycle (5 days)
    offset = {"bottom": 311, "top": 71, "middle": 240}[position]
    n = 9 * period + offset
    t = np.arange(n)
    close = 100.0 + 5.0 * np.sin(2.0 * np.pi * t / period)
    return frames_from_close(close)


BOX_PARAMS = {
    "pivot_k": 3, "entry_q": 0.25, "stop_buf": 0.03, "tp1_frac": 0.35,
    "leverage": 4,
}


def vwma_frames_long() -> dict[str, pd.DataFrame]:
    """40일 상승(100→140) → 2일 -5% 눌림으로 4h VWMA100 리테스트."""
    n_rise = 40 * 96
    rise = np.linspace(100.0, 140.0, n_rise)
    n_pull = 2 * 96
    pull = np.linspace(140.0, 133.0, n_pull)
    return frames_from_close(np.concatenate([rise, pull]))


def vwma_frames_short() -> dict[str, pd.DataFrame]:
    """30일 횡보(100~110, VPVR 매물대) → 4일 하락으로 VWMA·POC 동시 이탈."""
    period = 480
    n_range = 30 * 96
    t = np.arange(n_range)
    ranging = 105.0 + 5.0 * np.sin(2.0 * np.pi * t / period)
    n_fall = 4 * 96
    fall = np.linspace(ranging[-1], 92.0, n_fall)
    return frames_from_close(np.concatenate([ranging, fall]))


VWMA_PARAMS = {
    "window": 100, "band": 0.02, "vp_window": 120, "stop_pad": 0.015,
    "tp_r1": 3.0, "tp_r2": 5.0, "leverage": 4,
}


def candle_frames(direction: str) -> dict[str, pd.DataFrame]:
    """12일 횡보 → 장대양봉(음봉) 돌파(이탈) + 대량 거래 → 2봉 눌림 리테스트."""
    n_base = 12 * 96
    t = np.arange(n_base)
    base = 100.0 + 0.05 * np.sin(t / 7.0)
    if direction == "long":
        breakout = np.linspace(100.0, 107.0, 16)
        retest = np.linspace(107.0, 100.6, 32)
    else:
        breakout = np.linspace(100.0, 93.0, 16)
        retest = np.linspace(93.0, 99.4, 32)
    close = np.concatenate([base, breakout, retest])
    volume = np.full(len(close), 1_000.0)
    volume[n_base : n_base + 16] = 20_000.0
    return frames_from_close(close, volume)


CANDLE_PARAMS = {
    "body_mult": 2.0, "vol_mult": 1.5, "lookback": 8, "retest_band": 0.01,
    "tp_r1": 3.0, "tp_r2": 5.0, "leverage": 4,
}


# (template, params, frames builder, symbol, regime, expected side)
SCENARIOS = [
    ("topdown_pullback", TOPDOWN_PARAMS, topdown_frames, ALT, "long_alt", "long"),
    ("ma_confluence", CONFLUENCE_PARAMS, lambda: confluence_frames("long"), ALT, "long_alt", "long"),
    ("ma_confluence", CONFLUENCE_PARAMS, lambda: confluence_frames("short"), ALT, "short", "short"),
    ("ma_confluence", PANIC_PARAMS, panic_frames, ALT, "long_alt", "long"),
    ("box_range", BOX_PARAMS, lambda: box_frames("bottom"), ALT, "long_alt", "long"),
    ("box_range", BOX_PARAMS, lambda: box_frames("top"), ALT, "short", "short"),
    ("vwma_support", VWMA_PARAMS, vwma_frames_long, ALT, "long_alt", "long"),
    ("vwma_support", VWMA_PARAMS, vwma_frames_short, ALT, "short", "short"),
    ("candle_breakout", CANDLE_PARAMS, lambda: candle_frames("long"), ALT, "long_alt", "long"),
    ("candle_breakout", CANDLE_PARAMS, lambda: candle_frames("short"), ALT, "short", "short"),
]
SCENARIO_IDS = [
    "topdown-long", "confluence-long", "confluence-short", "panic400-long",
    "box-long", "box-short", "vwma-long", "vwma-short", "candle-long",
    "candle-short",
]


def make_plan(template, params, frames, symbol, regime, **kw) -> TradePlan | None:
    return generate_plan(StrategySpec(template, dict(params)), frames, regime, symbol=symbol, **kw)


# -- plan shape: RiskEngine static gates must pass ------------------------------
class TestPlanShape:
    @pytest.mark.parametrize(
        "template, params, builder, symbol, regime, side", SCENARIOS, ids=SCENARIO_IDS
    )
    def test_plan_shape_and_risk_gates(
        self, settings, template, params, builder, symbol, regime, side
    ):
        frames = builder()
        plan = make_plan(template, params, frames, symbol, regime)
        assert plan is not None, f"{template} scenario did not trigger"
        assert isinstance(plan, TradePlan)
        assert plan.symbol == symbol
        assert plan.side == side

        # 분할 진입 50/25/25 래더 (몰빵 금지).
        assert [leg.fraction for leg in plan.entries] == list(ENTRY_FRACTIONS)
        assert plan.entries_fraction_sum == pytest.approx(1.0)
        # 분할 익절 ≥ 2레그, 비중 합 1.0.
        assert len(plan.tps) >= 2
        assert plan.tps_fraction_sum == pytest.approx(1.0)
        # 근거 ≥ 2, 한국어 로그 vocabulary.
        assert len(plan.evidence) >= 2
        assert all(any("가" <= ch <= "힣" for ch in e) for e in plan.evidence)
        # 손절 레그 kind.
        assert plan.stop.kind == "stop"
        assert all(leg.kind == "entry" for leg in plan.entries)
        assert all(leg.kind == "tp" for leg in plan.tps)
        # 레버리지 캡: 최소 3배, BTC 10배 / 알트 5배.
        cap = 10 if symbol == "BTCUSDT" else 5
        assert 3 <= plan.leverage <= cap

        # 패시브 사이드: long 진입가 < mark, short 진입가 > mark.
        mark = float(frames["15m"]["close"].iloc[-1])
        if side == "long":
            assert all(leg.price < mark for leg in plan.entries)
        else:
            assert all(leg.price > mark for leg in plan.entries)

        # RiskEngine 게이트 (정적 + 런타임 기본 상태) 통과.
        state = MarketState(
            as_of_ts=ts_ms(frames["15m"].index[-1] + TF_15M), mark_price=mark
        )
        result = RiskEngine.review(plan, settings, state)
        assert result.approved, result.reason

    def test_box_final_tp_is_box_midpoint(self):
        frames = box_frames("bottom")
        plan = make_plan("box_range", BOX_PARAMS, frames, ALT, "long_alt")
        assert plan is not None
        # 사인파 박스 ≈ [95, 105] → 미드포인트 ≈ 100.
        assert 99.0 < plan.tps[-1].price < 101.0
        # 손절 = 박스 하단(≈95) 아래.
        assert plan.stop.price < 95.5
        # 1차 익절 < 미드포인트 (long).
        assert plan.tps[0].price < plan.tps[-1].price

    def test_box_middle_is_no_trade(self):
        frames = box_frames("middle")
        assert make_plan("box_range", BOX_PARAMS, frames, ALT, "long_alt") is None
        assert make_plan("box_range", BOX_PARAMS, frames, ALT, "short") is None

    def test_unknown_template_raises(self):
        frames = box_frames("bottom")
        with pytest.raises(ValueError, match="unknown strategy template"):
            generate_plan(StrategySpec("nope", {}), frames, "long_alt", symbol=ALT)


# -- regime gating (스펙 §3.1) ---------------------------------------------------
class TestRegimeGating:
    def test_long_setup_blocked_outside_long_regimes(self):
        frames = topdown_frames()
        assert make_plan("topdown_pullback", TOPDOWN_PARAMS, frames, ALT, "cash") is None
        assert make_plan("topdown_pullback", TOPDOWN_PARAMS, frames, ALT, "short") is None
        assert make_plan("topdown_pullback", TOPDOWN_PARAMS, frames, ALT, "long_alt") is not None

    def test_long_btc_allows_only_btc(self):
        frames = topdown_frames()
        # long_btc: 알트 롱 차단, BTC 롱 허용.
        assert make_plan("topdown_pullback", TOPDOWN_PARAMS, frames, ALT, "long_btc") is None
        assert make_plan("topdown_pullback", TOPDOWN_PARAMS, frames, BTC, "long_btc") is not None
        # long_alt: BTC 롱도 허용 (시장 전반 롱장).
        assert make_plan("topdown_pullback", TOPDOWN_PARAMS, frames, BTC, "long_alt") is not None

    def test_short_setup_only_in_short_regime(self):
        frames = box_frames("top")
        assert make_plan("box_range", BOX_PARAMS, frames, ALT, "short") is not None
        assert make_plan("box_range", BOX_PARAMS, frames, ALT, "long_alt") is None
        assert make_plan("box_range", BOX_PARAMS, frames, ALT, "long_btc") is None
        assert make_plan("box_range", BOX_PARAMS, frames, ALT, "cash") is None

    def test_cash_regime_always_none(self):
        for template, params, builder, symbol, _, _side in SCENARIOS:
            assert make_plan(template, params, builder(), symbol, "cash") is None


# -- look-ahead poison (스펙 §1.2·§3.2·§9) ----------------------------------------
def _poison_tail(df: pd.DataFrame, tf: str, bars: int = 12) -> pd.DataFrame:
    """Append absurd future bars (가격 ×10, 거래량 ×100) after the frame end."""
    freq = {"15m": "15min", "4h": "4h", "1d": "1D"}[tf]
    start = df.index[-1] + pd.tseries.frequencies.to_offset(freq)
    idx = pd.date_range(start=start, periods=bars, freq=freq)
    price = float(df["close"].iloc[-1]) * 10.0
    poison = pd.DataFrame(
        {
            "open": price,
            "high": price * 1.5,
            "low": price * 0.5,
            "close": price,
            "volume": float(df["volume"].max()) * 100.0,
            "quote_volume": float(df["volume"].max()) * 100.0 * price,
        },
        index=idx,
    )
    return pd.concat([df, poison])


class TestLookAheadPoison:
    @pytest.mark.parametrize(
        "template, params, builder, symbol, regime, side", SCENARIOS, ids=SCENARIO_IDS
    )
    def test_future_bars_never_change_the_plan(
        self, template, params, builder, symbol, regime, side
    ):
        frames = builder()
        as_of = frames["15m"].index[-1] + TF_15M
        base = make_plan(template, params, frames, symbol, regime, as_of=as_of)
        assert base is not None
        # 각 TF를 독립적으로 오염 + 전체 동시 오염.
        poisons = [{tf} for tf in frames] + [set(frames)]
        for tfs in poisons:
            poisoned = {
                tf: (_poison_tail(df, tf) if tf in tfs else df.copy())
                for tf, df in frames.items()
            }
            again = make_plan(template, params, poisoned, symbol, regime, as_of=as_of)
            assert again == base, f"look-ahead leak via poisoned {sorted(tfs)}"

    def test_pivot_vpvr_poison_4h_tail(self):
        """4h 마지막 k+1봉(미확정 피벗 후보) 이후 오염 → 박스/VPVR 레벨 불변."""
        frames = box_frames("bottom")
        as_of = frames["15m"].index[-1] + TF_15M
        base = make_plan("box_range", BOX_PARAMS, frames, ALT, "long_alt", as_of=as_of)
        assert base is not None
        k = int(BOX_PARAMS["pivot_k"])
        poisoned = dict(frames)
        poisoned["4h"] = _poison_tail(frames["4h"], "4h", bars=k + 1)
        again = make_plan("box_range", BOX_PARAMS, poisoned, ALT, "long_alt", as_of=as_of)
        assert again == base
        # 오염된 극단값(×10)이 박스 레벨(손절/익절)에 새어들지 않았다.
        assert again.stop.price < 110.0
        assert all(leg.price < 110.0 for leg in again.tps)

        # VPVR 사용 템플릿도 동일 (vwma_support 숏 = VWMA+POC 이탈).
        vframes = vwma_frames_short()
        v_as_of = vframes["15m"].index[-1] + TF_15M
        vbase = make_plan("vwma_support", VWMA_PARAMS, vframes, ALT, "short", as_of=v_as_of)
        assert vbase is not None
        vpoisoned = dict(vframes)
        vpoisoned["4h"] = _poison_tail(vframes["4h"], "4h", bars=6)
        vagain = make_plan("vwma_support", VWMA_PARAMS, vpoisoned, ALT, "short", as_of=v_as_of)
        assert vagain == vbase


# -- determinism -----------------------------------------------------------------
class TestDeterminism:
    @pytest.mark.parametrize(
        "template, params, builder, symbol, regime, side", SCENARIOS, ids=SCENARIO_IDS
    )
    def test_same_input_same_plan(self, template, params, builder, symbol, regime, side):
        frames = builder()
        snapshot = {tf: df.copy() for tf, df in frames.items()}
        first = make_plan(template, params, frames, symbol, regime)
        second = make_plan(template, params, frames, symbol, regime)
        assert first is not None and first == second
        for tf, df in frames.items():  # 입력 프레임 불변 (부수효과 없음)
            pd.testing.assert_frame_equal(df, snapshot[tf])


# -- spec / registry --------------------------------------------------------------
class TestSpec:
    def test_id_key_format(self):
        spec = StrategySpec("box_range", {"pivot_k": 3, "entry_q": 0.25})
        assert spec.id_key() == "box_range(entry_q=0.25,pivot_k=3)"

    def test_id_key_sorted(self):
        spec = StrategySpec("vwma_support", {"window": 100, "band": 0.01})
        assert spec.id_key() == "vwma_support(band=0.01,window=100)"


class TestRegistry:
    def test_templates_are_the_coin_templates(self):
        assert set(TEMPLATES) == {
            "topdown_pullback",
            "ma_confluence",
            "box_range",
            "vwma_support",
            "candle_breakout",
            "vpvr_accum",  # 가이드 p118-119 매집 시그널 (2026-07-22)
        }

    def test_grid_ranges_match_spec(self):
        g = TEMPLATES["topdown_pullback"]
        assert (g["fast"].low, g["fast"].high) == (30, 70)
        assert (g["slow"].low, g["slow"].high) == (120, 240)
        assert g["pull_band"].is_int is False
        g = TEMPLATES["box_range"]
        assert (g["pivot_k"].low, g["pivot_k"].high) == (2, 5)  # 피벗 확정 k
        assert (g["entry_q"].low, g["entry_q"].high) == (0.15, 0.30)  # 박스 분위
        assert g["entry_q"].is_int is False
        g = TEMPLATES["vwma_support"]
        assert (g["window"].low, g["window"].high) == (60, 150)  # VWMA 창
        assert (g["vp_window"].low, g["vp_window"].high) == (80, 200)
        g = TEMPLATES["candle_breakout"]
        assert (g["body_mult"].low, g["body_mult"].high) == (1.5, 3.0)
        assert (g["lookback"].low, g["lookback"].high) == (3, 12)

    def test_every_template_searches_leverage(self):
        for grid in TEMPLATES.values():
            pr = grid["leverage"]
            assert (pr.low, pr.high) == (3, 10)
            assert pr.is_int is True

    def test_tp_r_grids_keep_alt_rr_floor(self):
        """R-배수 익절 그리드 최솟값이 알트 손익비 하한 1:3을 밑돌지 않는다."""
        for name, grid in TEMPLATES.items():
            if "tp_r1" in grid:
                floor = 0.5 * (grid["tp_r1"].low + grid["tp_r2"].low)
                assert floor >= 3.0, name

    def test_random_candidates_within_ranges(self):
        rng = random.Random(1)
        specs = random_candidates(200, rng)
        assert len(specs) == 200
        assert {s.template for s in specs} == set(TEMPLATES)
        for spec in specs:
            grid = TEMPLATES[spec.template]
            assert set(spec.params) == set(grid)
            for key, value in spec.params.items():
                pr = grid[key]
                assert pr.low <= value <= pr.high
                if pr.is_int:
                    assert value == int(value)
            if spec.template == "topdown_pullback":
                assert spec.params["fast"] < spec.params["slow"]

    def test_mutate_clamps_and_preserves_template(self):
        rng = random.Random(3)
        spec = StrategySpec("box_range", dict(BOX_PARAMS))
        for _ in range(100):
            child = mutate(spec, rng)
            assert child.template == "box_range"
            for key, value in child.params.items():
                pr = TEMPLATES["box_range"][key]
                assert pr.low <= value <= pr.high
            spec = child

    def test_mutate_keeps_topdown_constraint(self):
        rng = random.Random(4)
        spec = StrategySpec("topdown_pullback", {**TOPDOWN_PARAMS, "fast": 70, "slow": 120})
        for _ in range(100):
            child = mutate(spec, rng)
            assert child.params["fast"] < child.params["slow"]
            spec = child

    def test_mutate_does_not_modify_original(self):
        spec = StrategySpec("vwma_support", dict(VWMA_PARAMS))
        before = dict(spec.params)
        mutate(spec, random.Random(5))
        assert spec.params == before

    def test_mutate_fills_params_missing_from_parent(self):
        """레버리지 탐색 이전 챔피언(leverage 없음)도 변이 시 새 차원을 얻는다."""
        legacy = StrategySpec("box_range", {k: v for k, v in BOX_PARAMS.items() if k != "leverage"})
        child = mutate(legacy, random.Random(42))
        assert "leverage" in child.params
        assert 3 <= child.params["leverage"] <= 10

    def test_random_candidates_produce_gate_passing_plans_or_none(
        self, settings, multi_tf_frames
    ):
        """무작위 후보가 낸 플랜은 전부 RiskEngine 정적 게이트를 통과한다."""
        rng = random.Random(2)
        mark = float(multi_tf_frames["15m"]["close"].iloc[-1])
        state = MarketState(
            as_of_ts=ts_ms(multi_tf_frames["15m"].index[-1] + TF_15M), mark_price=mark
        )
        for spec in random_candidates(40, rng):
            for regime, symbol in (("long_alt", BTC), ("short", ALT)):
                plan = generate_plan(spec, multi_tf_frames, regime, symbol=symbol)
                if plan is None:
                    continue
                result = RiskEngine.review(plan, settings, state)
                assert result.approved, f"{spec.id_key()}: {result.reason}"


# -- vpvr_accum: 상승 후 횡보 매집 → 2차 상승 롱 (가이드 p118-119) --------------------


def accum_frames(volume_at: str = "consol") -> dict:
    """상승(+12%, 40×4h) 후 전고점 아래 타이트 횡보(24×4h) 경로.

    volume_at='consol'이면 거래량이 횡보 구간에 집중(매집), 'high'면
    상승 고점 구간에 집중(매집 아님 — 시그널이 나오면 안 됨).
    """
    rise_bars = 40 * 16  # 15m 봉 수 (4h = 16×15m)
    consol_bars = 24 * 16
    warmup = 80 * 16
    rng = np.random.default_rng(7)
    warm = 100.0 + np.cumsum(rng.normal(0, 0.02, warmup))
    rise = np.linspace(warm[-1], warm[-1] * 1.12, rise_bars)
    top = rise[-1]
    consol_mid = top * 0.965  # 전고점 약 3.5% 아래에서 횡보
    # 고점→횡보 전환 1×4h봉(16×15m): 템플릿의 '최근 24봉' 횡보 윈도우가
    # 하락 전환봉의 고점을 포함하지 않게 분리한다.
    glide = np.linspace(top, consol_mid, 16)
    consol = consol_mid * (1 + 0.004 * np.sin(np.linspace(0, 12, consol_bars)))
    close = np.concatenate([warm, rise, glide, consol])
    volume = np.full(len(close), 500.0)
    if volume_at == "consol":
        volume[-consol_bars:] = 6_000.0
    else:
        volume[-consol_bars - rise_bars // 4 : -consol_bars] = 6_000.0
    return frames_from_close(close, volume=volume, spread=0.0002)


VPVR_PARAMS = dict(
    rise_bars=40, rise_min=0.05, consol_bars=24, consol_band=0.03,
    vp_window=120, conc_min=0.30, stop_pad=0.01, tp_r1=3.0, tp_r2=5.0,
    leverage=5,
)


class TestVpvrAccum:
    def test_accumulation_emits_long_plan(self):
        from app.strategies import vpvr_accum

        p = vpvr_accum.plan(accum_frames("consol"), ALT, **VPVR_PARAMS)
        assert p is not None and p.side == "long"
        # 손절 = 횡보 밴드 하단 아래 (시나리오 붕괴 지점).
        h4 = accum_frames("consol")["4h"]
        consol_low = float(h4["low"].to_numpy()[-24:].min())
        assert p.stop.price < consol_low
        assert len(p.evidence) >= 2 and any("매집" in e for e in p.evidence)
        # 정적 게이트 통과 (RR·기하·분할 구조).
        from app.config import Settings
        from app.risk.engine import RiskEngine
        from tests.test_risk_engine import make_state

        settings = Settings(_env_file=None)
        mark = float(h4["close"].iloc[-1])
        verdict = RiskEngine.review(
            p, settings, make_state(mark_price=mark)
        )
        assert verdict.approved, getattr(verdict, "reason", "")

    def test_volume_at_prior_high_stays_flat(self):
        from app.strategies import vpvr_accum

        assert vpvr_accum.plan(accum_frames("high"), ALT, **VPVR_PARAMS) is None

    def test_tail_poison_does_not_change_earlier_decision(self):
        from app.strategies import vpvr_accum

        frames = accum_frames("consol")
        clipped = {tf: df.iloc[:-16] for tf, df in frames.items()}
        before = vpvr_accum.plan(clipped, ALT, **VPVR_PARAMS)
        poisoned = {tf: df.copy() for tf, df in frames.items()}
        for tf, df in poisoned.items():
            df.iloc[-1, df.columns.get_loc("close")] = 1.0  # 미래 오염
        clipped_after = {tf: df.iloc[:-16] for tf, df in poisoned.items()}
        after = vpvr_accum.plan(clipped_after, ALT, **VPVR_PARAMS)
        assert (before is None) == (after is None)
        if before is not None:
            assert before.stop.price == after.stop.price


# -- box_range: 박스 이탈 상태에서는 진입하지 않는다 -------------------------------
def box_frames_at(q: float) -> dict[str, pd.DataFrame]:
    """사인파 박스(94.95~105.05)를 만든 뒤 마지막을 박스 대비 위치 ``q``로 이동.

    q < 0 이면 하단 이탈, q > 1 이면 상단 이탈이다.
    """
    period = 480
    n = 9 * period
    t = np.arange(n)
    close = 100.0 + 5.0 * np.sin(2.0 * np.pi * t / period)
    bottom, height = 94.953, 10.099
    tail = np.linspace(close[-1], bottom + q * height, 2 * 96)
    return frames_from_close(np.concatenate([close, tail]))


#: 이탈 진입이 **정적 리스크 게이트까지 통과**하는 조합. stop_buf가 클수록
#: 손절선이 멀어져 '최소 손절 거리' 게이트에 걸리지 않는다. 그리드 상한이
#: 0.10이므로 아래 값들은 목표 탐색이 실제로 뽑을 수 있는 파라미터다.
BREAKOUT_PARAMS = {
    "pivot_k": 3, "entry_q": 0.25, "stop_buf": 0.10, "tp1_frac": 0.35, "leverage": 4,
}


class TestBoxBreakoutGuard:
    """진입 근거가 "박스 하단 이탈 = 시나리오 붕괴 손절"인데 진입 시점에 이미
    이탈해 있으면 시나리오가 성립하지 않는다.

    하위 게이트(기하·최소 손절 거리)가 일부는 걸러내지만 전부는 아니다 —
    stop_buf가 크면 손절선이 멀어져 통과한다. 그 구간을 여기서 막는다."""

    @pytest.mark.parametrize("q", [-0.01, -0.03])
    def test_no_long_below_the_box(self, q):
        assert make_plan("box_range", BREAKOUT_PARAMS, box_frames_at(q), ALT, "long_alt") is None

    @pytest.mark.parametrize("q", [1.01, 1.03])
    def test_no_short_above_the_box(self, q):
        assert make_plan("box_range", BREAKOUT_PARAMS, box_frames_at(q), ALT, "short") is None

    def test_breakout_entry_would_otherwise_pass_every_risk_gate(self, settings):
        """가드가 없으면 리스크 게이트가 못 막는다는 증거 — 가드의 존재 이유.

        템플릿을 직접 호출해 가드를 우회한 뒤(=가드 이전 동작 재현) 정적
        게이트에 넣으면 승인된다. 손익비가 6:1을 넘는 것은 진입가가 이미
        박스 밖이라 생긴 착시다."""
        from app.risk.engine import MarketState, RiskEngine
        from app.strategies import box_range
        from app.strategies.base import mark_price

        frames = box_frames_at(-0.01)
        mark = mark_price(frames)
        # 가드 이전 로직: 박스 하단 근처이므로 롱 플랜을 만든다.
        box = _box_of(frames, BREAKOUT_PARAMS)
        tp1 = box.bottom + BREAKOUT_PARAMS["tp1_frac"] * box.height
        plan = build_plan(
            symbol=ALT, side="long", mark=mark,
            stop=box.bottom - BREAKOUT_PARAMS["stop_buf"] * box.height,
            evidence=["박스 하단 지지", "분할 진입", "하단 이탈 = 손절"],
            leverage=BREAKOUT_PARAMS["leverage"],
            tps=[(tp1, 0.5), (box.midpoint, 0.5)],
        )
        assert plan is not None, "기하 자체는 성립한다 (하위 게이트가 못 막는 구간)"
        state = MarketState(
            as_of_ts=0, mark_price=mark, open_positions=0, daily_realized_pnl=0.0,
            blackout_windows=[], open_plan_margin=0.0, wallet_balance=None,
        )
        assert RiskEngine._review_static(plan, settings, state).approved, (
            "이 조합은 정적 게이트를 통과한다 — 그래서 템플릿에서 막아야 한다"
        )
        # 그런데 같은 시장 상태에서 템플릿은 이제 관망한다.
        assert make_plan("box_range", BREAKOUT_PARAMS, frames, ALT, "long_alt") is None

    def test_normal_box_entries_still_fire(self):
        """가드가 정상 진입까지 막지 않는지 — 회귀 방지."""
        assert make_plan("box_range", BOX_PARAMS, box_frames("bottom"), ALT, "long_alt") is not None
        assert make_plan("box_range", BOX_PARAMS, box_frames("top"), ALT, "short") is not None

    def test_guard_is_independent_of_the_regime(self):
        """레짐을 열어줘도 이탈 상태면 진입하지 않는다."""
        for regime in ("long_alt", "short"):
            assert make_plan("box_range", BREAKOUT_PARAMS, box_frames_at(-0.01), ALT, regime) is None
            assert make_plan("box_range", BREAKOUT_PARAMS, box_frames_at(1.01), ALT, regime) is None


def _box_of(frames, params):
    from app.data.indicators import build_box, swing_pivots
    from app.strategies.box_range import RECENT_PIVOTS

    return build_box(
        swing_pivots(frames["4h"], k=int(params["pivot_k"])),
        as_of=None, recent=RECENT_PIVOTS,
    )


# -- explain_plan: 관망 사유를 남긴다 ---------------------------------------------
class TestExplainPlan:
    """왜 거래가 없었는지가 로그에 안 남으면 '고장인가'를 매번 코드로
    확인해야 한다. generate_plan 과 **같은 판단**에 사유만 덧붙인다."""

    def _spec(self):
        return StrategySpec("box_range", dict(BOX_PARAMS))

    def test_same_verdict_as_generate_plan(self):
        """사유를 붙인다고 판단이 달라지면 안 된다."""
        for builder, regime in (
            (lambda: box_frames("bottom"), "long_alt"),
            (lambda: box_frames("top"), "short"),
            (lambda: box_frames("middle"), "long_alt"),
            (lambda: box_frames("bottom"), "cash"),
        ):
            frames = builder()
            expected = generate_plan(self._spec(), frames, regime, symbol=ALT)
            got, _reason = explain_plan(self._spec(), frames, regime, symbol=ALT)
            assert (got is None) == (expected is None)
            if expected is not None:
                assert got.side == expected.side

    def test_entry_has_no_reason(self):
        plan, reason = explain_plan(self._spec(), box_frames("top"), "short", symbol=ALT)
        assert plan is not None and reason == ""

    def test_cash_regime_is_reported(self):
        plan, reason = explain_plan(self._spec(), box_frames("top"), "cash", symbol=ALT)
        assert plan is None
        assert "cash" in reason and "관망" in reason

    def test_regime_blocked_signal_names_the_side(self):
        """이번 사고의 핵심 — '롱 신호인데 숏장이라 막힘'이 드러나야 한다."""
        plan, reason = explain_plan(
            self._spec(), box_frames("bottom"), "short", symbol=ALT
        )
        assert plan is None
        assert "롱 신호" in reason and "차단" in reason

    def test_no_setup_reports_the_box_position(self):
        """'셋업 없음'만으로는 고장인지 정상인지 알 수 없다 — 위치를 남긴다."""
        plan, reason = explain_plan(
            self._spec(), box_frames("middle"), "long_alt", symbol=ALT
        )
        assert plan is None
        assert reason.startswith("셋업 없음")
        assert "박스" in reason and "중간" in reason

    def test_breakout_is_distinguished_from_mid_box(self):
        """박스 이탈과 박스 중간은 둘 다 관망이지만 원인이 다르다."""
        _p, mid = explain_plan(self._spec(), box_frames("middle"), "long_alt", symbol=ALT)
        _p2, out = explain_plan(
            self._spec(), box_frames_at(-0.2), "long_alt", symbol=ALT
        )
        assert "중간" in mid
        assert "이탈" in out
        assert mid != out

    def test_unknown_template_does_not_raise(self):
        plan, reason = explain_plan(
            StrategySpec("nope", {}), box_frames("top"), "short", symbol=ALT
        )
        assert plan is None and "알 수 없는" in reason

    def test_broken_frames_do_not_raise(self):
        plan, reason = explain_plan(self._spec(), {}, "short", symbol=ALT)
        assert plan is None
        assert "데이터 부족" in reason or "판단 실패" in reason

    def test_template_without_describe_still_reports(self):
        """진단 함수가 없는 템플릿도 사유는 남아야 한다 (일반 문구)."""
        spec = StrategySpec("vwma_support", {
            "window": 100, "band": 0.01, "vp_window": 120,
            "stop_pad": 0.02, "tp_r1": 3.0, "tp_r2": 5.0, "leverage": 4,
        })
        plan, reason = explain_plan(spec, box_frames("middle"), "long_alt", symbol=ALT)
        if plan is None:
            assert reason.startswith("셋업 없음")
