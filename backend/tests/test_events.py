"""EventBus: activity_log persistence + subscriber broadcast."""
from __future__ import annotations

import json

from app.events import Event, EventBus


async def test_publish_log_event_persists_and_broadcasts(db):
    bus = EventBus(db)
    q = bus.subscribe()

    await bus.publish(
        Event(
            type="log",
            agent="quant",
            level="info",
            message="백테스트 완료",
            data={"count": 3},
        )
    )

    rows = db.execute("SELECT * FROM activity_log")
    assert len(rows) == 1
    row = rows[0]
    assert row["agent"] == "quant"
    assert row["level"] == "info"
    assert row["event_type"] == "log"
    assert row["message"] == "백테스트 완료"
    assert json.loads(row["data_json"]) == {"count": 3}

    payload = q.get_nowait()
    assert payload["type"] == "log"
    assert payload["id"] == row["id"]
    assert payload["agent"] == "quant"
    assert payload["level"] == "info"
    assert payload["message"] == "백테스트 완료"
    assert payload["data"] == {"count": 3}
    assert "ts" in payload


async def test_publish_typed_event_flattens_data(db):
    bus = EventBus(db)
    q = bus.subscribe()

    await bus.publish(
        Event(
            type="agent_state",
            agent="quant",
            data={"agent_id": "quant", "state": "working", "detail": "백테스트 중"},
        )
    )

    payload = q.get_nowait()
    assert payload == {
        "type": "agent_state",
        "agent_id": "quant",
        "state": "working",
        "detail": "백테스트 중",
    }
    rows = db.execute("SELECT event_type FROM activity_log")
    assert rows[0]["event_type"] == "agent_state"


async def test_persist_false_broadcasts_without_activity_log(db):
    """spec §7 telemetry path: persist=False events reach every subscriber
    but never write an activity_log row (하트비트류 볼륨 제어)."""
    bus = EventBus(db)
    q = bus.subscribe()

    await bus.publish(
        Event(
            type="position_update",
            agent="trader",
            data={"symbol": "BTCUSDT", "margin_ratio": 0.12},
            persist=False,
        )
    )

    assert db.execute("SELECT * FROM activity_log") == []
    payload = q.get_nowait()
    assert payload == {
        "type": "position_update",
        "symbol": "BTCUSDT",
        "margin_ratio": 0.12,
    }


async def test_persist_false_log_event_broadcasts_with_null_id(db):
    """A non-persisted log event still broadcasts the log-shaped payload;
    its id is None because no activity_log row exists."""
    bus = EventBus(db)
    q = bus.subscribe()

    await bus.publish(
        Event(type="log", agent="trader", message="펀딩 정산", persist=False)
    )

    assert db.execute("SELECT * FROM activity_log") == []
    payload = q.get_nowait()
    assert payload["type"] == "log"
    assert payload["id"] is None
    assert payload["message"] == "펀딩 정산"


async def test_persist_default_true_keeps_legacy_behaviour(db):
    """New crypto event types are plain strings — persisted by default."""
    bus = EventBus(db)
    q = bus.subscribe()

    await bus.publish(
        Event(type="order_filled", data={"symbol": "ETHUSDT", "qty": 0.5})
    )

    rows = db.execute("SELECT event_type FROM activity_log")
    assert rows[0]["event_type"] == "order_filled"
    assert q.get_nowait() == {"type": "order_filled", "symbol": "ETHUSDT", "qty": 0.5}


async def test_multiple_subscribers_and_unsubscribe(db):
    bus = EventBus(db)
    q1 = bus.subscribe()
    q2 = bus.subscribe()

    await bus.publish(Event(type="log", agent="pm", message="one"))
    assert q1.get_nowait()["message"] == "one"
    assert q2.get_nowait()["message"] == "one"

    bus.unsubscribe(q1)
    await bus.publish(Event(type="log", agent="pm", message="two"))
    assert q1.empty()
    assert q2.get_nowait()["message"] == "two"

    bus.unsubscribe(q1)  # idempotent
