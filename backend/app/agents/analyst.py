"""Analyst 하나 (Hana): writes the cycle's markdown report."""
from __future__ import annotations

from ..db import Database
from ..reports.generator import generate_report, generate_validation_report
from .base import AgentBase


class Analyst(AgentBase):
    id = "analyst"
    name = "하나"
    role = "Analyst"

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
