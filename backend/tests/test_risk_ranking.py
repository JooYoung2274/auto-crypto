"""Risk 로건의 지표 필터(청산 즉시 탈락·MDD·펀딩 드래그·활동성) +
리더보드 랭킹(새 perp 지표 계약)."""
from __future__ import annotations

import json

import pytest

from app.agents.risk import FUNDING_DRAG_MAX, LOOKBACK_YEARS, Risk, trades_per_year
from app.config import Settings
from app.db import Database
from app.events import EventBus
from app.orchestrator import compute_leaderboard

SEED = 10_000.0


def _metrics(**overrides) -> dict:
    base = dict(
        mdd=0.1,
        trade_count=100,
        trades_per_year=100.0,
        liquidation_count=0,
        funding_paid=0.0,
        total_return=0.10,
        sharpe=1.0,
        win_rate=0.6,
        cagr=0.2,
        profit_factor=2.0,
        fee_paid=10.0,
    )
    base.update(overrides)
    return base


def _results(**by_id: dict) -> dict[int, dict]:
    """Build a Risk.review results dict from ``id -> avg_metrics`` kwargs."""
    return {
        int(sid): {"avg_metrics": metrics, "low_confidence": False}
        for sid, metrics in by_id.items()
    }


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        db_path=str(tmp_path / "risk.db"), initial_seed_usdt=SEED, _env_file=None
    )


# -- metrics filters --------------------------------------------------------------
async def test_liquidation_auto_rejects(db, settings):
    """강제 청산 이력 > 0 → 다른 지표가 아무리 좋아도 즉시 탈락 (스펙 §4)."""
    risk = Risk(EventBus(db))
    results = _results(
        **{
            "1": _metrics(),
            "2": _metrics(sharpe=5.0, win_rate=0.9, liquidation_count=2),
        }
    )
    passed, rejected = await risk.review(results, settings)
    assert passed == [1]
    assert "청산" in rejected[2] and "즉시 탈락" in rejected[2]


async def test_mdd_filter_uses_margin_equity_mdd(db, settings):
    risk = Risk(EventBus(db))
    results = _results(
        **{
            "1": _metrics(mdd=settings.max_mdd - 0.01),
            "2": _metrics(mdd=settings.max_mdd + 0.05),
        }
    )
    passed, rejected = await risk.review(results, settings)
    assert passed == [1]
    assert "MDD" in rejected[2]


async def test_missing_metrics_rejected(db, settings):
    risk = Risk(EventBus(db))
    passed, rejected = await risk.review(
        _results(**{"1": _metrics(mdd=None)}), settings
    )
    assert passed == []
    assert "지표 산출 불가" in rejected[1]


async def test_funding_drag_rejects_when_funding_eats_the_edge(db, settings):
    """펀딩 지불이 총이익(gross)의 절반 이상을 잠식하면 탈락."""
    risk = Risk(EventBus(db))
    # pnl 500 USDT (5% × 시드), 펀딩 600 → gross 1,100의 55% > 한도 50%.
    dragged = _metrics(total_return=0.05, funding_paid=600.0)
    # 같은 수익, 펀딩 300 → 37.5% ≤ 50% → 통과.
    fine = _metrics(total_return=0.05, funding_paid=300.0)
    passed, rejected = await risk.review(
        _results(**{"1": fine, "2": dragged}), settings
    )
    assert passed == [1]
    assert "펀딩 드래그" in rejected[2]
    assert FUNDING_DRAG_MAX == 0.5


async def test_funding_received_never_drags(db, settings):
    """펀딩 수취(음수 funding_paid)는 드래그 사유가 될 수 없다 (숏 수취)."""
    risk = Risk(EventBus(db))
    passed, rejected = await risk.review(
        _results(**{"1": _metrics(funding_paid=-500.0, total_return=0.01)}),
        settings,
    )
    assert passed == [1]
    assert rejected == {}


async def test_activity_filter_rejects_passive_keeps_active(db, settings):
    """A regularly-trading strategy passes; an ultra-passive one is rejected
    with a distinct low-activity reason."""
    risk = Risk(EventBus(db))
    results = _results(
        **{
            "1": _metrics(trades_per_year=settings.min_trades_per_year + 5),
            "2": _metrics(trades_per_year=settings.min_trades_per_year - 5),
        }
    )
    passed, rejected = await risk.review(results, settings)
    assert passed == [1]
    assert 2 in rejected
    assert "활동성" in rejected[2]
    # The MDD check must not be what rejected it — MDD is within the limit.
    assert "MDD" not in rejected[2]


async def test_activity_filter_disabled_when_threshold_zero(db, settings):
    """min_trades_per_year=0 turns the activity filter off entirely."""
    settings.min_trades_per_year = 0.0
    risk = Risk(EventBus(db))
    results = _results(**{"1": _metrics(trade_count=0, trades_per_year=0.0)})
    passed, rejected = await risk.review(results, settings)
    assert passed == [1]
    assert rejected == {}


def test_trades_per_year_falls_back_to_lookback():
    assert trades_per_year({"trade_count": 24}) == 24 / LOOKBACK_YEARS
    assert trades_per_year({"trade_count": 24, "trades_per_year": 3.0}) == 3.0


# -- leaderboard ------------------------------------------------------------------
def _insert_passed_strategy(
    db: Database, template: str, metrics: dict, symbol: str = "BTCUSDT"
) -> int:
    rows = db.execute(
        "INSERT INTO strategies (template, params_json, status) "
        "VALUES (?, ?, 'passed')",
        (template, json.dumps({})),
    )
    sid = int(rows[0]["id"])
    db.execute(
        "INSERT INTO backtests (strategy_id, symbol, metrics_json) VALUES (?, ?, ?)",
        (sid, symbol, json.dumps(metrics)),
    )
    return sid


def test_cagr_affects_leaderboard_order(db):
    """CAGR is a ranked component: with the default weights the all-round
    strong strategy wins, but weighting CAGR exclusively flips the order to
    the high-growth one."""
    # A: strong sharpe/win_rate/mdd but low growth.
    a = _insert_passed_strategy(
        db,
        "box_range",
        _metrics(sharpe=2.0, win_rate=0.9, mdd=0.1, cagr=0.05, years=1.0),
    )
    # B: weak on everything except a much higher CAGR.
    b = _insert_passed_strategy(
        db,
        "vwma_support",
        _metrics(sharpe=0.1, win_rate=0.1, mdd=0.9, cagr=0.5, years=1.0),
    )

    default = compute_leaderboard(db, settings=Settings(_env_file=None))
    assert [r["strategy_id"] for r in default] == [a, b]

    cagr_only = Settings(
        _env_file=None,
        rank_w_sharpe=0.0, rank_w_win_rate=0.0, rank_w_mdd=0.0, rank_w_cagr=1.0,
    )
    board = compute_leaderboard(db, settings=cagr_only)
    assert [r["strategy_id"] for r in board] == [b, a]


def test_leaderboard_excludes_grandfathered_passive_strategies(db):
    """Strategies that passed in earlier cycles (before the activity filter
    existed or under a laxer threshold) must not stay champion-eligible: the
    leaderboard re-applies the activity check and unranks them."""
    passive = _insert_passed_strategy(
        db,
        "box_range",
        # 5년 스팬에 30건 → 연 6회 < 기본 12회 → 저활동.
        _metrics(sharpe=2.0, win_rate=0.9, mdd=0.05, cagr=0.02,
                 trade_count=30, years=5.0),
    )
    active = _insert_passed_strategy(
        db,
        "candle_breakout",
        _metrics(sharpe=0.5, win_rate=0.55, mdd=0.2, cagr=0.08,
                 trade_count=100, years=5.0),  # 20/year → fine
    )

    board = compute_leaderboard(db, settings=Settings(_env_file=None))
    by_id = {r["strategy_id"]: r for r in board}
    assert by_id[passive]["low_activity"] is True
    assert by_id[active]["low_activity"] is False
    # Active strategy ranks first despite far worse headline metrics; the
    # passive one is appended after the ranked rows (champion-ineligible).
    assert board[0]["strategy_id"] == active


def test_leaderboard_single_query_matches_metrics(db):
    """compute_leaderboard's batched metrics fetch must aggregate the same
    values as per-strategy queries (regression for the N+1 rewrite) —
    합산 키(trade_count/liquidation_count)와 평균 키(win_rate/funding_paid)."""
    sid = _insert_passed_strategy(
        db,
        "box_range",
        _metrics(sharpe=1.0, win_rate=0.6, mdd=0.15, cagr=0.07,
                 trade_count=80, liquidation_count=0, funding_paid=40.0, years=1.0),
    )
    db.execute(
        "INSERT INTO backtests (strategy_id, symbol, metrics_json) VALUES (?, ?, ?)",
        (sid, "ETHUSDT", json.dumps(_metrics(
            sharpe=0.5, win_rate=0.5, mdd=0.25, cagr=0.03,
            trade_count=60, liquidation_count=1, funding_paid=20.0, years=1.0,
        ))),
    )
    board = compute_leaderboard(db, settings=Settings(_env_file=None))
    row = next(r for r in board if r["strategy_id"] == sid)
    avg = row["avg_metrics"]
    assert avg["trade_count"] == 140  # summed across symbols
    assert avg["liquidation_count"] == 1  # summed
    assert abs(avg["win_rate"] - 0.55) < 1e-9  # averaged
    assert abs(avg["funding_paid"] - 30.0) < 1e-9  # averaged
    assert avg["trades_per_year"] == pytest.approx(140.0)  # 총 거래 / 최대 스팬
