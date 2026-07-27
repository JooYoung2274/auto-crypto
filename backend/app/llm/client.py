"""Anthropic Claude API 얇은 래퍼.

설계 원칙 (README "AI(LLM) 기능 — 기획"):

- **opt-in** — `CA_LLM_ENABLED=true` + `CA_ANTHROPIC_API_KEY`가 둘 다 있어야
  활성. 하나라도 없으면 `enabled=False`이고 `complete()`는 곧바로 None.
- **실패 무해** — SDK 미설치·타임아웃·API 오류·빈 응답 전부 None으로 흡수한다.
  이 모듈은 어떤 경우에도 예외를 밖으로 던지지 않는다 (호출부가 사이클 도중이라
  예외 하나로 매매 사이클이 죽으면 안 된다).
- **읽기 전용** — 툴/함수 호출 없이 텍스트만 받는다. LLM이 시스템 상태를
  바꿀 수 있는 경로는 존재하지 않는다.
"""
from __future__ import annotations

import asyncio
import logging

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

# SDK는 선택 의존성 — `pip install -r requirements-llm.txt`로만 들어온다.
MISSING_SDK_HINT = (
    "anthropic SDK 미설치 — `pip install -r backend/requirements-llm.txt`"
)


class LLMClient:
    """단일 프롬프트 → 단일 텍스트. 상태를 갖지 않는다."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._client = None
        self._sdk_failed = False

    # ── 활성 여부 ────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        s = self.settings
        return bool(getattr(s, "llm_enabled", False) and getattr(s, "anthropic_api_key", ""))

    def status(self) -> dict:
        """UI/진단용 상태 — 키 값 자체는 절대 노출하지 않는다."""
        return {
            "enabled": self.enabled,
            "configured": bool(getattr(self.settings, "anthropic_api_key", "")),
            "model": self.settings.llm_model,
            "effort": self.settings.llm_effort,
        }

    # ── 내부: SDK 지연 로딩 ─────────────────────────────────────────────
    def _get_client(self):
        if self._client is not None or self._sdk_failed:
            return self._client
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            self._sdk_failed = True
            log.warning("LLM 해설 비활성 — %s", MISSING_SDK_HINT)
            return None
        self._client = AsyncAnthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    # ── 호출 ────────────────────────────────────────────────────────────
    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int | None = None,
    ) -> str | None:
        """텍스트 한 덩어리를 받는다. 실패하면 None (예외 없음)."""
        if not self.enabled:
            return None
        client = self._get_client()
        if client is None:
            return None

        s = self.settings
        try:
            message = await asyncio.wait_for(
                client.messages.create(
                    model=s.llm_model,
                    max_tokens=max_tokens or s.llm_max_tokens,
                    system=system,
                    # 해설은 짧은 요약 — 저노력으로 비용·지연을 낮춘다.
                    output_config={"effort": s.llm_effort},
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=s.llm_timeout_seconds,
            )
        except asyncio.TimeoutError:
            log.warning("LLM 해설 타임아웃 (%.0fs) — 섹션 생략", s.llm_timeout_seconds)
            return None
        except Exception as exc:  # noqa: BLE001 — 어떤 실패도 사이클을 죽이지 않는다.
            log.warning("LLM 해설 호출 실패 (%s) — 섹션 생략", exc)
            return None

        # 안전 분류기 거부 등은 content가 비어 있을 수 있다 — 반드시 먼저 확인.
        if getattr(message, "stop_reason", None) == "refusal":
            log.warning("LLM 해설 거부됨 — 섹션 생략")
            return None
        parts = [
            block.text
            for block in getattr(message, "content", [])
            if getattr(block, "type", None) == "text"
        ]
        text = "\n".join(parts).strip()
        return text or None
