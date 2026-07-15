"""Paper (simulated) futures broker — 1m 마감 봉 지정가 체결 시뮬 (스펙 §5).

체결 모델 (규칙 §5 '봉이 지정가를 관통해야 체결', 보수적):
- long 진입(buy): bar.low < P 일 때만 체결, 체결가 = min(bar.open, P)
  (갭스루 오픈 처리). sell은 대칭 (bar.high > P, 체결가 = max(bar.open, P)).
- 주문은 **발주 이후에 open하는 봉**부터만 매칭 (same-bar 체결 금지).
- 크로싱 주문은 post-only 거부 — aggressive reduce_only(손절/청산회피 exit)만
  예외로 다음 1m 시가에 taker 요율로 체결.
- 동일 봉 우선순위: 청산 > 손절 exit(aggressive) > 진입 > TP.

격리마진 계정 시뮬:
- 지갑(wallet)은 실현 잔고 — 격리마진 잠금 포함, 미실현 손익 불포함.
- 매 체결마다 avg_entry·격리마진·청산가를 정확식으로 재계산 (스펙 §4).
- 청산은 intrabar low/high 기준, 모든 것에 우선. 청산 시 격리마진 전액 손실.
- 펀딩 정산: 8h 경계 봉마다 cash_flow = −sign(pos) × rate × notional.
- TTL: order_ttl_bars × 실행 TF 경과 시 만료(expired).
- 출금 스킴 (복리 금지): 실현 잔고 기준 max(0, wallet − margin_used − seed)를
  UTC 일 1회 withdrawal_ledger로 분리.

상태는 SQLite(paper_orders / paper_positions / paper_state / funding_payments /
portfolio_snapshots / withdrawal_ledger)에 지속된다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Callable

from ..config import Settings
from ..db import Database
from ..risk.plan import liquidation_price
from .base import (
    Balance,
    Broker,
    Order,
    OrderRequest,
    Position,
    Quote,
)

logger = logging.getLogger(__name__)

_WALLET_KEY = "wallet"
_FUNDING_CUM_KEY = "funding_cum"
_LAST_SKIM_KEY = "last_skim_date"
_CURSOR_KEY = "settle_cursor:{symbol}"

#: 펀딩 정산 주기 (8시간, epoch ms 경계).
FUNDING_INTERVAL_MS = 8 * 3600 * 1000

_TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

_QTY_EPS = 1e-12


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


class PaperBroker(Broker):
    def __init__(
        self,
        db: Database,
        loader=None,
        settings: Settings | None = None,
        *,
        clock: Callable[[], int] | None = None,
    ):
        if settings is None:
            raise ValueError("PaperBroker requires settings")
        super().__init__(plan_lookup=self._db_plan_status)
        self.db = db
        self.loader = loader  # main.py 시그니처 호환 (시세는 ohlcv_cache 직접 조회)
        self.settings = settings
        #: epoch ms 시계 — 테스트에서 주입 가능.
        self.clock: Callable[[], int] = clock or (lambda: int(time.time() * 1000))
        self._leverage: dict[str, int] = {}
        self._ensure_wallet()

    # -- plan gate (규칙 §2) ------------------------------------------------------
    def _db_plan_status(self, plan_id: int) -> str | None:
        rows = self.db.execute(
            "SELECT status FROM trade_plans WHERE id = ?", (plan_id,)
        )
        return rows[0]["status"] if rows else None

    # -- state ---------------------------------------------------------------------
    def _ensure_wallet(self) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO paper_state (key, value) VALUES (?, ?)",
            (_WALLET_KEY, str(float(self.settings.initial_seed_usdt))),
        )
        self.db.execute(
            "INSERT OR IGNORE INTO paper_state (key, value) VALUES (?, ?)",
            (_FUNDING_CUM_KEY, "0.0"),
        )

    def _get_state(self, key: str) -> str | None:
        rows = self.db.execute("SELECT value FROM paper_state WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def _set_state(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO paper_state (key, value) VALUES (?, ?)",
            (key, value),
        )

    def _get_wallet(self) -> float:
        return float(self._get_state(_WALLET_KEY) or self.settings.initial_seed_usdt)

    def _set_wallet(self, wallet: float) -> None:
        self._set_state(_WALLET_KEY, repr(float(wallet)))

    def _get_funding_cum(self) -> float:
        return float(self._get_state(_FUNDING_CUM_KEY) or 0.0)

    def _set_funding_cum(self, value: float) -> None:
        self._set_state(_FUNDING_CUM_KEY, repr(float(value)))

    # -- positions -------------------------------------------------------------------
    def _row_to_position(self, r: dict) -> Position:
        return Position(
            symbol=r["symbol"],
            side=r["side"],
            qty=float(r["qty"]),
            avg_entry=float(r["avg_entry"]),
            leverage=int(r["leverage"]),
            isolated_margin=float(r["isolated_margin"]),
            liq_price=float(r["liq_price"]) if r["liq_price"] is not None else 0.0,
        )

    def _get_position(self, symbol: str) -> Position | None:
        rows = self.db.execute(
            "SELECT * FROM paper_positions WHERE symbol = ?", (symbol,)
        )
        if not rows or float(rows[0]["qty"]) <= _QTY_EPS:
            return None
        return self._row_to_position(rows[0])

    def _all_positions(self) -> list[Position]:
        rows = self.db.execute(
            "SELECT * FROM paper_positions WHERE qty > 0 ORDER BY symbol"
        )
        return [self._row_to_position(r) for r in rows]

    def _save_position(self, pos: Position) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO paper_positions "
            "(symbol, side, qty, avg_entry, leverage, isolated_margin, liq_price) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                pos.symbol,
                pos.side,
                pos.qty,
                pos.avg_entry,
                pos.leverage,
                pos.isolated_margin,
                pos.liq_price,
            ),
        )

    def _delete_position(self, symbol: str) -> None:
        self.db.execute("DELETE FROM paper_positions WHERE symbol = ?", (symbol,))

    # -- bars / quotes -----------------------------------------------------------------
    def _bars_1m(self, symbol: str, after_ms: int, until_ms: int) -> list[dict]:
        """발주 이후에 open했고(now 기준) 이미 마감된 1m 봉들."""
        return self.db.execute(
            "SELECT ts, open, high, low, close FROM ohlcv_cache "
            "WHERE symbol = ? AND timeframe = '1m' AND ts > ? AND ts + 60000 <= ? "
            "ORDER BY ts",
            (symbol, after_ms, until_ms),
        )

    def _latest_close(self, symbol: str) -> tuple[float, int] | None:
        for tf in ("1m", self.settings.execution_timeframe):
            rows = self.db.execute(
                "SELECT ts, close FROM ohlcv_cache "
                "WHERE symbol = ? AND timeframe = ? ORDER BY ts DESC LIMIT 1",
                (symbol, tf),
            )
            if rows:
                return float(rows[0]["close"]), int(rows[0]["ts"])
        return None

    async def get_quote(self, symbol: str) -> Quote:
        latest = await asyncio.to_thread(self._latest_close, symbol)
        if latest is None:
            raise ValueError(f"no price data available for {symbol}")
        price, ts = latest
        return Quote(symbol=symbol, price=price, ts=_iso(ts))

    # -- balance -------------------------------------------------------------------------
    def _margin_used(self) -> float:
        rows = self.db.execute(
            "SELECT COALESCE(SUM(isolated_margin), 0) AS total FROM paper_positions "
            "WHERE qty > 0"
        )
        return float(rows[0]["total"])

    def _reserved_margin(self) -> float:
        """미체결 진입 주문이 잠글 증거금 (qty × limit / leverage)."""
        rows = self.db.execute(
            "SELECT COALESCE(SUM(qty * limit_price / COALESCE(leverage, ?)), 0) AS total "
            "FROM paper_orders WHERE status = 'open' AND reduce_only = 0",
            (self.settings.min_leverage,),
        )
        return float(rows[0]["total"])

    def _unrealized(self) -> float:
        total = 0.0
        for pos in self._all_positions():
            latest = self._latest_close(pos.symbol)
            mark = latest[0] if latest else pos.avg_entry
            sign = 1.0 if pos.side == "long" else -1.0
            total += sign * (mark - pos.avg_entry) * pos.qty
        return total

    async def get_balance(self) -> Balance:
        wallet = self._get_wallet()
        margin_used = self._margin_used()
        upnl = self._unrealized()
        available = wallet - margin_used - self._reserved_margin()
        return Balance(
            wallet_balance=wallet,
            available=available,
            margin_used=margin_used,
            unrealized_pnl=upnl,
        )

    async def get_positions(self) -> list[Position]:
        positions = self._all_positions()
        for pos in positions:
            latest = self._latest_close(pos.symbol)
            mark = latest[0] if latest else pos.avg_entry
            sign = 1.0 if pos.side == "long" else -1.0
            pos.mark_price = mark
            pos.unrealized_pnl = sign * (mark - pos.avg_entry) * pos.qty
        return positions

    # -- orders -----------------------------------------------------------------------------
    def _row_to_order(self, r: dict) -> Order:
        return Order(
            id=str(r["id"]),
            symbol=r["symbol"],
            side=r["side"],
            qty=float(r["qty"]),
            limit_price=(
                None if r["limit_price"] is None else float(r["limit_price"])
            ),
            status=r["status"],
            filled_qty=float(r["filled_qty"] or 0.0),
            avg_fill_price=(
                None if r["avg_fill_price"] is None else float(r["avg_fill_price"])
            ),
            reduce_only=bool(r["reduce_only"]),
            aggressive=bool(r["aggressive"]),
            plan_id=r["plan_id"],
            client_order_id=r["client_order_id"],
            reason=r["reason"] or "",
        )

    def _record_order(
        self,
        request: OrderRequest,
        status: str,
        reason: str = "",
        ts_ms: int | None = None,
    ) -> Order:
        rows = self.db.execute(
            "INSERT INTO paper_orders (ts, symbol, side, qty, order_type, limit_price, "
            "reduce_only, aggressive, leverage, plan_id, client_order_id, status, reason) "
            "VALUES (?, ?, ?, ?, 'limit', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(ts_ms if ts_ms is not None else self.clock()),
                request.symbol,
                request.side,
                request.qty,
                request.limit_price,
                int(request.reduce_only),
                int(request.aggressive),
                request.leverage,
                request.plan_id,
                request.client_order_id,
                status,
                reason,
            ),
        )
        return Order(
            id=str(rows[0]["id"]),
            symbol=request.symbol,
            side=request.side,
            qty=request.qty,
            limit_price=request.limit_price,
            status=status,  # type: ignore[arg-type]
            reduce_only=request.reduce_only,
            aggressive=request.aggressive,
            plan_id=request.plan_id,
            client_order_id=request.client_order_id,
            reason=reason,
        )

    async def place_order(self, request: OrderRequest) -> Order:
        return await asyncio.to_thread(self._place_order_sync, request)

    def _place_order_sync(self, request: OrderRequest) -> Order:
        # 멱등성: 같은 client_order_id 재제출 → 기존 주문 반환 (중복 주문 0건).
        if request.client_order_id:
            rows = self.db.execute(
                "SELECT * FROM paper_orders WHERE client_order_id = ?",
                (request.client_order_id,),
            )
            if rows:
                return self._row_to_order(rows[0])

        request, reason = self.validate_order(request)
        if reason:
            return self._record_order(request, "rejected", reason)

        latest = self._latest_close(request.symbol)
        if latest is None:
            return self._record_order(
                request, "rejected", f"시세 없음: {request.symbol}"
            )
        mark = latest[0]

        # post-only: 크로싱 주문 거부 (aggressive reduce_only exit만 예외).
        crossing = (request.side == "buy" and request.limit_price >= mark) or (
            request.side == "sell" and request.limit_price <= mark
        )
        if crossing and not (request.aggressive and request.reduce_only):
            return self._record_order(
                request,
                "rejected",
                f"post-only 크로싱 거부: {request.side} {request.limit_price} vs mark {mark}",
            )

        pos = self._get_position(request.symbol)
        if request.reduce_only:
            closing_side = "sell" if pos is not None and pos.side == "long" else "buy"
            if pos is None:
                return self._record_order(
                    request, "rejected", "reduce-only 거부: 포지션 없음"
                )
            if request.side != closing_side:
                return self._record_order(
                    request, "rejected", "reduce-only 거부: 포지션 방향과 불일치"
                )
        else:
            opening_side = "long" if request.side == "buy" else "short"
            if pos is not None and pos.side != opening_side:
                return self._record_order(
                    request,
                    "rejected",
                    f"반대 방향 포지션 보유 중 ({pos.side}) — 신규 {opening_side} 거부",
                )
            leverage = request.leverage or self._leverage.get(
                request.symbol, self.settings.min_leverage
            )
            margin_needed = request.qty * request.limit_price / leverage
            available = (
                self._get_wallet() - self._margin_used() - self._reserved_margin()
            )
            if margin_needed > available + 1e-9:
                return self._record_order(
                    request,
                    "rejected",
                    f"증거금 부족: 필요 {margin_needed:.2f} > 가용 {available:.2f} USDT",
                )
            if request.leverage is None:
                request.leverage = leverage
        return self._record_order(request, "open")

    async def cancel_order(self, order_id: str, symbol: str | None = None) -> Order:
        return await asyncio.to_thread(self._cancel_order_sync, order_id)

    def _cancel_order_sync(self, order_id: str, reason: str = "취소") -> Order:
        rows = self.db.execute(
            "SELECT * FROM paper_orders WHERE id = ?", (int(order_id),)
        )
        if not rows:
            raise ValueError(f"unknown order id {order_id}")
        row = rows[0]
        if row["status"] == "open":
            self.db.execute(
                "UPDATE paper_orders SET status = 'cancelled', reason = ? WHERE id = ?",
                (reason, int(order_id)),
            )
            row = self.db.execute(
                "SELECT * FROM paper_orders WHERE id = ?", (int(order_id),)
            )[0]
        return self._row_to_order(row)

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        if symbol is None:
            rows = self.db.execute(
                "SELECT * FROM paper_orders WHERE status = 'open' ORDER BY id"
            )
        else:
            rows = self.db.execute(
                "SELECT * FROM paper_orders WHERE status = 'open' AND symbol = ? "
                "ORDER BY id",
                (symbol,),
            )
        return [self._row_to_order(r) for r in rows]

    async def set_leverage(self, symbol: str, leverage: int) -> None:
        self._leverage[symbol] = int(leverage)

    async def set_margin_mode(self, symbol: str, mode: str = "isolated") -> None:
        if mode != "isolated":
            raise ValueError("마진 모드는 격리(isolated) 고정 (규칙 §1)")

    # -- settlement (1m 마감 봉 시뮬) ------------------------------------------------------
    def settle(self, now_ms: int | None = None) -> list[Order]:
        """마감된 1m 봉으로 청산/펀딩/TTL/체결을 시뮬레이션한다.

        봉 시간순으로: 청산(intrabar, 최우선) → 펀딩 정산 → TTL 만료 →
        지정가 체결 (손절 exit > 진입 > TP 우선순위). 상태가 바뀐 주문 목록을
        반환한다.
        """
        now = now_ms if now_ms is not None else self.clock()
        changed: list[Order] = []
        open_rows = self.db.execute(
            "SELECT * FROM paper_orders WHERE status = 'open' ORDER BY id"
        )
        symbols = sorted(
            {r["symbol"] for r in open_rows}
            | {p.symbol for p in self._all_positions()}
        )
        for symbol in symbols:
            self._refresh_1m(symbol)
            changed.extend(self._settle_symbol(symbol, now))
        return changed

    #: settle용 1m 봉 로드 깊이 — 재기동/공백 후에도 최근 ~10시간을 복구.
    _SETTLE_1M_LIMIT = 600

    def _refresh_1m(self, symbol: str) -> None:
        """페이퍼 체결용 1m 마감봉 최신화 (지정가 관통 판정 데이터).

        설정 timeframes에 '1m'이 없으면 데이터 에이전트는 1m을 받지 않는다 —
        체결 시뮬이 필요로 하는 1m 봉은 settle이 직접 당겨온다. loader는
        캐시 우선이라 새 1m 봉이 마감됐을 때만 네트워크를 타고, 오프라인이면
        캐시만으로 동작한다(기존 폴백 유지). 테스트는 loader=None으로
        1m 봉을 직접 시드하므로 no-op."""
        if self.loader is None:
            return
        try:
            self.loader.get_ohlcv(symbol, "1m", limit=self._SETTLE_1M_LIMIT)
        except Exception:  # noqa: BLE001 — 시세 소스 장애가 정산을 죽이면 안 된다
            logger.warning("1m refresh failed for %s (cache-only settle)", symbol)

    # 원본(주식) 브로커의 duck-typed 이름 호환 — Wave C2가 settle()로 이관.
    def settle_pending(self) -> list[Order]:
        return self.settle()

    def _order_ts_ms(self, row: dict) -> int:
        raw = row["ts"]
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    def _ttl_ms(self) -> int:
        tf_ms = _TF_MS.get(self.settings.execution_timeframe, _TF_MS["15m"])
        return self.settings.order_ttl_bars * tf_ms

    def _settle_symbol(self, symbol: str, now_ms: int) -> list[Order]:
        changed: list[Order] = []
        cursor_key = _CURSOR_KEY.format(symbol=symbol)
        cursor_raw = self._get_state(cursor_key)
        open_rows = self.db.execute(
            "SELECT * FROM paper_orders WHERE status = 'open' AND symbol = ? "
            "ORDER BY id",
            (symbol,),
        )
        if cursor_raw is None:
            if not open_rows:
                return changed
            # 최초 정산: 가장 이른 발주 시각과 같은 시각에 open한 봉부터 포함.
            cursor = min(self._order_ts_ms(r) for r in open_rows) - 1
        else:
            cursor = int(cursor_raw)

        bars = self._bars_1m(symbol, cursor, now_ms)
        if not bars:
            return changed
        placed_ms = {int(r["id"]): self._order_ts_ms(r) for r in open_rows}
        ttl_ms = self._ttl_ms()

        prev_ts = cursor
        for bar in bars:
            bar_ts = int(bar["ts"])
            # 봉 하나의 정산(지갑·포지션·주문·커서)은 원자적으로 커밋 —
            # 크래시 시 통째로 롤백되어 재기동 이중 적용이 없다 (스펙 §9).
            with self.db.transaction():
                self._settle_bar(
                    symbol, bar, bar_ts, prev_ts, placed_ms, ttl_ms,
                    cursor_key, changed,
                )
            prev_ts = bar_ts
        return changed

    def _settle_bar(
        self,
        symbol: str,
        bar: dict,
        bar_ts: int,
        prev_ts: int,
        placed_ms: dict[int, int],
        ttl_ms: int,
        cursor_key: str,
        changed: list[Order],
    ) -> None:
        pos = self._get_position(symbol)

        # 1) 펀딩 정산 — 이전 처리 봉 이후 지나간 8h 경계마다 1회.
        #    (경계 시각의 1m 봉이 캐시에 없어도 건너뛰지 않는다.)
        if pos is not None:
            n_cross = (
                bar_ts // FUNDING_INTERVAL_MS - prev_ts // FUNDING_INTERVAL_MS
            )
            for k in range(max(0, n_cross)):
                boundary = (
                    prev_ts // FUNDING_INTERVAL_MS + 1 + k
                ) * FUNDING_INTERVAL_MS
                self._settle_funding(pos, boundary, float(bar["open"]))
                pos = self._get_position(symbol)
                if pos is None:
                    break

        # 2) 청산 — intrabar, 모든 것에 우선.
        if pos is not None and self._liq_hit(pos, bar):
            changed.extend(self._liquidate(pos, bar_ts))
            pos = None

        # 3) TTL 만료 → 4) 체결. 우선순위: 손절 exit(aggressive) > 진입 > TP.
        open_now = self.db.execute(
            "SELECT * FROM paper_orders WHERE status = 'open' AND symbol = ? "
            "ORDER BY id",
            (symbol,),
        )

        def _priority(r: dict) -> tuple[int, int]:
            if r["aggressive"]:
                rank = 0  # 손절/청산회피 exit
            elif not r["reduce_only"]:
                rank = 1  # 진입
            else:
                rank = 2  # TP
            return (rank, int(r["id"]))

        for row in sorted(open_now, key=_priority):
            oid = int(row["id"])
            placed = placed_ms.get(oid)
            if placed is None:
                placed = self._order_ts_ms(row)
                placed_ms[oid] = placed
            if bar_ts < placed:
                continue  # 발주 이후에 open하는 봉부터만 매칭 (발주 중이던 봉 제외)
            if bar_ts >= placed + ttl_ms:
                self.db.execute(
                    "UPDATE paper_orders SET status = 'expired', reason = ? "
                    "WHERE id = ?",
                    ("TTL 만료", oid),
                )
                changed.append(
                    self._row_to_order(
                        self.db.execute(
                            "SELECT * FROM paper_orders WHERE id = ?", (oid,)
                        )[0]
                    )
                )
                continue
            filled = self._try_fill(row, bar)
            if filled is not None:
                changed.append(filled)

        self._set_state(cursor_key, str(bar_ts))

    # -- funding ----------------------------------------------------------------
    def _settle_funding(self, pos: Position, ts_ms: int, mark: float) -> None:
        rows = self.db.execute(
            "SELECT rate FROM funding_rates WHERE symbol = ? AND ts = ?",
            (pos.symbol, ts_ms),
        )
        rate = float(rows[0]["rate"]) if rows else self.settings.funding_default_rate
        sign = 1.0 if pos.side == "long" else -1.0
        notional = pos.qty * mark
        cash_flow = -sign * rate * notional  # long + 양수 rate = 지불(차감)
        self._set_wallet(self._get_wallet() + cash_flow)
        self._set_funding_cum(self._get_funding_cum() + cash_flow)
        self.db.execute(
            "INSERT INTO funding_payments (ts, symbol, side, rate, payment) "
            "VALUES (?, ?, ?, ?, ?)",
            (_iso(ts_ms), pos.symbol, pos.side, rate, cash_flow),
        )

    # -- liquidation ---------------------------------------------------------------
    def _liq_hit(self, pos: Position, bar: dict) -> bool:
        if pos.liq_price <= 0:
            return False
        if pos.side == "long":
            return float(bar["low"]) <= pos.liq_price
        return float(bar["high"]) >= pos.liq_price

    def _liquidate(self, pos: Position, ts_ms: int) -> list[Order]:
        """강제 청산 — 격리마진 전액 손실, 해당 심볼 reduce-only 주문 취소."""
        changed: list[Order] = []
        self._set_wallet(self._get_wallet() - pos.isolated_margin)
        self._delete_position(pos.symbol)
        # 포지션이 사라졌으므로 exit 주문은 무의미 → 취소. (진입 래더 취소는
        # 플랜 상태머신/모니터 소관.)
        rows = self.db.execute(
            "SELECT id FROM paper_orders WHERE status = 'open' AND symbol = ? "
            "AND reduce_only = 1",
            (pos.symbol,),
        )
        for r in rows:
            changed.append(
                self._cancel_order_sync(str(r["id"]), reason="청산으로 취소")
            )
        close_side = "sell" if pos.side == "long" else "buy"
        rows = self.db.execute(
            "INSERT INTO paper_orders (ts, symbol, side, qty, order_type, limit_price, "
            "filled_qty, avg_fill_price, reduce_only, aggressive, leverage, status, reason) "
            "VALUES (?, ?, ?, ?, 'limit', ?, ?, ?, 1, 1, ?, 'filled', ?)",
            (
                _iso(ts_ms),
                pos.symbol,
                close_side,
                pos.qty,
                pos.liq_price,
                pos.qty,
                pos.liq_price,
                pos.leverage,
                f"강제 청산 — 격리마진 {pos.isolated_margin:.2f} USDT 전액 손실",
            ),
        )
        changed.append(
            self._row_to_order(
                self.db.execute(
                    "SELECT * FROM paper_orders WHERE id = ?", (rows[0]["id"],)
                )[0]
            )
        )
        return changed

    # -- fills ------------------------------------------------------------------------
    def _try_fill(self, row: dict, bar: dict) -> Order | None:
        side = row["side"]
        limit = float(row["limit_price"])
        open_ = float(bar["open"])
        aggressive = bool(row["aggressive"])
        reduce_only = bool(row["reduce_only"])

        if aggressive and reduce_only:
            # 손절/청산회피 exit: 다음 1m 시가에 taker 체결.
            price = open_
        elif side == "buy":
            if not (float(bar["low"]) < limit):
                return None  # 터치(=)로는 미체결 — 관통해야 체결
            price = min(open_, limit)
        else:
            if not (float(bar["high"]) > limit):
                return None
            price = max(open_, limit)

        fee_rate = self.settings.taker_fee if aggressive else self.settings.maker_fee
        if reduce_only:
            return self._fill_reduce(row, price, fee_rate)
        return self._fill_entry(row, price, fee_rate)

    def _finish_fill(self, order_id: int, qty: float, price: float, reason: str = "") -> Order:
        self.db.execute(
            "UPDATE paper_orders SET status = 'filled', filled_qty = ?, "
            "avg_fill_price = ?, reason = ? WHERE id = ?",
            (qty, price, reason, order_id),
        )
        return self._row_to_order(
            self.db.execute("SELECT * FROM paper_orders WHERE id = ?", (order_id,))[0]
        )

    def _fill_entry(self, row: dict, price: float, fee_rate: float) -> Order:
        symbol, side_req = row["symbol"], row["side"]
        qty = float(row["qty"])
        pos_side = "long" if side_req == "buy" else "short"
        leverage = int(row["leverage"] or self.settings.min_leverage)
        pos = self._get_position(symbol)
        if pos is not None and pos.side != pos_side:
            return self._row_to_order(
                self.db.execute(
                    "SELECT * FROM paper_orders WHERE id = ?", (int(row["id"]),)
                )[0]
            )

        fee = qty * price * fee_rate
        margin_add = qty * price / leverage
        self._set_wallet(self._get_wallet() - fee)

        if pos is None:
            new_qty, new_avg = qty, price
            new_margin = margin_add
        else:
            new_qty = pos.qty + qty
            new_avg = (pos.qty * pos.avg_entry + qty * price) / new_qty
            new_margin = pos.isolated_margin + margin_add
            leverage = pos.leverage  # 포지션 레버리지 유지
        # 매 체결 이벤트마다 avg_entry·마진·청산가 재계산 (스펙 §4).
        liq = liquidation_price(new_avg, pos_side, leverage, new_qty * new_avg)
        self._save_position(
            Position(
                symbol=symbol,
                side=pos_side,
                qty=new_qty,
                avg_entry=new_avg,
                leverage=leverage,
                isolated_margin=new_margin,
                liq_price=liq,
            )
        )
        return self._finish_fill(int(row["id"]), qty, price, reason="분할 진입 체결")

    def _fill_reduce(self, row: dict, price: float, fee_rate: float) -> Order | None:
        symbol = row["symbol"]
        pos = self._get_position(symbol)
        if pos is None:
            return self._cancel_order_sync(str(row["id"]), reason="포지션 없음 — 취소")
        qty = min(float(row["qty"]), pos.qty)
        sign = 1.0 if pos.side == "long" else -1.0
        pnl = sign * (price - pos.avg_entry) * qty
        fee = qty * price * fee_rate
        released_fraction = qty / pos.qty
        self._set_wallet(self._get_wallet() + pnl - fee)
        remaining = pos.qty - qty
        if remaining <= _QTY_EPS:
            self._delete_position(symbol)
        else:
            new_margin = pos.isolated_margin * (1.0 - released_fraction)
            liq = liquidation_price(
                pos.avg_entry, pos.side, pos.leverage, remaining * pos.avg_entry
            )
            self._save_position(
                Position(
                    symbol=symbol,
                    side=pos.side,
                    qty=remaining,
                    avg_entry=pos.avg_entry,
                    leverage=pos.leverage,
                    isolated_margin=new_margin,
                    liq_price=liq,
                )
            )
        reason = "손절 청산 체결" if bool(row["aggressive"]) else "분할 익절 체결"
        return self._finish_fill(int(row["id"]), qty, price, reason=reason)

    # -- withdrawal skim (복리 금지, 규칙 §1) ------------------------------------------
    def skim_withdrawal(self, now_ms: int | None = None) -> float:
        """실현 잔고 기준 시드 초과분을 UTC 일 1회 출금 원장으로 분리.

        withdrawable = max(0, wallet − margin_used − reserved − seed).
        오픈 진입 주문이 점유한 예약 마진(reserved)도 제외 — 이미 커밋된
        자금을 출금해 체결/청산 후 지갑이 음수가 되는 것을 방지한다.
        미실현 손익은 wallet에 없으므로 자연히 불포함. 사후 반전 없음.
        """
        now = now_ms if now_ms is not None else self.clock()
        today = _iso(now)[:10]
        if self._get_state(_LAST_SKIM_KEY) == today:
            return 0.0
        wallet = self._get_wallet()
        amount = max(
            0.0,
            wallet
            - self._margin_used()
            - self._reserved_margin()
            - self.settings.initial_seed_usdt,
        )
        if amount <= 0.0:
            return 0.0
        self._set_wallet(wallet - amount)
        self._set_state(_LAST_SKIM_KEY, today)
        self.db.execute(
            "INSERT INTO withdrawal_ledger (ts, amount, reason) VALUES (?, ?, ?)",
            (_iso(now), amount, "시드 초과 수익 출금 — 복리 금지"),
        )
        return amount

    # -- snapshot ---------------------------------------------------------------------
    def snapshot(self) -> dict:
        """portfolio_snapshots에 현재 선물 지갑 상태를 기록한다."""
        wallet = self._get_wallet()
        margin_used = self._margin_used()
        upnl = self._unrealized()
        funding_cum = self._get_funding_cum()
        total = wallet + upnl
        self.db.execute(
            "INSERT INTO portfolio_snapshots (wallet_balance, available, margin_used, "
            "unrealized_pnl, funding_cum, total_value) VALUES (?, ?, ?, ?, ?, ?)",
            (
                wallet,
                wallet - margin_used - self._reserved_margin(),
                margin_used,
                upnl,
                funding_cum,
                total,
            ),
        )
        return {
            "wallet_balance": wallet,
            "available": wallet - margin_used - self._reserved_margin(),
            "margin_used": margin_used,
            "unrealized_pnl": upnl,
            "funding_cum": funding_cum,
            "total_value": total,
        }
