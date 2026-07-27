"""LLM 해설 계층 — opt-in / 읽기 전용 / 실패 무해 불변식.

이 계층은 매매 판단에 관여하지 않는다. 여기서 지키는 것:
1. 키가 없으면 계층 전체가 비활성이고 리포트는 기존과 **바이트 동일**하다.
2. 타임아웃·API 오류·거부·빈 응답은 전부 None으로 흡수되고 예외가 새지 않는다.
3. 프롬프트에는 코드가 계산한 숫자만 들어간다 (LLM이 수치를 만들지 않는다).
"""
from __future__ import annotations

import asyncio

import pytest  # noqa: F401 — asyncio_mode=auto

from app.agents.analyst import Analyst
from app.config import Settings
from app.events import EventBus
from app.llm.client import LLMClient
from app.llm.narrator import (
    DISCLAIMER,
    SECTION_TITLE,
    build_research_prompt,
    build_validation_prompt,
    narrate_research_report,
    narrate_validation_report,
)
from app.reports.generator import generate_report, generate_validation_report

# ── 픽스처 ──────────────────────────────────────────────────────────────
LEADERBOARD = [
    {
        "strategy_id": 1,
        "template": "ma_golden",
        "params": {"fast": 20, "slow": 60},
        "status": "champion",
        "avg_metrics": {
            "sharpe": 1.42,
            "win_rate": 0.61,
            "mdd": 0.18,
            "cagr": 0.55,
            "total_return": 0.72,
            "profit_factor": 2.1,
            "trade_count": 34,
            "funding_paid": -12.5,
            "fee_paid": -30.0,
            "liquidation_count": 0,
        },
    },
    {
        "strategy_id": 2,
        "template": "vpvr_accum",
        "params": {"rise_min": 0.2},
        "low_confidence": True,
        "avg_metrics": {"sharpe": 0.8, "win_rate": 0.5, "mdd": 0.25, "trade_count": 7},
    },
]
SUMMARY = {
    "candidates": 60,
    "passed": 2,
    "rejected": 58,
    "universe": ["BTCUSDT", "ETHUSDT"],
    "regime": "short",
    "symbol_ranking": [{"symbol": "ETHUSDT", "relative": -0.04}],
    "champion": LEADERBOARD[0],
}
VALIDATION_PAYLOAD = {
    "train_start": "2024-01-01",
    "train_end": "2024-09-01",
    "test_start": "2024-09-01",
    "test_end": "2024-12-01",
    "symbols": ["BTCUSDT"],
    "regime": "long_btc",
    "champion": LEADERBOARD[0],
    "train_metrics": {"total_return": 0.7, "win_rate": 0.62, "mdd": 0.18, "sharpe": 1.4},
    "oos_metrics": {"total_return": 0.05, "win_rate": 0.44, "mdd": 0.22, "sharpe": 0.3},
    "per_symbol_oos": [{"symbol": "BTCUSDT", "total_return": 0.05, "win_rate": 0.44}],
    "verdict": {"pass": False, "reason": "OOS 승률 목표 미달"},
}


class FakeLLM(LLMClient):
    """테스트용 LLM — 네트워크 없이 지정한 동작만 재현한다."""

    def __init__(self, *, enabled=True, text=None, error=None):
        super().__init__(Settings(_env_file=None))
        self._enabled = enabled
        self._text = text
        self._error = error
        self.prompts: list[str] = []

    @property
    def enabled(self) -> bool:  # type: ignore[override]
        return self._enabled

    async def complete(self, *, system, prompt, max_tokens=None):  # type: ignore[override]
        self.prompts.append(prompt)
        if self._error:
            raise self._error
        return self._text


# ── 1. opt-in: 키 없으면 완전 비활성 ────────────────────────────────────
async def test_disabled_without_key():
    """기본 설정(키 없음)에서는 enabled=False이고 해설도 나오지 않는다."""
    client = LLMClient(Settings(_env_file=None))
    assert client.enabled is False
    assert await narrate_research_report(1, LEADERBOARD, SUMMARY, client=client) is None
    assert await narrate_validation_report(1, VALIDATION_PAYLOAD, client=client) is None


async def test_enabled_flag_alone_is_not_enough():
    """llm_enabled만 켜고 키가 없으면 여전히 비활성 (실수로 켜도 안전)."""
    client = LLMClient(Settings(llm_enabled=True, _env_file=None))
    assert client.enabled is False


async def test_status_never_leaks_key():
    s = Settings(llm_enabled=True, anthropic_api_key="sk-ant-secret", _env_file=None)
    status = LLMClient(s).status()
    assert status == {
        "enabled": True,
        "configured": True,
        "model": s.llm_model,
        "effort": s.llm_effort,
    }
    assert "sk-ant-secret" not in str(status)


# ── 2. 리포트 동등성: 비활성이면 기존 마크다운과 바이트 동일 ─────────────
async def test_report_unchanged_when_llm_disabled(db):
    bus = EventBus(db)
    analyst = Analyst(bus, FakeLLM(enabled=False, text="해설"))
    report_id = await analyst.write_report(7, LEADERBOARD, SUMMARY, db)

    stored = db.execute("SELECT markdown FROM reports WHERE id = ?", (report_id,))[0]
    assert stored["markdown"] == generate_report(7, LEADERBOARD, SUMMARY)
    assert SECTION_TITLE not in stored["markdown"]


async def test_validation_report_unchanged_when_llm_disabled(db):
    bus = EventBus(db)
    analyst = Analyst(bus, FakeLLM(enabled=False))
    report_id = await analyst.write_validation_report(8, VALIDATION_PAYLOAD, db)

    stored = db.execute("SELECT markdown FROM reports WHERE id = ?", (report_id,))[0]
    assert stored["markdown"] == generate_validation_report(8, VALIDATION_PAYLOAD)


# ── 3. 활성 시 섹션이 붙는다 (기존 본문은 보존) ─────────────────────────
async def test_narration_appended_when_enabled(db):
    bus = EventBus(db)
    llm = FakeLLM(text="- 챔피언은 샤프 1.42로 안정적입니다.")
    analyst = Analyst(bus, llm)
    report_id = await analyst.write_report(9, LEADERBOARD, SUMMARY, db)

    markdown = db.execute("SELECT markdown FROM reports WHERE id = ?", (report_id,))[0][
        "markdown"
    ]
    base = generate_report(9, LEADERBOARD, SUMMARY)
    assert markdown.startswith(base)  # 결정론 본문은 손대지 않는다
    assert SECTION_TITLE in markdown
    assert DISCLAIMER in markdown
    assert "샤프 1.42로 안정적" in markdown


async def test_validation_narration_appended(db):
    bus = EventBus(db)
    llm = FakeLLM(text="- 학습 대비 OOS 성적이 크게 하락했습니다.")
    analyst = Analyst(bus, llm)
    report_id = await analyst.write_validation_report(10, VALIDATION_PAYLOAD, db)

    markdown = db.execute("SELECT markdown FROM reports WHERE id = ?", (report_id,))[0][
        "markdown"
    ]
    assert markdown.startswith(generate_validation_report(10, VALIDATION_PAYLOAD))
    assert "OOS 성적이 크게 하락" in markdown


# ── 4. 실패 무해: 예외/빈 응답에도 리포트는 저장된다 ────────────────────
async def test_llm_exception_does_not_break_report(db):
    bus = EventBus(db)
    analyst = Analyst(bus, FakeLLM(error=RuntimeError("boom")))
    report_id = await analyst.write_report(11, LEADERBOARD, SUMMARY, db)

    markdown = db.execute("SELECT markdown FROM reports WHERE id = ?", (report_id,))[0][
        "markdown"
    ]
    assert markdown == generate_report(11, LEADERBOARD, SUMMARY)
    warns = db.execute(
        "SELECT message FROM activity_log WHERE level = 'warn' AND agent = 'analyst'"
    )
    assert any("AI 해설 생략" in r["message"] for r in warns)


async def test_empty_response_omits_section(db):
    bus = EventBus(db)
    analyst = Analyst(bus, FakeLLM(text=None))
    report_id = await analyst.write_report(12, LEADERBOARD, SUMMARY, db)

    markdown = db.execute("SELECT markdown FROM reports WHERE id = ?", (report_id,))[0][
        "markdown"
    ]
    assert markdown == generate_report(12, LEADERBOARD, SUMMARY)


async def test_client_absorbs_timeout(monkeypatch):
    """SDK 호출이 늘어져도 예외 대신 None — 사이클이 멈추지 않는다."""
    s = Settings(
        llm_enabled=True,
        anthropic_api_key="sk-test",
        llm_timeout_seconds=0.01,
        _env_file=None,
    )
    client = LLMClient(s)

    class _Messages:
        async def create(self, **kwargs):
            await asyncio.sleep(1.0)

    monkeypatch.setattr(client, "_get_client", lambda: type("C", (), {"messages": _Messages()})())
    assert await client.complete(system="s", prompt="p") is None


async def test_client_absorbs_api_error(monkeypatch):
    s = Settings(llm_enabled=True, anthropic_api_key="sk-test", _env_file=None)
    client = LLMClient(s)

    class _Messages:
        async def create(self, **kwargs):
            raise RuntimeError("401 unauthorized")

    monkeypatch.setattr(client, "_get_client", lambda: type("C", (), {"messages": _Messages()})())
    assert await client.complete(system="s", prompt="p") is None


async def test_client_absorbs_refusal(monkeypatch):
    """안전 분류기 거부(stop_reason='refusal')는 content가 비어 있을 수 있다."""
    s = Settings(llm_enabled=True, anthropic_api_key="sk-test", _env_file=None)
    client = LLMClient(s)

    class _Msg:
        stop_reason = "refusal"
        content: list = []

    class _Messages:
        async def create(self, **kwargs):
            return _Msg()

    monkeypatch.setattr(client, "_get_client", lambda: type("C", (), {"messages": _Messages()})())
    assert await client.complete(system="s", prompt="p") is None


async def test_client_returns_text_blocks(monkeypatch):
    s = Settings(llm_enabled=True, anthropic_api_key="sk-test", _env_file=None)
    client = LLMClient(s)
    seen: dict = {}

    class _Block:
        type = "text"
        text = "  해설 본문  "

    class _Msg:
        stop_reason = "end_turn"
        content = [_Block()]

    class _Messages:
        async def create(self, **kwargs):
            seen.update(kwargs)
            return _Msg()

    monkeypatch.setattr(client, "_get_client", lambda: type("C", (), {"messages": _Messages()})())
    assert await client.complete(system="sys", prompt="p") == "해설 본문"
    # 읽기 전용 — 툴을 넘기지 않는다.
    assert "tools" not in seen
    assert seen["model"] == s.llm_model
    assert seen["output_config"] == {"effort": s.llm_effort}


async def test_missing_sdk_disables_call(monkeypatch):
    """anthropic 미설치 → None (설치 안 해도 시스템은 그대로 돈다)."""
    s = Settings(llm_enabled=True, anthropic_api_key="sk-test", _env_file=None)
    client = LLMClient(s)
    monkeypatch.setattr(client, "_get_client", lambda: None)
    assert await client.complete(system="s", prompt="p") is None


# ── 5. 프롬프트: 코드가 계산한 숫자만 들어간다 ──────────────────────────
def test_research_prompt_carries_computed_numbers():
    prompt = build_research_prompt(3, LEADERBOARD, SUMMARY)
    assert "사이클 #3" in prompt
    assert "ma_golden" in prompt and "fast=20" in prompt
    assert "1.42" in prompt  # 샤프
    assert "61.0%" in prompt  # 승률
    assert "short" in prompt  # 레짐
    assert "저신뢰" in prompt  # 태그 전달
    assert "ETHUSDT -4.0%" in prompt  # 상대강도


def test_validation_prompt_contrasts_train_and_oos():
    prompt = build_validation_prompt(4, VALIDATION_PAYLOAD)
    assert "학습(train)" in prompt and "검증(OOS)" in prompt
    assert "불합격" in prompt and "OOS 승률 목표 미달" in prompt
    assert "2024-09-01" in prompt


def test_prompts_tolerate_empty_payloads():
    """챔피언 없음 / 리더보드 비어있음 / 지표 None 에도 죽지 않는다."""
    assert "챔피언" in build_research_prompt(1, [], {})
    assert "판정" in build_validation_prompt(1, {})


async def test_narrator_passes_prompt_to_client():
    llm = FakeLLM(text="- 요약")
    section = await narrate_research_report(5, LEADERBOARD, SUMMARY, client=llm)
    assert section is not None and section.strip().startswith(SECTION_TITLE)
    assert llm.prompts and "사이클 #5" in llm.prompts[0]
