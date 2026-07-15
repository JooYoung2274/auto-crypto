"""DataLoader + FundingLoader tests. NO network: httpx.MockTransport only."""
from __future__ import annotations

import httpx
import pandas as pd
import pytest

from app.data.funding import FUNDING_INTERVAL_MS, FundingLoader
from app.data.loader import TIMEFRAME_MS, DataLoader
from tests.conftest import klines_rows, make_perp_ohlcv, ts_ms

SYMBOL = "BTCUSDT"
TF = "15m"
TF_MS_15M = TIMEFRAME_MS[TF]
COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume"]


# -- mock transports -----------------------------------------------------------
def klines_transport(
    store: dict[tuple[str, str], pd.DataFrame],
    calls: list | None = None,
    fail_symbols: tuple[str, ...] = (),
) -> httpx.MockTransport:
    """Serve Binance /fapi/v1/klines from in-memory frames."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/klines"
        params = dict(request.url.params)
        if calls is not None:
            calls.append(params)
        symbol, interval = params["symbol"], params["interval"]
        if symbol in fail_symbols:
            raise httpx.ConnectError("exchange down")
        df = store.get((symbol, interval))
        if df is None:
            return httpx.Response(200, json=[])
        rows = klines_rows(df, interval)
        limit = int(params.get("limit", 500))
        if "startTime" in params:
            start = int(params["startTime"])
            rows = [r for r in rows if r[0] >= start][:limit]
        else:
            rows = rows[-limit:]  # Binance: no startTime → latest window
        return httpx.Response(200, json=rows)

    return httpx.MockTransport(handler)


def raising_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    return httpx.MockTransport(handler)


def funding_transport(
    rates_by_symbol: dict[str, list[tuple[int, float]]], calls: list | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fapi/v1/fundingRate"
        params = dict(request.url.params)
        if calls is not None:
            calls.append(params)
        rows = rates_by_symbol.get(params["symbol"], [])
        start = int(params.get("startTime", 0))
        end = int(params.get("endTime", 2**62))
        limit = int(params.get("limit", 1000))
        out = [
            {
                "symbol": params["symbol"],
                "fundingTime": ts,
                "fundingRate": f"{rate:.8f}",
                "markPrice": "0",
            }
            for ts, rate in rows
            if start <= ts <= end
        ][:limit]
        return httpx.Response(200, json=out)

    return httpx.MockTransport(handler)


# -- fixtures --------------------------------------------------------------------
@pytest.fixture
def raw() -> pd.DataFrame:
    """2 days of 15m perp bars (192 rows)."""
    return make_perp_ohlcv(n=192, seed=7, freq="15min", base_price=60_000.0)


def set_now(monkeypatch, now_ms: int) -> None:
    monkeypatch.setattr(DataLoader, "_now_ms", lambda self: now_ms)


def after_last_bar(df: pd.DataFrame) -> int:
    """now such that every bar in df is complete and the cache is fresh."""
    return ts_ms(df.index.max()) + TF_MS_15M


class TestFetchAndCache:
    def test_fetch_normalize_and_cache(self, db, raw, monkeypatch):
        calls: list = []
        loader = DataLoader(db, transport=klines_transport({(SYMBOL, TF): raw}, calls))
        set_now(monkeypatch, after_last_bar(raw))

        df = loader.get_ohlcv(SYMBOL, TF)
        assert len(calls) == 1
        assert calls[0]["symbol"] == SYMBOL and calls[0]["interval"] == TF
        assert len(df) == len(raw)
        assert list(df.columns) == COLUMNS
        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.is_monotonic_increasing
        assert (df.dtypes == float).all()
        pd.testing.assert_series_equal(
            df["close"],
            raw["close"],
            check_freq=False,
            check_exact=False,
            check_index_type=False,  # cache round-trip is datetime64[ms]
        )

    def test_fresh_cache_skips_network(self, db, raw, monkeypatch):
        loader = DataLoader(db, transport=klines_transport({(SYMBOL, TF): raw}))
        set_now(monkeypatch, after_last_bar(raw))
        loader.get_ohlcv(SYMBOL, TF)

        loader._transport = raising_transport()  # any network use would fail...
        df = loader.get_ohlcv(SYMBOL, TF)  # ...but the fresh cache is served
        assert len(df) == len(raw)

    def test_incremental_fetch_from_last_cached_bar(self, db, raw, monkeypatch):
        calls: list = []
        loader = DataLoader(db, transport=klines_transport({(SYMBOL, TF): raw}, calls))

        # First call: only the first 96 bars are complete.
        mid_open = ts_ms(raw.index[96])
        set_now(monkeypatch, mid_open)
        assert len(loader.get_ohlcv(SYMBOL, TF)) == 96

        # Later call resumes from the next bar after the cached max.
        set_now(monkeypatch, after_last_bar(raw))
        df = loader.get_ohlcv(SYMBOL, TF)
        assert len(df) == len(raw)
        assert int(calls[-1]["startTime"]) == mid_open

    def test_limit_returns_newest_bars(self, db, raw, monkeypatch):
        loader = DataLoader(db, transport=klines_transport({(SYMBOL, TF): raw}))
        set_now(monkeypatch, after_last_bar(raw))
        loader.get_ohlcv(SYMBOL, TF)

        df = loader.get_ohlcv(SYMBOL, TF, limit=50)
        assert len(df) == 50
        assert df.index[-1] == raw.index[-1]

    def test_unknown_timeframe_raises(self, db):
        loader = DataLoader(db, transport=raising_transport())
        with pytest.raises(ValueError):
            loader.get_ohlcv(SYMBOL, "3m")


class TestIncompleteBarExclusion:
    def test_in_progress_bar_is_dropped(self, db, raw, monkeypatch):
        loader = DataLoader(db, transport=klines_transport({(SYMBOL, TF): raw}))
        # now is mid-way through the last bar → its close_time > now.
        set_now(monkeypatch, ts_ms(raw.index[-1]) + 1_000)

        df = loader.get_ohlcv(SYMBOL, TF)
        assert len(df) == len(raw) - 1
        assert df.index.max() == raw.index[-2]

    def test_exact_close_boundary_is_complete(self, db, raw, monkeypatch):
        loader = DataLoader(db, transport=klines_transport({(SYMBOL, TF): raw}))
        # now == last bar's close_time → the bar just closed, keep it.
        set_now(monkeypatch, ts_ms(raw.index[-1]) + TF_MS_15M - 1)
        df = loader.get_ohlcv(SYMBOL, TF)
        assert len(df) == len(raw)


class TestCacheOnlyLastResort:
    def test_network_down_serves_stale_cache(self, db, raw, monkeypatch):
        loader = DataLoader(db, transport=klines_transport({(SYMBOL, TF): raw}))
        set_now(monkeypatch, after_last_bar(raw))
        loader.get_ohlcv(SYMBOL, TF)  # seed the cache

        # A day later the exchange is unreachable → stale cache is served.
        loader._transport = raising_transport()
        set_now(monkeypatch, after_last_bar(raw) + TIMEFRAME_MS["1d"])
        df = loader.get_ohlcv(SYMBOL, TF)
        assert len(df) == len(raw)

    def test_nothing_anywhere_returns_empty(self, db, monkeypatch):
        loader = DataLoader(db, transport=raising_transport())
        set_now(monkeypatch, 1_700_000_000_000)
        df = loader.get_ohlcv("NOPEUSDT", TF)
        assert df.empty
        assert list(df.columns) == COLUMNS


class TestRefresh:
    def test_refresh_reports_new_row_counts(self, db, raw, monkeypatch):
        store = {(SYMBOL, TF): raw, ("ETHUSDT", TF): raw.iloc[:100]}
        loader = DataLoader(db, transport=klines_transport(store))
        set_now(monkeypatch, after_last_bar(raw))

        counts = loader.refresh([SYMBOL, "ETHUSDT"], timeframes=[TF])
        assert counts == {SYMBOL: len(raw), "ETHUSDT": 100}
        assert loader.refresh([SYMBOL], timeframes=[TF]) == {SYMBOL: 0}

    def test_refresh_survives_per_symbol_failure(self, db, raw, monkeypatch):
        store = {(SYMBOL, TF): raw}
        loader = DataLoader(
            db, transport=klines_transport(store, fail_symbols=("BROKENUSDT",))
        )
        set_now(monkeypatch, after_last_bar(raw))

        counts = loader.refresh(["BROKENUSDT", SYMBOL], timeframes=[TF])
        assert counts["BROKENUSDT"] == 0
        assert counts[SYMBOL] == len(raw)


# -- funding ---------------------------------------------------------------------
def eight_hour_marks(start_iso: str, n: int) -> list[int]:
    base = ts_ms(pd.Timestamp(start_iso))
    return [base + i * FUNDING_INTERVAL_MS for i in range(n)]


class TestFunding:
    def make_rates(self) -> list[tuple[int, float]]:
        marks = eight_hour_marks("2024-01-01", 9)  # 3 days of 8h settlements
        return [(ts, 0.0001 * ((i % 3) - 1)) for i, ts in enumerate(marks)]

    def test_refresh_fetches_and_caches(self, db, settings):
        rates = self.make_rates()
        fl = FundingLoader(db, settings, transport=funding_transport({SYMBOL: rates}))
        assert fl.refresh(SYMBOL, start_ts=rates[0][0], end_ts=rates[-1][0]) == len(rates)

        series = fl.get_funding(SYMBOL, rates[0][0], rates[-1][0])
        assert len(series) == len(rates)
        assert isinstance(series.index, pd.DatetimeIndex)
        assert series.iloc[0] == pytest.approx(rates[0][1])
        assert series.iloc[-1] == pytest.approx(rates[-1][1])

    def test_get_funding_is_range_inclusive(self, db, settings):
        rates = self.make_rates()
        fl = FundingLoader(db, settings, transport=funding_transport({SYMBOL: rates}))
        fl.refresh(SYMBOL, start_ts=rates[0][0], end_ts=rates[-1][0])

        sub = fl.get_funding(SYMBOL, rates[2][0], rates[5][0])
        assert len(sub) == 4
        assert ts_ms(sub.index[0]) == rates[2][0]
        assert ts_ms(sub.index[-1]) == rates[5][0]

    def test_default_rate_fallback_when_no_history(self, db, settings):
        fl = FundingLoader(db, settings, transport=raising_transport())
        start = ts_ms(pd.Timestamp("2024-01-01 03:00"))
        end = ts_ms(pd.Timestamp("2024-01-02 05:00"))
        series = fl.get_funding(SYMBOL, start, end)
        # 8h UTC boundaries inside the range: 08:00, 16:00, 00:00 → 3 rows.
        assert len(series) == 3
        assert (series == settings.funding_default_rate).all()
        assert ts_ms(series.index[0]) % FUNDING_INTERVAL_MS == 0

    def test_refresh_offline_returns_zero(self, db, settings):
        fl = FundingLoader(db, settings, transport=raising_transport())
        assert fl.refresh(SYMBOL) == 0

    def test_refresh_is_incremental(self, db, settings):
        rates = self.make_rates()
        calls: list = []
        fl = FundingLoader(
            db, settings, transport=funding_transport({SYMBOL: rates[:5]}, calls)
        )
        assert fl.refresh(SYMBOL, end_ts=rates[-1][0]) == 5

        fl._transport = funding_transport({SYMBOL: rates}, calls)
        assert fl.refresh(SYMBOL, end_ts=rates[-1][0]) == 4  # only the new rows
        assert int(calls[-1]["startTime"]) == rates[4][0] + 1
