"""PM 준 (Jun): starts/finishes cycles, dispatches work, gathers results."""
from __future__ import annotations

from .base import AgentBase


class PM(AgentBase):
    id = "pm"
    name = "준"
    role = "PM"

    async def announce_start(self, cycle_id: int) -> None:
        await self.set_state("working", f"사이클 #{cycle_id} 진행 관리")
        await self.log(f"사이클 #{cycle_id} 시작 — 팀 업무 분배", cycle_id=cycle_id)

    async def announce_finish(self, cycle_id: int, summary: dict) -> None:
        await self.log(
            f"사이클 #{cycle_id} 종료 — 후보 {summary.get('candidates', 0)}개 중 "
            f"{summary.get('passed', 0)}개 통과",
            cycle_id=cycle_id,
            **summary,
        )
        await self.set_state("idle")
