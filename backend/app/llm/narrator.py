"""리포트 해설 생성 — 기능 #1 (README "AI(LLM) 기능 — 기획").

결정론 리포트(`reports/generator.py`)가 만든 표 아래에 붙는 **해설 섹션**만
담당한다. 규칙:

- 숫자는 전부 코드가 계산해서 프롬프트에 넣는다. LLM은 **새 수치를 만들지
  않고** 주어진 값만 해석한다.
- 실패(비활성·타임아웃·오류)하면 `None`을 돌려주고, 호출부는 섹션 없이
  기존 리포트를 그대로 저장한다.
- 매매 판단에는 절대 반영되지 않는다. 이 텍스트를 읽는 코드는 없다.
"""
from __future__ import annotations

from ..config import Settings
from .client import LLMClient

SECTION_TITLE = "## 🤖 AI 해설"
DISCLAIMER = (
    "> 이 섹션은 위 표의 수치를 언어모델이 요약한 것입니다. 수치 자체는 "
    "백테스트 엔진이 계산했으며, 해설은 매매 판단에 사용되지 않습니다."
)

_SYSTEM = """당신은 암호화폐 선물 자동매매 시스템의 리포트를 요약하는 애널리스트입니다.

규칙:
- 한국어로, 마크다운 불릿 3~5개만 씁니다. 제목·표·코드블록은 쓰지 않습니다.
- 주어진 수치만 인용합니다. 새로운 숫자를 계산하거나 추정하지 않습니다.
- "무엇이 좋았고/나빴는지"와 "다음 사이클에서 확인할 점"에 집중합니다.
- 투자 권유·수익 보장·가격 예측은 하지 않습니다.
- 표에 이미 있는 내용을 그대로 나열하지 말고, 수치 사이의 관계를 설명합니다."""


def _fmt(value, kind: str = "num") -> str:
    if value is None:
        return "–"
    if kind == "pct":
        return f"{value:.1%}"
    if kind == "usdt":
        return f"{value:+,.2f} USDT"
    return f"{value:.2f}"


def _params(params: dict) -> str:
    return ", ".join(f"{k}={v:g}" for k, v in sorted((params or {}).items()))


def _metrics_line(label: str, m: dict | None) -> str:
    m = m or {}
    return (
        f"- {label}: 총수익 {_fmt(m.get('total_return'), 'pct')} · "
        f"승률 {_fmt(m.get('win_rate'), 'pct')} · MDD {_fmt(m.get('mdd'), 'pct')} · "
        f"샤프 {_fmt(m.get('sharpe'))} · PF {_fmt(m.get('profit_factor'))} · "
        f"거래수 {m.get('trade_count', 0)} · "
        f"펀딩 {_fmt(m.get('funding_paid'), 'usdt')} · "
        f"강제청산 {m.get('liquidation_count', 0)}회"
    )


def _section(text: str) -> str:
    return f"\n{SECTION_TITLE}\n\n{text.strip()}\n\n{DISCLAIMER}\n"


# ── 프롬프트 빌더 (순수 함수 — 테스트 대상) ─────────────────────────────
def build_research_prompt(cycle_id: int, leaderboard: list[dict], summary: dict) -> str:
    """전략 발굴 사이클 해설용 프롬프트. 이미 계산된 값만 담는다."""
    lines = [
        f"전략 발굴 사이클 #{cycle_id} 결과입니다.",
        "",
        f"- 후보 전략 {summary.get('candidates', 0)}개 중 "
        f"리스크 통과 {summary.get('passed', 0)}개 / 탈락 {summary.get('rejected', 0)}개",
        f"- 시장 레짐: {summary.get('regime', '–')}",
        f"- 유니버스: {', '.join(summary.get('universe', []) or []) or '–'}",
    ]
    ranking = summary.get("symbol_ranking") or []
    if ranking:
        lines.append(
            "- 상대강도(BTC 대비): "
            + ", ".join(f"{r['symbol']} {r['relative']:+.1%}" for r in ranking)
        )

    champion = summary.get("champion")
    lines += ["", "챔피언 전략:"]
    if champion:
        lines.append(f"- {champion.get('template')} ({_params(champion.get('params', {}))})")
        lines.append(_metrics_line("성과", champion.get("avg_metrics")))
    else:
        lines.append("- 없음 (이번 사이클에서 챔피언 미선정)")

    lines += ["", "리더보드 상위:"]
    if leaderboard:
        for rank, row in enumerate(leaderboard[:5], 1):
            tags = []
            if row.get("low_confidence"):
                tags.append("저신뢰")
            if row.get("low_activity"):
                tags.append("저활동")
            suffix = f" [{'·'.join(tags)}]" if tags else ""
            lines.append(
                f"{rank}. {row.get('template')} ({_params(row.get('params', {}))})"
                f"{suffix}"
            )
            lines.append("  " + _metrics_line("성과", row.get("avg_metrics")))
    else:
        lines.append("- 통과 전략 없음")

    lines += ["", "위 수치를 근거로 해설을 작성하세요."]
    return "\n".join(lines)


def build_validation_prompt(cycle_id: int, payload: dict) -> str:
    """워크포워드 검증 사이클 해설용 프롬프트."""
    champion = payload.get("champion")
    verdict = payload.get("verdict", {}) or {}
    lines = [
        f"워크포워드 검증 사이클 #{cycle_id} 결과입니다.",
        "",
        f"- 학습 구간: {payload.get('train_start', '–')} ~ {payload.get('train_end', '–')}",
        f"- 검증 구간(OOS): {payload.get('test_start', '–')} ~ {payload.get('test_end', '–')}",
        f"- 검증 심볼: {', '.join(payload.get('symbols', []) or []) or '–'}",
        f"- 시장 레짐: {payload.get('regime', '–')}",
        "",
        "학습 챔피언: "
        + (
            f"{champion.get('template')} ({_params(champion.get('params', {}))})"
            if champion
            else "없음"
        ),
        "",
        _metrics_line("학습(train)", payload.get("train_metrics")),
        _metrics_line("검증(OOS)", payload.get("oos_metrics")),
    ]

    per_symbol = payload.get("per_symbol_oos") or []
    if per_symbol:
        lines += ["", "심볼별 OOS:"]
        lines += [_metrics_line(p.get("symbol", "–"), p) for p in per_symbol]

    lines += [
        "",
        f"판정: {'합격' if verdict.get('pass') else '불합격'} — {verdict.get('reason', '')}",
        "",
        "학습 대비 검증 성적의 괴리(과적합 여부)에 특히 주목해서 해설을 작성하세요.",
    ]
    return "\n".join(lines)


# ── 공개 API ────────────────────────────────────────────────────────────
async def narrate_research_report(
    cycle_id: int,
    leaderboard: list[dict],
    summary: dict,
    *,
    client: LLMClient | None = None,
    settings: Settings | None = None,
) -> str | None:
    """전략 발굴 리포트에 덧붙일 해설 섹션. 비활성/실패 시 None."""
    llm = client or LLMClient(settings)
    if not llm.enabled:
        return None
    text = await llm.complete(
        system=_SYSTEM,
        prompt=build_research_prompt(cycle_id, leaderboard, summary),
    )
    return _section(text) if text else None


async def narrate_validation_report(
    cycle_id: int,
    payload: dict,
    *,
    client: LLMClient | None = None,
    settings: Settings | None = None,
) -> str | None:
    """검증 리포트에 덧붙일 해설 섹션. 비활성/실패 시 None."""
    llm = client or LLMClient(settings)
    if not llm.enabled:
        return None
    text = await llm.complete(
        system=_SYSTEM,
        prompt=build_validation_prompt(cycle_id, payload),
    )
    return _section(text) if text else None
