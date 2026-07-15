"""OKX 공개 시세 소스 — USDT 무기한(SWAP) klines·펀딩 이력.

- klines: ``GET /api/v5/market/candles`` (최근분) + ``/api/v5/market/history-candles``
  (더 오래된 페이지). 응답은 최신순, ``[ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]``.
  Binance 로더와 동일 스키마로 정규화 (close_time = ts + tf − 1, 미완성 봉 배제는
  로더가 close_time으로 처리 — 의미 동일).
- 펀딩: ``GET /api/v5/public/funding-rate-history`` (최신순, after 커서 페이지네이션).
- 심볼 매핑 ``BTCUSDT`` ↔ ``BTC-USDT-SWAP``, TF 매핑 15m→15m/4h→4H/1d→1Dutc.

transport/base_url/timeout은 로더가 호출 시점에 넘겨준다 (오프라인 MockTransport
테스트 및 ``loader._transport`` 사후 교체 호환).
"""
from __future__ import annotations

import logging

import httpx
import pandas as pd

from . import MarketSource

logger = logging.getLogger(__name__)

OKX_BASE = "https://www.okx.com"
CANDLES_PATH = "/api/v5/market/candles"
HISTORY_CANDLES_PATH = "/api/v5/market/history-candles"
FUNDING_HISTORY_PATH = "/api/v5/public/funding-rate-history"

#: OKX 캔들/펀딩 페이지 상한 (candles 300, funding-history 100).
MAX_CANDLES_PER_REQUEST = 300
MAX_FUNDING_PER_REQUEST = 100
MAX_FETCH_PAGES = 50

#: 실행/멀티 TF → OKX bar 문자열. 일봉은 UTC 정렬(1Dutc).
_BAR_MAP: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1H",
    "4h": "4H",
    "1d": "1Dutc",
}

_TF_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

_COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume"]


def to_okx_symbol(symbol: str) -> str:
    """``BTCUSDT`` → ``BTC-USDT-SWAP`` (USDT 무기한)."""
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}-USDT-SWAP"
    if symbol.endswith("USD"):
        return f"{symbol[:-3]}-USD-SWAP"
    return symbol


def from_okx_symbol(inst_id: str) -> str:
    """``BTC-USDT-SWAP`` → ``BTCUSDT``."""
    parts = inst_id.split("-")
    if len(parts) >= 2:
        return f"{parts[0]}{parts[1]}"
    return inst_id


def to_okx_bar(timeframe: str) -> str:
    bar = _BAR_MAP.get(timeframe)
    if bar is None:
        raise ValueError(f"unknown OKX timeframe: {timeframe}")
    return bar


class OKXSource(MarketSource):
    base_url = OKX_BASE

    def fetch(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int | None,
        limit: int,
        *,
        transport: httpx.BaseTransport | None,
        base_url: str,
        timeout: float,
    ) -> pd.DataFrame | None:
        inst_id = to_okx_symbol(symbol)
        bar = to_okx_bar(timeframe)
        cap = min(limit, MAX_CANDLES_PER_REQUEST)
        rows: list[list] = []
        try:
            with httpx.Client(
                base_url=base_url, transport=transport, timeout=timeout
            ) as client:
                after: str | None = None  # after= → 이보다 오래된 봉 (최신→과거 페이지)
                for _ in range(MAX_FETCH_PAGES):
                    # 최신 페이지는 candles, 과거 페이지는 history-candles.
                    path = CANDLES_PATH if after is None else HISTORY_CANDLES_PATH
                    params: dict = {"instId": inst_id, "bar": bar, "limit": cap}
                    if after is not None:
                        params["after"] = after
                    resp = client.get(path, params=params)
                    resp.raise_for_status()
                    batch = resp.json().get("data", [])
                    if not batch:
                        break
                    rows.extend(batch)
                    oldest_ts = int(batch[-1][0])  # 응답은 최신순 → 마지막이 가장 오래됨
                    if start_ms is not None:
                        if oldest_ts <= start_ms:
                            break
                    elif len(rows) >= limit:
                        break
                    if len(batch) < cap:
                        break
                    after = str(oldest_ts)
        except Exception:  # noqa: BLE001 — 오프라인 저하, 캐시-온리
            logger.warning(
                "OKX candles fetch failed for %s %s; using cache only", symbol, timeframe
            )
            if not rows:
                return None
        if not rows:
            return None
        df = self._normalize(rows, _TF_MS[timeframe])
        if start_ms is not None:
            df = df[df.index.map(lambda ix: int(ix.value // 1_000_000)) >= start_ms]
        else:
            df = df.iloc[-limit:]
        return df if not df.empty else None

    @staticmethod
    def _normalize(rows: list[list], tf_ms: int) -> pd.DataFrame:
        """OKX 캔들 배열 → Binance 로더와 동일 스키마 프레임 (close_time 계산)."""
        df = pd.DataFrame(
            [
                {
                    "ts": int(r[0]),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    # SWAP: volCcy(=계약수×ctVal) = 기초자산 수량 → Binance volume과 비교 가능.
                    "volume": float(r[6]),
                    "close_time": int(r[0]) + tf_ms - 1,
                    "quote_volume": float(r[7]),
                }
                for r in rows
            ]
        )
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("ts"), unit="ms"))
        df.index.name = None
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df[_COLUMNS + ["close_time"]].dropna()

    def fetch_funding(
        self,
        symbol: str,
        start_ts: int | None,
        end_ts: int,
        *,
        transport: httpx.BaseTransport | None,
        base_url: str,
        timeout: float,
    ) -> list[dict]:
        inst_id = to_okx_symbol(symbol)
        out: list[dict] = []
        try:
            with httpx.Client(
                base_url=base_url, transport=transport, timeout=timeout
            ) as client:
                after: str | None = None
                for _ in range(MAX_FETCH_PAGES):
                    params: dict = {"instId": inst_id, "limit": MAX_FUNDING_PER_REQUEST}
                    if after is not None:
                        params["after"] = after
                    resp = client.get(FUNDING_HISTORY_PATH, params=params)
                    resp.raise_for_status()
                    batch = resp.json().get("data", [])
                    if not batch:
                        break
                    oldest_ts = int(batch[-1]["fundingTime"])  # 최신순
                    for r in batch:
                        ts = int(r["fundingTime"])
                        if ts > end_ts:
                            continue
                        if start_ts is not None and ts < start_ts:
                            continue
                        out.append(
                            {"fundingTime": ts, "fundingRate": float(r["fundingRate"])}
                        )
                    if start_ts is not None and oldest_ts <= start_ts:
                        break
                    if len(batch) < MAX_FUNDING_PER_REQUEST:
                        break
                    after = str(oldest_ts)
        except Exception:  # noqa: BLE001 — 오프라인 저하, 캐시-온리
            logger.warning("OKX funding fetch failed for %s; using cache only", symbol)
        return out
