"""백테스트 성과 지표 (스펙 §4).

- per-bar 수익률은 마진 조정 에쿼티 기준 (엔진이 Δequity/seed로 제공).
- Sharpe = mean/std × √bars_per_year[tf] (map: 1d 365 / 4h 2190 / 15m 35040 / 5m 105120).
- 총수익 = **비복리 합산 PnL / seed** (출금 규칙과 일치 — 챔피언 선정 목적함수가
  라이브가 금지한 복리를 가정하지 않는다).
- 연환산 수익은 실제 타임스탬프 스팬 기준 선형 스케일 (비복리).
- holding_hours, funding_paid, fee_paid, liquidation_count 포함.

가드: 지표가 정의되지 않으면(트레이드 0건, std 0, 빈 시리즈) NaN/inf 대신 None.
"""
from __future__ import annotations

import math

import numpy as np

from .engine import _TF_MS, BacktestResult

PROFIT_FACTOR_CAP = 99.0
LOW_CONFIDENCE_TRADES = 10

#: config.Settings.bars_per_year 기본값과 동일한 폴백 맵.
DEFAULT_BARS_PER_YEAR = {"1d": 365, "4h": 2190, "15m": 35040, "5m": 105120}

_MS_PER_YEAR = 365.0 * 24.0 * 3_600_000.0


def compute_metrics(
    result: BacktestResult, bars_per_year: dict[str, int] | None = None
) -> dict:
    bpy_map = bars_per_year or DEFAULT_BARS_PER_YEAR
    trades = result.trades
    returns = result.returns
    equity = result.equity
    seed = float(result.seed)

    trade_count = int(len(trades))
    low_confidence = trade_count < LOW_CONFIDENCE_TRADES

    win_rate = None
    profit_factor = None
    avg_holding_hours = None
    if trade_count > 0:
        rets = trades["net_ret"].to_numpy(dtype=float)
        win_rate = float((rets > 0).mean())
        gross_profit = float(rets[rets > 0].sum())
        gross_loss = float(-rets[rets < 0].sum())
        if gross_loss > 0:
            profit_factor = float(min(gross_profit / gross_loss, PROFIT_FACTOR_CAP))
        elif gross_profit > 0:
            profit_factor = PROFIT_FACTOR_CAP
        # gross_profit == gross_loss == 0 → undefined → None
        avg_holding_hours = float(trades["holding_hours"].mean())

    n = len(returns)

    sharpe = None
    if n >= 2:
        std = float(returns.std())  # ddof=1
        if std > 0 and math.isfinite(std):
            # 미지의 TF를 15m 계수로 잘못 연환산하지 않는다 — 맵에 없으면
            # 봉 길이에서 직접 계산하고, 그것도 불가하면 None 유지.
            periods = bpy_map.get(result.timeframe)
            if periods is None and result.timeframe in _TF_MS:
                periods = _MS_PER_YEAR / _TF_MS[result.timeframe]
            if periods is not None:
                sharpe = float(returns.mean() / std * np.sqrt(periods))

    mdd = None
    total_return = None
    annual_return = None
    if n > 0 and seed > 0:
        # 시드 시작점 포함 — 초기 하락도 드로다운으로 집계.
        eq = np.concatenate(([seed], equity.to_numpy(dtype=float)))
        peak = np.maximum.accumulate(eq)
        with np.errstate(divide="ignore", invalid="ignore"):
            dd = np.where(peak > 0, 1.0 - eq / peak, 0.0)
        mdd = float(np.max(dd))
        # 비복리 총수익 = 합산 PnL / seed (에쿼티가 가산적이므로 동일).
        total_return = float((equity.iloc[-1] - seed) / seed)
        # 실제 타임스탬프 스팬 기준 연환산 (마지막 봉 마감까지 포함).
        tf_ms = _TF_MS.get(result.timeframe, 0)
        span_ms = (
            equity.index[-1].value - equity.index[0].value
        ) / 1_000_000.0 + tf_ms
        if span_ms > 0:
            annual_return = float(total_return * (_MS_PER_YEAR / span_ms))

    return {
        "trade_count": trade_count,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "sharpe": sharpe,
        "mdd": mdd,
        "total_return": total_return,
        "cagr": annual_return,  # 비복리 스팬 기준 연환산 (키 이름은 랭킹 호환)
        "avg_holding_hours": avg_holding_hours,
        "funding_paid": float(result.funding_paid),
        "fee_paid": float(result.fee_paid),
        "liquidation_count": int(result.liquidation_count),
        "low_confidence": low_confidence,
    }
