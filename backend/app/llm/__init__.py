"""LLM(Claude) 해설 계층 — opt-in, 읽기 전용, 실패 무해.

이 패키지는 **매매 판단에 관여하지 않는다**. 숫자는 전부 코드가 계산하고,
LLM은 이미 확정된 숫자를 사람이 읽기 좋은 문장으로 옮기기만 한다.
키(`CA_ANTHROPIC_API_KEY`)가 없거나 `CA_LLM_ENABLED=false`면 계층 전체가
비활성이며, 호출이 실패해도 해설 섹션만 빠지고 사이클은 정상 종료된다.
"""
from __future__ import annotations

from .client import LLMClient
from .narrator import narrate_research_report, narrate_validation_report

__all__ = ["LLMClient", "narrate_research_report", "narrate_validation_report"]
