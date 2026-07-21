"""SQLite wrapper: WAL mode, thread lock, dict rows.

``execute`` returns SELECT rows as list[dict]; for writes it returns
``[{"id": lastrowid}]``. Schema migration = CREATE TABLE IF NOT EXISTS
plus additive ALTER TABLE column migrations.

Coin Agents schema (spec §6): multi-timeframe ``ohlcv_cache`` keyed on
``(symbol, timeframe, ts)`` with ts = bar open time (epoch ms), funding
rates/payments, daily market-regime proxy cache, ``trade_plans`` state
machine rows, futures-shaped paper orders/positions, perp trade rows,
futures portfolio snapshots, withdrawal ledger (복리 금지), econ events
(블랙아웃), and an activity_log retention helper (:meth:`Database.prune_activity_log`).
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv_cache (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts INTEGER NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL NOT NULL,
    quote_volume REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, timeframe, ts)
);
CREATE TABLE IF NOT EXISTS funding_rates (
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    rate REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS funding_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    rate REAL NOT NULL,
    payment REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS market_regime (
    date TEXT PRIMARY KEY,
    alt_index REAL,
    dom_proxy REAL,
    regime TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trade_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    reject_reason TEXT NOT NULL DEFAULT '',
    filled_fraction REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    params_json TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER,
    template TEXT NOT NULL,
    params_json TEXT NOT NULL,
    universe_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'candidate'
);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    equity_curve_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_id INTEGER NOT NULL,
    entry_ts TEXT,
    exit_ts TEXT,
    entry_price REAL,
    exit_price REAL,
    net_ret REAL,
    holding_hours REAL,
    side TEXT,
    leverage INTEGER,
    timeframe TEXT,
    funding_paid REAL NOT NULL DEFAULT 0,
    fee_paid REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER,
    markdown TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    agent TEXT,
    level TEXT NOT NULL DEFAULT 'info',
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    order_type TEXT NOT NULL DEFAULT 'limit',
    limit_price REAL,
    filled_qty REAL NOT NULL DEFAULT 0,
    avg_fill_price REAL,
    reduce_only INTEGER NOT NULL DEFAULT 0,
    aggressive INTEGER NOT NULL DEFAULT 0,
    leverage INTEGER,
    plan_id INTEGER,
    leg_kind TEXT,
    leg_index INTEGER,
    client_order_id TEXT UNIQUE,
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS paper_positions (
    symbol TEXT PRIMARY KEY,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    avg_entry REAL NOT NULL,
    leverage INTEGER NOT NULL,
    isolated_margin REAL NOT NULL,
    liq_price REAL
);
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    wallet_balance REAL NOT NULL,
    available REAL NOT NULL,
    margin_used REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    funding_cum REAL NOT NULL DEFAULT 0,
    total_value REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS withdrawal_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    amount REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS econ_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS champion_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL,
    crowned_at TEXT NOT NULL DEFAULT (datetime('now')),
    demoted_at TEXT
);
"""

# activity_log retention defaults (spec §6: 30일 또는 N행 초과 프루닝).
ACTIVITY_LOG_RETENTION_DAYS = 30
ACTIVITY_LOG_MAX_ROWS = 100_000


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {desc[0]: row[i] for i, desc in enumerate(cursor.description)}


class Database:
    """Thread-safe SQLite access with dict rows."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._in_tx = False
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = _dict_factory
        self._conn.execute("PRAGMA journal_mode=WAL")
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            # Additive migrations — CREATE TABLE IF NOT EXISTS cannot extend
            # tables that already exist in older databases.
            # Cycle/report kind: 'research' | 'validate'/'trade' cycles,
            # 'research' | 'validation' reports. Old rows default to research.
            self._add_columns(
                "cycles", (("kind", "TEXT NOT NULL DEFAULT 'research'"),)
            )
            self._add_columns(
                "reports", (("kind", "TEXT NOT NULL DEFAULT 'research'"),)
            )
            self._conn.commit()

    def _add_columns(self, table: str, columns: tuple[tuple[str, str], ...]) -> None:
        existing = {
            r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")
        }
        for col, decl in columns:
            if col not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def execute(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            if cur.description is not None:
                return cur.fetchall()
            if not self._in_tx:
                self._conn.commit()
            return [{"id": cur.lastrowid}]

    def executemany(self, sql: str, seq_of_params: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, seq_of_params)
            if not self._in_tx:
                self._conn.commit()

    @contextmanager
    def transaction(self):
        """여러 문장을 하나의 원자 단위로 묶는다 (RLock 유지, 단일 커밋).

        본문 예외 시 전부 롤백 — 정산처럼 지갑/포지션/주문/커서를 함께
        바꾸는 다문장 변이가 크래시로 반쪽만 커밋되는 것을 방지한다
        (재기동 시 이중 적용 차단). 중첩되면 바깥 트랜잭션에 합류한다.
        """
        with self._lock:
            if self._in_tx:
                yield self
                return
            self._in_tx = True
            try:
                yield self
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            finally:
                self._in_tx = False

    def prune_activity_log(
        self,
        days: int = ACTIVITY_LOG_RETENTION_DAYS,
        max_rows: int = ACTIVITY_LOG_MAX_ROWS,
    ) -> int:
        """activity_log 보존 정책 (spec §6): ``days``일 초과 행 삭제 +
        최신 ``max_rows``행만 유지. 삭제된 행 수를 반환한다."""
        with self._lock:
            # ts는 events.py가 'T' 구분자(+00:00)로 쓰고 threshold는 SQLite
            # 공백 구분자라 바이트 비교가 어긋난다 ('T'=0x54 > ' '=0x20) —
            # 두 쪽 다 'YYYY-MM-DD HH:MM:SS'로 정규화해 경계일 행이 하루 더
            # 남는 것을 막는다 (finding #15).
            cur = self._conn.execute(
                "DELETE FROM activity_log "
                "WHERE substr(replace(ts, 'T', ' '), 1, 19) < datetime('now', ?)",
                (f"-{int(days)} days",),
            )
            deleted = cur.rowcount
            cur = self._conn.execute(
                "DELETE FROM activity_log WHERE id NOT IN "
                "(SELECT id FROM activity_log ORDER BY id DESC LIMIT ?)",
                (int(max_rows),),
            )
            deleted += cur.rowcount
            self._conn.commit()
            return deleted

    def close(self) -> None:
        with self._lock:
            self._conn.close()
