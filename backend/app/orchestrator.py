"""Cycle state machine: drives the 7 agents through the spec flow and
choreographs meetings for the 2D office visualization.

Cycle kinds (스펙 §3): research discovers plan-generating strategies over the
crypto universe, validate runs a walk-forward profitability check, trade
executes one pass of the champion's TradePlans through the broker provider.

Meetings happen at real logic hand-off points; their minimum duration
(~4s in production) is configurable via the ``meeting_seconds`` parameter
so tests run fast.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import random
import threading
import uuid
from typing import Callable

import pandas as pd

from .agents import PM, Analyst, DataEngineer, Quant, Risk, Strategist, Trader
from .agents.base import AgentBase
from .agents.quant import aggregate_metrics, evaluate_spec
from .agents.risk import LOOKBACK_YEARS, trades_per_year
from .backtest.costs import PerpCostModel
from .broker.base import Broker
from .broker.paper import PaperBroker
from .config import Settings, get_settings
from .data.funding import FundingLoader
from .data.loader import DataLoader
from .data.regime import RegimeService
from .db import Database
from .events import Event, EventBus
from .strategies.base import StrategySpec, clip_frames

# Cycle kinds (spec §3): research discovers strategies, validate runs a
# walk-forward profitability check, trade executes the champion's plans.
CYCLE_KINDS = ("research", "validate", "trade")
# Walk-forward split: the last OOS_FRACTION of execution-TF bars is held out.
OOS_FRACTION = 0.25
MIN_VALIDATE_BARS = 160

# 목표 탐색 모드: OOS 거래가 이 횟수 이상이어야 표본이 유효하다고 본다.
GOAL_MIN_TRADES = 10
# 목표 탐색 루프가 수동 사이클/자동 사이클과 러너를 두고 경합할 때 재시도 간격(초).
GOAL_BUSY_POLL_SECONDS = 2.0

_MS = 1_000_000  # pandas ns → ms


class CycleInProgressError(Exception):
    """A cycle is already running."""


class GoalInProgressError(Exception):
    """Goal-seek mode is already running."""


def goal_met(oos: dict | None, settings: Settings) -> bool:
    """목표 탐색 성공 판정 — validate 리포트의 판정보다 엄격하다.

    OOS 승률 ≥ ``goal_win_rate`` AND OOS 총수익 > 0 AND
    OOS 거래 ≥ ``GOAL_MIN_TRADES`` AND OOS MDD ≤ ``max_mdd`` 를 모두 만족해야 한다.
    지표가 하나라도 없으면(챔피언 미발굴 등) 미달로 본다.
    """
    if not oos:
        return False
    win_rate = oos.get("win_rate")
    total_return = oos.get("total_return")
    trade_count = oos.get("trade_count")
    mdd = oos.get("mdd")
    if win_rate is None or total_return is None or trade_count is None or mdd is None:
        return False
    return (
        win_rate >= settings.goal_win_rate
        and total_return > 0
        and trade_count >= GOAL_MIN_TRADES
        and mdd <= settings.max_mdd
    )


def _pct_ranks(values: list[float | None], higher_better: bool = True) -> list[float]:
    """Percentile ranks in [0,1]; None counts as worst."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [1.0]
    worst = min((v for v in values if v is not None), default=0.0)
    keyed = [v if v is not None else (worst - 1.0) for v in values]
    if not higher_better:
        keyed = [-v for v in keyed]
    order = sorted(range(n), key=lambda i: keyed[i])
    ranks = [0.0] * n
    for pos, i in enumerate(order):
        ranks[i] = pos / (n - 1)
    return ranks


def compute_leaderboard(
    db: Database,
    limit: int = 20,
    min_trades: int = 10,
    settings: Settings | None = None,
) -> list[dict]:
    """Strategy ranking: weighted sharpe/win_rate/mdd/cagr percentile-rank sum
    (weights from settings, default 0.35/0.15/0.2/0.3); low-confidence and
    low-activity strategies are excluded from the ranking (appended after the
    ranked ones). The activity check re-applies to already-passed strategies
    from earlier cycles so a lowered threshold cannot grandfather passive
    champions."""
    settings = settings or get_settings()
    strategies = db.execute(
        "SELECT id, template, params_json, status FROM strategies "
        "WHERE status IN ('passed', 'champion')"
    )
    # Single query for every ranked strategy's metrics — a per-strategy query
    # here (N+1) made the leaderboard take seconds once goal-seek accumulated
    # hundreds of passed rows.
    all_bt = db.execute(
        "SELECT b.strategy_id, b.metrics_json FROM backtests b "
        "JOIN strategies s ON s.id = b.strategy_id "
        "WHERE s.status IN ('passed', 'champion')"
    )
    metrics_by_sid: dict[int, list[dict]] = {}
    for b in all_bt:
        metrics_by_sid.setdefault(int(b["strategy_id"]), []).append(
            json.loads(b["metrics_json"])
        )
    rows: list[dict] = []
    for s in strategies:
        metrics_list = metrics_by_sid.get(int(s["id"]))
        if not metrics_list:
            continue
        avg, low_conf = aggregate_metrics(metrics_list, min_trades)
        rows.append(
            {
                "strategy_id": int(s["id"]),
                "template": s["template"],
                "params": json.loads(s["params_json"]),
                "avg_metrics": avg,
                "low_confidence": low_conf,
                "low_activity": trades_per_year(avg) < settings.min_trades_per_year,
                "status": s["status"],
            }
        )

    ranked = [r for r in rows if not r["low_confidence"] and not r["low_activity"]]
    low_conf_rows = [r for r in rows if r["low_confidence"] or r["low_activity"]]
    if ranked:
        sharpe_r = _pct_ranks([r["avg_metrics"]["sharpe"] for r in ranked])
        win_r = _pct_ranks([r["avg_metrics"]["win_rate"] for r in ranked])
        mdd_r = _pct_ranks(
            [r["avg_metrics"]["mdd"] for r in ranked], higher_better=False
        )
        cagr_r = _pct_ranks([r["avg_metrics"]["cagr"] for r in ranked])
        for i, r in enumerate(ranked):
            r["_score"] = (
                settings.rank_w_sharpe * sharpe_r[i]
                + settings.rank_w_win_rate * win_r[i]
                + settings.rank_w_mdd * mdd_r[i]
                + settings.rank_w_cagr * cagr_r[i]
            )
        ranked.sort(key=lambda r: r["_score"], reverse=True)
        for r in ranked:
            del r["_score"]
    return (ranked + low_conf_rows)[:limit]


class Orchestrator:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        settings: Settings,
        loader: DataLoader | None = None,
        broker: Broker | None = None,
        broker_provider: Callable[[], Broker] | None = None,
        meeting_seconds: float = 4.0,
        rng: random.Random | None = None,
    ):
        self.db = db
        self.bus = bus
        self.settings = settings
        self.loader = loader or DataLoader(db)
        if broker_provider is None:
            default_broker = broker or PaperBroker(db, self.loader, settings)
            broker_provider = lambda: default_broker  # noqa: E731
        # 브로커는 직접 들지 않고 매 사이클 시작 시 provider로 조회한다 —
        # trading-mode 핫스왑이 즉시 반영된다 (스펙 §5).
        self.broker_provider = broker_provider
        # trade 사이클 ↔ PositionMonitor 주문/포지션 변이 직렬화 락 (스펙 §1.1).
        # main.py가 자신의 공유 락으로 교체할 수 있게 속성으로 노출한다.
        self.trade_lock = asyncio.Lock()
        self.regime_service = RegimeService(db, self.loader, settings)
        self.funding_loader = FundingLoader(db, settings)
        self.meeting_seconds = meeting_seconds
        self.rng = rng or random.Random()
        self.agents: dict[str, AgentBase] = {
            "pm": PM(bus),
            "data": DataEngineer(bus),
            "strategist": Strategist(bus),
            "quant": Quant(bus),
            "risk": Risk(bus),
            "analyst": Analyst(bus),
            "trader": Trader(bus),
        }
        self.cycle_task: asyncio.Task | None = None
        self._cycle_id: int | None = None
        self._kind: str = "research"
        self._step: str = ""
        self._meeting: dict | None = None
        # 목표 탐색 모드 상태. _goal_state is None until goal mode is started once
        # this process (so /api/status can report `goal: null` before first use).
        self._goal_task: asyncio.Task | None = None
        self._goal_state: dict | None = None
        # Cooperative stop flag for CPU-bound worker threads (asyncio.Task
        # cancellation cannot interrupt asyncio.to_thread work).
        self._stop_flag = threading.Event()

    # -- public API -------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.cycle_task is not None and not self.cycle_task.done()

    async def start_cycle(self, kind: str = "research") -> int:
        """Start one cycle of ``kind`` (research|validate|trade). A single
        runner mutex applies across all kinds — only one run at a time."""
        if kind not in CYCLE_KINDS:
            raise ValueError(f"unknown cycle kind: {kind!r}")
        if self.running:
            raise CycleInProgressError("cycle already in progress")
        rows = self.db.execute(
            "INSERT INTO cycles (started_at, status, kind, params_json) "
            "VALUES (datetime('now'), 'running', ?, ?)",
            (
                kind,
                json.dumps(
                    {
                        "universe": self.settings.universe,
                        "candidates": self.settings.candidates_per_cycle,
                    }
                ),
            ),
        )
        cycle_id = int(rows[0]["id"])
        self._cycle_id = cycle_id
        self._kind = kind
        self._step = "start"
        self._stop_flag.clear()
        runner = {
            "research": self._run_research,
            "validate": self._run_validate,
            "trade": self._run_trade,
        }[kind]
        self.cycle_task = asyncio.create_task(self._run(runner, cycle_id))
        return cycle_id

    async def stop_cycle(self) -> None:
        if not self.running:
            return
        assert self.cycle_task is not None
        self._stop_flag.set()  # let in-flight worker threads exit early
        self.cycle_task.cancel()
        try:
            await self.cycle_task
        except asyncio.CancelledError:
            pass

    def status(self) -> dict:
        cycle = None
        if self.running and self._cycle_id is not None:
            cycle = {
                "id": self._cycle_id,
                "status": "running",
                "step": self._step,
                "kind": self._kind,
            }
        return {
            "trading_mode": self.settings.trading_mode,
            "cycle": cycle,
            "agents": [a.describe() for a in self.agents.values()],
            "goal": self.goal_status(),
        }

    def snapshot(self) -> dict:
        """WS snapshot payload (스펙 §7) — 포지션·마진·레짐 포함."""
        st = self.status()
        positions = [
            {
                "symbol": r["symbol"],
                "side": r["side"],
                "qty": float(r["qty"]),
                "avg_entry": float(r["avg_entry"]),
                "leverage": int(r["leverage"]),
                "isolated_margin": float(r["isolated_margin"]),
                "liq_price": float(r["liq_price"] or 0.0),
            }
            for r in self.db.execute(
                "SELECT * FROM paper_positions WHERE qty > 0 ORDER BY symbol"
            )
        ]
        margin = None
        snaps = self.db.execute(
            "SELECT wallet_balance, available, margin_used, unrealized_pnl, "
            "funding_cum, total_value FROM portfolio_snapshots "
            "ORDER BY id DESC LIMIT 1"
        )
        if snaps:
            margin = {k: float(v) for k, v in snaps[0].items() if v is not None}
        regime_rows = self.db.execute(
            "SELECT regime FROM market_regime ORDER BY date DESC LIMIT 1"
        )
        return {
            "agents": [
                {"id": a["id"], "state": a["state"], "detail": a["detail"]}
                for a in st["agents"]
            ],
            "cycle": st["cycle"],
            "meeting": self._meeting,
            "positions": positions,
            "margin": margin,
            "regime": regime_rows[0]["regime"] if regime_rows else "cash",
            # SnapshotEvent 계약 (frontend types.ts) — 접속 즉시 모드 뱃지 표시.
            "trading_mode": self.settings.trading_mode,
        }

    async def hold_meeting(self, a: str, b: str, topic: str) -> None:
        """meeting_start → sleep(meeting_seconds) → meeting_end."""
        meeting_id = uuid.uuid4().hex[:8]
        self._meeting = {"id": meeting_id, "agents": [a, b]}
        await self.bus.publish(
            Event(
                type="meeting_start",
                data={"meeting_id": meeting_id, "agents": [a, b], "topic": topic},
            )
        )
        try:
            await asyncio.sleep(self.meeting_seconds)
        finally:
            self._meeting = None
            await self.bus.publish(
                Event(type="meeting_end", data={"meeting_id": meeting_id})
            )

    # -- goal-seek (목표 탐색 모드) ------------------------------------------------
    @property
    def goal_running(self) -> bool:
        return self._goal_task is not None and not self._goal_task.done()

    def goal_status(self) -> dict | None:
        """/api/status `goal` payload; None until goal mode is started once."""
        if self._goal_state is None:
            return None
        return {
            "running": self.goal_running,
            "cycles_done": self._goal_state["cycles_done"],
            "best_win_rate": self._goal_state["best_win_rate"],
            "target_win_rate": self.settings.goal_win_rate,
            "max_cycles": self.settings.goal_max_cycles,
        }

    def start_goal(self) -> None:
        """Launch the goal-seek loop as a background task (idempotency guarded
        by GoalInProgressError). Runs alongside/above the auto-cycle loop: both
        drive start_cycle, so the single-runner mutex interleaves them and the
        goal loop simply waits out any cycle it did not start."""
        if self.goal_running:
            raise GoalInProgressError("goal mode already running")
        self._goal_state = {"cycles_done": 0, "best_win_rate": None}
        self._goal_task = asyncio.create_task(self._goal_loop())

    async def stop_goal(self) -> None:
        """Cancel the goal loop. A cycle it already started keeps running to
        completion (or is stopped separately via stop_cycle); the final
        cycles_done / best_win_rate stay readable via goal_status()."""
        task = self._goal_task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _goal_loop(self) -> None:
        """Alternate research and validation cycles until a walk-forward-validated
        strategy meets the user's targets (goal_met) or the research-cycle budget
        is spent. Individual cycle failures are logged by _run and skipped here."""
        s = self.settings
        st = self._goal_state
        assert st is not None
        await self._goal_publish(
            f"🎯 목표 탐색 시작 — 목표 OOS 승률 {s.goal_win_rate:.0%}, "
            f"{s.goal_validate_every}회 연구마다 검증, 최대 {s.goal_max_cycles}회"
        )
        while st["cycles_done"] < s.goal_max_cycles:
            await self._goal_run_cycle("research")
            st["cycles_done"] += 1
            if st["cycles_done"] % max(1, s.goal_validate_every) != 0:
                continue
            summary = await self._goal_run_cycle("validate")
            oos = (summary or {}).get("oos_metrics")
            win_rate = oos.get("win_rate") if oos else None
            total_return = oos.get("total_return") if oos else None
            # Best-so-far is tracked only among validations that were profitable
            # OOS — a high win rate on a losing run is not real progress.
            if win_rate is not None and total_return is not None and total_return > 0:
                if st["best_win_rate"] is None or win_rate > st["best_win_rate"]:
                    st["best_win_rate"] = win_rate
            if goal_met(oos, s):
                await self._goal_announce_success(oos, st["cycles_done"])
                break
            await self._goal_log_progress(oos, st["cycles_done"])
        else:
            await self._goal_log_exhausted(st["cycles_done"])
        # 목표 달성이든 예산 소진이든 — 탐색이 끝나면 곧바로 모의거래 사이클을
        # 자동 실행해 손을 떼도 매매까지 이어지게 한다. 사용자 취소(CancelledError)로
        # 빠져나올 때는 여기 도달하지 않으므로 자동 매매도 하지 않는다.
        await self._goal_auto_trade()

    async def _goal_auto_trade(self) -> None:
        """Run one trade cycle after the goal loop finishes. Reuses the same
        single-runner discipline as the loop (waits out any cycle it did not
        start via _goal_run_cycle)."""
        await self._goal_publish("목표 탐색 종료 — 모의거래 자동 실행")
        await self._goal_run_cycle("trade")

    async def _goal_run_cycle(self, kind: str) -> dict | None:
        """Start a cycle of ``kind`` — waiting out any cycle we did not start —
        await it, and return its persisted summary (None on failure)."""
        while True:
            try:
                cycle_id = await self.start_cycle(kind)
                break
            except CycleInProgressError:
                await asyncio.sleep(GOAL_BUSY_POLL_SECONDS)
        task = self.cycle_task
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a failed cycle must not kill goal mode
                pass
        rows = self.db.execute(
            "SELECT summary_json FROM cycles WHERE id = ?", (cycle_id,)
        )
        if rows and rows[0]["summary_json"]:
            try:
                return json.loads(rows[0]["summary_json"])
            except (ValueError, TypeError):
                return None
        return None

    async def _goal_publish(self, message: str, level: str = "info") -> None:
        await self.bus.publish(
            Event(
                type="log", agent="pm", level=level, message=message,
                data={"goal": True},
            )
        )

    async def _goal_announce_success(self, oos: dict, cycles_done: int) -> None:
        await self._goal_publish(
            f"🎯 목표 달성 — OOS 승률 {oos['win_rate']:.0%}, "
            f"총수익 {oos['total_return']:+.1%}, 거래 {oos['trade_count']}건 "
            f"(연구 {cycles_done}회). 워크포워드 검증된 전략을 확보했습니다 — "
            f"이제 모의거래를 시작하세요."
        )

    async def _goal_log_progress(self, oos: dict | None, cycles_done: int) -> None:
        s = self.settings
        win_rate = oos.get("win_rate") if oos else None
        wr_txt = f"{win_rate:.0%}" if win_rate is not None else "N/A"
        best = self._goal_state["best_win_rate"] if self._goal_state else None
        best_txt = f"{best:.0%}" if best is not None else "N/A"
        await self._goal_publish(
            f"목표 미달 — 이번 OOS 승률 {wr_txt} / 목표 {s.goal_win_rate:.0%}, "
            f"최고 기록 {best_txt}, 사이클 {cycles_done}/{s.goal_max_cycles}"
        )

    async def _goal_log_exhausted(self, cycles_done: int) -> None:
        s = self.settings
        best = self._goal_state["best_win_rate"] if self._goal_state else None
        best_txt = f"{best:.0%}" if best is not None else "N/A"
        await self._goal_publish(
            f"목표 탐색 종료 — 연구 {cycles_done}회 소진, "
            f"목표 승률 {s.goal_win_rate:.0%} 미달 (최고 OOS 승률 {best_txt}). "
            f"설정을 조정하거나 다시 시도하세요.",
            level="warning",
        )

    # -- cycle flow (spec §3) -----------------------------------------------------
    async def _run(self, runner, cycle_id: int) -> None:
        """Shared cycle scaffolding: run ``runner`` and apply the same
        abort/failure handling and agent-idle cleanup to every kind."""
        try:
            await runner(cycle_id)
        except asyncio.CancelledError:
            self._finalize(cycle_id, "aborted")
            await self.bus.publish(
                Event(
                    type="log",
                    agent="pm",
                    level="warning",
                    message=f"사이클 #{cycle_id} 중단됨",
                    data={"cycle_id": cycle_id},
                )
            )
            raise
        except Exception as exc:  # noqa: BLE001 — a failed cycle must not crash the app
            self._finalize(cycle_id, "failed")
            await self.bus.publish(
                Event(
                    type="log",
                    agent="pm",
                    level="error",
                    message=f"사이클 #{cycle_id} 실패: {exc}",
                    data={"cycle_id": cycle_id, "error": str(exc)},
                )
            )
        finally:
            self._meeting = None
            for agent in self.agents.values():
                if agent.state != "idle":
                    await agent.set_state("idle")

    # -- data assembly helpers ------------------------------------------------------
    async def _load_market(
        self, data_agent: DataEngineer, universe: list[str]
    ) -> tuple[dict, str]:
        """Refresh + load multi-TF frames, funding history and the regime."""
        s = self.settings
        await data_agent.refresh_universe(self.loader, universe, s.timeframes)
        await data_agent.refresh_funding(self.funding_loader, universe)
        regime = await data_agent.refresh_regime(self.regime_service)
        data = await data_agent.load_universe(
            self.loader, universe, s.timeframes, required_tf=s.execution_timeframe
        )
        return data, regime

    def _prep_regimes(self, data: dict) -> dict[str, pd.Series]:
        """심볼별 실행 TF 인덱스에 정렬된 레짐 시리즈 (1일 시프트, 스펙 §1.2)."""
        tf = self.settings.execution_timeframe
        return {
            symbol: self.regime_service.align_to(frames[tf].index)
            for symbol, frames in data.items()
            if tf in frames
        }

    def _prep_fundings(self, data: dict) -> dict[str, pd.Series]:
        """심볼별 실행 TF 구간의 펀딩 시리즈 (캐시 없으면 기본요율 근사)."""
        tf = self.settings.execution_timeframe
        out: dict[str, pd.Series] = {}
        for symbol, frames in data.items():
            if tf not in frames:
                continue
            idx = frames[tf].index
            start = int(idx[0].value // _MS)
            end = int(idx[-1].value // _MS)
            out[symbol] = self.funding_loader.get_funding(symbol, start, end)
        return out

    # -- research (전략 발굴) -------------------------------------------------------
    async def _run_research(self, cycle_id: int) -> None:
        """Strategy-discovery round: data → regime/relative strength →
        candidates → plan-driven backtests → risk → champion selection →
        report. Places no orders (that is the trade cycle's job)."""
        s = self.settings
        pm: PM = self.agents["pm"]  # type: ignore[assignment]
        data_agent: DataEngineer = self.agents["data"]  # type: ignore[assignment]
        strategist: Strategist = self.agents["strategist"]  # type: ignore[assignment]
        quant: Quant = self.agents["quant"]  # type: ignore[assignment]
        risk: Risk = self.agents["risk"]  # type: ignore[assignment]
        analyst: Analyst = self.agents["analyst"]  # type: ignore[assignment]

        await pm.announce_start(cycle_id)
        await self._progress(cycle_id, "start", 0)
        await self.hold_meeting("pm", "strategist", "탐색 방향 결정")

        # Data Engineer: multi-TF refresh + funding + regime.
        await self._progress(cycle_id, "data", 5)
        universe = list(s.universe)
        data, regime = await self._load_market(data_agent, universe)
        if not data:
            raise RuntimeError("no OHLCV data available for the universe")

        # Strategist: relative-strength ranking (규칙 §4-4) + candidates
        # (exploit champion when present; per-template elites keep
        # non-champion templates in the gene pool).
        await self._progress(cycle_id, "candidates", 15)
        daily_closes = {
            sym: frames["1d"]["close"]
            for sym, frames in data.items()
            if "1d" in frames
        }
        symbol_ranking = await strategist.rank_symbols(daily_closes)
        champion_spec = self._load_champion_spec()
        elites = self._load_template_elites()
        specs = await strategist.propose(
            s.candidates_per_cycle, champion_spec, self.rng, elites=elites
        )
        candidates: list[tuple[int, StrategySpec]] = []
        for spec in specs:
            rows = self.db.execute(
                "INSERT INTO strategies (cycle_id, template, params_json, "
                "universe_json, status) VALUES (?, ?, ?, ?, 'candidate')",
                (
                    cycle_id,
                    spec.template,
                    json.dumps(spec.params),
                    json.dumps(list(data.keys())),
                ),
            )
            candidates.append((int(rows[0]["id"]), spec))
        await self.hold_meeting("strategist", "quant", "후보 인계")

        # Quant: plan-driven backtests across universe × timeframes.
        await self._progress(cycle_id, "backtest", 25)
        cost = self._cost()
        regimes = await asyncio.to_thread(self._prep_regimes, data)
        fundings = await asyncio.to_thread(self._prep_fundings, data)
        results = await quant.backtest_candidates(
            candidates, data, cost, self.db, s,
            regimes=regimes, fundings=fundings, stop_flag=self._stop_flag,
        )
        await self.hold_meeting("quant", "risk", "결과 인계")

        # Risk: filter and persist statuses.
        await self._progress(cycle_id, "risk", 70)
        passed, rejected = await risk.review(results, s)
        for sid in passed:
            self.db.execute(
                "UPDATE strategies SET status = 'passed' WHERE id = ?", (sid,)
            )
        for sid in rejected:
            self.db.execute(
                "UPDATE strategies SET status = 'rejected' WHERE id = ?", (sid,)
            )
        await self.hold_meeting("risk", "strategist", "탈락 사유 피드백")
        spec_by_id = dict(candidates)
        await strategist.receive_feedback(
            [
                (spec_by_id[sid].id_key() if sid in spec_by_id else str(sid), reason)
                for sid, reason in rejected.items()
            ]
        )

        # Champion demotion gate (rolling drawdown on the live paper equity)
        # then selection from the global leaderboard.
        await self._maybe_demote_champion()
        board = compute_leaderboard(
            self.db, limit=20, min_trades=s.min_trades, settings=s
        )
        champion_row = next(
            (r for r in board if not r["low_confidence"] and not r["low_activity"]),
            None,
        )
        # A champion crowned before champion_history existed has no open reign;
        # give it one so its tenure is tracked from this cycle onward.
        self._backfill_champion_history()
        if champion_row is not None:
            self.db.execute(
                "UPDATE strategies SET status = 'passed' "
                "WHERE status = 'champion' AND id != ?",
                (champion_row["strategy_id"],),
            )
            self.db.execute(
                "UPDATE strategies SET status = 'champion' WHERE id = ?",
                (champion_row["strategy_id"],),
            )
            champion_row["status"] = "champion"
            self._record_champion(champion_row["strategy_id"])

        # Keep the ranked pool bounded: goal-seek adds dozens of passed rows
        # per cycle and near-duplicate mutants far down the ranking never
        # rank again — archive everything outside the top 100 so the
        # leaderboard query stays fast. (Archived rows keep their backtests;
        # nothing is deleted.)
        keep_board = compute_leaderboard(
            self.db, limit=100, min_trades=s.min_trades, settings=s
        )
        keep_ids = [r["strategy_id"] for r in keep_board]
        if keep_ids:
            placeholders = ",".join("?" * len(keep_ids))
            self.db.execute(
                f"UPDATE strategies SET status = 'archived' "
                f"WHERE status = 'passed' AND id NOT IN ({placeholders})",
                tuple(keep_ids),
            )

        # Champion trade history + per-TF attribution for the report.
        # Candidate trades are NOT logged — dozens of candidates would flood
        # the activity log; the champion table alone is what the user needs.
        champion_trades: list[dict] = []
        champion_per_symbol: list[dict] = []
        if champion_row is not None:
            _, champion_per_symbol, champion_trades = await asyncio.to_thread(
                evaluate_spec,
                StrategySpec(champion_row["template"], champion_row["params"]),
                data, cost, s, regimes=regimes, fundings=fundings,
            )

        # Analyst: report + PM 보고 meeting.
        await self._progress(cycle_id, "report", 80)
        summary = {
            "candidates": len(candidates),
            "passed": len(passed),
            "rejected": len(rejected),
            "universe": list(data.keys()),
            "regime": regime,
            "symbol_ranking": symbol_ranking,
            "champion": champion_row,
            "champion_trades": champion_trades,
            "champion_per_symbol": champion_per_symbol,
            "timeframe": s.execution_timeframe,
        }
        report_id = await analyst.write_report(cycle_id, board, summary, self.db)
        await self.hold_meeting("analyst", "pm", "보고")

        # Finish: persist summary, broadcast leaderboard top-3. The champion
        # trade list lives only in the report, not the persisted summary_json.
        summary["report_id"] = report_id
        summary.pop("champion_trades", None)
        summary.pop("champion_per_symbol", None)
        self._finish_cycle(cycle_id, summary)
        await self.bus.publish(
            Event(type="leaderboard_update", data={"top": board[:3]})
        )
        await self.bus.publish(
            Event(type="regime_update", data={"regime": regime}, persist=False)
        )
        await self._progress(cycle_id, "done", 100)
        await pm.announce_finish(cycle_id, summary)

    # -- validate (워크포워드 수익성 검증) ------------------------------------------
    async def _run_validate(self, cycle_id: int) -> None:
        """Split each symbol's execution-TF history into train (all but the
        last ``OOS_FRACTION``) and OOS; discover a champion on train only,
        then judge it out-of-sample. No strategies/backtests rows are written,
        so the real leaderboard stays clean."""
        s = self.settings
        pm: PM = self.agents["pm"]  # type: ignore[assignment]
        data_agent: DataEngineer = self.agents["data"]  # type: ignore[assignment]
        strategist: Strategist = self.agents["strategist"]  # type: ignore[assignment]
        quant: Quant = self.agents["quant"]  # type: ignore[assignment]
        risk: Risk = self.agents["risk"]  # type: ignore[assignment]
        analyst: Analyst = self.agents["analyst"]  # type: ignore[assignment]

        await pm.announce_start(cycle_id)
        await self._progress(cycle_id, "start", 0)

        # Data: refresh + load, then split train / OOS per symbol.
        await self._progress(cycle_id, "data", 10)
        universe = list(s.universe)
        data, regime = await self._load_market(data_agent, universe)
        exec_tf = s.execution_timeframe

        train_data: dict = {}
        cutoffs: dict[str, pd.Timestamp] = {}
        skipped: list[str] = []
        for symbol, frames in data.items():
            idx = frames[exec_tf].index
            if len(idx) < MIN_VALIDATE_BARS:
                skipped.append(symbol)
                continue
            oos_n = max(1, int(len(idx) * OOS_FRACTION))
            cutoff = idx[-oos_n]
            cutoffs[symbol] = cutoff
            train_data[symbol] = clip_frames(frames, as_of=cutoff)
        await self.hold_meeting("pm", "quant", "검증 구간 분리")
        if len(train_data) < 2:
            await data_agent.log(
                f"검증 불가 — {exec_tf} 봉 {MIN_VALIDATE_BARS}개 이상 심볼이 "
                f"{len(train_data)}개뿐 (최소 2개 필요). "
                f"제외: {', '.join(skipped) or '없음'}",
                level="error",
            )
            raise RuntimeError("검증에 필요한 심볼 수 부족")

        cost = self._cost()
        regimes = await asyncio.to_thread(self._prep_regimes, data)
        fundings = await asyncio.to_thread(self._prep_fundings, data)
        train_start = min(
            frames[exec_tf].index[0] for frames in train_data.values()
        ).isoformat()
        train_end = max(cutoffs.values()).isoformat()
        test_start = min(cutoffs.values()).isoformat()
        test_end = max(
            data[sym][exec_tf].index[-1] for sym in train_data
        ).isoformat()

        # Train: full discovery pipeline on the train window (in-memory only).
        # Exploration is random-only here: the live champion was selected on
        # FULL history (including the OOS window), so seeding mutations from it
        # would leak future information into where the search starts.
        await self._progress(cycle_id, "train", 35)
        specs = await strategist.propose(s.candidates_per_cycle, None, self.rng)
        candidates = list(enumerate(specs))  # throwaway ids; not persisted
        results = await quant.backtest_candidates(
            candidates, train_data, cost, self.db, s,
            regimes=regimes, fundings=fundings,
            stop_flag=self._stop_flag, persist=False,
        )
        passed, _rejected = await risk.review(results, s)
        train_champion = self._pick_train_champion(passed, results, dict(candidates))

        # OOS: evaluate the frozen train champion on the held-out window
        # (full frames for indicator warm-up; plan candidates gated to the
        # OOS timestamps only).
        await self._progress(cycle_id, "oos", 65)
        train_metrics = oos_metrics = per_symbol_oos = None
        oos_trades: list[dict] = []
        if train_champion is not None:
            train_metrics, _, _ = await asyncio.to_thread(
                evaluate_spec, train_champion, train_data, cost, s,
                regimes=regimes, fundings=fundings,
            )
            oos_metrics, per_symbol_oos, oos_trades = await asyncio.to_thread(
                evaluate_spec, train_champion,
                {sym: data[sym] for sym in train_data}, cost, s,
                regimes=regimes, fundings=fundings, after=cutoffs,
            )
            # Log every OOS trade (bounded — one strategy over the held-out window).
            await quant.log_trades(oos_trades)
        await self.hold_meeting("quant", "risk", "OOS 결과 검토")

        verdict = self._validation_verdict(train_champion, oos_metrics)

        # Report: analyst writes the validation report (kind='validation').
        await self._progress(cycle_id, "report", 85)
        payload = {
            "train_start": train_start,
            "train_end": train_end,
            "test_start": test_start,
            "test_end": test_end,
            "symbols": list(train_data.keys()),
            "skipped": skipped,
            "regime": regime,
            "champion": (
                {"template": train_champion.template, "params": train_champion.params}
                if train_champion is not None
                else None
            ),
            "train_metrics": train_metrics,
            "oos_metrics": oos_metrics,
            "per_symbol_oos": per_symbol_oos,
            "oos_trades": oos_trades,
            "verdict": verdict,
        }
        report_id = await analyst.write_validation_report(cycle_id, payload, self.db)
        await self.hold_meeting("analyst", "pm", "검증 보고")

        summary = {
            "symbols": list(train_data.keys()),
            "skipped": skipped,
            "champion": payload["champion"],
            "oos_metrics": oos_metrics,
            "verdict": verdict,
            "report_id": report_id,
        }
        self._finish_cycle(cycle_id, summary)
        await self._progress(cycle_id, "done", 100)
        await pm.announce_finish(cycle_id, {"candidates": len(candidates), "passed": len(passed)})

    # -- trade (챔피언 플랜 실행) ---------------------------------------------------
    async def _run_trade(self, cycle_id: int) -> None:
        """One Trader pass: settle → reconcile fills → champion TradePlans →
        RiskEngine review → laddered limit entries. Order/position mutations
        run under the shared ``trade_lock`` so the PositionMonitor can never
        interleave a cancel-replace with a new entry (스펙 §1.1)."""
        s = self.settings
        pm: PM = self.agents["pm"]  # type: ignore[assignment]
        data_agent: DataEngineer = self.agents["data"]  # type: ignore[assignment]
        trader: Trader = self.agents["trader"]  # type: ignore[assignment]
        risk: Risk = self.agents["risk"]  # type: ignore[assignment]

        await pm.announce_start(cycle_id)
        await self._progress(cycle_id, "start", 0)
        await self.hold_meeting("pm", "trader", "매매 지시")

        await self._maybe_demote_champion()
        champion_spec = self._load_champion_spec()
        if champion_spec is None:
            await trader.log(
                "챔피언 없음 — 먼저 연구 사이클을 돌리세요", level="warning"
            )
            self._finish_cycle(cycle_id, {"orders": 0, "champion": None})
            await self._progress(cycle_id, "done", 100)
            await pm.announce_finish(cycle_id, {"candidates": 0, "passed": 0})
            return

        # Data refresh so newly completed bars arrive, then one trader pass.
        await self._progress(cycle_id, "data", 20)
        universe = list(s.universe)
        data, regime = await self._load_market(data_agent, universe)
        if not data:
            raise RuntimeError("no OHLCV data available for the universe")

        broker = self.broker_provider()
        await self._progress(cycle_id, "settle", 40)
        async with self.trade_lock:
            await trader.settle(broker)
            await trader.reconcile(self.db, broker, s)
            await self._progress(cycle_id, "plans", 60)
            orders = await trader.execute(
                champion_spec, data, self.db, broker, s, regime, risk_agent=risk
            )
        await self._progress(cycle_id, "orders", 85)

        summary = {
            "orders": len(orders),
            "regime": regime,
            "champion": {
                "template": champion_spec.template,
                "params": champion_spec.params,
            },
        }
        self._finish_cycle(cycle_id, summary)
        await self._progress(cycle_id, "done", 100)
        await pm.announce_finish(cycle_id, {"candidates": 0, "passed": 0})

    # -- helpers ----------------------------------------------------------------
    def _cost(self) -> PerpCostModel:
        s = self.settings
        return PerpCostModel(
            maker_fee=s.maker_fee, taker_fee=s.taker_fee, slippage=s.slippage
        )

    def _finish_cycle(self, cycle_id: int, summary: dict) -> None:
        self.db.execute(
            "UPDATE cycles SET finished_at = datetime('now'), status = 'done', "
            "summary_json = ? WHERE id = ?",
            (json.dumps(summary, ensure_ascii=False, default=str), cycle_id),
        )

    def _pick_train_champion(
        self,
        passed: list[int],
        results: dict[int, dict],
        spec_by_id: dict[int, StrategySpec],
    ) -> StrategySpec | None:
        """Rank the passed (non-low-confidence) train results with the same
        weighted percentile-rank score as the leaderboard and return the top
        spec. Returns None when nothing ranks."""
        s = self.settings
        rankable = [
            sid
            for sid in passed
            if sid in results and not results[sid]["low_confidence"]
        ]
        if not rankable:
            return None
        avgs = [results[sid]["avg_metrics"] for sid in rankable]
        sharpe_r = _pct_ranks([m["sharpe"] for m in avgs])
        win_r = _pct_ranks([m["win_rate"] for m in avgs])
        mdd_r = _pct_ranks([m["mdd"] for m in avgs], higher_better=False)
        cagr_r = _pct_ranks([m["cagr"] for m in avgs])
        scored = []
        for i, sid in enumerate(rankable):
            score = (
                s.rank_w_sharpe * sharpe_r[i]
                + s.rank_w_win_rate * win_r[i]
                + s.rank_w_mdd * mdd_r[i]
                + s.rank_w_cagr * cagr_r[i]
            )
            scored.append((score, sid))
        scored.sort(reverse=True)
        return spec_by_id.get(scored[0][1])

    def _validation_verdict(
        self, champion: StrategySpec | None, oos: dict | None
    ) -> dict:
        """합격: OOS 총수익 > 0 AND OOS MDD ≤ max_mdd AND 강제 청산 0회."""
        max_mdd = self.settings.max_mdd
        if champion is None or oos is None:
            return {"pass": False, "reason": "학습 구간에서 챔피언 전략을 찾지 못했습니다."}
        total_return = oos.get("total_return")
        mdd = oos.get("mdd")
        liq = int(oos.get("liquidation_count") or 0)
        if total_return is None or mdd is None:
            return {"pass": False, "reason": "검증 구간 지표를 산출할 수 없습니다 (데이터 부족)."}
        if liq > 0:
            return {
                "pass": False,
                "reason": f"OOS 강제 청산 {liq}회 발생 — 즉시 불합격",
            }
        if total_return <= 0:
            return {
                "pass": False,
                "reason": f"OOS 총수익 {total_return:.1%} ≤ 0 — 검증 구간 수익성 미달",
            }
        if mdd > max_mdd:
            return {
                "pass": False,
                "reason": f"OOS MDD {mdd:.1%} > 한도 {max_mdd:.0%}",
            }
        return {
            "pass": True,
            "reason": f"OOS 총수익 {total_return:.1%} > 0, MDD {mdd:.1%} ≤ 한도 {max_mdd:.0%}",
        }

    async def _progress(self, cycle_id: int, step: str, pct: int) -> None:
        self._step = step
        await self.bus.publish(
            Event(
                type="cycle_progress",
                data={
                    "cycle_id": cycle_id,
                    "step": step,
                    "pct": pct,
                    "kind": self._kind,
                },
            )
        )

    async def _maybe_demote_champion(self) -> None:
        """롤링 드로다운 강등 게이트: 챔피언 재위 기간의 페이퍼 에쿼티
        (portfolio_snapshots.total_value)가 max_mdd 이상 드로다운되면 챔피언을
        강등한다 — champion_history의 열린 재위를 닫고 전략을 archived 처리."""
        open_rows = self.db.execute(
            "SELECT id, strategy_id, crowned_at FROM champion_history "
            "WHERE demoted_at IS NULL ORDER BY id DESC LIMIT 1"
        )
        if not open_rows:
            return
        reign = open_rows[0]
        snaps = self.db.execute(
            "SELECT total_value FROM portfolio_snapshots WHERE ts >= ? ORDER BY id",
            (reign["crowned_at"],),
        )
        if len(snaps) < 2:
            return
        peak = float("-inf")
        drawdown = 0.0
        for r in snaps:
            v = float(r["total_value"])
            peak = max(peak, v)
            if peak > 0:
                drawdown = max(drawdown, 1.0 - v / peak)
        if drawdown <= self.settings.max_mdd:
            return
        self.db.execute(
            "UPDATE champion_history SET demoted_at = datetime('now') WHERE id = ?",
            (reign["id"],),
        )
        self.db.execute(
            "UPDATE strategies SET status = 'archived' "
            "WHERE id = ? AND status = 'champion'",
            (reign["strategy_id"],),
        )
        await self.bus.publish(
            Event(
                type="log",
                agent="risk",
                level="warning",
                message=(
                    f"챔피언 강등 — 재위 중 롤링 드로다운 {drawdown:.1%} > "
                    f"한도 {self.settings.max_mdd:.0%} (champion_history 기록)"
                ),
                data={
                    "strategy_id": reign["strategy_id"],
                    "drawdown": drawdown,
                },
            )
        )

    def _backfill_champion_history(self) -> None:
        """One-time migration bridge: if a champion strategy exists but no reign
        is open in champion_history, open one (crowned_at = now)."""
        open_row = self.db.execute(
            "SELECT 1 FROM champion_history WHERE demoted_at IS NULL LIMIT 1"
        )
        if open_row:
            return
        champ = self.db.execute(
            "SELECT id FROM strategies WHERE status = 'champion' "
            "ORDER BY id DESC LIMIT 1"
        )
        if champ:
            self.db.execute(
                "INSERT INTO champion_history (strategy_id, crowned_at) "
                "VALUES (?, datetime('now'))",
                (champ[0]["id"],),
            )

    def _record_champion(self, champion_id: int) -> None:
        """Track champion reigns: when the champion changes, close the open
        reign (set demoted_at) and open a new one; when unchanged, do nothing.
        Also opens the first reign when none is open yet."""
        open_rows = self.db.execute(
            "SELECT id, strategy_id FROM champion_history WHERE demoted_at IS NULL "
            "ORDER BY id DESC LIMIT 1"
        )
        current = open_rows[0] if open_rows else None
        if current is not None and current["strategy_id"] == champion_id:
            return
        if current is not None:
            self.db.execute(
                "UPDATE champion_history SET demoted_at = datetime('now') WHERE id = ?",
                (current["id"],),
            )
        self.db.execute(
            "INSERT INTO champion_history (strategy_id, crowned_at) "
            "VALUES (?, datetime('now'))",
            (champion_id,),
        )

    def _load_champion_spec(self) -> StrategySpec | None:
        rows = self.db.execute(
            "SELECT template, params_json FROM strategies "
            "WHERE status = 'champion' ORDER BY id DESC LIMIT 1"
        )
        if not rows:
            return None
        return StrategySpec(rows[0]["template"], json.loads(rows[0]["params_json"]))

    def _load_template_elites(self) -> list[StrategySpec]:
        """Best individual per template (any status, ≥10 trades, avg sharpe),
        over the most recent 2000 strategies. Rejected-but-promising specs
        deliberately count: a template that keeps failing the risk filter can
        still be refined toward configurations that pass it."""
        rows = self.db.execute(
            "SELECT s.template, s.params_json, "
            "  AVG(json_extract(b.metrics_json, '$.sharpe')) AS avg_sharpe, "
            "  SUM(COALESCE(json_extract(b.metrics_json, '$.trade_count'), 0)) AS trades "
            "FROM strategies s JOIN backtests b ON b.strategy_id = s.id "
            "WHERE s.id IN (SELECT id FROM strategies ORDER BY id DESC LIMIT 2000) "
            "GROUP BY s.id HAVING trades >= 10"
        )
        best: dict[str, tuple[float, StrategySpec]] = {}
        for r in rows:
            score = r["avg_sharpe"] if r["avg_sharpe"] is not None else float("-inf")
            if r["template"] not in best or score > best[r["template"]][0]:
                best[r["template"]] = (
                    score,
                    StrategySpec(r["template"], json.loads(r["params_json"])),
                )
        return [spec for _, spec in best.values()]

    def _finalize(self, cycle_id: int, status: str) -> None:
        self.db.execute(
            "UPDATE cycles SET finished_at = datetime('now'), status = ? "
            "WHERE id = ? AND status = 'running'",
            (status, cycle_id),
        )
