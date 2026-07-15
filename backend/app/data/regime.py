"""Market regime proxy from Binance daily closes (스펙 §3.1 — 키 프리,
백테스트 가능한 TOTAL2/3·도미넌스 프록시).

- ``ALT_INDEX`` = equal-weight normalized index of non-BTC universe closes.
- ``DOM_PROXY`` = BTC close / ALT_INDEX (dominance *direction* proxy).
- Judgment via 50/200 SMA on both series:
    시장↑(ALT 50>200) + 도미넌스↓ → 'long_alt'   (알트 불장)
    시장↑ + 도미넌스↑            → 'long_btc'   (BTC 주도장)
    시장↓ + 도미넌스↑            → 'short'      (알트 이탈 → 숏)
    시장↓ + 도미넌스↓            → 'cash'       (시장 이탈)
- History < 200 daily bars → 'cash' (진입 차단).
- Cached in the ``market_regime`` table; intraday consumers must read only
  the previous *completed* UTC day's row (스펙 §1.2 — ``align_to`` applies
  the daily series with a 1-day shift + ffill).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..config import Settings
from ..db import Database
from .indicators import sma

logger = logging.getLogger(__name__)

REGIMES = ("long_alt", "long_btc", "short", "cash")
REGIME_MIN_BARS = 200
SMA_FAST = 50
SMA_SLOW = 200
BTC_SYMBOL = "BTCUSDT"
REGIME_COLUMNS = ["alt_index", "dom_proxy", "regime"]


def compute_regime_frame(
    daily_closes: dict[str, pd.Series], btc_symbol: str = BTC_SYMBOL
) -> pd.DataFrame:
    """Daily regime frame (columns alt_index/dom_proxy/regime) from a dict
    of daily close Series keyed by symbol. Index = daily bar open (UTC)."""
    alts = {s: c for s, c in daily_closes.items() if s != btc_symbol and len(c)}
    btc = daily_closes.get(btc_symbol)
    if btc is None or btc.empty or not alts:
        return pd.DataFrame(columns=REGIME_COLUMNS, index=pd.DatetimeIndex([]))

    # Equal-weight normalized alt closes (each series / its first value).
    aligned = pd.concat(alts, axis=1).dropna()
    if aligned.empty:
        return pd.DataFrame(columns=REGIME_COLUMNS, index=pd.DatetimeIndex([]))
    alt_index = (aligned / aligned.iloc[0]).mean(axis=1)
    btc = btc.reindex(alt_index.index).ffill()
    dom_proxy = btc / alt_index

    market_fast, market_slow = sma(alt_index, SMA_FAST), sma(alt_index, SMA_SLOW)
    dom_fast, dom_slow = sma(dom_proxy, SMA_FAST), sma(dom_proxy, SMA_SLOW)

    ready = market_slow.notna() & dom_slow.notna()
    market_up = (market_fast > market_slow).to_numpy()
    dom_up = (dom_fast > dom_slow).to_numpy()
    regime = np.select(
        [
            ~ready.to_numpy(),  # 히스토리 < 200봉 → 진입 차단
            market_up & ~dom_up,
            market_up & dom_up,
            ~market_up & dom_up,
        ],
        ["cash", "long_alt", "long_btc", "short"],
        default="cash",
    )
    return pd.DataFrame(
        {"alt_index": alt_index, "dom_proxy": dom_proxy, "regime": regime},
        index=alt_index.index,
    )


class RegimeService:
    """Compute/cache/serve the daily regime series (market_regime table)."""

    def __init__(self, db: Database, loader=None, settings: Settings | None = None):
        self.db = db
        self.loader = loader  # needs .get_ohlcv(symbol, '1d', limit)
        self.settings = settings

    # -- compute + cache --------------------------------------------------------
    def refresh(self, limit: int = 400) -> pd.DataFrame:
        """Load universe daily closes via the loader, compute the regime
        frame and upsert it into ``market_regime``. Symbols with no data
        are skipped (offline the loader serves its cache)."""
        if self.loader is None:
            raise ValueError("RegimeService.refresh requires a loader")
        universe = list(self.settings.universe) if self.settings else [BTC_SYMBOL]
        closes: dict[str, pd.Series] = {}
        for symbol in universe:
            try:
                df = self.loader.get_ohlcv(symbol, "1d", limit)
            except Exception:  # noqa: BLE001 — a bad symbol must not kill it
                logger.exception("regime daily load failed for %s", symbol)
                continue
            if not df.empty:
                closes[symbol] = df["close"]
        frame = compute_regime_frame(closes)
        if not frame.empty:
            self._upsert(frame)
        return frame

    def _upsert(self, frame: pd.DataFrame) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO market_regime "
            "(date, alt_index, dom_proxy, regime) VALUES (?, ?, ?, ?)",
            [
                (
                    idx.date().isoformat(),
                    float(row["alt_index"]),
                    float(row["dom_proxy"]),
                    str(row["regime"]),
                )
                for idx, row in frame.iterrows()
            ],
        )

    # -- read --------------------------------------------------------------------
    def series(self) -> pd.DataFrame:
        """Cached daily regime frame indexed by (UTC-naive) DatetimeIndex."""
        rows = self.db.execute(
            "SELECT date, alt_index, dom_proxy, regime FROM market_regime "
            "ORDER BY date"
        )
        if not rows:
            return pd.DataFrame(columns=REGIME_COLUMNS, index=pd.DatetimeIndex([]))
        df = pd.DataFrame(rows)
        df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("date")))
        df.index.name = None
        return df[REGIME_COLUMNS]

    def current(self) -> str:
        """Latest cached daily regime; 'cash' when no history exists."""
        rows = self.db.execute(
            "SELECT regime FROM market_regime ORDER BY date DESC LIMIT 1"
        )
        return str(rows[0]["regime"]) if rows else "cash"

    def align_to(self, intraday_index: pd.DatetimeIndex) -> pd.Series:
        """Regime per intraday bar with the mandatory 1-day shift: bar t
        reads the previous *completed* UTC day's regime (스펙 §1.2) — day
        d's row only becomes visible from d+1 00:00 UTC. Bars before any
        completed regime day get 'cash'."""
        daily = self.series()
        if daily.empty:
            return pd.Series("cash", index=intraday_index, name="regime")
        visible = daily["regime"].copy()
        visible.index = visible.index + pd.Timedelta(days=1)
        aligned = (
            visible.reindex(visible.index.union(intraday_index))
            .ffill()
            .reindex(intraday_index)
        )
        return aligned.fillna("cash").rename("regime")
