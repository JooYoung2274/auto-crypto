"""Analyst 하나 (Hana): writes the cycle's markdown report."""
from __future__ import annotations

from ..db import Database
from ..events import EventBus
from ..llm import LLMClient, narrate_research_report, narrate_validation_report
from ..reports.generator import generate_report, generate_validation_report
from .base import AgentBase


class Analyst(AgentBase):
    id = "analyst"
    name = "하나"
    role = "Analyst"

    def __init__(self, bus: EventBus, llm: LLMClient | None = None):
        super().__init__(bus)
        # LLM 해설은 opt-in — 키가 없으면 enabled=False라 아래 호출은 즉시 None.
        self.llm = llm or LLMClient()

    async def _append_narration(self, markdown: str, make_coro) -> str:
        """해설 섹션을 덧붙인다. 어떤 실패도 리포트 저장을 막지 않는다.

        ``make_coro``는 코루틴 **팩토리** — 비활성일 때 코루틴을 아예 만들지
        않아야 "never awaited" 경고가 생기지 않는다.
        """
        if not self.llm.enabled:
            return markdown
        try:
            section = await make_coro()
        except Exception as exc:  # noqa: BLE001 — 해설 실패로 사이클을 죽이지 않는다.
            await self.log(f"AI 해설 생략 — {exc}", level="warn")
            return markdown
        if not section:
            await self.log("AI 해설 생략 — 응답 없음", level="warn")
            return markdown
        return markdown + section

    async def write_report(
        self,
        cycle_id: int,
        leaderboard: list[dict],
        summary: dict,
        db: Database,
    ) -> int:
        """Generate the markdown report, persist it, return the report id."""
        await self.set_state("working", f"사이클 #{cycle_id} 리포트 작성")
        markdown = generate_report(cycle_id, leaderboard, summary)
        markdown = await self._append_narration(
            markdown,
            lambda: narrate_research_report(
                cycle_id, leaderboard, summary, client=self.llm
            ),
        )
        rows = db.execute(
            "INSERT INTO reports (cycle_id, markdown) VALUES (?, ?)",
            (cycle_id, markdown),
        )
        report_id = int(rows[0]["id"])
        await self.log(
            f"리포트 #{report_id} 작성 완료 (사이클 #{cycle_id})",
            report_id=report_id,
            cycle_id=cycle_id,
        )
        await self.set_state("idle")
        return report_id

    async def write_validation_report(
        self, cycle_id: int, payload: dict, db: Database
    ) -> int:
        """Generate the walk-forward validation report (kind='validation'),
        persist it, return the report id."""
        await self.set_state("working", f"사이클 #{cycle_id} 검증 보고서 작성")
        markdown = generate_validation_report(cycle_id, payload)
        markdown = await self._append_narration(
            markdown,
            lambda: narrate_validation_report(cycle_id, payload, client=self.llm),
        )
        rows = db.execute(
            "INSERT INTO reports (cycle_id, markdown, kind) VALUES (?, ?, 'validation')",
            (cycle_id, markdown),
        )
        report_id = int(rows[0]["id"])
        verdict = payload.get("verdict", {})
        await self.log(
            f"검증 리포트 #{report_id} 작성 완료 — "
            f"판정: {'합격' if verdict.get('pass') else '불합격'}",
            report_id=report_id,
            cycle_id=cycle_id,
            passed=bool(verdict.get("pass")),
        )
        await self.set_state("idle")
        return report_id
