"""EventBus: publish(event) → activity_log persistence + WS broadcast.

Every published event is stored as an activity_log row and pushed to all
subscriber queues as a JSON-serializable dict payload:

- ``log`` events: ``{"type": "log", "id", "ts", "agent", "level",
  "event_type", "message", "data"}`` (same shape as an activity_log row)
- other events (``agent_state``, ``meeting_start``, ``meeting_end``,
  ``cycle_progress``, ``leaderboard_update``, ``order_filled``,
  ``order_cancelled``, ``position_update``, ``funding_payment``,
  ``liquidation_warning``, ``regime_update``, ...):
  ``{"type": <type>, **event.data}`` — spec §7 schemas verbatim.
  Event types are plain strings — no enum.

High-volume telemetry (position heartbeats, throttled updates) sets
``persist=False``: the payload is still broadcast to every subscriber but
no activity_log row is written (spec §7 이벤트 볼륨 제어).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from dataclasses import dataclass, field

from .db import Database

#: 구독자 큐 상한 — 초과 시 drop-oldest (스펙 §7 이벤트 볼륨 제어).
SUBSCRIBER_QUEUE_MAX = 2000


@dataclass
class Event:
    type: str
    agent: str | None = None
    level: str = "info"
    message: str = ""
    data: dict = field(default_factory=dict)
    # False = telemetry path: broadcast only, no activity_log row (spec §7).
    persist: bool = True


class EventBus:
    def __init__(self, db: Database):
        self.db = db
        self.subscribers: list[asyncio.Queue] = []

    async def publish(self, event: Event) -> None:
        ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        row_id: int | None = None
        if event.persist:
            rows = self.db.execute(
                "INSERT INTO activity_log (ts, agent, level, event_type, message, data_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    event.agent,
                    event.level,
                    event.type,
                    event.message,
                    json.dumps(event.data, ensure_ascii=False, default=str),
                ),
            )
            row_id = rows[0]["id"]

        if event.type == "log":
            payload = {
                "type": "log",
                "id": row_id,
                "ts": ts,
                "agent": event.agent,
                "level": event.level,
                "event_type": "log",
                "message": event.message,
                "data": event.data,
            }
        else:
            payload = {"type": event.type, **event.data}

        for q in list(self.subscribers):
            # 유계 큐 + drop-oldest — 느린(끊기지는 않은) WS 클라이언트가
            # 메모리를 무한 누적하지 않는다. 로그는 재접속 시 REST
            # 페이지네이션으로 복구 가능하므로 가장 오래된 것부터 버린다.
            while q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover — race guard
                    break
            q.put_nowait(payload)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)
