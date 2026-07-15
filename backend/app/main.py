"""FastAPI app assembly.

Lifespan wires Database / EventBus / DataLoader / Broker / Orchestrator into
``app.state``. Serves the built frontend (``frontend/dist``) when present.

Coin Agents specifics (spec §1.1/§5):
- The broker is exposed through a provider: ``app.state.broker`` plus a
  ``get_broker()`` closure handed to the orchestrator/monitor, so a
  trading-mode hot-swap only has to replace ``app.state.broker``.
- ``_bar_close_trade_loop`` triggers trade cycles aligned to the execution
  timeframe's bar close — **skip-not-queue**: if the cycle mutex is busy the
  trigger is dropped and logged, never queued.
- A PositionMonitor task (risk-critical: 4h-close stop rulings, TTL expiry,
  funding settlement, liquidation warnings) runs outside the cycle state
  machine. Until Wave D lands ``app.monitor`` the import is guarded.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.routes import router as api_router
from .broker.paper import PaperBroker
from .config import Settings, get_settings
from .data.loader import DataLoader
from .db import Database
from .events import EventBus
from .orchestrator import CycleInProgressError, Orchestrator
from .ws import WsManager

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

AUTO_CYCLE_POLL_SECONDS = 30.0

# 실행 TF 봉마감 정렬용 봉 길이(초). execution_timeframe이 미지의 값이면 15m.
_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}

logger = logging.getLogger(__name__)


def _seconds_until_next_bar_close(timeframe: str, now: float) -> float:
    """Seconds from ``now`` (epoch) to the next UTC-aligned close of
    ``timeframe`` bars. Unknown timeframes fall back to 15m."""
    interval = _TF_SECONDS.get(timeframe, _TF_SECONDS["15m"])
    return interval - (now % interval)


async def _run_trade_cycle(orchestrator: Orchestrator) -> None:
    """Start a trade cycle — waiting out any cycle we did not start — and await
    it. Used by the auto loop to chain 모의거래 after an automated research cycle."""
    while True:
        try:
            await orchestrator.start_cycle("trade")
            break
        except CycleInProgressError:
            await asyncio.sleep(AUTO_CYCLE_POLL_SECONDS)
    task = orchestrator.cycle_task
    if task is not None:
        with contextlib.suppress(Exception):
            await task


async def _auto_cycle_loop(orchestrator: Orchestrator, settings: Settings) -> None:
    """Kick off research cycles on the configured interval (0 = off; the
    setting is re-read every iteration so runtime config changes take effect).
    The auto loop starts research cycles; when auto_trade_after_research is set
    it awaits each and chains a trade cycle. validate is user-driven.

    Transient start failures (e.g. a locked DB) are logged and retried on
    the next interval — a single error must not silently kill auto-cycling."""
    while True:
        minutes = settings.auto_cycle_minutes
        if minutes <= 0:
            await asyncio.sleep(AUTO_CYCLE_POLL_SECONDS)
            continue
        await asyncio.sleep(minutes * 60.0)
        if settings.auto_cycle_minutes <= 0:
            continue
        try:
            await orchestrator.start_cycle("research")
        except CycleInProgressError:
            continue
        except Exception:  # noqa: BLE001 — keep looping on transient errors
            logger.exception("auto-cycle start failed; will retry next interval")
            continue
        if settings.auto_trade_after_research:
            research_task = orchestrator.cycle_task
            if research_task is not None:
                with contextlib.suppress(Exception):
                    await research_task
            try:
                await _run_trade_cycle(orchestrator)
            except Exception:  # noqa: BLE001 — a trade failure must not kill the loop
                logger.exception("auto-trade after research failed")


async def _maintenance_loop(db) -> None:
    """activity_log 보존 정책 실행 (스펙 §6) — 6시간마다 프루닝."""
    while True:
        await asyncio.sleep(6 * 3600.0)
        try:
            deleted = await asyncio.to_thread(db.prune_activity_log)
            if deleted:
                logger.info("activity_log pruned: %d rows", deleted)
        except Exception:  # noqa: BLE001 — maintenance must not die
            logger.exception("activity_log prune failed")


async def _bar_close_trade_loop(orchestrator: Orchestrator, settings: Settings) -> None:
    """실행 TF 봉마감마다 trade 사이클 트리거 (spec §1.1).

    **Skip-not-queue**: 사이클 뮤텍스가 점유 중이면 이번 봉의 트리거는
    드랍하고 로그만 남긴다 — 재시도 큐 없음, 다음 봉마감에 다시 시도.
    ``bar_close_trade_enabled``는 매 봉마다 재확인 (런타임 토글 가능)."""
    while True:
        delay = _seconds_until_next_bar_close(settings.execution_timeframe, time.time())
        await asyncio.sleep(delay)
        if not settings.bar_close_trade_enabled:
            continue
        try:
            await orchestrator.start_cycle("trade")
        except CycleInProgressError:
            # skip-not-queue: 드랍 후 로그, 다음 봉마감까지 대기.
            logger.info("봉마감 trade 트리거 스킵 — 사이클 진행 중 (skip-not-queue)")
            continue
        except Exception:  # noqa: BLE001 — keep the trigger alive
            logger.exception("bar-close trade start failed; will retry next bar")
            continue


def create_app(
    settings: Settings | None = None, meeting_seconds: float = 4.0
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        s = settings or get_settings()
        db = Database(s.db_path)
        # Cycles left 'running' by a previous process are marked aborted (spec §8).
        db.execute(
            "UPDATE cycles SET status = 'aborted', finished_at = datetime('now') "
            "WHERE status = 'running'"
        )
        bus = EventBus(db)
        loader = DataLoader(db, settings=s)
        if s.trading_mode == "live":
            # Lazy import: paper mode never touches a live adapter or its deps.
            # 거래소 선택 — Binance 주(primary), OKX 추가 (CA_EXCHANGE). 어댑터는
            # 각자 API 키 없이 기동을 거부한다.
            if s.exchange == "okx":
                from .broker.okx import OKXBroker

                broker = OKXBroker(s, db=db)
            else:
                from .broker.binance import BinanceBroker

                broker = BinanceBroker(s, db=db)
        else:
            broker = PaperBroker(db, loader, s)
        if s.trading_mode == "live":
            # 라이브 기동 리컨실 (스펙 §2/§5): 심볼별 isolated 마진 + 레버리지 캡
            # 설정, 오픈 주문/포지션 재부착. 실패하면 키 미검증 상태로 매매를
            # 시작할 수 없으므로 기동을 크게 실패시킨다 (fail-fast).
            await broker.reconcile()
        app.state.settings = s
        app.state.db = db
        app.state.bus = bus
        app.state.loader = loader
        app.state.broker = broker

        def get_broker():
            """Broker provider (spec §5): orchestrator/monitor look the broker
            up at every cycle/tick start so a trading-mode hot-swap that
            replaces ``app.state.broker`` takes effect immediately."""
            return app.state.broker

        app.state.get_broker = get_broker
        orchestrator = Orchestrator(
            db,
            bus,
            s,
            loader=loader,
            meeting_seconds=meeting_seconds,
            broker_provider=get_broker,
        )
        # 주문/포지션 변이 직렬화 락 — trade 사이클과 PositionMonitor 공유
        # (spec §1.1). 오케스트레이터가 락을 소유하고 노출한다.
        trade_lock = orchestrator.trade_lock
        app.state.trade_lock = trade_lock
        app.state.orchestrator = orchestrator
        app.state.ws_manager = WsManager()
        auto_task = asyncio.create_task(_auto_cycle_loop(orchestrator, s))
        bar_task = asyncio.create_task(_bar_close_trade_loop(orchestrator, s))
        maintenance_task = asyncio.create_task(_maintenance_loop(db))
        # PositionMonitor (spec §1.1) — Wave D owns app/monitor.py; guard the
        # import so the app boots before it lands.
        monitor_task: asyncio.Task | None = None
        try:
            from .monitor import PositionMonitor
        except ImportError:
            PositionMonitor = None  # noqa: N806
        if PositionMonitor is not None:
            monitor = PositionMonitor(
                db=db,
                bus=bus,
                settings=s,
                broker_provider=get_broker,
                trade_lock=trade_lock,
            )
            app.state.monitor = monitor
            monitor_task = asyncio.create_task(monitor.run())
        try:
            yield
        finally:
            background = [auto_task, bar_task, maintenance_task]
            if monitor_task is not None:
                background.append(monitor_task)
            for task in background:
                task.cancel()
            # A task may hold a stored exception instead of CancelledError;
            # cleanup below must run regardless (CancelledError is a
            # BaseException, so both must be suppressed).
            for task in background:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            try:
                await orchestrator.stop_goal()
                await orchestrator.stop_cycle()
            finally:
                db.close()

    app = FastAPI(title="Coin Agents Office", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        state = websocket.app.state
        await state.ws_manager.handle(
            websocket, state.bus, state.orchestrator.snapshot()
        )

    if FRONTEND_DIST.is_dir():
        app.mount(
            "/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend"
        )
    return app


app = create_app()
