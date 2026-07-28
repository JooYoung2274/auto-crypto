"""Regime proxy tests: quadrant judgment, <200-bar cash gate, market_regime
cache roundtrip, 1-day-shift intraday alignment, look-ahead poison."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.data.regime import (
    REGIME_MIN_BARS,
    RegimeService,
    compute_regime_frame,
)
from app.config import Settings
from tests.conftest import seed_market_regime

BTC = "BTCUSDT"


def trend_closes(
    n: int, daily_ret: float, base: float = 100.0, start: str = "2023-01-01"
) -> pd.Series:
    idx = pd.date_range(start, periods=n, freq="1D")
    return pd.Series(base * (1.0 + daily_ret) ** np.arange(n), index=idx)


def universe_closes(n: int, btc_ret: float, alt_ret: float) -> dict[str, pd.Series]:
    return {
        BTC: trend_closes(n, btc_ret, base=60_000.0),
        "ETHUSDT": trend_closes(n, alt_ret, base=3_000.0),
        "SOLUSDT": trend_closes(n, alt_ret, base=150.0),
    }


class StubLoader:
    """Duck-typed DataLoader serving fixed daily frames."""

    def __init__(self, daily: dict[str, pd.DataFrame]):
        self.daily = daily

    def get_ohlcv(self, symbol: str, timeframe: str = "1d", limit: int = 1500):
        assert timeframe == "1d"
        return self.daily.get(
            symbol,
            pd.DataFrame(columns=["open", "high", "low", "close", "volume"]),
        ).tail(limit)


def daily_frame(close: pd.Series) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
            "volume": 100.0,
        },
        index=close.index,
    )


class TestJudgment:
    @pytest.mark.parametrize(
        ("btc_ret", "alt_ret", "expected"),
        [
            (0.000, 0.004, "long_alt"),  # 시장↑ + 도미넌스↓ = 알트 불장
            (0.006, 0.002, "long_btc"),  # 시장↑ + 도미넌스↑ = BTC 주도장
            (0.000, -0.003, "short"),  # 시장↓ + 도미넌스↑ = 숏
            (-0.005, -0.001, "cash"),  # 둘 다 하락 = 시장 이탈
        ],
        ids=["long_alt", "long_btc", "short", "cash"],
    )
    def test_quadrants(self, btc_ret, alt_ret, expected):
        frame = compute_regime_frame(universe_closes(400, btc_ret, alt_ret))
        assert frame["regime"].iloc[-1] == expected

    def test_warmup_rows_are_cash(self):
        frame = compute_regime_frame(universe_closes(400, 0.0, 0.004))
        assert (frame["regime"].iloc[: REGIME_MIN_BARS - 1] == "cash").all()
        assert frame["regime"].iloc[-1] == "long_alt"

    def test_under_200_bars_is_all_cash(self):
        frame = compute_regime_frame(universe_closes(150, 0.0, 0.004))
        assert len(frame) == 150
        assert (frame["regime"] == "cash").all()

    def test_missing_btc_or_alts_yields_empty(self):
        closes = universe_closes(300, 0.0, 0.004)
        assert compute_regime_frame({k: v for k, v in closes.items() if k != BTC}).empty
        assert compute_regime_frame({BTC: closes[BTC]}).empty

    def test_alt_index_and_dom_proxy_shapes(self):
        closes = universe_closes(300, 0.0, 0.004)
        frame = compute_regime_frame(closes)
        assert list(frame.columns) == ["alt_index", "dom_proxy", "regime"]
        assert frame["alt_index"].iloc[0] == pytest.approx(1.0)  # 정규화 시작 = 1
        assert (frame["alt_index"] > 0).all() and (frame["dom_proxy"] > 0).all()


class TestServiceCache:
    def make_service(self, db, settings, n=400, btc_ret=0.0, alt_ret=0.004):
        closes = universe_closes(n, btc_ret, alt_ret)
        loader = StubLoader({s: daily_frame(c) for s, c in closes.items()})
        return RegimeService(db, loader=loader, settings=settings)

    def test_refresh_caches_market_regime(self, db, settings):
        service = self.make_service(db, settings)
        frame = service.refresh()
        assert len(frame) == 400

        rows = db.execute("SELECT COUNT(*) AS n FROM market_regime")
        assert rows[0]["n"] == 400

        cached = service.series()
        assert list(cached["regime"]) == list(frame["regime"])
        assert cached["alt_index"].iloc[-1] == pytest.approx(frame["alt_index"].iloc[-1])
        assert service.current() == frame["regime"].iloc[-1] == "long_alt"

    def test_current_defaults_to_cash_without_history(self, db):
        assert RegimeService(db).current() == "cash"

    def test_refresh_skips_symbols_without_data(self, db, settings):
        closes = universe_closes(400, 0.0, 0.004)
        # settings.universe has 5 symbols; only 3 have data → still computes.
        loader = StubLoader({s: daily_frame(c) for s, c in closes.items()})
        frame = RegimeService(db, loader=loader, settings=settings).refresh()
        assert frame["regime"].iloc[-1] == "long_alt"


class TestIntradayAlignment:
    def test_intraday_bar_reads_previous_completed_day(self, db):
        seed_market_regime(
            db,
            [
                ("2024-01-01", 1.0, 1.0, "cash"),
                ("2024-01-02", 1.1, 0.9, "long_alt"),
                ("2024-01-03", 1.2, 1.1, "long_btc"),
                ("2024-01-04", 0.9, 1.2, "short"),
            ],
        )
        service = RegimeService(db)
        intraday = pd.date_range("2024-01-02", "2024-01-05 23:45", freq="15min")
        aligned = service.align_to(intraday)

        assert aligned.loc["2024-01-02 00:00"] == "cash"  # 전일(01-01) 레짐
        assert aligned.loc["2024-01-02 23:45"] == "cash"
        assert aligned.loc["2024-01-03 09:00"] == "long_alt"
        assert aligned.loc["2024-01-04 12:00"] == "long_btc"
        assert aligned.loc["2024-01-05 04:00"] == "short"  # 마지막 완결 일자

    def test_align_without_history_is_cash(self, db):
        intraday = pd.date_range("2024-01-02", periods=10, freq="15min")
        aligned = RegimeService(db).align_to(intraday)
        assert (aligned == "cash").all()


class TestLookAheadPoison:
    @pytest.mark.parametrize("victim", [BTC, "ETHUSDT"], ids=["btc", "alt"])
    def test_tail_poison_leaves_earlier_regimes_unchanged(self, victim):
        closes = universe_closes(400, 0.0, 0.004)
        clean = compute_regime_frame(closes)

        poisoned_closes = {s: c.copy() for s, c in closes.items()}
        poisoned_closes[victim].iloc[-1] *= 5.0
        poisoned = compute_regime_frame(poisoned_closes)

        pd.testing.assert_series_equal(
            clean["regime"].iloc[:-1], poisoned["regime"].iloc[:-1]
        )
        pd.testing.assert_series_equal(
            clean["alt_index"].iloc[:-1], poisoned["alt_index"].iloc[:-1]
        )

    def test_last_day_poison_invisible_to_intraday_alignment(self, db, settings):
        closes = universe_closes(400, 0.0, 0.004)
        frames = {s: daily_frame(c) for s, c in closes.items()}
        service = RegimeService(db, loader=StubLoader(frames), settings=settings)
        service.refresh()
        # Intraday bars across the final cached days (incl. the last one).
        last_day = closes[BTC].index[-1]
        intraday = pd.date_range(
            closes[BTC].index[-3], last_day + pd.Timedelta(hours=23, minutes=45),
            freq="15min",
        )
        aligned_clean = service.align_to(intraday)

        poisoned = {s: c.copy() for s, c in closes.items()}
        poisoned[BTC].iloc[-1] *= 5.0
        service_bad = RegimeService(
            db,
            loader=StubLoader({s: daily_frame(c) for s, c in poisoned.items()}),
            settings=settings,
        )
        service_bad.refresh()  # overwrites the cache with poisoned rows
        aligned_bad = service_bad.align_to(intraday)

        # Bars on the last day read the PREVIOUS day's row → 오염 불가시.
        pd.testing.assert_series_equal(aligned_clean, aligned_bad)


# -- 레짐 바스켓은 매매 유니버스와 분리돼 있다 ------------------------------------
class TestRegimeBasketIsFixed:
    """가이드의 TOTAL2/도미넌스는 시장 전체 지수라 매매 종목과 무관하다.
    유니버스로 계산하면 종목 추가만으로 판정이 뒤집힌다 — 실제로 7종→10종
    확장 시 도미넌스 방향이 반대가 되어 short → cash 로 매매가 멈췄다."""

    @staticmethod
    def _settings(tmp_path, **over):
        return Settings(db_path=str(tmp_path / "r.db"), _env_file=None, **over)

    def test_basket_ignores_the_trading_universe(self, db, tmp_path):
        s = self._settings(
            tmp_path,
            universe=["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "HYPEUSDT"],
            regime_basket=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        )
        assert RegimeService(db, loader=None, settings=s).basket() == [
            "BTCUSDT", "ETHUSDT", "SOLUSDT"
        ]

    def test_btc_is_always_included(self, db, tmp_path):
        """BTC는 도미넌스 분자라 바스켓에 빠져 있어도 넣어야 한다."""
        s = self._settings(tmp_path, regime_basket=["ETHUSDT", "SOLUSDT"])
        basket = RegimeService(db, loader=None, settings=s).basket()
        assert basket[0] == "BTCUSDT"
        assert set(basket) == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}

    def test_empty_basket_degrades_to_universe(self, db, tmp_path):
        """설정이 비면 과거 동작(유니버스)으로 떨어진다 — 레짐이 사라지진 않는다."""
        s = self._settings(
            tmp_path, universe=["BTCUSDT", "ETHUSDT"], regime_basket=[]
        )
        assert set(RegimeService(db, loader=None, settings=s).basket()) == {
            "BTCUSDT", "ETHUSDT"
        }

    def test_expanding_the_universe_does_not_change_the_verdict(self, db, tmp_path):
        """이번 사고의 재현 — 매매 종목을 늘려도 레짐은 그대로여야 한다."""
        loader = _BasketLoader()
        base = dict(regime_basket=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        seven = RegimeService(
            db, loader, self._settings(tmp_path, universe=["BTCUSDT", "ETHUSDT", "SOLUSDT"], **base)
        ).refresh()
        ten = RegimeService(
            db, loader,
            self._settings(
                tmp_path,
                universe=["BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "HYPEUSDT", "SUIUSDT"],
                **base,
            ),
        ).refresh()
        assert list(seven["regime"]) == list(ten["regime"])
        assert seven["alt_index"].equals(ten["alt_index"])
        # 바스켓에 없는 심볼은 로더가 아예 조회하지 않는다.
        assert "LINKUSDT" not in loader.requested

    def test_changing_the_basket_does_change_the_verdict(self, db, tmp_path):
        """분리가 '무시'가 되면 안 된다 — 바스켓을 바꾸면 지수도 바뀐다."""
        loader = _BasketLoader()
        a = RegimeService(
            db, loader, self._settings(tmp_path, regime_basket=["BTCUSDT", "ETHUSDT"])
        ).refresh()
        b = RegimeService(
            db, loader,
            self._settings(tmp_path, regime_basket=["BTCUSDT", "ETHUSDT", "SOLUSDT"]),
        ).refresh()
        assert not a["alt_index"].equals(b["alt_index"])


class _BasketLoader:
    """심볼별로 서로 다른 결정론 일봉을 주는 테스트 로더."""

    SHAPES = {
        "ETHUSDT": (100.0, -0.15),
        "SOLUSDT": (50.0, 0.40),
        "BTCUSDT": (30000.0, 0.10),
        "LINKUSDT": (10.0, -0.60),
        "HYPEUSDT": (60.0, 0.90),
        "SUIUSDT": (1.0, -0.30),
    }

    def __init__(self, n: int = 400):
        self.n = n
        self.requested: list[str] = []

    def get_ohlcv(self, symbol: str, timeframe: str = "1d", limit: int = 400):
        self.requested.append(symbol)
        start, drift = self.SHAPES[symbol]
        idx = pd.date_range("2025-01-01", periods=self.n, freq="D")
        close = start * (1.0 + drift * np.linspace(0.0, 1.0, self.n))
        return pd.DataFrame({"close": close}, index=idx)
