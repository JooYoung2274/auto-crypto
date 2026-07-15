"""시세 소스 추상화 — DataLoader/FundingLoader가 거래소별 공개 REST를
같은 정규화 프레임으로 뽑아 쓰게 하는 얇은 전략 계층.

- Binance는 DataLoader/FundingLoader 안에 원래 코드가 그대로 남아있다
  (바이트 호환). ``get_market_source``는 ``settings.exchange == 'okx'``일
  때만 :class:`~app.data.sources.okx.OKXSource`를 돌려주고, 그 외에는
  ``None``을 돌려줘 로더가 기존 Binance 경로를 탄다.
- 소스는 transport/base_url/timeout을 호출 시점에 로더로부터 받는다
  (테스트가 ``loader._transport``를 사후 교체해도 반영되도록).
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import httpx
import pandas as pd


class MarketSource(ABC):
    """거래소 공개 시세 소스 — klines/펀딩을 정규화 프레임/행으로 반환."""

    #: 이 거래소 공개 REST 기본 URL (로더가 base_url 미지정 시 기본값).
    base_url: str

    @abstractmethod
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
        """OHLCV를 Binance 로더와 동일한 스키마로 반환.

        컬럼 ``open/high/low/close/volume/quote_volume/close_time``, 인덱스는
        봉 open time(UTC-naive DatetimeIndex). ``start_ms``가 있으면 그 이후
        봉만, 없으면 최신 ``limit``봉. 총 실패 시 None(캐시-온리)."""

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
        """펀딩 이력을 ``{'fundingTime': ms, 'fundingRate': float}`` 행 리스트로
        반환 (FundingLoader의 Binance 업서트 코드와 키 호환). 오프라인 시 []."""
        raise NotImplementedError


def get_market_source(settings) -> MarketSource | None:
    """``settings.exchange``에 맞는 소스 — Binance면 None(기존 경로), OKX면 소스."""
    if settings is not None and getattr(settings, "exchange", "binance") == "okx":
        from .okx import OKXSource

        return OKXSource()
    return None


__all__ = ["MarketSource", "get_market_source"]
