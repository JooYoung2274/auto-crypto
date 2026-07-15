"""Trade log construction — plan_id 단위 롤업 (스펙 §4, §6 trades 테이블).

한 TradePlan의 분할 진입/분할 익절/손절/청산 체결을 하나의 트레이드 행으로
롤업한다. DB `trades` 테이블 컬럼(entry_ts, exit_ts, entry_price, exit_price,
net_ret, holding_hours, side, leverage, timeframe, funding_paid, fee_paid)과
정확히 일치하는 이름을 쓰고, 백테스트 전용 부가 컬럼(plan_id, pnl, qty,
margin_usdt, exit_reason, open)을 덧붙인다.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

TRADE_COLUMNS = [
    "plan_id",
    "entry_ts",
    "exit_ts",
    "entry_price",
    "exit_price",
    "net_ret",
    "pnl",
    "qty",
    "margin_usdt",
    "holding_hours",
    "side",
    "leverage",
    "timeframe",
    "funding_paid",
    "fee_paid",
    "exit_reason",
    "open",
]


@dataclass
class TradeRecord:
    plan_id: int
    entry_ts: pd.Timestamp  # 첫 진입 체결 봉 open 시각
    exit_ts: pd.Timestamp  # 마지막 청산 체결 봉 open 시각 (미청산이면 마지막 봉)
    entry_price: float  # 체결 가중 평균 진입가
    exit_price: float  # 체결 가중 평균 청산가 (미청산이면 마지막 종가)
    net_ret: float  # pnl / 투입 격리마진 (수수료·펀딩 반영)
    pnl: float  # 순손익 USDT (실현손익 − 수수료 − 펀딩 지불)
    qty: float  # 누적 진입 수량
    margin_usdt: float  # 투입 격리마진 합
    holding_hours: float
    side: str  # 'long' | 'short'
    leverage: int
    timeframe: str  # 실행 TF
    funding_paid: float  # 펀딩 순지불액 (양수 = 비용)
    fee_paid: float  # 수수료 합 (maker+taker)
    exit_reason: str  # 'tp' | 'stop' | 'liquidation' | 'eod'
    open: bool  # 데이터 끝에서 미청산(마크-투-마켓)이면 True


def empty_trades_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "plan_id": pd.Series(dtype="int64"),
            "entry_ts": pd.Series(dtype="datetime64[ns]"),
            "exit_ts": pd.Series(dtype="datetime64[ns]"),
            "entry_price": pd.Series(dtype="float64"),
            "exit_price": pd.Series(dtype="float64"),
            "net_ret": pd.Series(dtype="float64"),
            "pnl": pd.Series(dtype="float64"),
            "qty": pd.Series(dtype="float64"),
            "margin_usdt": pd.Series(dtype="float64"),
            "holding_hours": pd.Series(dtype="float64"),
            "side": pd.Series(dtype="object"),
            "leverage": pd.Series(dtype="int64"),
            "timeframe": pd.Series(dtype="object"),
            "funding_paid": pd.Series(dtype="float64"),
            "fee_paid": pd.Series(dtype="float64"),
            "exit_reason": pd.Series(dtype="object"),
            "open": pd.Series(dtype="bool"),
        }
    )


def build_trades_frame(records: list[TradeRecord]) -> pd.DataFrame:
    """트레이드 레코드 목록 → 정본 trades DataFrame."""
    if not records:
        return empty_trades_frame()
    return pd.DataFrame(
        [
            {
                "plan_id": r.plan_id,
                "entry_ts": r.entry_ts,
                "exit_ts": r.exit_ts,
                "entry_price": r.entry_price,
                "exit_price": r.exit_price,
                "net_ret": r.net_ret,
                "pnl": r.pnl,
                "qty": r.qty,
                "margin_usdt": r.margin_usdt,
                "holding_hours": r.holding_hours,
                "side": r.side,
                "leverage": r.leverage,
                "timeframe": r.timeframe,
                "funding_paid": r.funding_paid,
                "fee_paid": r.fee_paid,
                "exit_reason": r.exit_reason,
                "open": r.open,
            }
            for r in records
        ],
        columns=TRADE_COLUMNS,
    )
