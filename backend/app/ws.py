"""WebSocket connection handling.

Each connection immediately receives a ``snapshot`` event, then relays every
EventBus payload until the client disconnects. The snapshot dict is supplied
by the caller (Orchestrator) and — spec §7 — includes open positions and
margin state alongside agents/cycle/meeting; the manager stays generic and
forwards whatever it is given.
"""
from __future__ import annotations

import asyncio
import contextlib

from fastapi import WebSocket, WebSocketDisconnect

from .events import EventBus


class WsManager:
    """Tracks active connections and pumps EventBus payloads to each one."""

    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def handle(self, websocket: WebSocket, bus: EventBus, snapshot: dict) -> None:
        """Accept the socket, send the snapshot, then relay bus events until
        the client disconnects."""
        await websocket.accept()
        self.active.add(websocket)
        queue = bus.subscribe()
        try:
            await websocket.send_json({"type": "snapshot", **snapshot})
            forward = asyncio.create_task(self._forward(queue, websocket))
            drain = asyncio.create_task(self._drain(websocket))
            done, pending = await asyncio.wait(
                {forward, drain}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for task in done:  # surface unexpected errors (disconnects excluded)
                exc = task.exception()
                if exc is not None and not isinstance(exc, WebSocketDisconnect):
                    raise exc
        except WebSocketDisconnect:
            pass
        finally:
            bus.unsubscribe(queue)
            self.active.discard(websocket)

    @staticmethod
    async def _forward(queue: asyncio.Queue, websocket: WebSocket) -> None:
        while True:
            payload = await queue.get()
            await websocket.send_json(payload)

    @staticmethod
    async def _drain(websocket: WebSocket) -> None:
        """Consume incoming frames so a client close is detected promptly."""
        while True:
            await websocket.receive_text()
