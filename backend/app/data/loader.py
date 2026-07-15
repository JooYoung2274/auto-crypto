"""Binance USDT-M perpetual OHLCV loader: SQLite cache first, public fapi
klines REST (no API key — testnet-agnostic public data), cache-only as a
last resort when the network is down.

- Symbols are Binance perp symbols ('BTCUSDT'), timeframes '1m'..'1d'.
- Incomplete bars are always excluded (bar whose close_time > now).
- Fetched rows are upserted into ``ohlcv_cache (symbol, timeframe, ts)``
  with ``ts`` = bar open time in epoch ms.
- ``transport`` is injectable (httpx.MockTransport in tests — zero network).
"""
from __future__ import annotations

import logging
import time

import httpx
import pandas as pd

from ..db import Database
from .sources import get_market_source

logger = logging.getLogger(__name__)

BINANCE_FAPI_BASE = "https://fapi.binance.com"
KLINES_PATH = "/fapi/v1/klines"
MAX_KLINES_PER_REQUEST = 1500
MAX_FETCH_PAGES = 50
DEFAULT_LIMIT = 1500
DEFAULT_TIMEFRAMES = ("1d", "4h", "15m", "5m")

TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

_COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume"]


class DataLoader:
    def __init__(
        self,
        db: Database,
        transport: httpx.BaseTransport | None = None,
        base_url: str | None = None,
        timeout: float = 10.0,
        settings=None,
    ):
        self.db = db
        self._transport = transport
        self._timeout = timeout
        # exchange=='okx'면 OKX 소스, 아니면 None → 기존 Binance 경로 (바이트 호환).
        self._source = get_market_source(settings)
        self._base_url = base_url or (
            self._source.base_url if self._source is not None else BINANCE_FAPI_BASE
        )

    # -- time helper (overridable in tests) -----------------------------------
    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    # -- public API ------------------------------------------------------------
    def get_ohlcv(
        self, symbol: str, timeframe: str = "15m", limit: int = DEFAULT_LIMIT
    ) -> pd.DataFrame:
        """OHLCV DataFrame (DatetimeIndex = bar open time, UTC-naive; float
        columns open/high/low/close/volume/quote_volume), newest ``limit``
        completed bars. Cache-first; degrades to cache-only offline."""
        tf_ms = TIMEFRAME_MS.get(timeframe)
        if tf_ms is None:
            raise ValueError(f"unknown timeframe: {timeframe}")
        now = self._now_ms()
        last_complete_open = (now // tf_ms) * tf_ms - tf_ms

        cached = self._read_cache(symbol, timeframe, limit)
        if not cached.empty and _ts_ms(cached.index.max()) >= last_complete_open:
            return cached

        start_ms = None if cached.empty else _ts_ms(cached.index.max()) + tf_ms
        fetched = self._fetch(symbol, timeframe, start_ms, limit)
        if fetched is not None and not fetched.empty:
            # 미완성 봉 제외 — close_time이 아직 지나지 않은 봉은 버린다.
            fetched = fetched[fetched["close_time"] <= now]
            if not fetched.empty:
                self._upsert(symbol, timeframe, fetched)
        return self._read_cache(symbol, timeframe, limit)

    def refresh(
        self, symbols: list[str], timeframes: list[str] | None = None
    ) -> dict[str, int]:
        """Update the cache for each symbol across ``timeframes`` (default
        ('1d','4h','15m','5m')); returns symbol → newly cached row count.
        A bad symbol must never kill the cycle."""
        tfs = list(timeframes) if timeframes else list(DEFAULT_TIMEFRAMES)
        result: dict[str, int] = {}
        for symbol in symbols:
            before = self._row_count(symbol)
            for tf in tfs:
                try:
                    self.get_ohlcv(symbol, tf)
                except Exception:  # noqa: BLE001 — degrade, don't die
                    logger.exception("refresh failed for %s %s", symbol, tf)
            result[symbol] = self._row_count(symbol) - before
        return result

    # -- fetch -------------------------------------------------------------------
    def _fetch(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int | None,
        limit: int = DEFAULT_LIMIT,
    ) -> pd.DataFrame | None:
        """Fetch klines from the public fapi endpoint. With ``start_ms``
        paginates forward until caught up; without it returns the latest
        ``limit`` bars. Returns None on total failure (cache-only mode)."""
        if self._source is not None:
            # OKX 등 대체 소스: fetch를 위임 (transport/base_url은 호출 시점 조회).
            return self._source.fetch(
                symbol,
                timeframe,
                start_ms,
                limit,
                transport=self._transport,
                base_url=self._base_url,
                timeout=self._timeout,
            )
        tf_ms = TIMEFRAME_MS[timeframe]
        rows: list[list] = []
        try:
            with httpx.Client(
                base_url=self._base_url,
                transport=self._transport,
                timeout=self._timeout,
            ) as client:
                cursor = start_ms
                for _ in range(MAX_FETCH_PAGES):
                    params: dict = {
                        "symbol": symbol,
                        "interval": timeframe,
                        "limit": min(limit, MAX_KLINES_PER_REQUEST),
                    }
                    if cursor is not None:
                        params["startTime"] = cursor
                    resp = client.get(KLINES_PATH, params=params)
                    resp.raise_for_status()
                    batch = resp.json()
                    if not batch:
                        break
                    rows.extend(batch)
                    if cursor is None or len(batch) < params["limit"]:
                        break  # latest-window request, or caught up
                    cursor = int(batch[-1][0]) + tf_ms
        except Exception:  # noqa: BLE001
            logger.warning(
                "Binance klines fetch failed for %s %s; using cache only",
                symbol,
                timeframe,
            )
            if not rows:
                return None
        if not rows:
            return None
        return self._normalize(rows)

    @staticmethod
    def _normalize(rows: list[list]) -> pd.DataFrame:
        """Binance kline array → float OHLCV frame indexed by open time,
        with a working ``close_time`` (ms) column for completeness checks."""
        df = pd.DataFrame(
            [
                {
                    "ts": int(r[0]),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5]),
                    "close_time": int(r[6]),
                    "quote_volume": float(r[7]),
                }
                for r in rows
            ]
        )
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("ts"), unit="ms"))
        df.index.name = None
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df[_COLUMNS + ["close_time"]].dropna()

    # -- cache ---------------------------------------------------------------------
    def _read_cache(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        rows = self.db.execute(
            "SELECT ts, open, high, low, close, volume, quote_volume "
            "FROM ohlcv_cache WHERE symbol = ? AND timeframe = ? "
            "ORDER BY ts DESC LIMIT ?",
            (symbol, timeframe, int(limit)),
        )
        if not rows:
            return pd.DataFrame(columns=_COLUMNS, index=pd.DatetimeIndex([]))
        df = pd.DataFrame(rows[::-1])
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("ts"), unit="ms"))
        df.index.name = None
        return df[_COLUMNS].astype(float)

    def _upsert(self, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO ohlcv_cache "
            "(symbol, timeframe, ts, open, high, low, close, volume, quote_volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    symbol,
                    timeframe,
                    _ts_ms(idx),
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row["volume"]),
                    float(row.get("quote_volume", 0.0)),
                )
                for idx, row in df.iterrows()
            ],
        )

    def _row_count(self, symbol: str) -> int:
        rows = self.db.execute(
            "SELECT COUNT(*) AS n FROM ohlcv_cache WHERE symbol = ?", (symbol,)
        )
        return int(rows[0]["n"])


def _ts_ms(ts: pd.Timestamp) -> int:
    """Timestamp → epoch ms."""
    return int(pd.Timestamp(ts).value // 1_000_000)
