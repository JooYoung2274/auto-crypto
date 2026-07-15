"""Data Engineer 다온 (Daon): multi-timeframe OHLCV collection, funding-rate
history and market-regime refresh (스펙 §1.2·§3.1).

Every loader call runs in ``asyncio.to_thread`` so the event loop stays free;
network failures degrade to cache-only inside the loaders (offline-safe)."""
from __future__ import annotations

import asyncio

import pandas as pd

from ..data.loader import DataLoader
from .base import AgentBase

#: TF별 캐시 로드 상한 (완결 봉 기준) — 연구 사이클이 쓰는 히스토리 깊이.
TF_LIMITS = {"1d": 500, "4h": 2000, "15m": 6000, "5m": 6000, "1m": 3000}
DEFAULT_TF_LIMIT = 1500


class DataEngineer(AgentBase):
    id = "data"
    name = "다온"
    role = "Data Engineer"

    async def refresh_universe(
        self,
        loader: DataLoader,
        symbols: list[str],
        timeframes: list[str] | None = None,
    ) -> dict[str, int]:
        """Refresh the multi-TF OHLCV cache for every symbol (network
        failures fall back to cached data inside the loader). Returns
        symbol → new rows."""
        await self.set_state(
            "working", f"유니버스 {len(symbols)}심볼 멀티TF 데이터 최신화"
        )
        counts = await asyncio.to_thread(loader.refresh, symbols, timeframes)
        total = sum(counts.values())
        await self.log(
            f"데이터 최신화 완료 — {len(symbols)}심볼, 신규 {total}행",
            counts=counts,
        )
        return counts

    async def load_universe(
        self,
        loader: DataLoader,
        symbols: list[str],
        timeframes: list[str],
        required_tf: str | None = None,
    ) -> dict[str, dict[str, pd.DataFrame]]:
        """Load cached multi-TF OHLCV per symbol → ``{symbol: {tf: df}}``.

        Symbols missing the ``required_tf`` (execution timeframe) frame are
        dropped with an error log; the cycle continues with the rest."""
        data: dict[str, dict[str, pd.DataFrame]] = {}
        for symbol in symbols:
            frames: dict[str, pd.DataFrame] = {}
            for tf in timeframes:
                df = await asyncio.to_thread(
                    loader.get_ohlcv, symbol, tf, TF_LIMITS.get(tf, DEFAULT_TF_LIMIT)
                )
                if not df.empty:
                    frames[tf] = df
            if required_tf is not None and required_tf not in frames:
                await self.log(
                    f"{symbol} {required_tf} 데이터 없음 — 이번 사이클에서 제외",
                    level="error",
                    symbol=symbol,
                )
                continue
            if frames:
                data[symbol] = frames
        await self.set_state("idle")
        return data

    async def refresh_funding(
        self, funding_loader, symbols: list[str]
    ) -> dict[str, int]:
        """Refresh funding-rate history per symbol (0 rows offline — the
        backtest then falls back to the default 0.01%/8h rate)."""
        await self.set_state("working", "펀딩비 이력 갱신")
        counts: dict[str, int] = {}
        for symbol in symbols:
            counts[symbol] = await asyncio.to_thread(funding_loader.refresh, symbol)
        await self.log(
            f"펀딩 이력 갱신 — 신규 {sum(counts.values())}행", counts=counts
        )
        return counts

    async def refresh_regime(self, regime_service) -> str:
        """Recompute the daily market-regime proxy (스펙 §3.1) and return the
        current regime. Refresh failures degrade to the cached table."""
        await self.set_state("working", "시장 레짐 판정 (TOTAL2/3·도미넌스 프록시)")
        try:
            await asyncio.to_thread(regime_service.refresh)
        except Exception as exc:  # noqa: BLE001 — degrade to cache
            await self.log(f"레짐 갱신 실패 — 캐시 사용: {exc}", level="warning")
        regime = await asyncio.to_thread(regime_service.current)
        await self.log(f"시장 레짐 판정: {regime}", regime=regime)
        await self.set_state("idle")
        return regime
