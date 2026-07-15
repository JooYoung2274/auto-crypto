"""Strategist 세라 (Sera): regime-aware candidate proposal + relative-strength
symbol ranking (규칙 §4-4).

Evolutionary search with per-template niching: mutations concentrate on the
global champion, but each template's best individual (elite) also gets a
mutation share — otherwise non-champion templates would only ever receive
random draws and never get refined.
"""
from __future__ import annotations

import random

import pandas as pd

from ..strategies.base import StrategySpec
from ..strategies.registry import mutate, random_candidates
from .base import AgentBase

EXPLOITATION_RATIO = 0.7  # champion share when no elites are available
CHAMPION_RATIO = 0.4  # champion share when elites participate
ELITE_RATIO = 0.3  # spread round-robin across per-template elites

#: 상대강도 비교 기간 (일봉 기준, 규칙 §4-4).
RS_LOOKBACK_DAYS = 30
BTC_SYMBOL = "BTCUSDT"


class Strategist(AgentBase):
    id = "strategist"
    name = "세라"
    role = "Strategist"

    async def rank_symbols(
        self,
        daily_closes: dict[str, pd.Series],
        lookback: int = RS_LOOKBACK_DAYS,
    ) -> list[dict]:
        """상대강도 종목 선정 (규칙 §4-4): 유니버스 동시 비교 — BTC 대비
        상대 수익률이 높은(덜 빠지는) 심볼 = 롱 후보, 낮은(더 빠지는) 심볼
        = 숏 후보. Returns ``[{symbol, return, relative}, ...]`` sorted by
        relative strength descending."""
        await self.set_state("working", "상대강도 심볼 랭킹")
        returns: dict[str, float] = {}
        for symbol, closes in daily_closes.items():
            c = closes.dropna()
            if len(c) < 2:
                continue
            window = c.iloc[-min(int(lookback), len(c)):]
            if float(window.iloc[0]) == 0.0:
                continue
            returns[symbol] = float(window.iloc[-1] / window.iloc[0] - 1.0)
        btc_ret = returns.get(BTC_SYMBOL, 0.0)
        ranked = [
            {"symbol": s, "return": r, "relative": r - btc_ret}
            for s, r in returns.items()
        ]
        ranked.sort(key=lambda row: row["relative"], reverse=True)
        if ranked:
            txt = ", ".join(f"{r['symbol']} {r['relative']:+.1%}" for r in ranked)
            await self.log(
                f"상대강도 랭킹 (BTC 대비 {lookback}일) — {txt} "
                f"(상위=롱 후보 / 하위=숏 후보)",
                ranking=ranked,
            )
        else:
            await self.log("상대강도 랭킹 불가 — 일봉 데이터 부족", level="warning")
        await self.set_state("idle")
        return ranked

    async def propose(
        self,
        n: int,
        champion: StrategySpec | None,
        rng: random.Random,
        elites: list[StrategySpec] | None = None,
    ) -> list[StrategySpec]:
        await self.set_state("working", f"전략 후보 {n}개 생성")
        elites = [e for e in (elites or []) if champion is None or e.template != champion.template]
        if champion is None:
            specs = random_candidates(n, rng)
            await self.log(f"후보 {n}개 생성 (전부 랜덤 탐색)", candidates=n)
        elif not elites:
            n_mutate = round(n * EXPLOITATION_RATIO)
            specs = [mutate(champion, rng) for _ in range(n_mutate)]
            specs += random_candidates(n - n_mutate, rng)
            await self.log(
                f"후보 {n}개 생성 — 챔피언 {champion.id_key()} 변이 {n_mutate}개 + "
                f"랜덤 {n - n_mutate}개",
                candidates=n,
                mutated=n_mutate,
                champion=champion.id_key(),
            )
        else:
            n_champ = round(n * CHAMPION_RATIO)
            n_elite = round(n * ELITE_RATIO)
            specs = [mutate(champion, rng) for _ in range(n_champ)]
            for i in range(n_elite):  # round-robin across template elites
                specs.append(mutate(elites[i % len(elites)], rng))
            specs += random_candidates(n - len(specs), rng)
            await self.log(
                f"후보 {n}개 생성 — 챔피언 변이 {n_champ}개 + 템플릿 엘리트 "
                f"{len(elites)}종 변이 {n_elite}개 + 랜덤 {n - n_champ - n_elite}개",
                candidates=n,
                mutated=n_champ,
                elite_mutated=n_elite,
                elites=[e.id_key() for e in elites],
                champion=champion.id_key(),
            )
        await self.set_state("idle")
        return specs

    async def receive_feedback(self, rejected: list[tuple[str, str]]) -> None:
        """Log rejection reasons; they steer the next cycle's search."""
        await self.set_state("working", "탈락 사유 검토")
        if rejected:
            await self.log(
                f"탈락 {len(rejected)}건 피드백 수신 — 다음 사이클 탐색에 반영",
                rejected=[{"strategy": key, "reason": r} for key, r in rejected[:20]],
            )
        else:
            await self.log("탈락 전략 없음 — 현재 탐색 방향 유지")
        await self.set_state("idle")
