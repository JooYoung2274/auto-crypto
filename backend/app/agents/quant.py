"""Quant 민 (Min): plan-driven backtests across universe × timeframes.

Each candidate spec is turned into a ``plan_fn`` (generate_plan at every
execution-bar close, regime-gated per bar via the aligned daily regime
series) and driven through the perp backtest engine with funding applied.
CPU-bound work runs in ``asyncio.to_thread`` so the event loop stays free.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import threading
from typing import Callable

import pandas as pd

from ..backtest.costs import PerpCostModel
from ..backtest.engine import run_backtest
from ..backtest.metrics import compute_metrics
from ..config import Settings
from ..db import Database
from ..strategies.base import TF_MINUTES, StrategySpec, generate_plan
from .base import AgentBase

EQUITY_MAX_POINTS = 200

#: 심볼 간 평균으로 집계하는 지표 키.
_AVG_KEYS = (
    "win_rate",
    "sharpe",
    "mdd",
    "cagr",
    "profit_factor",
    "total_return",
    "funding_paid",
    "fee_paid",
)
#: 심볼 간 합산으로 집계하는 지표 키.
_SUM_KEYS = ("trade_count", "liquidation_count")

_MS_PER_YEAR = 365.0 * 24.0 * 3_600_000.0


def downsample_equity(equity: pd.Series, max_points: int = EQUITY_MAX_POINTS) -> list:
    """Downsample a USDT equity curve to ``[[ts_iso, value], ...]``."""
    if equity.empty:
        return []
    step = max(1, len(equity) // max_points)
    sampled = equity.iloc[::step]
    if sampled.index[-1] != equity.index[-1]:
        sampled = pd.concat([sampled, equity.iloc[[-1]]])
    return [
        [idx.isoformat(), round(float(val), 6)] for idx, val in sampled.items()
    ]


def aggregate_metrics(
    metrics_list: list[dict], min_trades: int
) -> tuple[dict, bool]:
    """Aggregate per-(symbol, timeframe) metrics into ``avg_metrics``.

    Averages win_rate/sharpe/mdd/cagr/profit_factor/total_return and the
    funding/fee costs over symbols (ignoring None); sums trade_count and
    liquidation_count. ``trades_per_year`` is derived from the widest data
    span (a ``years`` key added by the quant). ``low_confidence`` is True
    when the total trade count is below ``min_trades``.
    """
    avg: dict = {}
    for key in _AVG_KEYS:
        vals = [m[key] for m in metrics_list if m.get(key) is not None]
        avg[key] = float(sum(vals) / len(vals)) if vals else None
    for key in _SUM_KEYS:
        avg[key] = int(sum(m.get(key) or 0 for m in metrics_list))
    years = max((m.get("years") or 0.0 for m in metrics_list), default=0.0)
    avg["trades_per_year"] = (
        float(avg["trade_count"] / years) if years > 0 else None
    )
    return avg, avg["trade_count"] < min_trades


def make_plan_fn(
    spec: StrategySpec,
    frames: dict[str, pd.DataFrame],
    symbol: str,
    regime_series: pd.Series | None,
    timeframe: str,
    margin_usdt: float,
    after: pd.Timestamp | None = None,
) -> Callable[[pd.Timestamp], object]:
    """Build the engine's ``plan_fn`` for one (spec, symbol) pair.

    - decision time = bar close (``ts + tf``); ``generate_plan`` clips every
      frame to bars fully closed by then (교차 TF 룩어헤드 차단).
    - regime is looked up per bar from the 1-day-shifted aligned series
      (스펙 §1.2); missing → 'cash' (진입 차단).
    - ``after``: plan candidates only from this timestamp on (워크포워드 OOS).
    - the plan's margin is re-sized to the fixed-seed budget (복리 금지).
    """
    tfd = pd.Timedelta(minutes=TF_MINUTES.get(timeframe, 15))

    def plan_fn(ts: pd.Timestamp):
        if after is not None and ts < after:
            return None
        regime = "cash"
        if regime_series is not None:
            value = regime_series.get(ts)
            if isinstance(value, str):
                regime = value
        plan = generate_plan(spec, frames, regime, symbol=symbol, as_of=ts + tfd)
        if plan is None:
            return None
        return dataclasses.replace(plan, margin_usdt=margin_usdt)

    return plan_fn


def evaluate_spec(
    spec: StrategySpec,
    data: dict[str, dict[str, pd.DataFrame]],
    cost: PerpCostModel,
    settings: Settings,
    *,
    regimes: dict[str, pd.Series] | None = None,
    fundings: dict[str, pd.Series] | None = None,
    timeframe: str | None = None,
    after: dict[str, pd.Timestamp] | None = None,
) -> tuple[dict | None, list[dict], list[dict]]:
    """Run ``spec`` over every symbol; returns (aggregate, per_symbol, trades).

    ``trades`` is the flat entry-time-sorted list with actual entry/exit
    prices, USDT P/L, side/leverage/timeframe/funding — the shape consumed by
    the analyst's trade tables and the quant's Korean trade log."""
    tf = timeframe or settings.execution_timeframe
    margin = settings.initial_seed_usdt / max(1, settings.max_concurrent_positions)
    per_symbol: list[dict] = []
    trades: list[dict] = []
    for symbol, frames in data.items():
        if tf not in frames:
            continue
        plan_fn = make_plan_fn(
            spec, frames, symbol,
            (regimes or {}).get(symbol), tf, margin,
            after=(after or {}).get(symbol),
        )
        try:
            result = run_backtest(
                frames, plan_fn, cost, settings,
                timeframe=tf, funding=(fundings or {}).get(symbol),
            )
            m = compute_metrics(result, settings.bars_per_year)
        except Exception:  # noqa: BLE001 — skip a bad (spec, symbol) pair
            continue
        per_symbol.append(
            {
                "symbol": symbol,
                "total_return": m["total_return"],
                "win_rate": m["win_rate"],
                "mdd": m["mdd"],
                "sharpe": m["sharpe"],
                "trade_count": m["trade_count"],
                "funding_paid": m["funding_paid"],
                "liquidation_count": m["liquidation_count"],
            }
        )
        for tr in result.trades.itertuples(index=False):
            trades.append(
                {
                    "symbol": symbol,
                    "side": str(tr.side),
                    "leverage": int(tr.leverage),
                    "timeframe": str(tr.timeframe),
                    "entry_ts": pd.Timestamp(tr.entry_ts).isoformat(),
                    "entry_price": float(tr.entry_price),
                    "exit_ts": pd.Timestamp(tr.exit_ts).isoformat(),
                    "exit_price": float(tr.exit_price),
                    "net_ret": float(tr.net_ret),
                    "pnl": float(tr.pnl),
                    "funding_paid": float(tr.funding_paid),
                    "holding_hours": float(tr.holding_hours),
                    "exit_reason": str(tr.exit_reason),
                    "open": bool(tr.open),
                }
            )
    trades.sort(key=lambda t: t["entry_ts"])

    if not per_symbol:
        return None, [], []

    def _mean(key: str) -> float | None:
        vals = [p[key] for p in per_symbol if p[key] is not None]
        return float(sum(vals) / len(vals)) if vals else None

    aggregate = {
        "total_return": _mean("total_return"),
        "win_rate": _mean("win_rate"),
        "mdd": _mean("mdd"),
        "sharpe": _mean("sharpe"),
        "funding_paid": _mean("funding_paid"),
        "trade_count": int(sum(p["trade_count"] for p in per_symbol)),
        "liquidation_count": int(sum(p["liquidation_count"] for p in per_symbol)),
    }
    return aggregate, per_symbol, trades


class Quant(AgentBase):
    id = "quant"
    name = "민"
    role = "Quant"

    async def log_trades(self, trades: list[dict]) -> None:
        """Emit one Korean activity_log line per trade, e.g.
        "BTCUSDT 롱 x5 진입 2026-07-01T04:00 @61,200.00 → 청산 … +4.2%
        (+140.00 USDT, 펀딩 -0.52, 보유 16.0시간)"."""
        for t in trades:
            side_txt = "롱" if t["side"] == "long" else "숏"
            entry = (
                f"{t['symbol']} {side_txt} x{t['leverage']} 진입 "
                f"{t['entry_ts']} @{t['entry_price']:,.2f}"
            )
            if t.get("open"):
                exit_leg = "보유 중"
            else:
                exit_leg = f"청산 {t['exit_ts']} @{t['exit_price']:,.2f}"
            await self.log(
                f"{entry} → {exit_leg}, {t['net_ret']:+.1%} "
                f"({t['pnl']:+,.2f} USDT, 펀딩 {t['funding_paid']:+,.2f}, "
                f"보유 {t['holding_hours']:.1f}시간)",
                symbol=t["symbol"],
                side=t["side"],
                net_ret=t["net_ret"],
                pnl=t["pnl"],
                open=bool(t.get("open")),
            )

    async def backtest_candidates(
        self,
        candidates: list[tuple[int, StrategySpec]],
        data: dict[str, dict[str, pd.DataFrame]],
        cost: PerpCostModel,
        db: Database,
        settings: Settings,
        *,
        regimes: dict[str, pd.Series] | None = None,
        fundings: dict[str, pd.Series] | None = None,
        min_trades: int | None = None,
        stop_flag: threading.Event | None = None,
        persist: bool = True,
        timeframes: list[str] | None = None,
    ) -> dict[int, dict]:
        """Backtest every candidate over universe × timeframes, returning
        strategy_id → {"avg_metrics", "low_confidence"}. When ``persist`` is
        True the per-(symbol, tf) backtests/trades rows are written;
        validation runs pass ``persist=False`` so throwaway train backtests
        never reach the real leaderboard. Individual pair failures are
        logged and skipped.

        Cancellation cannot interrupt a running worker thread, so on
        CancelledError the ``stop_flag`` is set (the thread checks it per
        pair and exits early) and the thread is awaited via ``asyncio.shield``
        before re-raising — no writes ever happen after ``stop_cycle()``."""
        min_trades = settings.min_trades if min_trades is None else min_trades
        tfs = list(timeframes) if timeframes else [settings.execution_timeframe]
        total = len(candidates)
        results: dict[int, dict] = {}
        for i, (strategy_id, spec) in enumerate(candidates, 1):
            await self.set_state(
                "working", f"{spec.id_key()} 백테스트 중 ({i}/{total})"
            )
            fut = asyncio.ensure_future(
                asyncio.to_thread(
                    self._backtest_one,
                    strategy_id, spec, data, cost, db, settings,
                    regimes, fundings, tfs, stop_flag, persist,
                )
            )
            try:
                metrics_list = await asyncio.shield(fut)
            except asyncio.CancelledError:
                if stop_flag is not None:
                    stop_flag.set()
                with contextlib.suppress(Exception):
                    await fut  # wait for the worker thread to actually exit
                raise
            if not metrics_list:
                await self.log(
                    f"{spec.id_key()} 전 심볼 백테스트 실패 — 스킵",
                    level="warning",
                    strategy_id=strategy_id,
                )
                continue
            avg, low_conf = aggregate_metrics(metrics_list, min_trades)
            results[strategy_id] = {"avg_metrics": avg, "low_confidence": low_conf}
        await self.log(
            f"백테스트 완료 — 후보 {total}개 × 심볼 {len(data)}개 × "
            f"TF {len(tfs)}개",
            candidates=total,
            symbols=len(data),
            timeframes=tfs,
        )
        await self.set_state("idle")
        return results

    def _backtest_one(
        self,
        strategy_id: int,
        spec: StrategySpec,
        data: dict[str, dict[str, pd.DataFrame]],
        cost: PerpCostModel,
        db: Database,
        settings: Settings,
        regimes: dict[str, pd.Series] | None,
        fundings: dict[str, pd.Series] | None,
        timeframes: list[str],
        stop_flag: threading.Event | None = None,
        persist: bool = True,
    ) -> list[dict]:
        """Backtest one spec across every (symbol, timeframe) pair (runs in a
        worker thread). Returns the successfully computed metric dicts (each
        tagged with symbol / timeframe / data-span years). Exits early once
        ``stop_flag`` is set."""
        margin = settings.initial_seed_usdt / max(
            1, settings.max_concurrent_positions
        )
        metrics_list: list[dict] = []
        for symbol, frames in data.items():
            for tf in timeframes:
                if stop_flag is not None and stop_flag.is_set():
                    return metrics_list
                if tf not in frames:
                    continue
                plan_fn = make_plan_fn(
                    spec, frames, symbol,
                    (regimes or {}).get(symbol), tf, margin,
                )
                try:
                    result = run_backtest(
                        frames, plan_fn, cost, settings,
                        timeframe=tf, funding=(fundings or {}).get(symbol),
                    )
                    metrics = compute_metrics(result, settings.bars_per_year)
                except Exception:  # noqa: BLE001 — one bad pair must not kill it
                    continue
                idx = frames[tf].index
                metrics["symbol"] = symbol
                metrics["timeframe"] = tf
                metrics["years"] = (
                    float((idx[-1].value - idx[0].value) / 1e6 / _MS_PER_YEAR)
                    if len(idx) >= 2
                    else 0.0
                )
                if not persist:
                    metrics_list.append(metrics)
                    continue
                rows = db.execute(
                    "INSERT INTO backtests (strategy_id, symbol, metrics_json, "
                    "equity_curve_json) VALUES (?, ?, ?, ?)",
                    (
                        strategy_id,
                        symbol,
                        json.dumps(metrics),
                        json.dumps(downsample_equity(result.equity)),
                    ),
                )
                backtest_id = rows[0]["id"]
                trades = result.trades
                if len(trades):
                    db.executemany(
                        "INSERT INTO trades (backtest_id, entry_ts, exit_ts, "
                        "entry_price, exit_price, net_ret, holding_hours, side, "
                        "leverage, timeframe, funding_paid, fee_paid) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [
                            (
                                backtest_id,
                                pd.Timestamp(t.entry_ts).isoformat(),
                                pd.Timestamp(t.exit_ts).isoformat(),
                                float(t.entry_price),
                                float(t.exit_price),
                                float(t.net_ret),
                                float(t.holding_hours),
                                str(t.side),
                                int(t.leverage),
                                str(t.timeframe),
                                float(t.funding_paid),
                                float(t.fee_paid),
                            )
                            for t in trades.itertuples(index=False)
                        ],
                    )
                metrics_list.append(metrics)
        return metrics_list
