"""main.py robustness: auto/bar-close loops and lifespan cleanup.

The auto-cycle loop must survive unexpected start_cycle() errors (log and
retry next interval), the bar-close trade trigger must drop — never queue —
triggers that hit a busy cycle mutex (spec §1.1 skip-not-queue), and the
lifespan finally block must still run stop_cycle() / db.close() even when a
background task holds a stored exception.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

import app.main as main_module
from app.config import Settings
from app.main import (
    _auto_cycle_loop,
    _bar_close_trade_loop,
    _seconds_until_next_bar_close,
    create_app,
)
from app.orchestrator import CycleInProgressError


class RecordingOrchestrator:
    """Records the kind of every started cycle; each start_cycle produces an
    already-finished cycle_task so the auto loop's await returns immediately."""

    def __init__(self):
        self.kinds: list[str] = []
        self.cycle_task = None

    async def start_cycle(self, kind: str = "research") -> int:
        self.kinds.append(kind)

        async def _done() -> None:
            return None

        self.cycle_task = asyncio.create_task(_done())
        return len(self.kinds)


class FlakyOrchestrator:
    """start_cycle fails with a transient error, then a busy signal, then works."""

    def __init__(self):
        self.calls = 0

    async def start_cycle(self, kind: str = "research") -> int:
        assert kind == "research"  # auto loop only starts research cycles
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("database is locked")  # e.g. sqlite3.OperationalError
        if self.calls == 2:
            raise CycleInProgressError("cycle already in progress")
        return self.calls


class BusyThenFreeOrchestrator:
    """First two trade triggers hit a busy cycle mutex, later ones succeed."""

    def __init__(self):
        self.calls = 0
        self.kinds: list[str] = []

    async def start_cycle(self, kind: str = "trade") -> int:
        self.calls += 1
        self.kinds.append(kind)
        if self.calls <= 2:
            raise CycleInProgressError("cycle already in progress")
        return self.calls


class TinyIntervalSettings:
    auto_cycle_minutes = 1e-7  # ~6µs between iterations
    auto_trade_after_research = False


class TradeAfterResearchSettings:
    auto_cycle_minutes = 1e-7
    auto_trade_after_research = True


class BarTriggerSettings:
    execution_timeframe = "15m"
    bar_close_trade_enabled = True


class BarTriggerDisabledSettings:
    execution_timeframe = "15m"
    bar_close_trade_enabled = False


class DummyBroker:
    """Wave-B placeholder so the lifespan can be exercised before the paper
    broker is transformed to the futures shape."""

    def __init__(self, db, loader, settings):
        self.db = db
        self.loader = loader
        self.settings = settings


@pytest.fixture
def paper_broker_stub(monkeypatch):
    monkeypatch.setattr(main_module, "PaperBroker", DummyBroker)


async def test_auto_cycle_loop_survives_unexpected_start_errors():
    """A transient start_cycle() failure must not kill auto-cycling: the loop
    keeps running and retries on the next interval."""
    orch = FlakyOrchestrator()
    task = asyncio.create_task(_auto_cycle_loop(orch, TinyIntervalSettings()))

    async def _wait_for_calls() -> None:
        while orch.calls < 3:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait_for_calls(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Survived both the RuntimeError and CycleInProgressError iterations.
    assert orch.calls >= 3


async def test_auto_cycle_loop_chains_trade_when_flag_on():
    """With auto_trade_after_research set, each automated research cycle is
    followed by a trade cycle before the loop sleeps again."""
    orch = RecordingOrchestrator()
    task = asyncio.create_task(
        _auto_cycle_loop(orch, TradeAfterResearchSettings())
    )

    async def _wait_for_research_then_trade() -> None:
        while orch.kinds[:2] != ["research", "trade"]:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait_for_research_then_trade(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # research always precedes the trade it triggers.
    assert orch.kinds[0] == "research"
    assert orch.kinds[1] == "trade"


async def test_auto_cycle_loop_no_trade_when_flag_off():
    """Default (flag off): the auto loop starts only research cycles."""
    orch = RecordingOrchestrator()
    task = asyncio.create_task(_auto_cycle_loop(orch, TinyIntervalSettings()))

    async def _wait_for_two_research() -> None:
        while orch.kinds.count("research") < 2:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_wait_for_two_research(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "trade" not in orch.kinds


def test_seconds_until_next_bar_close_alignment():
    """Bar-close alignment is UTC epoch-modular per timeframe; unknown
    timeframes fall back to 15m."""
    # 10s past a 15m boundary → 890s to the next close.
    assert _seconds_until_next_bar_close("15m", 900.0 * 4 + 10.0) == 890.0
    assert _seconds_until_next_bar_close("4h", 14_400.0 + 1.0) == 14_399.0
    assert _seconds_until_next_bar_close("unknown", 10.0) == 890.0


async def test_bar_close_loop_skip_not_queue(monkeypatch):
    """spec §1.1: a trigger that hits a busy cycle mutex is dropped (one
    start attempt per bar close, no retry queue) and the loop keeps firing
    on subsequent bar closes."""
    monkeypatch.setitem(main_module._TF_SECONDS, "15m", 0.001)
    orch = BusyThenFreeOrchestrator()
    task = asyncio.create_task(_bar_close_trade_loop(orch, BarTriggerSettings()))

    async def _wait_for_success() -> None:
        while orch.calls < 3:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_wait_for_success(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Every trigger is a trade cycle; the two busy bars produced exactly one
    # (dropped) attempt each — nothing was queued for immediate retry.
    assert orch.kinds[:3] == ["trade", "trade", "trade"]
    assert orch.calls >= 3


async def test_bar_close_loop_disabled_by_default(monkeypatch):
    """bar_close_trade_enabled=False: the loop wakes at bar closes but never
    starts a cycle."""
    monkeypatch.setitem(main_module._TF_SECONDS, "15m", 0.001)
    orch = BusyThenFreeOrchestrator()
    task = asyncio.create_task(
        _bar_close_trade_loop(orch, BarTriggerDisabledSettings())
    )
    await asyncio.sleep(0.05)  # dozens of bar closes at the tiny interval
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert orch.calls == 0


async def test_lifespan_wires_broker_provider_and_title(
    tmp_path, paper_broker_stub
):
    """The lifespan exposes the broker via app.state.broker plus a
    get_broker() provider (spec §5) and the app is the Coin Agents Office."""
    settings = Settings(db_path=str(tmp_path / "wiring.db"), _env_file=None)
    app = create_app(settings, meeting_seconds=0.0)
    assert app.title == "Coin Agents Office"

    async with app.router.lifespan_context(app):
        assert isinstance(app.state.broker, DummyBroker)
        assert app.state.get_broker() is app.state.broker
        # Hot-swap: the provider must follow app.state.broker, not capture it.
        sentinel = object()
        app.state.broker = sentinel
        assert app.state.get_broker() is sentinel
        assert isinstance(app.state.trade_lock, asyncio.Lock)


async def test_lifespan_cleanup_runs_even_if_auto_task_stored_an_exception(
    tmp_path, monkeypatch, paper_broker_stub
):
    """If the auto-cycle task died with a stored exception, awaiting it at
    shutdown re-raises — the finally block must swallow that and still run
    stop_cycle() and db.close()."""

    async def crash_immediately(orchestrator, settings) -> None:
        raise RuntimeError("auto loop crashed")

    monkeypatch.setattr(main_module, "_auto_cycle_loop", crash_immediately)
    settings = Settings(db_path=str(tmp_path / "lifespan.db"), _env_file=None)
    app = create_app(settings, meeting_seconds=0.0)

    # Exiting the lifespan must not re-raise the stored RuntimeError.
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)  # let the auto task fail and store it
        assert app.state.orchestrator is not None

    # ... and the DB really was closed by the finally block.
    with pytest.raises(sqlite3.ProgrammingError):
        app.state.db.execute("SELECT 1")


async def test_lifespan_live_mode_reconciles_on_boot(tmp_path, monkeypatch):
    """live 모드 기동은 서빙 전에 broker.reconcile()을 호출한다 (isolated/레버리지
    설정 + 오픈 주문/포지션 재부착) — 스펙 §2/§5."""
    import app.broker.binance as binance_mod

    class BootBroker:
        def __init__(self, settings, db=None, **kwargs):
            self.reconciled = False

        async def reconcile(self) -> dict:
            self.reconciled = True
            return {"open_orders": [], "positions": []}

        async def get_positions(self):
            return []

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(binance_mod, "BinanceBroker", BootBroker)
    settings = Settings(
        db_path=str(tmp_path / "live_boot.db"),
        trading_mode="live",
        binance_api_key="k",
        binance_api_secret="s",
        _env_file=None,
    )
    app = create_app(settings, meeting_seconds=0.0)
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.broker, BootBroker)
        assert app.state.broker.reconciled is True


async def test_lifespan_live_mode_fails_boot_when_reconcile_fails(tmp_path, monkeypatch):
    """리컨실 실패는 기동을 크게 실패시킨다 (fail-fast) — 키 미검증 상태로
    라이브 매매를 시작하지 않는다."""
    import app.broker.binance as binance_mod

    class FailingBootBroker:
        def __init__(self, settings, db=None, **kwargs):
            pass

        async def reconcile(self) -> dict:
            raise RuntimeError("invalid api key")

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(binance_mod, "BinanceBroker", FailingBootBroker)
    settings = Settings(
        db_path=str(tmp_path / "live_boot_fail.db"),
        trading_mode="live",
        binance_api_key="k",
        binance_api_secret="s",
        _env_file=None,
    )
    app = create_app(settings, meeting_seconds=0.0)
    with pytest.raises(RuntimeError, match="invalid api key"):
        async with app.router.lifespan_context(app):
            pass
