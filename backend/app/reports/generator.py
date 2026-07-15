"""Deterministic markdown report generation (LLM narration is a future
extension point — the structure here is plain data-driven Korean text).

Coin edition: USDT 포맷, side/레버리지/TF/펀딩/청산 컬럼, 타임프레임별
성과 어트리뷰션, 레짐·상대강도 표기. 미정의 값은 항상 '–'."""
from __future__ import annotations

import datetime as dt


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value:.1%}"


def _num(value: float | None, digits: int = 2) -> str:
    return "–" if value is None else f"{value:.{digits}f}"


def _params_str(params: dict) -> str:
    return ", ".join(f"{k}={v:g}" for k, v in sorted(params.items()))


def _usdt(value: float | None) -> str:
    return "–" if value is None else f"{value:+,.2f} USDT"


def _price(value: float | None) -> str:
    return "–" if value is None else f"{value:,.2f}"


def _side(value: str | None) -> str:
    return {"long": "롱", "short": "숏"}.get(value or "", "–")


def _trade_table(trades: list[dict], title: str) -> list[str]:
    """Render a full trade log as a markdown table. Each trade shows its
    side/leverage/TF, actual entry/exit prices, return, USDT P/L, funding
    paid and holding hours; positions still open show '보유 중'."""
    lines = [
        "",
        f"## {title}",
        "",
        "| 심볼 | 방향 | 레버리지 | TF | 진입시각 | 진입가 | 청산시각 | 청산가 "
        "| 수익률 | 손익 | 펀딩 | 보유 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    if not trades:
        lines.append("| – | – | – | – | – | – | – | – | – | – | – | – |")
        return lines
    for t in trades:
        is_open = t.get("open")
        exit_ts = "보유 중" if is_open else t.get("exit_ts", "–")
        exit_price = "–" if is_open else _price(t.get("exit_price"))
        ret = t.get("net_ret")
        holding = t.get("holding_hours")
        lines.append(
            f"| {t['symbol']} | {_side(t.get('side'))} | "
            f"x{t.get('leverage', '–')} | {t.get('timeframe', '–')} | "
            f"{t.get('entry_ts', '–')} | {_price(t.get('entry_price'))} | "
            f"{exit_ts} | {exit_price} | "
            f"{'–' if ret is None else f'{ret:+.1%}'} | "
            f"{_usdt(t.get('pnl'))} | {_usdt(t.get('funding_paid'))} | "
            f"{'–' if holding is None else f'{holding:.1f}h'} |"
        )
    return lines


def _tf_attribution(per_symbol: list[dict], trades: list[dict]) -> list[str]:
    """타임프레임별 성과 어트리뷰션 — 챔피언 거래를 TF로 묶어 손익/펀딩 집계."""
    lines = [
        "",
        "## 타임프레임별 성과",
        "",
        "| TF | 거래수 | 손익 | 펀딩 지불 | 승률 |",
        "|---|---|---|---|---|",
    ]
    by_tf: dict[str, list[dict]] = {}
    for t in trades:
        by_tf.setdefault(str(t.get("timeframe", "–")), []).append(t)
    if not by_tf:
        lines.append("| – | – | – | – | – |")
        return lines
    for tf in sorted(by_tf):
        rows = by_tf[tf]
        pnls = [t.get("pnl") for t in rows if t.get("pnl") is not None]
        fundings = [
            t.get("funding_paid") for t in rows if t.get("funding_paid") is not None
        ]
        wins = [t for t in rows if (t.get("net_ret") or 0) > 0]
        win_rate = len(wins) / len(rows) if rows else None
        lines.append(
            f"| {tf} | {len(rows)} | {_usdt(sum(pnls) if pnls else None)} | "
            f"{_usdt(sum(fundings) if fundings else None)} | {_pct(win_rate)} |"
        )
    return lines


def generate_report(cycle_id: int, leaderboard: list[dict], summary: dict) -> str:
    """Build the research cycle's markdown report.

    ``leaderboard`` rows follow the leaderboard shape
    (strategy_id/template/params/avg_metrics/low_confidence/status);
    ``summary`` carries cycle stats (candidates/passed/rejected/regime/
    symbol_ranking/champion/champion_trades...).
    """
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = [
        f"# 전략 발굴 리포트 — 사이클 #{cycle_id}",
        "",
        f"생성: {now} · 작성: 하나 (Analyst)",
        "",
        "## 사이클 요약",
        "",
        f"- 후보 전략: {summary.get('candidates', 0)}개",
        f"- 리스크 통과: {summary.get('passed', 0)}개 / "
        f"탈락: {summary.get('rejected', 0)}개",
        f"- 유니버스: {', '.join(summary.get('universe', []))}",
        f"- 시장 레짐: {summary.get('regime', '–')}",
    ]

    ranking = summary.get("symbol_ranking") or []
    if ranking:
        rs = ", ".join(
            f"{r['symbol']} {r['relative']:+.1%}" for r in ranking
        )
        lines.append(f"- 상대강도 (BTC 대비): {rs}")

    champion = summary.get("champion")
    if champion:
        m = champion.get("avg_metrics", {})
        liq = m.get("liquidation_count", 0)
        lines += [
            "",
            "## 챔피언 전략",
            "",
            f"**{champion['template']}** ({_params_str(champion.get('params', {}))})",
            "",
            f"- 샤프: {_num(m.get('sharpe'))} · 승률: {_pct(m.get('win_rate'))} · "
            f"MDD: {_pct(m.get('mdd'))}",
            f"- 총수익: {_pct(m.get('total_return'))} · CAGR: {_pct(m.get('cagr'))} · "
            f"PF: {_num(m.get('profit_factor'))} · 거래수: {m.get('trade_count', 0)}",
            f"- 펀딩 지불: {_usdt(m.get('funding_paid'))} · "
            f"수수료: {_usdt(m.get('fee_paid'))} · 강제 청산: {liq}회",
        ]
    else:
        lines += ["", "## 챔피언 전략", "", "이번 사이클에서는 챔피언이 선정되지 않았습니다."]

    lines += [
        "",
        "## 리더보드",
        "",
        "| 순위 | 전략 | 파라미터 | 승률 | 샤프 | MDD | CAGR | PF | 거래수 "
        "| 펀딩 | 청산 | 비고 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for rank, row in enumerate(leaderboard[:10], 1):
        m = row.get("avg_metrics", {})
        note_parts = []
        if row.get("status") == "champion":
            note_parts.append("챔피언")
        if row.get("low_confidence"):
            note_parts.append("저신뢰")
        if row.get("low_activity"):
            note_parts.append("저활동")
        lines.append(
            f"| {rank} | {row['template']} | {_params_str(row.get('params', {}))} | "
            f"{_pct(m.get('win_rate'))} | {_num(m.get('sharpe'))} | "
            f"{_pct(m.get('mdd'))} | {_pct(m.get('cagr'))} | "
            f"{_num(m.get('profit_factor'))} | {m.get('trade_count', 0)} | "
            f"{_usdt(m.get('funding_paid'))} | {m.get('liquidation_count', 0)}회 | "
            f"{' · '.join(note_parts)} |"
        )
    if not leaderboard:
        lines.append("| – | 통과 전략 없음 | | | | | | | | | | |")

    low_conf_count = sum(1 for r in leaderboard if r.get("low_confidence"))
    lines += ["", "## 코멘트", ""]
    if leaderboard:
        top = leaderboard[0]
        tm = top.get("avg_metrics", {})
        lines.append(
            f"- 최상위 전략은 {top['template']} 계열로, 평균 샤프 "
            f"{_num(tm.get('sharpe'))}·MDD {_pct(tm.get('mdd'))}을 기록했습니다."
        )
    if low_conf_count:
        lines.append(
            f"- 거래 표본이 부족한 저신뢰 전략 {low_conf_count}개는 랭킹에서 "
            "제외 대상으로 태깅했습니다 (과적합 방지)."
        )
    rejected = summary.get("rejected", 0)
    if rejected:
        lines.append(
            f"- 탈락 {rejected}개의 사유(MDD·강제 청산·펀딩 드래그·활동성)는 "
            "Strategist에게 피드백되어 다음 사이클 탐색 방향에 반영됩니다."
        )
    if not leaderboard and not rejected:
        lines.append("- 유효한 백테스트 결과가 없어 다음 사이클에서 재탐색합니다.")

    # Champion's full trade log (USDT P/L per trade) + per-TF attribution.
    if champion:
        trades = summary.get("champion_trades") or []
        lines += _trade_table(trades, "챔피언 거래 내역")
        lines += _tf_attribution(summary.get("champion_per_symbol") or [], trades)

    return "\n".join(lines) + "\n"


def _row(label: str, m: dict | None) -> str:
    if not m:
        return f"| {label} | – | – | – | – | – | – | – |"
    liq = m.get("liquidation_count")
    return (
        f"| {label} | {_pct(m.get('total_return'))} | {_pct(m.get('win_rate'))} | "
        f"{_pct(m.get('mdd'))} | {_num(m.get('sharpe'))} | {m.get('trade_count', 0)} | "
        f"{_usdt(m.get('funding_paid'))} | {'–' if liq is None else f'{liq}회'} |"
    )


def generate_validation_report(cycle_id: int, payload: dict) -> str:
    """Walk-forward validation report (validate 사이클).

    ``payload`` carries the train/test window timestamps, the frozen train
    champion, its train vs out-of-sample (OOS) aggregate metrics (incl 펀딩·
    청산), per-symbol OOS results and the pass/fail verdict.
    """
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    champion = payload.get("champion")
    verdict = payload.get("verdict", {})
    lines: list[str] = [
        f"# 워크포워드 검증 리포트 — 사이클 #{cycle_id}",
        "",
        f"생성: {now} · 작성: 하나 (Analyst)",
        "",
        "## 구간",
        "",
        f"- 학습 구간: {payload.get('train_start', '–')} ~ {payload.get('train_end', '–')}",
        f"- 검증 구간(OOS): {payload.get('test_start', '–')} ~ {payload.get('test_end', '–')}",
        f"- 검증 심볼: {', '.join(payload.get('symbols', []))}",
        f"- 시장 레짐: {payload.get('regime', '–')}",
    ]
    skipped = payload.get("skipped") or []
    if skipped:
        lines.append(f"- 데이터 부족으로 제외: {', '.join(skipped)}")

    lines += ["", "## 학습 챔피언", ""]
    if champion:
        lines.append(
            f"**{champion['template']}** ({_params_str(champion.get('params', {}))})"
        )
    else:
        lines.append("학습 구간에서 통과한 챔피언 전략이 없습니다.")

    lines += [
        "",
        "## 학습 vs 검증(OOS) 성적",
        "",
        "| 구간 | 총수익 | 승률 | MDD | 샤프 | 거래수 | 펀딩 지불 | 강제 청산 |",
        "|---|---|---|---|---|---|---|---|",
        _row("학습(train)", payload.get("train_metrics")),
        _row("검증(OOS)", payload.get("oos_metrics")),
    ]

    per_symbol = payload.get("per_symbol_oos") or []
    lines += [
        "",
        "## 심볼별 검증(OOS) 결과",
        "",
        "| 심볼 | 총수익 | 승률 | MDD | 샤프 | 거래수 | 펀딩 지불 | 강제 청산 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    if per_symbol:
        for pt in per_symbol:
            lines.append(_row(pt.get("symbol", "–"), pt))
    else:
        lines.append("| – | – | – | – | – | – | – | – |")

    # Full OOS trade log with actual entry/exit prices and per-trade USDT P/L.
    lines += _trade_table(payload.get("oos_trades") or [], "검증(OOS) 거래 내역")

    passed = bool(verdict.get("pass"))
    lines += [
        "",
        "## 판정",
        "",
        f"**{'합격' if passed else '불합격'}** — {verdict.get('reason', '')}",
    ]
    return "\n".join(lines) + "\n"
