"""Funding-rate history: Binance ``/fapi/v1/fundingRate`` fetch + SQLite
``funding_rates`` cache + range query with a default-rate fallback.

- ``refresh(symbol, ...)`` fetches history and upserts the cache; offline it
  degrades silently (returns 0 new rows, never raises).
- ``get_funding(symbol, start_ts, end_ts)`` reads the cache only; when no
  history covers the range it synthesizes the settings default rate
  (기본 0.01%/8h 근사 — 스펙 §4 펀딩) at each 8h settlement boundary.
- Timestamps are epoch ms; the returned series is indexed by UTC-naive
  DatetimeIndex so it aligns with loader OHLCV frames (each row applies at
  its own settlement ts — 룩어헤드 없음).
"""
from __future__ import annotations

import logging
import time

import httpx
import pandas as pd

from ..config import Settings
from ..db import Database
from .loader import BINANCE_FAPI_BASE
from .sources import get_market_source

logger = logging.getLogger(__name__)

FUNDING_PATH = "/fapi/v1/fundingRate"
FUNDING_INTERVAL_MS = 8 * 60 * 60 * 1000  # 8h settlement cadence
MAX_FUNDING_PER_REQUEST = 1000
MAX_FETCH_PAGES = 50
DEFAULT_FUNDING_RATE = 0.0001  # 0.01% / 8h


class FundingLoader:
    def __init__(
        self,
        db: Database,
        settings: Settings | None = None,
        transport: httpx.BaseTransport | None = None,
        base_url: str | None = None,
        timeout: float = 10.0,
    ):
        self.db = db
        self.settings = settings
        self._transport = transport
        self._timeout = timeout
        # exchange=='okx'면 OKX 소스로 펀딩 이력 fetch (아니면 Binance 경로).
        self._source = get_market_source(settings)
        self._base_url = base_url or (
            self._source.base_url if self._source is not None else BINANCE_FAPI_BASE
        )

    # -- time helper (overridable in tests) -----------------------------------
    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    @property
    def default_rate(self) -> float:
        if self.settings is not None:
            return float(self.settings.funding_default_rate)
        return DEFAULT_FUNDING_RATE

    # -- public API --------------------------------------------------------------
    def refresh(
        self, symbol: str, start_ts: int | None = None, end_ts: int | None = None
    ) -> int:
        """Fetch funding history for ``[start_ts, end_ts]`` (ms) and upsert
        into ``funding_rates``. Incremental by default: resumes after the
        newest cached row. Returns newly cached row count; 0 offline."""
        if start_ts is None:
            last = self.db.execute(
                "SELECT MAX(ts) AS ts FROM funding_rates WHERE symbol = ?",
                (symbol,),
            )
            start_ts = (last[0]["ts"] + 1) if last and last[0]["ts"] is not None else None
        if end_ts is None:
            end_ts = self._now_ms()

        rows: list[dict] = []
        if self._source is not None:
            # OKX 등 대체 소스: 펀딩 이력 fetch 위임 (fundingTime/fundingRate 키 호환).
            rows = self._source.fetch_funding(
                symbol,
                start_ts,
                end_ts,
                transport=self._transport,
                base_url=self._base_url,
                timeout=self._timeout,
            )
            if not rows:
                return 0
        else:
            try:
                with httpx.Client(
                    base_url=self._base_url,
                    transport=self._transport,
                    timeout=self._timeout,
                ) as client:
                    cursor = start_ts
                    for _ in range(MAX_FETCH_PAGES):
                        params: dict = {
                            "symbol": symbol,
                            "limit": MAX_FUNDING_PER_REQUEST,
                            "endTime": end_ts,
                        }
                        if cursor is not None:
                            params["startTime"] = cursor
                        resp = client.get(FUNDING_PATH, params=params)
                        resp.raise_for_status()
                        batch = resp.json()
                        if not batch:
                            break
                        rows.extend(batch)
                        if len(batch) < MAX_FUNDING_PER_REQUEST:
                            break
                        cursor = int(batch[-1]["fundingTime"]) + 1
            except Exception:  # noqa: BLE001 — offline degradation, cache-only
                logger.warning("funding fetch failed for %s; using cache only", symbol)
                if not rows:
                    return 0
        if not rows:
            return 0
        params_seq = [
            (symbol, int(r["fundingTime"]), float(r["fundingRate"])) for r in rows
        ]
        before = self._row_count(symbol)
        self.db.executemany(
            "INSERT OR REPLACE INTO funding_rates (symbol, ts, rate) "
            "VALUES (?, ?, ?)",
            params_seq,
        )
        return self._row_count(symbol) - before

    def get_funding(self, symbol: str, start_ts: int, end_ts: int) -> pd.Series:
        """Funding rates in ``[start_ts, end_ts]`` (epoch ms, inclusive) as a
        float Series named 'rate' with a UTC-naive DatetimeIndex.

        Cache-only (no network). When the cache has no row in the range, a
        default-rate series (settings ``funding_default_rate``) is generated
        at every 8h settlement boundary inside the range."""
        rows = self.db.execute(
            "SELECT ts, rate FROM funding_rates "
            "WHERE symbol = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            (symbol, int(start_ts), int(end_ts)),
        )
        if not rows:
            return self._default_series(start_ts, end_ts)
        series = pd.Series(
            [float(r["rate"]) for r in rows],
            index=pd.DatetimeIndex(
                pd.to_datetime([int(r["ts"]) for r in rows], unit="ms")
            ),
            name="rate",
            dtype=float,
        )
        series.index.name = None
        return series

    # -- helpers ----------------------------------------------------------------
    def _default_series(self, start_ts: int, end_ts: int) -> pd.Series:
        """Default-rate series at 8h UTC boundaries (00/08/16h) in range."""
        first = ((int(start_ts) + FUNDING_INTERVAL_MS - 1) // FUNDING_INTERVAL_MS) * (
            FUNDING_INTERVAL_MS
        )
        ts_list = list(range(first, int(end_ts) + 1, FUNDING_INTERVAL_MS))
        idx = pd.DatetimeIndex(pd.to_datetime(ts_list, unit="ms"))
        return pd.Series(self.default_rate, index=idx, name="rate", dtype=float)

    def _row_count(self, symbol: str) -> int:
        rows = self.db.execute(
            "SELECT COUNT(*) AS n FROM funding_rates WHERE symbol = ?", (symbol,)
        )
        return int(rows[0]["n"])
