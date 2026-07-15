"""Shared fixtures: deterministic synthetic multi-timeframe perp OHLCV
(coarse frames are always resampled FROM the same fine base series so
cross-TF consistency holds), Binance kline payload builders, funding /
regime cache seeders, temp Database, Settings. All offline — zero network."""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd
import pytest

from app.config import Settings
from app.db import Database

try:  # 코인 비용 모델(B웨이브)로 전환되면 자동 사용, 아니면 기존 모델 유지
    from app.backtest.costs import PerpCostModel as _CostModel

    _ZERO_COST_KW = {"maker_fee": 0.0, "taker_fee": 0.0, "slippage": 0.0}
except ImportError:  # pragma: no cover — pre-transform fork state
    from app.backtest.costs import KoreaCostModel as _CostModel

    _ZERO_COST_KW = {"commission": 0.0, "sell_tax": 0.0, "slippage": 0.0}

# -- timeframe helpers ---------------------------------------------------------
TF_RULES = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1D",
}
TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
_OHLCV_AGG = {
    "open": "first",
    "high": "max",
    "low": "min",
    "close": "last",
    "volume": "sum",
    "quote_volume": "sum",
}


def ts_ms(ts) -> int:
    """Timestamp → epoch ms."""
    return int(pd.Timestamp(ts).value // 1_000_000)


# -- synthetic data ------------------------------------------------------------
def make_synthetic_ohlcv(
    n: int = 300,
    seed: int = 42,
    start: str = "2023-01-02",
    limit_up_at: int | None = 150,
    limit_down_at: int | None = 200,
) -> pd.DataFrame:
    """Legacy daily synthetic OHLCV (sine + trend + seeded noise) kept for
    tests that predate the crypto transform."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start=start, periods=n)
    t = np.arange(n)
    close = 10_000 + 40.0 * t + 1_500.0 * np.sin(t / 12.0) + rng.normal(0, 60, n)
    close = np.maximum(close, 1_000.0)

    open_ = np.empty(n)
    open_[0] = close[0]
    open_[1:] = close[:-1] * (1.0 + rng.normal(0, 0.004, n - 1))

    if limit_up_at is not None and 0 < limit_up_at < n:
        open_[limit_up_at] = close[limit_up_at - 1] * 1.30
        close[limit_up_at] = open_[limit_up_at] * 0.99
    if limit_down_at is not None and 0 < limit_down_at < n:
        open_[limit_down_at] = close[limit_down_at - 1] * 0.70
        close[limit_down_at] = open_[limit_down_at] * 1.01

    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0, 0.003, n)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0, 0.003, n)))
    volume = rng.integers(100_000, 1_000_000, n).astype(float)

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def make_perp_ohlcv(
    n: int = 2000,
    seed: int = 42,
    start: str = "2024-01-01",
    freq: str = "5min",
    base_price: float = 100.0,
    drift: float = 0.00002,
    vol: float = 0.002,
) -> pd.DataFrame:
    """Deterministic continuous perp OHLCV: geometric random walk, gapless
    opens (crypto trades 24/7), positive volume + quote_volume."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start=start, periods=n, freq=freq)
    close = base_price * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    open_ = np.empty(n)
    open_[0] = base_price
    open_[1:] = close[:-1]
    spread = np.abs(rng.normal(0, vol / 2.0, n))
    high = np.maximum(open_, close) * (1.0 + spread)
    low = np.minimum(open_, close) * (1.0 - spread)
    volume = rng.uniform(100.0, 1_000.0, n)
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "quote_volume": volume * close,
        },
        index=idx,
    )


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample a fine OHLCV frame to a coarser bar (left-labeled opens)."""
    agg = {k: v for k, v in _OHLCV_AGG.items() if k in df.columns}
    return df.resample(rule, label="left", closed="left").agg(agg).dropna()


@lru_cache(maxsize=16)
def _multi_tf_cached(
    seed: int, days: int, start: str, base_price: float, drift: float, tfs: tuple
) -> dict[str, pd.DataFrame]:
    base_tf = "1m" if "1m" in tfs else "5m"
    per_day = 1440 if base_tf == "1m" else 288
    base = make_perp_ohlcv(
        n=days * per_day,
        seed=seed,
        start=start,
        freq=TF_RULES[base_tf],
        base_price=base_price,
        drift=drift,
    )
    frames = {base_tf: base}
    for tf in tfs:
        if tf != base_tf:
            frames[tf] = resample_ohlcv(base, TF_RULES[tf])
    return frames


def make_multi_tf_frames(
    seed: int = 42,
    days: int = 45,
    start: str = "2024-01-01",
    base_price: float = 100.0,
    drift: float = 0.00002,
    tfs: tuple[str, ...] = ("5m", "15m", "4h", "1d"),
) -> dict[str, pd.DataFrame]:
    """Multi-TF frames where every coarse frame is resampled FROM the same
    fine base series (교차 TF 일관성). Returns fresh copies (safe to poison)."""
    frames = _multi_tf_cached(seed, days, start, base_price, drift, tuple(tfs))
    return {tf: df.copy() for tf, df in frames.items()}


# -- Binance payload / cache seeders -------------------------------------------
def klines_rows(df: pd.DataFrame, timeframe: str) -> list[list]:
    """OHLCV frame → Binance fapi kline arrays (numeric strings, open/close
    time in epoch ms) for httpx.MockTransport handlers."""
    tf_ms = TF_MS[timeframe]
    rows = []
    for idx, row in df.iterrows():
        open_ms = ts_ms(idx)
        rows.append(
            [
                open_ms,
                f"{row['open']:.8f}",
                f"{row['high']:.8f}",
                f"{row['low']:.8f}",
                f"{row['close']:.8f}",
                f"{row['volume']:.8f}",
                open_ms + tf_ms - 1,
                f"{row.get('quote_volume', 0.0):.8f}",
                100,
                "0",
                "0",
                "0",
            ]
        )
    return rows


def seed_ohlcv_cache(db: Database, symbol: str, timeframe: str, df: pd.DataFrame) -> None:
    """Insert an OHLCV frame into the ohlcv_cache table (ts = open epoch ms)."""
    db.executemany(
        "INSERT OR REPLACE INTO ohlcv_cache "
        "(symbol, timeframe, ts, open, high, low, close, volume, quote_volume) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                symbol,
                timeframe,
                ts_ms(idx),
                float(r["open"]),
                float(r["high"]),
                float(r["low"]),
                float(r["close"]),
                float(r["volume"]),
                float(r.get("quote_volume", 0.0)),
            )
            for idx, r in df.iterrows()
        ],
    )


def seed_funding_rates(
    db: Database, symbol: str, rates: pd.Series | list[tuple[int, float]]
) -> None:
    """Insert funding-rate history: a Series (DatetimeIndex → rate) or a
    list of (epoch_ms, rate) tuples."""
    if isinstance(rates, pd.Series):
        params = [(symbol, ts_ms(idx), float(v)) for idx, v in rates.items()]
    else:
        params = [(symbol, int(ts), float(rate)) for ts, rate in rates]
    db.executemany(
        "INSERT OR REPLACE INTO funding_rates (symbol, ts, rate) VALUES (?, ?, ?)",
        params,
    )


def seed_market_regime(db: Database, rows: list[tuple[str, float, float, str]]) -> None:
    """Insert market_regime rows: (date_iso, alt_index, dom_proxy, regime)."""
    db.executemany(
        "INSERT OR REPLACE INTO market_regime (date, alt_index, dom_proxy, regime) "
        "VALUES (?, ?, ?, ?)",
        rows,
    )


# -- fixtures --------------------------------------------------------------------
@pytest.fixture
def synthetic_ohlcv() -> pd.DataFrame:
    return make_synthetic_ohlcv()


@pytest.fixture
def plain_ohlcv() -> pd.DataFrame:
    """Synthetic OHLCV without any price-limit locked days."""
    return make_synthetic_ohlcv(limit_up_at=None, limit_down_at=None)


@pytest.fixture
def multi_tf_frames() -> dict[str, pd.DataFrame]:
    """Deterministic multi-TF frames (5m base → 15m/4h/1d) for one symbol."""
    return make_multi_tf_frames()


@pytest.fixture
def cost():
    return _CostModel()


@pytest.fixture
def zero_cost():
    return _CostModel(**_ZERO_COST_KW)


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(str(tmp_path / "test.db"))
    yield database
    database.close()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(db_path=str(tmp_path / "test.db"), _env_file=None)
