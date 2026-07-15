"""Agent base class: identity + state publication over the EventBus."""
from __future__ import annotations

from typing import Literal

from ..events import Event, EventBus

AgentState = Literal["idle", "working"]


class AgentBase:
    id: str = ""
    name: str = ""
    role: str = ""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.state: AgentState = "idle"
        self.detail: str = ""

    async def set_state(self, state: AgentState, detail: str = "") -> None:
        self.state = state
        self.detail = detail
        await self.bus.publish(
            Event(
                type="agent_state",
                agent=self.id,
                data={"agent_id": self.id, "state": state, "detail": detail},
            )
        )

    async def log(self, message: str, level: str = "info", **data) -> None:
        await self.bus.publish(
            Event(type="log", agent=self.id, level=level, message=message, data=data)
        )

    def describe(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "state": self.state,
            "detail": self.detail,
        }
