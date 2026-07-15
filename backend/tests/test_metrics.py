"""지표 공식 테스트 — 손계산 값 대조 (스펙 §4).

- 총수익 = 비복리 합산 PnL / seed (복리 아님을 명시 검증)
- Sharpe = mean/std × √bars_per_year[tf]
- 연환산(cagr 키) = 실제 타임스탬프 스팬 기준 선형 스케일
- MDD는 시드 시작점 포함 USDT 에쿼티 기준
- holding_hours / funding_paid / fee_paid / liquidation_count 포함
- 미정의 지표는 NaN/inf 대신 None
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.backtest.engine import BacktestResult, run_backtest
from app.backtest.metrics import (
    DEFAULT_BARS_PER_YEAR,
    LOW_CONFIDENCE_TRADES,
    PROFIT_FACTOR_CAP,
    compute_metrics,
)
from app.backtest.trades import TradeRecord, build_trades_frame, empty_trades_frame

SEED = 10_000.0

METRIC_KEYS = {
    "trade_count",
    "win_rate",
    "profit_factor",
    "sharpe",
    "mdd",
    "total_return",
    "cagr",
    "avg_holding_hours",
    "funding_paid",
    "fee_paid",
    "liquidation_count",
    "low_confidence",
}


def make_trade(
    net_ret: float = 0.1,
    pnl: float = 100.0,
    holding_hours: float = 5.0,
    plan_id: int = 1,
    side: str = "long",
    exit_reason: str = "tp",
    open: bool = False,
) -> TradeRecord:
    return TradeRecord(
        plan_id=plan_id,
        entry_ts=pd.Timestamp("2024-01-01 00:15"),
        exit_ts=pd.Timestamp("2024-01-01 00:15") + pd.Timedelta(hours=holding_hours),
        entry_price=100.0,
        exit_price=110.0,
        net_ret=net_ret,
        pnl=pnl,
        qty=1.0,
        margin_usdt=1000.0,
        holding_hours=holding_hours,
        side=side,
        leverage=5,
        timeframe="15m",
        funding_paid=0.0,
        fee_paid=0.0,
        exit_reason=exit_reason,
        open=open,
    )


def make_result(
    bar_pnls: list[float] | None = None,
    trades: list[TradeRecord] | None = None,
    seed: float = SEED,
    timeframe: str = "15m",
    start: str = "2024-01-01",
    funding_paid: float = 0.0,
    fee_paid: float = 0.0,
    liquidation_count: int = 0,
) -> BacktestResult:
    """봉별 PnL(USDT) 목록 → 비복리 에쿼티/수익률을 가진 결과 객체."""
    bar_pnls = bar_pnls if bar_pnls is not None else []
    freq = {"15m": "15min", "4h": "4h", "1d": "1D", "5m": "5min"}[timeframe]
    idx = pd.date_range(start, periods=len(bar_pnls), freq=freq)
    equity = pd.Series(
        seed + np.cumsum(np.asarray(bar_pnls, dtype=float)),
        index=idx,
        name="equity",
        dtype=float,
    )
    returns = pd.Series(
        np.asarray(bar_pnls, dtype=float) / seed, index=idx, name="returns"
    )
    return BacktestResult(
        equity=equity,
        returns=returns,
        trades=build_trades_frame(trades or []),
        fills=pd.DataFrame(),
        order_events=[],
        rejections=[],
        funding_paid=funding_paid,
        fee_paid=fee_paid,
        liquidation_count=liquidation_count,
        seed=seed,
        timeframe=timeframe,
    )


class TestTotalReturnNonCompounded:
    def test_total_return_is_sum_pnl_over_seed(self):
        # +10% 마진 수익이 두 번 → 비복리 0.2 (복리라면 0.21).
        res = make_result(bar_pnls=[1000.0, 1000.0])
        m = compute_metrics(res)
        assert m["total_return"] == pytest.approx(0.2)
        assert m["total_return"] != pytest.approx(1.1 * 1.1 - 1.0)

    def test_losses_sum_linearly(self):
        res = make_result(bar_pnls=[-1000.0, -1000.0])
        m = compute_metrics(res)
        assert m["total_return"] == pytest.approx(-0.2)


class TestSharpe:
    def test_sharpe_uses_bars_per_year_map(self):
        pnls = [100.0, 200.0, -100.0, 50.0]
        res = make_result(bar_pnls=pnls, timeframe="15m")
        m = compute_metrics(res)
        rets = np.array(pnls) / SEED
        expected = rets.mean() / rets.std(ddof=1) * np.sqrt(35040)  # 15m 연환산
        assert m["sharpe"] == pytest.approx(expected)

    def test_sharpe_per_timeframe(self):
        pnls = [100.0, 200.0, -100.0, 50.0]
        rets = np.array(pnls) / SEED
        base = rets.mean() / rets.std(ddof=1)
        for tf, periods in DEFAULT_BARS_PER_YEAR.items():
            m = compute_metrics(make_result(bar_pnls=pnls, timeframe=tf))
            assert m["sharpe"] == pytest.approx(base * np.sqrt(periods)), tf

    def test_sharpe_custom_map(self):
        pnls = [100.0, 200.0, -100.0, 50.0]
        res = make_result(bar_pnls=pnls)
        m = compute_metrics(res, bars_per_year={"15m": 100})
        rets = np.array(pnls) / SEED
        assert m["sharpe"] == pytest.approx(rets.mean() / rets.std(ddof=1) * 10.0)

    def test_sharpe_zero_std_is_none(self):
        m = compute_metrics(make_result(bar_pnls=[100.0, 100.0, 100.0]))
        assert m["sharpe"] is None


class TestAnnualizedReturn:
    def test_span_based_linear_annualization(self):
        # 1d 봉 73개 = 73일 스팬(마지막 봉 마감 포함), 총수익 5% → 연환산 25%.
        pnls = [0.0] * 72 + [500.0]
        m = compute_metrics(make_result(bar_pnls=pnls, timeframe="1d"))
        assert m["total_return"] == pytest.approx(0.05)
        assert m["cagr"] == pytest.approx(0.05 * 365 / 73)

    def test_not_compounded_annualization(self):
        # 182일(≈반년) 만에 +50% → 선형 연환산 ≈100% (복리라면 ≈125%).
        pnls = [0.0] * 181 + [5000.0]  # 1d 봉 182개 = 182일 스팬
        m = compute_metrics(make_result(bar_pnls=pnls, timeframe="1d"))
        assert m["cagr"] == pytest.approx(0.5 * 365 / 182)
        assert m["cagr"] < 1.25  # (1.5)^2 − 1 이 아님


class TestDrawdown:
    def test_mdd_hand_computed(self):
        # 에쿼티: 10000 → 11000 → 8900 → 9400, 피크 11000 → mdd = 1 − 8900/11000.
        m = compute_metrics(make_result(bar_pnls=[1000.0, -2100.0, 500.0]))
        assert m["mdd"] == pytest.approx(1 - 8900.0 / 11000.0)

    def test_mdd_counts_initial_dip(self):
        m = compute_metrics(make_result(bar_pnls=[-1000.0, 500.0]))
        assert m["mdd"] == pytest.approx(0.10)


class TestTradeMetrics:
    def test_win_rate_and_profit_factor(self):
        trades = [
            make_trade(net_ret=0.10, holding_hours=3.0),
            make_trade(net_ret=-0.05, holding_hours=5.0),
            make_trade(net_ret=0.20, holding_hours=7.0),
            make_trade(net_ret=0.0, holding_hours=9.0),
        ]
        m = compute_metrics(make_result(bar_pnls=[100.0, -50.0, 200.0], trades=trades))
        assert m["trade_count"] == 4
        assert m["win_rate"] == pytest.approx(0.5)  # 0.0은 승리가 아님
        assert m["profit_factor"] == pytest.approx(0.30 / 0.05)
        assert m["avg_holding_hours"] == pytest.approx(6.0)
        assert m["low_confidence"] is True  # 4 < 10

    def test_profit_factor_capped(self):
        m = compute_metrics(
            make_result(bar_pnls=[100.0], trades=[make_trade(net_ret=0.1)])
        )
        assert m["profit_factor"] == PROFIT_FACTOR_CAP  # 손실 0 → 캡
        m2 = compute_metrics(
            make_result(
                bar_pnls=[100.0],
                trades=[make_trade(net_ret=10.0), make_trade(net_ret=-0.0001)],
            )
        )
        assert m2["profit_factor"] == PROFIT_FACTOR_CAP

    def test_all_zero_returns_profit_factor_none(self):
        m = compute_metrics(
            make_result(
                bar_pnls=[0.0],
                trades=[make_trade(net_ret=0.0), make_trade(net_ret=0.0)],
            )
        )
        assert m["profit_factor"] is None
        assert m["win_rate"] == 0.0

    def test_low_confidence_threshold(self):
        trades = [make_trade(net_ret=0.01) for _ in range(LOW_CONFIDENCE_TRADES)]
        m = compute_metrics(make_result(bar_pnls=[10.0] * 20, trades=trades))
        assert m["low_confidence"] is False  # 정확히 10건이면 신뢰

    def test_no_trades_all_none(self):
        m = compute_metrics(make_result(bar_pnls=[0.0, 0.0]))
        assert m["trade_count"] == 0
        assert m["win_rate"] is None
        assert m["profit_factor"] is None
        assert m["avg_holding_hours"] is None
        assert m["low_confidence"] is True


class TestPerpFields:
    def test_funding_fee_liquidation_passthrough(self):
        m = compute_metrics(
            make_result(
                bar_pnls=[100.0],
                funding_paid=12.5,
                fee_paid=7.25,
                liquidation_count=2,
            )
        )
        assert m["funding_paid"] == pytest.approx(12.5)
        assert m["fee_paid"] == pytest.approx(7.25)
        assert m["liquidation_count"] == 2

    def test_key_set_exact(self):
        m = compute_metrics(make_result(bar_pnls=[100.0]))
        assert set(m.keys()) == METRIC_KEYS

    def test_empty_result_guards(self):
        m = compute_metrics(make_result())
        assert m["sharpe"] is None
        assert m["mdd"] is None
        assert m["total_return"] is None
        assert m["cagr"] is None
        assert m["trade_count"] == 0
        assert m["low_confidence"] is True


class TestIntegrationWithEngine:
    def test_metrics_on_engine_run(self, zero_cost, settings):
        from test_engine import flat_ohlcv, frames_of, make_plan, plan_at, set_bar

        df = flat_ohlcv()
        set_bar(df, 2, l=98.5)  # leg1 체결 @99
        set_bar(df, 4, h=105.5, c=104.0)  # TP1
        set_bar(df, 5, h=110.5, c=109.0)  # TP2 (최종)
        plan = make_plan(entries=((99.0, 0.5), (96.0, 0.5)))
        res = run_backtest(
            frames_of(df), plan_at(df.index[1], plan), zero_cost, settings
        )
        m = compute_metrics(res)
        assert m["trade_count"] == len(res.trades) == 1
        assert m["win_rate"] == 1.0
        assert m["liquidation_count"] == 0
        assert m["mdd"] >= 0.0
        pnl = float(res.trades.iloc[0]["pnl"])
        assert m["total_return"] == pytest.approx(pnl / settings.initial_seed_usdt)
        # 스팬 기반 연환산: 64봉 × 15m = 16h.
        assert m["cagr"] == pytest.approx(
            m["total_return"] * (365 * 24) / 16.0
        )
        assert m["avg_holding_hours"] == pytest.approx(0.75)
        assert set(m.keys()) == METRIC_KEYS

    def test_empty_trades_frame_columns(self):
        from app.backtest.trades import TRADE_COLUMNS

        assert list(empty_trades_frame().columns) == TRADE_COLUMNS
        assert len(empty_trades_frame()) == 0
