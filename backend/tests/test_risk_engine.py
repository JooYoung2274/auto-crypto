"""RiskEngine + TradePlan 게이트 테스트 (스펙 §2, 규칙 §1·§2·§3).

순수 함수 검증 — DB/네트워크 없음. 거부 사유의 한국어 vocabulary
('손익비', '레버리지', '분할', '블랙아웃', '복리', '손절선', '청산')는
리포트/로그 계약이므로 문자열로 함께 검증한다.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.risk.engine import Approval, MarketState, Rejection, RiskEngine
from app.risk.plan import (
    PlanLeg,
    TradePlan,
    liquidation_price,
    maintenance_margin_rate,
)


def make_settings(**overrides) -> Settings:
    return Settings(_env_file=None, db_path=":memory:", **overrides)


def make_state(**overrides) -> MarketState:
    kwargs = dict(as_of_ts=1_752_000_000_000, mark_price=100.5)
    kwargs.update(overrides)
    return MarketState(**kwargs)


def long_plan(**overrides) -> TradePlan:
    """유효한 BTC long 플랜: wEntry=98.5, stop=92 → rr=(115-98.5)/6.5≈2.54."""
    kwargs = dict(
        symbol="BTCUSDT",
        side="long",
        evidence=["일봉 200선 지지", "4h 골든크로스"],
        entries=[
            PlanLeg("entry", 100.0, 0.5),
            PlanLeg("entry", 98.0, 0.25),
            PlanLeg("entry", 96.0, 0.25),
        ],
        stop=PlanLeg("stop", 92.0, 1.0),
        tps=[PlanLeg("tp", 110.0, 0.5), PlanLeg("tp", 120.0, 0.5)],
        leverage=5,
        margin_usdt=1_000.0,
    )
    kwargs.update(overrides)
    return TradePlan(**kwargs)


def short_plan(**overrides) -> TradePlan:
    """유효한 SOL short 플랜: wEntry=106, stop=112 → rr=(106-85)/6=3.5."""
    kwargs = dict(
        symbol="SOLUSDT",
        side="short",
        evidence=["저항대 윗꼬리 음봉", "VWMA 하향 이탈"],
        entries=[PlanLeg("entry", 105.0, 0.5), PlanLeg("entry", 107.0, 0.5)],
        stop=PlanLeg("stop", 112.0, 1.0),
        tps=[PlanLeg("tp", 90.0, 0.5), PlanLeg("tp", 80.0, 0.5)],
        leverage=3,
        margin_usdt=500.0,
    )
    kwargs.update(overrides)
    return TradePlan(**kwargs)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


# -- plan geometry / RR ------------------------------------------------------------


def test_long_plan_rr_normalized():
    plan = long_plan()
    assert plan.weighted_entry == pytest.approx(98.5)
    assert plan.weighted_tp == pytest.approx(115.0)
    assert plan.rr == pytest.approx((115.0 - 98.5) / (98.5 - 92.0))


def test_short_plan_rr_normalized():
    plan = short_plan()
    assert plan.weighted_entry == pytest.approx(106.0)
    assert plan.rr == pytest.approx((106.0 - 85.0) / (112.0 - 106.0))


def test_broken_geometry_rr_is_zero():
    plan = long_plan(stop=PlanLeg("stop", 99.0, 1.0))  # stop above wEntry
    assert plan.rr == 0.0


def test_geometry_ok_long_and_short():
    assert long_plan().geometry_ok()
    assert short_plan().geometry_ok()
    # long: stop이 진입가 위 → 위반
    assert not long_plan(stop=PlanLeg("stop", 97.0, 1.0)).geometry_ok()
    # short: tp가 진입가 위 → 위반
    assert not short_plan(
        tps=[PlanLeg("tp", 108.0, 0.5), PlanLeg("tp", 80.0, 0.5)]
    ).geometry_ok()


def test_plan_json_roundtrip():
    plan = long_plan()
    restored = TradePlan.from_json(plan.to_json())
    assert restored == plan
    assert restored.rr == pytest.approx(plan.rr)


# -- liquidation math (shared helper) -------------------------------------------------


def test_liquidation_exact_formula_long_short():
    # tier-1 (notional <= 50k) → MMR 0.004
    assert maintenance_margin_rate(5_000.0) == 0.004
    assert liquidation_price(100.0, "long", 10, 5_000.0) == pytest.approx(
        100.0 * (1 - 1 / 10) / (1 - 0.004)
    )
    assert liquidation_price(100.0, "short", 10, 5_000.0) == pytest.approx(
        100.0 * (1 + 1 / 10) / (1 + 0.004)
    )


def test_mmr_tier_table_monotonic():
    assert maintenance_margin_rate(60_000.0) == 0.005
    assert maintenance_margin_rate(1e9) == 0.05


# -- static gates ------------------------------------------------------------------


def test_valid_long_and_short_plans_approved(settings):
    assert RiskEngine.review(long_plan(), settings, make_state()).approved
    result = RiskEngine.review(short_plan(), settings, make_state(mark_price=104.0))
    assert result.approved, getattr(result, "reason", "")


def test_whitelist_gate(settings):
    result = RiskEngine.review(
        long_plan(symbol="SHIBUSDT"), settings, make_state()
    )
    assert isinstance(result, Rejection)
    assert "화이트리스트" in result.reason


def test_leverage_cap_btc_10x(settings):
    assert RiskEngine.review(
        long_plan(leverage=10), settings, make_state()
    ).approved
    result = RiskEngine.review(long_plan(leverage=11), settings, make_state())
    assert not result.approved
    assert "레버리지" in result.reason


def test_leverage_cap_eth_and_alt_5x(settings):
    eth = long_plan(symbol="ETHUSDT", leverage=6)
    result = RiskEngine.review(eth, settings, make_state())
    assert not result.approved
    assert "레버리지" in result.reason and "5배" in result.reason
    sol = short_plan(leverage=6)
    assert not RiskEngine.review(sol, settings, make_state(mark_price=104.0)).approved


def test_leverage_minimum_3x(settings):
    result = RiskEngine.review(long_plan(leverage=2), settings, make_state())
    assert not result.approved
    assert "최소" in result.reason


def test_evidence_gate(settings):
    result = RiskEngine.review(
        long_plan(evidence=["일봉 200선 지지"]), settings, make_state()
    )
    assert not result.approved
    assert "근거" in result.reason


def test_split_entry_structure_gate(settings):
    # 진입 1레그 = 몰빵 → 거부
    single = long_plan(entries=[PlanLeg("entry", 98.5, 1.0)])
    result = RiskEngine.review(single, settings, make_state())
    assert not result.approved
    assert "분할 진입" in result.reason

    # 비중 합 ≠ 1.0 → 거부
    bad_sum = long_plan(
        entries=[PlanLeg("entry", 100.0, 0.5), PlanLeg("entry", 98.0, 0.3)]
    )
    result = RiskEngine.review(bad_sum, settings, make_state())
    assert not result.approved
    assert "비중 합" in result.reason


def test_split_tp_structure_gate(settings):
    single_tp = long_plan(tps=[PlanLeg("tp", 115.0, 1.0)])
    result = RiskEngine.review(single_tp, settings, make_state())
    assert not result.approved
    assert "분할 익절" in result.reason

    bad_sum = long_plan(tps=[PlanLeg("tp", 110.0, 0.5), PlanLeg("tp", 120.0, 0.4)])
    result = RiskEngine.review(bad_sum, settings, make_state())
    assert not result.approved
    assert "비중 합" in result.reason


def test_geometry_gate(settings):
    bad = long_plan(stop=PlanLeg("stop", 97.0, 1.0))  # stop > 최저 진입가 96
    result = RiskEngine.review(bad, settings, make_state())
    assert not result.approved
    assert "기하" in result.reason


def test_rr_gate_long_major(settings):
    # stop 90 → risk 8.5, rr = 16.5/8.5 ≈ 1.94 < 2 (BTC) → 거부
    weak = long_plan(stop=PlanLeg("stop", 90.0, 1.0))
    result = RiskEngine.review(weak, settings, make_state())
    assert not result.approved
    assert "손익비" in result.reason


def test_rr_gate_short_alt(settings):
    # tps [95, 85] → wTP 90 → rr = 16/6 ≈ 2.67 < 3 (알트) → 거부
    weak = short_plan(tps=[PlanLeg("tp", 95.0, 0.5), PlanLeg("tp", 85.0, 0.5)])
    result = RiskEngine.review(weak, settings, make_state(mark_price=104.0))
    assert not result.approved
    assert "손익비" in result.reason


def test_rr_gate_major_2x_vs_alt_3x(settings):
    # 같은 기하 (rr≈2.54): BTC는 통과, 알트(XRP)는 rr_min 3 미달로 거부.
    assert RiskEngine.review(long_plan(), settings, make_state()).approved
    alt = long_plan(symbol="XRPUSDT", leverage=5)
    result = RiskEngine.review(alt, settings, make_state())
    assert not result.approved
    assert "손익비" in result.reason


def test_passive_side_gate_long(settings):
    # mark 아래 진입만 허용 — mark=97이면 100/98 진입 레그가 크로스.
    result = RiskEngine.review(long_plan(), settings, make_state(mark_price=97.0))
    assert not result.approved
    assert "패시브" in result.reason


def test_passive_side_gate_short(settings):
    # short 진입가는 mark 위여야 — mark=106이면 105 레그가 크로스.
    result = RiskEngine.review(
        short_plan(), settings, make_state(mark_price=106.0)
    )
    assert not result.approved
    assert "패시브" in result.reason


def test_liq_buffer_gate(settings):
    # 10x long: liq ≈ 98.5×0.9/0.996 ≈ 89.01 — stop 89.2는 버퍼(거리의 10%) 미달.
    tight = long_plan(
        leverage=10,
        stop=PlanLeg("stop", 89.2, 1.0),
        tps=[PlanLeg("tp", 130.0, 0.5), PlanLeg("tp", 140.0, 0.5)],
    )
    result = RiskEngine.review(tight, settings, make_state())
    assert not result.approved
    assert "청산 버퍼" in result.reason


# -- runtime gates -------------------------------------------------------------------


def test_max_concurrent_positions_gate(settings):
    result = RiskEngine.review(
        long_plan(), settings, make_state(open_positions=3)
    )
    assert not result.approved
    assert "동시 포지션" in result.reason


def test_daily_loss_circuit_breaker(settings):
    # seed 10,000 × 5% = 500 USDT 한도.
    result = RiskEngine.review(
        long_plan(), settings, make_state(daily_realized_pnl=-500.0)
    )
    assert not result.approved
    assert "서킷브레이커" in result.reason
    assert RiskEngine.review(
        long_plan(), settings, make_state(daily_realized_pnl=-499.0)
    ).approved


def test_blackout_gate(settings):
    ts = 1_752_000_000_000
    windows = ((ts - 3_600_000, ts + 3_600_000),)
    result = RiskEngine.review(
        long_plan(), settings, make_state(as_of_ts=ts, blackout_windows=windows)
    )
    assert not result.approved
    assert "블랙아웃" in result.reason
    # 창 밖이면 통과
    assert RiskEngine.review(
        long_plan(),
        settings,
        make_state(as_of_ts=ts + 7_200_000, blackout_windows=windows),
    ).approved


def test_sizing_gate_margin_budget(settings):
    # 오픈 플랜 마진 9,500 + 신규 1,000 > 시드 10,000 → 거부.
    result = RiskEngine.review(
        long_plan(), settings, make_state(open_plan_margin=9_500.0)
    )
    assert not result.approved
    assert "복리 금지" in result.reason


def test_no_compounding_profit_does_not_expand_budget(settings):
    # 수익으로 지갑이 12,000이어도 예산은 min(wallet, seed) = 10,000 (복리 금지).
    result = RiskEngine.review(
        long_plan(margin_usdt=1_000.0),
        settings,
        make_state(open_plan_margin=9_500.0, wallet_balance=12_000.0),
    )
    assert not result.approved
    assert "복리 금지" in result.reason
    # 손실로 지갑이 시드 미만이면 지갑이 상한.
    result = RiskEngine.review(
        long_plan(margin_usdt=1_000.0),
        settings,
        make_state(open_plan_margin=7_500.0, wallet_balance=8_000.0),
    )
    assert not result.approved


# -- stop modification (규칙 §3) -------------------------------------------------------


def test_stop_update_favorable_only_long():
    assert RiskEngine.review_stop_update("long", 92.0, 94.0).approved  # 타이트닝
    assert RiskEngine.review_stop_update("long", 92.0, 92.0).approved  # 동일 허용
    result = RiskEngine.review_stop_update("long", 92.0, 88.0)  # 손절 미루기
    assert isinstance(result, Rejection)
    assert "손절선" in result.reason and "거부" in result.reason


def test_stop_update_favorable_only_short():
    assert RiskEngine.review_stop_update("short", 112.0, 110.0).approved
    result = RiskEngine.review_stop_update("short", 112.0, 115.0)
    assert not result.approved
    assert "손절선" in result.reason


# -- result types ---------------------------------------------------------------------


def test_approval_and_rejection_shapes(settings):
    ok = RiskEngine.review(long_plan(), settings, make_state())
    assert isinstance(ok, Approval)
    assert ok.approved is True
    bad = RiskEngine.review(long_plan(leverage=1), settings, make_state())
    assert isinstance(bad, Rejection)
    assert bad.approved is False
    assert bad.reason


def test_liq_buffer_uses_worst_case_partial_fill(settings):
    # 가중 평균 진입 기준으론 버퍼를 통과하지만, 최상단 레그 단독 체결
    # (부분 체결 최악 케이스) 기준으론 미달인 플랜 → 거부 (리뷰 반영).
    plan = long_plan(leverage=10, stop=PlanLeg("stop", 90.5, 1.0))
    liq_weighted = plan.estimated_liq_price()
    assert plan.stop.price >= liq_weighted + 0.10 * (
        plan.weighted_entry - liq_weighted
    )  # 이전(가중) 기준이라면 통과했을 플랜
    result = RiskEngine.review(plan, settings, make_state())
    assert isinstance(result, Rejection)
    assert "청산 버퍼" in result.reason
