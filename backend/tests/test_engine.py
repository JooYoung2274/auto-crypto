"""백테스트 엔진 불변식 테스트 (스펙 §4·§9, 오프라인 합성 데이터).

- 관통 체결(터치 미체결) / 갭스루 체결가 / 발주 봉 제외
- 동일 봉 우선순위: 청산 > 손절 exit > 진입 > TP (진입+TP 동시 터치 → 진입만,
  종가가 TP 초과 시 예외적 종가 체결)
- 4h 종가 손절 판정: 첫 15m 시가 taker 청산, 미완결 4h봉 판정 금지
- 청산 정확식·체결마다 재계산, 청산 > 손절
- 펀딩 부호 (long + 양수 rate = 지불)
- TTL 원가격 재큐 (추격 금지) / 플랜 TTL abandoned
- 플랜 종료(손절·최종 TP·무효화·청산) 시 자식 주문 전량 취소, TP 수량 = 체결 수량
- RiskEngine 게이트 거부(손익비·블랙아웃)는 트레이드에서 제외
- 비용 단조 감소, look-ahead 오염(TF별 + 펀딩) 금지
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from conftest import make_multi_tf_frames, resample_ohlcv, ts_ms

from app.backtest.costs import PerpCostModel
from app.backtest.engine import FILL_COLUMNS, run_backtest
from app.backtest.trades import TRADE_COLUMNS
from app.risk.engine import Approval
from app.risk.plan import PlanLeg, TradePlan, liquidation_price

# ---------------------------------------------------------------- helpers


def flat_ohlcv(n: int = 64, price: float = 100.0, start: str = "2024-01-01") -> pd.DataFrame:
    """모든 봉이 o=h=l=c=price 인 15m 프레임 (개별 봉만 덮어써서 시나리오 구성)."""
    idx = pd.date_range(start, periods=n, freq="15min")
    return pd.DataFrame(
        {
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": 1.0,
            "quote_volume": price,
        },
        index=idx,
        dtype=float,
    )


def set_bar(df: pd.DataFrame, i: int, o=None, h=None, l=None, c=None) -> None:
    for col, v in (("open", o), ("high", h), ("low", l), ("close", c)):
        if v is not None:
            df.iloc[i, df.columns.get_loc(col)] = float(v)


def frames_of(df15: pd.DataFrame, with_4h: bool = True) -> dict[str, pd.DataFrame]:
    frames = {"15m": df15}
    if with_4h:
        frames["4h"] = resample_ohlcv(df15, "4h")
    return frames


def make_plan(
    side: str = "long",
    symbol: str = "BTCUSDT",
    entries: tuple = ((99.0, 0.5), (98.0, 0.5)),
    stop: float = 94.0,
    tps: tuple = ((105.0, 0.5), (110.0, 0.5)),
    leverage: int = 5,
    margin: float = 1000.0,
) -> TradePlan:
    return TradePlan(
        symbol=symbol,
        side=side,
        evidence=["지지선 리테스트", "VWMA 지지"],
        entries=[PlanLeg("entry", p, f) for p, f in entries],
        stop=PlanLeg("stop", stop, 1.0),
        tps=[PlanLeg("tp", p, f) for p, f in tps],
        leverage=leverage,
        margin_usdt=margin,
    )


def plan_at(trigger_ts, plan):
    """trigger_ts 봉 마감에 한 번만 플랜을 내는 전략 콜백."""

    def fn(ts):
        return plan if ts == trigger_ts else None

    return fn


class _ApproveAll:
    """게이트 우회 스텁 — 엔진 내부 분기(무효화 등) 단독 검증용."""

    @staticmethod
    def review(plan, settings, state):
        return Approval()


# 기본 롱 플랜 수량 (margin 1000, lev 5, 50/25... 아니고 50/50):
Q1 = 1000.0 * 5 * 0.5 / 99.0  # 25.2525...
Q2 = 1000.0 * 5 * 0.5 / 98.0  # 25.5102...


class TestPerpCostModel:
    def test_defaults_match_spec(self):
        cost = PerpCostModel()
        assert cost.maker_fee == 0.00025
        assert cost.taker_fee == 0.0005
        assert cost.slippage == 0.0005

    def test_maker_taker_costs(self):
        cost = PerpCostModel(maker_fee=0.00025, taker_fee=0.0005, slippage=0.0005)
        assert cost.maker_cost == pytest.approx(0.00025)  # 패시브 = 슬리피지 없음
        assert cost.taker_cost == pytest.approx(0.001)  # taker + 슬리피지
        assert cost.taker_cost > cost.maker_cost
        assert cost.fee(10_000.0, taker=True) == pytest.approx(10.0)
        assert cost.fee(10_000.0, taker=False) == pytest.approx(2.5)


class TestFillModel:
    """관통해야 체결 (규칙 §5) — 터치 미체결, 갭스루 오픈, 발주 봉 제외."""

    def test_touch_does_not_fill_trade_through_does(self, zero_cost, settings):
        df = flat_ohlcv()
        set_bar(df, 2, l=99.0)  # 터치(=99) — 미체결
        set_bar(df, 3, l=98.9)  # 관통(<99) — 체결
        plan = make_plan()
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        assert len(res.fills) == 1
        f = res.fills.iloc[0]
        assert f["ts"] == df.index[3]
        assert f["kind"] == "entry"
        assert f["price"] == pytest.approx(99.0)  # min(open=100, 99)
        assert f["qty"] == pytest.approx(Q1)
        assert f["fee_type"] == "maker"

    def test_placement_bar_excluded(self, zero_cost, settings):
        # 발주 봉(결정 봉) 자신이 지정가를 관통해도 매칭은 다음 봉부터.
        df = flat_ohlcv()
        set_bar(df, 1, l=98.9, c=100.0)
        set_bar(df, 4, l=98.9)
        plan = make_plan()
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        assert len(res.fills) == 1
        assert res.fills.iloc[0]["ts"] == df.index[4]

    def test_gap_through_fills_at_open(self, zero_cost, settings):
        # 갭다운 오픈(97.5 < 지정가 99·98) → 두 레그 모두 시가 체결.
        df = flat_ohlcv()
        set_bar(df, 2, o=97.5, h=97.6, l=97.0, c=97.5)
        plan = make_plan()
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        entries = res.fills[res.fills["kind"] == "entry"]
        assert len(entries) == 2
        assert list(entries["price"]) == pytest.approx([97.5, 97.5])
        assert list(entries["qty"]) == pytest.approx([Q1, Q2])

    def test_short_entry_symmetric(self, zero_cost, settings):
        df = flat_ohlcv()
        set_bar(df, 2, h=102.5)
        plan = make_plan(
            side="short",
            entries=((101.0, 0.5), (102.0, 0.5)),
            stop=106.0,
            tps=((95.0, 0.5), (90.0, 0.5)),
        )
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        entries = res.fills[res.fills["kind"] == "entry"]
        assert list(entries["price"]) == pytest.approx([101.0, 102.0])  # max(open, P)
        assert list(entries["side"]) == ["sell", "sell"]


class TestSameBarPriority:
    def test_entry_and_tp_same_bar_entry_only(self, zero_cost, settings):
        # 봉 2에서 진입가(99)와 TP(105) 동시 터치 → 진입만, TP는 다음 봉.
        df = flat_ohlcv()
        set_bar(df, 2, l=98.5, h=106.0, c=104.0)
        set_bar(df, 3, o=104.0, h=106.0, c=105.5)
        plan = make_plan()
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        bar2 = res.fills[res.fills["ts"] == df.index[2]]
        assert list(bar2["kind"]) == ["entry"]
        bar3 = res.fills[res.fills["ts"] == df.index[3]]
        assert list(bar3["kind"]) == ["tp"]
        assert bar3.iloc[0]["price"] == pytest.approx(105.0)  # max(open=104, 105)
        # TP 수량 = 실제 체결 수량 기준 (미체결 leg2 제외).
        assert bar3.iloc[0]["qty"] == pytest.approx(Q1 * 0.5)

    def test_close_beyond_tp_exception_fills_at_close(self, zero_cost, settings):
        # 진입 봉 종가(106)가 TP(105) 초과 → 예외적 종가 체결.
        df = flat_ohlcv()
        set_bar(df, 2, l=98.5, h=107.0, c=106.0)
        plan = make_plan()
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        bar2 = res.fills[res.fills["ts"] == df.index[2]]
        assert list(bar2["kind"]) == ["entry", "tp"]
        tp = bar2[bar2["kind"] == "tp"].iloc[0]
        assert tp["price"] == pytest.approx(106.0)  # 종가 체결
        assert tp["qty"] == pytest.approx(Q1 * 0.5)

    def test_liquidation_beats_stop_same_bar(self, zero_cost, settings):
        # 4h 종가 손절 판정 봉에서 청산가도 관통 → 청산이 우선 (스펙 §4).
        df = flat_ohlcv()
        set_bar(df, 2, l=97.9)  # 두 진입 레그 체결 (99·98)
        set_bar(df, 31, l=93.0, c=93.0)  # 4h 봉 1 종가 93 < 손절 94
        set_bar(df, 32, o=93.5, h=94.0, l=88.5, c=93.0)  # low ≤ liq(≈89.0)
        for i in range(33, 64):
            set_bar(df, i, o=93.0, h=93.0, l=93.0, c=93.0)
        plan = make_plan(leverage=10)
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        assert res.liquidation_count == 1
        kinds = list(res.fills["kind"])
        assert "liquidation" in kinds and "stop" not in kinds
        liq_fill = res.fills[res.fills["kind"] == "liquidation"].iloc[0]
        assert liq_fill["ts"] == df.index[32]
        t = res.trades.iloc[0]
        assert t["exit_reason"] == "liquidation"
        # 청산 시 격리마진 전액 손실 (zero cost → pnl = -마진).
        assert t["pnl"] == pytest.approx(-1000.0)


class TestFourHourStop:
    def _stop_scenario(self):
        df = flat_ohlcv()
        set_bar(df, 3, l=98.9)  # leg1 체결 @99
        set_bar(df, 31, l=93.0, c=93.0)  # leg2 갭스루 아님: min(100,98)=98 체결,
        # 4h 봉 1(04:00~08:00) 종가 93 < 손절 94 → 이탈
        set_bar(df, 32, o=93.5, h=94.0, l=93.0, c=93.5)
        for i in range(33, 64):
            set_bar(df, i, o=93.5, h=93.5, l=93.5, c=93.5)
        return df

    def test_stop_exits_at_first_15m_open_after_4h_close(self, zero_cost, settings):
        df = self._stop_scenario()
        plan = make_plan()
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        stops = res.fills[res.fills["kind"] == "stop"]
        assert len(stops) == 1
        s = stops.iloc[0]
        assert s["ts"] == df.index[32]  # 4h 마감(08:00) ≥ open인 첫 15m봉
        assert s["price"] == pytest.approx(93.5)  # 그 봉의 시가
        qt = Q1 + Q2
        assert s["qty"] == pytest.approx(qt)
        t = res.trades.iloc[0]
        assert t["exit_reason"] == "stop"
        assert not t["open"]
        avg = (Q1 * 99.0 + Q2 * 98.0) / qt
        assert t["pnl"] == pytest.approx((93.5 - avg) * qt)
        # 손절 시 잔여 TP 레그 전량 취소 (플랜 스코프 취소).
        cancelled = [e for e in res.order_events if e["event"] == "cancelled"]
        assert len(cancelled) == 2
        assert all("손절" in e["reason"] for e in cancelled)
        assert {e["kind"] for e in cancelled} == {"tp"}

    def test_stop_exit_is_taker_with_slippage(self, settings):
        df = self._stop_scenario()
        plan = make_plan()
        cost = PerpCostModel()
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), cost, settings)
        s = res.fills[res.fills["kind"] == "stop"].iloc[0]
        assert s["fee_type"] == "taker"
        qt = Q1 + Q2
        assert s["fee"] == pytest.approx(93.5 * qt * (cost.taker_fee + cost.slippage))
        entries = res.fills[res.fills["kind"] == "entry"]
        assert set(entries["fee_type"]) == {"maker"}

    def test_no_judgment_before_4h_close(self, zero_cost, settings):
        # 15m 종가(봉 31, 93)는 이미 손절선 아래지만 4h 봉이 마감되기 전에는
        # 판정하지 않는다 → 청산은 정확히 봉 32 시가.
        df = self._stop_scenario()
        plan = make_plan()
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        stops = res.fills[res.fills["kind"] == "stop"]
        assert (stops["ts"] >= df.index[32]).all()

    def test_incomplete_4h_bar_never_judged(self, zero_cost, settings):
        # 손절선 이탈이 데이터 마지막(미완결) 4h 윈도에서만 발생 → 판정 없음.
        df = flat_ohlcv()
        set_bar(df, 3, l=98.9)  # leg1 체결
        for i in range(49, 64):  # 4h 봉 3 (12:00~16:00)은 16:00 마감 — 도달 불가
            set_bar(df, i, o=93.0, h=93.0, l=93.0, c=93.0)
        plan = make_plan()
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        assert "stop" not in set(res.fills["kind"])
        t = res.trades.iloc[0]
        assert bool(t["open"]) and t["exit_reason"] == "eod"

    def test_flat_invalidation_cancels_ladder_before_entry(self, zero_cost, settings):
        # 진입 전 무효화 (스펙 §2): 플랫 상태에서 4h 종가가 손절선 이탈 →
        # 래더 전량 취소. 정합 플랜은 게이트(패시브/기하)가 이 상황을 선차단하므로
        # 게이트 우회 스텁으로 엔진 분기 자체를 검증한다 — 판정(취소)이 같은 봉
        # 진입 체결보다 우선함도 함께 확인.
        df = flat_ohlcv()
        set_bar(df, 31, o=93.0, h=93.0, l=92.5, c=93.0)  # 4h 봉 1 종가 93
        set_bar(df, 32, o=93.0, h=93.2, l=88.0, c=93.0)  # 진입가(90·89) 관통 봉
        plan = make_plan(entries=((90.0, 0.5), (89.0, 0.5)), stop=95.0)
        res = run_backtest(
            frames_of(df), plan_at(df.index[30], plan), zero_cost, settings,
            risk=_ApproveAll,
        )
        assert len(res.fills) == 0
        assert len(res.trades) == 0
        cancelled = [e for e in res.order_events if e["event"] == "cancelled"]
        assert len(cancelled) == 4  # 진입 2 + TP 2
        assert all("무효화" in e["reason"] for e in cancelled)


class TestLiquidation:
    def test_liquidation_loses_isolated_margin_and_cancels_orders(
        self, zero_cost, settings
    ):
        df = flat_ohlcv()
        set_bar(df, 2, l=97.9)  # 두 레그 체결 → avg ≈ 98.4975 (lev 10)
        set_bar(df, 4, o=95.0, h=95.0, l=88.0, c=90.0)  # low ≤ liq ≈ 89.0
        for i in range(5, 64):
            set_bar(df, i, o=90.0, h=90.0, l=90.0, c=90.0)
        plan = make_plan(leverage=10)
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        assert res.liquidation_count == 1
        liq = res.fills[res.fills["kind"] == "liquidation"].iloc[0]
        qt = Q1 * (10 / 5) / 2 + Q2 * (10 / 5) / 2  # lev 10 수량 = margin*lev*f/price
        q1, q2 = 1000.0 * 10 * 0.5 / 99.0, 1000.0 * 10 * 0.5 / 98.0
        avg = (q1 * 99.0 + q2 * 98.0) / (q1 + q2)
        expected_liq = liquidation_price(avg, "long", 10, (q1 + q2) * avg)
        assert liq["price"] == pytest.approx(expected_liq)
        assert liq["ts"] == df.index[4]
        t = res.trades.iloc[0]
        assert t["exit_reason"] == "liquidation"
        assert t["pnl"] == pytest.approx(-1000.0)  # 격리마진 전액 손실
        assert res.equity.iloc[-1] == pytest.approx(settings.initial_seed_usdt - 1000.0)
        cancelled = [e for e in res.order_events if e["event"] == "cancelled"]
        assert len(cancelled) == 2 and all("청산" in e["reason"] for e in cancelled)

    def test_liq_price_recomputed_on_every_fill(self, zero_cost, settings):
        # 레그별 체결마다 avg_entry·청산가 정확식 재계산 (스펙 §4).
        df = flat_ohlcv()
        set_bar(df, 2, l=98.5)  # leg1만 체결 @99
        set_bar(df, 5, l=97.9)  # leg2 체결 @98
        plan = make_plan(leverage=10)
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        entries = res.fills[res.fills["kind"] == "entry"]
        q1, q2 = 1000.0 * 10 * 0.5 / 99.0, 1000.0 * 10 * 0.5 / 98.0
        first = entries.iloc[0]
        assert first["avg_entry"] == pytest.approx(99.0)
        assert first["liq_price"] == pytest.approx(
            liquidation_price(99.0, "long", 10, q1 * 99.0)
        )
        second = entries.iloc[1]
        avg2 = (q1 * 99.0 + q2 * 98.0) / (q1 + q2)
        assert second["avg_entry"] == pytest.approx(avg2)
        assert second["liq_price"] == pytest.approx(
            liquidation_price(avg2, "long", 10, (q1 + q2) * avg2)
        )
        assert second["liq_price"] < first["liq_price"]  # 평단 하락 → 청산가 하락


class TestFunding:
    def test_long_pays_positive_rate(self, zero_cost, settings):
        df = flat_ohlcv()
        set_bar(df, 2, l=97.9)  # 두 레그 체결, 이후 100 유지
        plan = make_plan()
        funding = pd.Series([0.001], index=[df.index[32]], name="rate")  # 08:00 정산
        res = run_backtest(
            frames_of(df), plan_at(df.index[1], plan), zero_cost, settings,
            funding=funding,
        )
        qt = Q1 + Q2
        paid = 0.001 * qt * 100.0  # rate × qty × 봉 open (마크가)
        assert res.funding_paid == pytest.approx(paid)
        # 정산 봉에서 에쿼티가 정확히 지불액만큼 감소.
        assert res.equity.iloc[32] - res.equity.iloc[31] == pytest.approx(-paid)
        t = res.trades.iloc[0]
        assert t["funding_paid"] == pytest.approx(paid)
        avg = (Q1 * 99.0 + Q2 * 98.0) / qt
        assert t["pnl"] == pytest.approx((100.0 - avg) * qt - paid)

    def test_long_receives_negative_rate(self, zero_cost, settings):
        df = flat_ohlcv()
        set_bar(df, 2, l=97.9)
        plan = make_plan()
        funding = pd.Series([-0.001], index=[df.index[32]], name="rate")
        res = run_backtest(
            frames_of(df), plan_at(df.index[1], plan), zero_cost, settings,
            funding=funding,
        )
        assert res.funding_paid == pytest.approx(-0.001 * (Q1 + Q2) * 100.0)
        assert res.equity.iloc[32] > res.equity.iloc[31]

    def test_short_receives_positive_rate(self, zero_cost, settings):
        df = flat_ohlcv()
        set_bar(df, 2, h=102.5)
        plan = make_plan(
            side="short",
            entries=((101.0, 0.5), (102.0, 0.5)),
            stop=106.0,
            tps=((95.0, 0.5), (90.0, 0.5)),
        )
        funding = pd.Series([0.001], index=[df.index[32]], name="rate")
        res = run_backtest(
            frames_of(df), plan_at(df.index[1], plan), zero_cost, settings,
            funding=funding,
        )
        qt = 2500.0 / 101.0 + 2500.0 / 102.0
        assert res.funding_paid == pytest.approx(-0.001 * qt * 100.0)  # 수취(음수)
        assert res.equity.iloc[32] - res.equity.iloc[31] == pytest.approx(
            0.001 * qt * 100.0
        )

    def test_no_position_no_funding(self, zero_cost, settings):
        df = flat_ohlcv()
        plan = make_plan()  # 진입가 미관통 → 플랫 유지
        funding = pd.Series([0.001], index=[df.index[32]], name="rate")
        res = run_backtest(
            frames_of(df), plan_at(df.index[1], plan), zero_cost, settings,
            funding=funding,
        )
        assert res.funding_paid == 0.0


class TestTTL:
    def test_order_ttl_requeues_at_original_price_only(self, zero_cost, settings):
        df = flat_ohlcv(n=20)
        set_bar(df, 12, l=98.9)  # 재큐 후 원가격(99)으로 체결
        plan = make_plan()
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        requeued = [e for e in res.order_events if e["event"] == "requeued"]
        assert requeued, "order_ttl_bars 경과 시 재큐되어야 함"
        # 발주 봉 1 + TTL 8 → 첫 재큐는 봉 10, 가격은 원래 레그 가격 그대로.
        first_cycle = [e for e in requeued if e["attempt"] == 1]
        assert first_cycle and all(e["ts"] == df.index[10] for e in first_cycle)
        entry_prices = {e["price"] for e in requeued if e["kind"] == "entry"}
        assert entry_prices == {99.0, 98.0}
        plan_prices = {99.0, 98.0, 105.0, 110.0}
        assert all(e["price"] in plan_prices for e in res.order_events)  # 추격 금지
        fill = res.fills.iloc[0]
        assert fill["ts"] == df.index[12]
        assert fill["price"] == pytest.approx(99.0)

    def test_plan_ttl_abandons_unfilled_ladder(self, zero_cost, settings):
        df = flat_ohlcv(n=120)  # 진입가 미관통 상태 지속
        plan = make_plan()
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        assert len(res.fills) == 0
        assert len(res.trades) == 0
        cancelled = [e for e in res.order_events if e["event"] == "cancelled"]
        assert len(cancelled) == 4
        assert all("TTL" in e["reason"] and "abandoned" in e["reason"] for e in cancelled)
        # 발주 봉 1 + plan_ttl 96 → 봉 97에서 abandoned.
        assert all(e["ts"] == df.index[97] for e in cancelled)
        assert (res.equity == settings.initial_seed_usdt).all()


class TestPlanScopedCancellation:
    def test_final_tp_cancels_unfilled_entry_and_tp_qty_from_fills(
        self, zero_cost, settings
    ):
        df = flat_ohlcv()
        set_bar(df, 2, l=98.5)  # leg1(99)만 체결 — leg2(96)는 영영 미체결
        set_bar(df, 4, h=105.5, c=104.0)  # TP1 @105
        set_bar(df, 5, h=110.5, c=109.0)  # TP2(최종) @110 → 잔량 전량
        plan = make_plan(entries=((99.0, 0.5), (96.0, 0.5)))
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        tps = res.fills[res.fills["kind"] == "tp"]
        assert len(tps) == 2
        # TP 수량 = 실제 체결 수량(leg1) 기준 재계산.
        assert tps.iloc[0]["qty"] == pytest.approx(Q1 * 0.5)
        assert tps.iloc[1]["qty"] == pytest.approx(Q1 * 0.5)  # 최종 = 잔량 전량
        cancelled = [e for e in res.order_events if e["event"] == "cancelled"]
        assert len(cancelled) == 1
        assert cancelled[0]["kind"] == "entry" and cancelled[0]["price"] == 96.0
        assert "익절" in cancelled[0]["reason"]
        t = res.trades.iloc[0]
        assert t["exit_reason"] == "tp" and not t["open"]
        assert t["entry_price"] == pytest.approx(99.0)
        assert t["exit_price"] == pytest.approx(107.5)  # (105+110)/2 가중
        assert t["pnl"] == pytest.approx(Q1 * 0.5 * (105 - 99) + Q1 * 0.5 * (110 - 99))
        assert t["net_ret"] == pytest.approx(t["pnl"] / (Q1 * 99.0 / 5))
        assert t["holding_hours"] == pytest.approx(0.75)  # 봉 2 → 봉 5 (45분)


class TestRiskGateIntegration:
    def test_rr_gate_rejection_excluded_from_trades(self, zero_cost, settings):
        df = flat_ohlcv()
        set_bar(df, 2, l=97.9)  # 승인됐다면 체결됐을 봉
        bad = make_plan(tps=((103.0, 0.5), (104.0, 0.5)))  # rr ≈ 1.11 < 2
        res = run_backtest(frames_of(df), plan_at(df.index[1], bad), zero_cost, settings)
        assert len(res.trades) == 0
        assert len(res.fills) == 0
        assert len(res.order_events) == 0
        assert len(res.rejections) == 1
        ts, reason = res.rejections[0]
        assert ts == df.index[1]
        assert "손익비" in reason

    def test_blackout_window_from_econ_events(self, zero_cost, settings):
        df = flat_ohlcv()
        set_bar(df, 2, l=97.9)
        plan = make_plan()
        res = run_backtest(
            frames_of(df), plan_at(df.index[1], plan), zero_cost, settings,
            econ_events=[ts_ms(df.index[2])],  # ±12h 블랙아웃이 결정 시점 포함
        )
        assert len(res.trades) == 0
        assert "블랙아웃" in res.rejections[0][1]


class TestCostMonotonicity:
    def test_costs_strictly_reduce_performance(self, settings):
        # 진입(maker) + 4h 손절(taker) 왕복 — 비용이 클수록 최종 에쿼티 감소.
        df = flat_ohlcv()
        set_bar(df, 3, l=98.9)
        set_bar(df, 31, l=93.0, c=93.0)
        set_bar(df, 32, o=93.5, h=94.0, l=93.0, c=93.5)
        for i in range(33, 64):
            set_bar(df, i, o=93.5, h=93.5, l=93.5, c=93.5)
        plan = make_plan()

        def run(cost):
            return run_backtest(
                frames_of(df), plan_at(df.index[1], plan), cost, settings
            )

        free = run(PerpCostModel(0.0, 0.0, 0.0))
        default = run(PerpCostModel())
        heavy = run(PerpCostModel(maker_fee=0.0005, taker_fee=0.001, slippage=0.001))
        assert free.equity.iloc[-1] > default.equity.iloc[-1] > heavy.equity.iloc[-1]
        assert (
            free.trades.iloc[0]["pnl"]
            > default.trades.iloc[0]["pnl"]
            > heavy.trades.iloc[0]["pnl"]
        )


class TestNoLookAhead:
    """봉 k 이후 데이터를 오염시켜도 k까지의 결과는 불변 — OHLCV TF별 + 펀딩."""

    @staticmethod
    def _plan_fn_for(frames):
        f15 = frames["15m"]

        def fn(ts):
            if ts.minute != 0 or ts.hour % 8 != 0:
                return None
            c = float(f15.at[ts, "close"])  # 현재 봉 종가까지만 사용 (인과적)
            return make_plan(
                entries=((c * 0.99, 0.5), (c * 0.98, 0.5)),
                stop=c * 0.90,
                tps=((c * 1.15, 0.5), (c * 1.17, 0.5)),
                margin=500.0,
            )

        return fn

    @staticmethod
    def _default_funding(index, rate=0.0001):
        settle = pd.date_range(index[0], index[-1], freq="8h")
        return pd.Series(rate, index=settle, name="rate")

    def _run(self, frames, funding):
        settings = __import__("app.config", fromlist=["Settings"]).Settings(
            _env_file=None
        )
        return run_backtest(
            frames, self._plan_fn_for(frames), PerpCostModel(), settings,
            funding=funding,
        )

    def test_poison_per_timeframe_and_funding(self):
        base_frames = make_multi_tf_frames(days=20)
        funding = self._default_funding(base_frames["15m"].index)
        base = self._run(base_frames, funding)
        assert len(base.fills) > 0, "시나리오가 실제로 체결을 만들어야 유의미"

        idx15 = base_frames["15m"].index
        k = len(idx15) // 2
        cutoff = idx15[k]

        # 15m(실행 TF) 오염.
        poisoned = make_multi_tf_frames(days=20)
        mask = poisoned["15m"].index > cutoff
        poisoned["15m"].loc[mask, ["open", "high", "low", "close"]] *= 5.0
        res = self._run(poisoned, funding)
        np.testing.assert_array_equal(
            base.returns.iloc[: k + 1].to_numpy(),
            res.returns.iloc[: k + 1].to_numpy(),
            err_msg="look-ahead: 15m 미래 오염이 과거 수익률을 바꿈",
        )

        # 4h 오염 — cutoff를 포함한 미완결 4h봉부터 오염 (미완결 봉 판정 금지 포함).
        poisoned = make_multi_tf_frames(days=20)
        mask4 = poisoned["4h"].index >= cutoff.floor("4h")
        poisoned["4h"].loc[mask4, ["open", "high", "low", "close"]] *= 5.0
        res = self._run(poisoned, funding)
        np.testing.assert_array_equal(
            base.returns.iloc[: k + 1].to_numpy(),
            res.returns.iloc[: k + 1].to_numpy(),
            err_msg="look-ahead: 4h 미래 오염이 과거 수익률을 바꿈",
        )

        # 펀딩 오염.
        poisoned_funding = funding.copy()
        poisoned_funding.loc[poisoned_funding.index > cutoff] = 0.05
        res = self._run(make_multi_tf_frames(days=20), poisoned_funding)
        np.testing.assert_array_equal(
            base.returns.iloc[: k + 1].to_numpy(),
            res.returns.iloc[: k + 1].to_numpy(),
            err_msg="look-ahead: 펀딩 미래 오염이 과거 수익률을 바꿈",
        )


class TestResultShape:
    def test_result_alignment_and_columns(self, zero_cost, settings):
        df = flat_ohlcv()
        set_bar(df, 2, l=97.9)
        plan = make_plan()
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), zero_cost, settings)
        assert res.equity.index.equals(df.index)
        assert res.returns.index.equals(df.index)
        assert list(res.trades.columns) == TRADE_COLUMNS
        assert list(res.fills.columns) == FILL_COLUMNS
        assert res.timeframe == "15m"
        assert res.seed == settings.initial_seed_usdt
        # 비복리: returns 합 == (최종 에쿼티 − 시드)/시드.
        assert res.returns.sum() == pytest.approx(
            (res.equity.iloc[-1] - res.seed) / res.seed
        )

    def test_no_plan_no_trades(self, zero_cost, settings):
        df = flat_ohlcv()
        res = run_backtest(frames_of(df), lambda ts: None, zero_cost, settings)
        assert len(res.trades) == 0
        assert (res.equity == settings.initial_seed_usdt).all()
        assert (res.returns == 0.0).all()


class TestAdversarialReviewFixes:
    """적대적 리뷰(2026-07-14) 반영 회귀 테스트."""

    def test_funding_ts_jitter_still_settles(self, zero_cost, settings):
        # 실데이터 fundingTime의 ms 지터 — 봉 open+16ms 정산도 그 봉에 귀속.
        df = flat_ohlcv()
        set_bar(df, 2, l=97.9)  # 두 진입 레그 체결
        plan = make_plan()
        jitter_ts = df.index[32] + pd.Timedelta(milliseconds=16)
        funding = pd.Series([0.001], index=pd.DatetimeIndex([jitter_ts]), name="rate")
        res = run_backtest(
            frames_of(df), plan_at(df.index[1], plan), zero_cost, settings,
            funding=funding,
        )
        assert res.funding_paid == pytest.approx(0.001 * (Q1 + Q2) * 100.0)

    def test_liquidation_checked_on_entry_bar(self, zero_cost, settings):
        # 진입 봉에서 청산가까지 스윕 → 같은 봉 청산 (adverse-first 6b).
        df = flat_ohlcv()
        set_bar(df, 2, l=88.0, c=95.0)  # 진입(99·98) 후 liq(≈89.0)까지 스윕
        for i in range(3, 64):
            set_bar(df, i, o=95.0, h=95.0, l=95.0, c=95.0)
        plan = make_plan(leverage=10, stop=85.0)  # 손절 판정 배제 (게이트는 스텁)
        res = run_backtest(
            frames_of(df), plan_at(df.index[1], plan), zero_cost, settings,
            risk=_ApproveAll,
        )
        assert res.liquidation_count == 1
        liq = res.fills[res.fills["kind"] == "liquidation"].iloc[0]
        assert liq["ts"] == df.index[2]

    def test_blackout_blocks_entry_fills(self, zero_cost, settings):
        # 승인 뒤 블랙아웃 윈도에 들어간 진입 레그는 체결되지 않는다 (규칙 §2).
        df = flat_ohlcv()
        set_bar(df, 4, l=97.0)   # 블랙아웃 안 — 체결 금지
        set_bar(df, 60, l=97.0)  # 블랙아웃 밖(+15h) — 체결
        plan = make_plan()
        res = run_backtest(
            frames_of(df), plan_at(df.index[1], plan), zero_cost, settings,
            econ_events=[ts_ms(df.index[4])], risk=_ApproveAll,
        )
        efills = res.fills[res.fills["kind"] == "entry"]
        assert not (efills["ts"] == df.index[4]).any()
        assert (efills["ts"] == df.index[60]).any()

    def test_same_bar_close_beyond_tp_is_taker(self, settings):
        # 예외적 종가 체결은 크로싱 — taker 요율로 과금.
        cost = PerpCostModel(maker_fee=0.00025, taker_fee=0.0005, slippage=0.0)
        df = flat_ohlcv()
        set_bar(df, 2, l=98.5, h=107.0, c=106.0)
        plan = make_plan()
        res = run_backtest(frames_of(df), plan_at(df.index[1], plan), cost, settings)
        tp = res.fills[res.fills["kind"] == "tp"].iloc[0]
        assert tp["fee_type"] == "taker"
        assert tp["fee"] == pytest.approx(106.0 * tp["qty"] * 0.0005)
