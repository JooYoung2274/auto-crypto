"""플랜 구동 상태 보존 백테스트 엔진 (스펙 §4).

벡터화 계층은 지표 계산과 봉별 '플랜 후보 트리거'(plan_fn 콜백)까지만이고,
주문 수명주기(레스팅 레그, 체결, TTL, 평단/마진/청산가 재계산, 부분 TP)는
실행 TF 봉 순차 루프가 소유한다. trades/metrics는 plan_id로 롤업.

체결 모델 (규칙 §5 '관통해야 체결', 보수적):
- long 진입(buy): bar.low < P 일 때 체결, 체결가 = min(bar.open, P)
  (갭스루 오픈 처리). short/sell은 대칭 (bar.high > P, max(bar.open, P)).
- 주문은 발주 이후에 open하는 봉부터 매칭 (발주 봉 자신은 제외).
- 동일 봉 우선순위(결정론): 청산 > 손절 exit > 진입 > (진입 봉 청산 재확인) > TP.
  진입과 TP가 같은 봉이면 진입만 체결, TP는 다음 봉부터
  (봉 종가가 TP를 초과하면 예외적 종가 체결 — 크로싱으로 보고 taker 요율).
- 레그 단위 all-or-none.
- 블랙아웃 윈도 안에서는 신규 진입 레그가 체결되지 않는다 (규칙 §2).

손절 = 4h 종가 판정: 완결된 4h봉마다 1회, 4h 마감 시각 ≥ open인 첫 실행 TF
봉의 시가에서 taker+슬리피지로 청산. 미완결 4h봉으로는 절대 판정하지 않는다.

청산 = intrabar low/high 기준, 손절보다 우선. 정확식(risk.plan.liquidation_price,
MMR 노셔널 티어)으로 매 체결 이벤트마다 avg_entry·마진·청산가 재계산.
청산 시 격리마진 전액 손실.

펀딩 = funding 시리즈의 각 정산 시각이 **속한** 실행 TF 봉에서 정산
(실데이터 fundingTime의 ms 지터, 상위 TF 봉 내 다중 정산 모두 허용).
cash_flow = −sign(pos) × rate × qty × bar.open (long + 양수 rate = 지불).

TTL: order_ttl_bars 경과 시 **원래 플랜 레그 가격 그대로** 재큐 (가격 추격 금지).
플랜 무효화/손절/최종 TP 시 plan_id 공유 미체결 자식 주문 전량 취소.
진입 전 plan_ttl_bars 경과 → 래더 전량 취소(abandoned).

RiskEngine 게이트는 봉마다 MarketState(엔진이 추적하는 오픈 포지션/일손실/
실현 잔고, econ_events가 주어지면 블랙아웃 윈도)로 동일 적용된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

import numpy as np
import pandas as pd

from ..config import Settings
from ..risk.engine import MarketState, RiskEngine
from ..risk.plan import TradePlan, liquidation_price
from .costs import PerpCostModel
from .trades import TradeRecord, build_trades_frame

_TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

_QTY_EPS = 1e-12

FILL_COLUMNS = [
    "ts",
    "plan_id",
    "kind",  # 'entry' | 'tp' | 'stop' | 'liquidation'
    "side",  # 'buy' | 'sell'
    "price",
    "qty",
    "fee",
    "fee_type",  # 'maker' | 'taker' | ''
    "leg_index",
    "avg_entry",  # 체결 직후 평단 (exit 계열은 체결 시점 평단)
    "liq_price",  # 체결 직후 청산가 (포지션 종료 시 0.0)
]


def _ts_ms(ts: pd.Timestamp) -> int:
    return int(pd.Timestamp(ts).value // 1_000_000)


@dataclass
class BacktestResult:
    equity: pd.Series  # 마진 조정 에쿼티 (USDT, 시드에서 시작)
    returns: pd.Series  # per-bar 수익률 = Δequity / seed (비복리)
    trades: pd.DataFrame  # plan_id 롤업 (trades.TRADE_COLUMNS)
    fills: pd.DataFrame  # 체결 로그 (FILL_COLUMNS, 요율 태깅)
    order_events: list[dict]  # placed / requeued / cancelled 이벤트
    rejections: list[tuple[pd.Timestamp, str]]  # RiskEngine 거부 (트레이드 제외)
    funding_paid: float  # 펀딩 순지불 합 (양수 = 비용)
    fee_paid: float  # 수수료 합
    liquidation_count: int
    seed: float
    timeframe: str


def empty_fills_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=FILL_COLUMNS)


@dataclass
class _RestingOrder:
    kind: str  # 'entry' | 'tp'
    leg_index: int
    side: str  # 'buy' | 'sell'
    price: float  # 원래 플랜 레그 가격 — TTL 재큐에도 불변 (RR 불변)
    qty: float
    fraction: float
    placed_bar: int
    attempt: int = 0
    final: bool = False  # 마지막 TP 레그 = 잔량 전량


@dataclass
class _Position:
    side: str
    qty: float
    avg_entry: float
    leverage: int
    isolated_margin: float
    liq_price: float


@dataclass
class _PlanState:
    plan: TradePlan
    plan_id: int
    created_bar: int
    created_close_ms: int
    orders: list[_RestingOrder]
    judge_idx: int  # 다음 판정할 4h 봉 인덱스
    any_fill: bool = False
    filled_qty: float = 0.0
    entry_px_qty: float = 0.0
    exit_qty: float = 0.0
    exit_px_qty: float = 0.0
    margin_alloc: float = 0.0
    realized_pnl: float = 0.0
    fee_paid: float = 0.0
    funding_paid: float = 0.0
    first_entry_ts: pd.Timestamp | None = None


@dataclass
class _Sim:
    cash: float  # 실현 지갑 잔고 (격리마진 포함, 미실현 제외)
    position: _Position | None = None
    plan_state: _PlanState | None = None
    daily_realized: float = 0.0
    daily_date: object = None
    plan_seq: int = 0
    liquidation_count: int = 0
    funding_paid_total: float = 0.0
    fee_paid_total: float = 0.0
    fills: list[dict] = field(default_factory=list)
    order_events: list[dict] = field(default_factory=list)
    rejections: list = field(default_factory=list)
    trade_records: list[TradeRecord] = field(default_factory=list)


def run_backtest(
    frames: dict[str, pd.DataFrame],
    plan_fn: Callable[[pd.Timestamp], TradePlan | None],
    cost: PerpCostModel,
    settings: Settings,
    *,
    timeframe: str | None = None,
    funding: pd.Series | None = None,
    econ_events: Sequence[int] | None = None,
    risk: type[RiskEngine] | RiskEngine = RiskEngine,
) -> BacktestResult:
    """멀티 TF ``frames``에서 ``plan_fn``이 생성하는 TradePlan을 구동한다.

    - ``frames``: TF → OHLCV DataFrame (DatetimeIndex = 봉 open). 실행 TF 필수,
      '4h' 프레임이 있으면 4h 종가 손절 판정을 수행한다.
    - ``plan_fn(ts)``: 실행 TF 봉 ts(open 시각)의 **마감 시점**에 호출되는
      전략 콜백. 플랫이고 활성 플랜이 없을 때만 호출된다. ts 이후 데이터를
      읽으면 look-ahead — 오염 테스트로 회귀 방지.
    - ``funding``: 정산 시각(DatetimeIndex, 봉 open과 일치) → rate 시리즈.
      None이면 펀딩 미적용 (호출자가 FundingLoader.get_funding 결과를 넘긴다).
    - ``econ_events``: 이벤트 ts(epoch ms) 목록 → ±blackout_hours 블랙아웃 윈도.
    - 사이징은 항상 고정 시드 기준 plan.margin_usdt (복리 금지).
    """
    tf = timeframe or settings.execution_timeframe
    if tf not in frames:
        raise ValueError(f"execution timeframe frame missing: {tf}")
    tf_ms = _TF_MS[tf]
    df = frames[tf]
    n = len(df)
    seed = float(settings.initial_seed_usdt)

    index = df.index
    open_ = df["open"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    open_ms_arr = np.array([_ts_ms(ts) for ts in index], dtype=np.int64)

    # 4h 프레임 (손절 판정) — 마감 시각 = open + 4h.
    h4 = frames.get("4h")
    if h4 is not None and len(h4) > 0:
        h4_close = h4["close"].to_numpy(dtype=float)
        h4_close_ms = (
            np.array([_ts_ms(ts) for ts in h4.index], dtype=np.int64) + _TF_MS["4h"]
        )
    else:
        h4_close = np.empty(0)
        h4_close_ms = np.empty(0, dtype=np.int64)

    # 정산 시각 → 그 시각이 속한 실행 TF 봉 인덱스로 귀속 (지터·다중 정산 허용).
    funding_by_bar: dict[int, list[float]] = {}
    if funding is not None and len(funding) > 0 and n > 0:
        f_ts = np.array([_ts_ms(t) for t in funding.index], dtype=np.int64)
        bar_idx = np.searchsorted(open_ms_arr, f_ts, side="right") - 1
        for j, bi in enumerate(bar_idx):
            if bi >= 0 and f_ts[j] < int(open_ms_arr[bi]) + tf_ms:
                funding_by_bar.setdefault(int(bi), []).append(float(funding.iloc[j]))

    blackout: tuple[tuple[int, int], ...] = ()
    if econ_events:
        half = int(settings.blackout_hours * 3_600_000)
        blackout = tuple((int(t) - half, int(t) + half) for t in econ_events)

    sim = _Sim(cash=seed)
    equity = np.full(n, seed, dtype=float)

    # ---------------------------------------------------------------- helpers
    def _sign(side: str) -> float:
        return 1.0 if side == "long" else -1.0

    def _record_fill(
        ts, kind: str, side: str, price: float, qty: float,
        fee: float, fee_type: str, leg_index: int,
    ) -> None:
        pos = sim.position
        sim.fills.append(
            {
                "ts": ts,
                "plan_id": sim.plan_state.plan_id if sim.plan_state else 0,
                "kind": kind,
                "side": side,
                "price": price,
                "qty": qty,
                "fee": fee,
                "fee_type": fee_type,
                "leg_index": leg_index,
                "avg_entry": pos.avg_entry if pos else 0.0,
                "liq_price": pos.liq_price if pos else 0.0,
            }
        )

    def _cancel_open_orders(ts, reason: str) -> None:
        st = sim.plan_state
        for o in st.orders:
            sim.order_events.append(
                {
                    "ts": ts,
                    "event": "cancelled",
                    "kind": o.kind,
                    "leg_index": o.leg_index,
                    "price": o.price,
                    "attempt": o.attempt,
                    "reason": reason,
                }
            )
        st.orders.clear()

    def _close_trade(exit_ts, exit_reason: str, *, still_open: bool = False,
                     mark: float | None = None) -> None:
        """플랜 종료 시 trades 롤업 행 생성 (체결이 있었던 플랜만)."""
        st = sim.plan_state
        if not st.any_fill:
            return
        entry_price = st.entry_px_qty / st.filled_qty
        realized = st.realized_pnl
        if still_open:
            pos = sim.position
            exit_price = float(mark)
            realized += _sign(pos.side) * (exit_price - pos.avg_entry) * pos.qty
        elif st.exit_qty > _QTY_EPS:
            exit_price = st.exit_px_qty / st.exit_qty
        else:
            exit_price = entry_price
        pnl = realized - st.fee_paid - st.funding_paid
        net_ret = pnl / st.margin_alloc if st.margin_alloc > 0 else 0.0
        holding_hours = float(
            (pd.Timestamp(exit_ts) - pd.Timestamp(st.first_entry_ts)).total_seconds()
            / 3600.0
        )
        sim.trade_records.append(
            TradeRecord(
                plan_id=st.plan_id,
                entry_ts=pd.Timestamp(st.first_entry_ts),
                exit_ts=pd.Timestamp(exit_ts),
                entry_price=float(entry_price),
                exit_price=float(exit_price),
                net_ret=float(net_ret),
                pnl=float(pnl),
                qty=float(st.filled_qty),
                margin_usdt=float(st.margin_alloc),
                holding_hours=holding_hours,
                side=st.plan.side,
                leverage=int(st.plan.leverage),
                timeframe=tf,
                funding_paid=float(st.funding_paid),
                fee_paid=float(st.fee_paid),
                exit_reason=exit_reason,
                open=still_open,
            )
        )

    def _realize(amount: float) -> None:
        sim.cash += amount
        sim.daily_realized += amount

    def _settle_funding(i: int, ts) -> None:
        pos = sim.position
        for rate in funding_by_bar.get(i, ()):
            notional = pos.qty * open_[i]  # 마크가 = 봉 open
            paid = _sign(pos.side) * rate * notional  # long + 양수 rate = 지불
            _realize(-paid)
            sim.funding_paid_total += paid
            if sim.plan_state is not None:
                sim.plan_state.funding_paid += paid

    def _liquidate(i: int, ts) -> None:
        """강제 청산 — 격리마진 전액 손실, 자식 주문 전량 취소 (최우선)."""
        pos = sim.position
        st = sim.plan_state
        realized = -pos.isolated_margin
        _realize(realized)
        st.realized_pnl += realized
        st.exit_qty += pos.qty
        st.exit_px_qty += pos.liq_price * pos.qty
        sim.liquidation_count += 1
        _record_fill(ts, "liquidation", "sell" if pos.side == "long" else "buy",
                     pos.liq_price, pos.qty, 0.0, "", -1)
        _cancel_open_orders(ts, "청산으로 취소")
        sim.position = None
        _close_trade(ts, "liquidation")
        sim.plan_state = None

    def _stop_exit(i: int, ts) -> None:
        """4h 종가 손절 — 첫 실행 TF 봉 시가에서 taker+슬리피지 청산."""
        pos = sim.position
        st = sim.plan_state
        price = open_[i]
        qty = pos.qty
        realized = _sign(pos.side) * (price - pos.avg_entry) * qty
        fee = cost.fee(price * qty, taker=True)
        _realize(realized - fee)
        st.realized_pnl += realized
        st.fee_paid += fee
        sim.fee_paid_total += fee
        st.exit_qty += qty
        st.exit_px_qty += price * qty
        _record_fill(ts, "stop", "sell" if pos.side == "long" else "buy",
                     price, qty, fee, "taker", -1)
        _cancel_open_orders(ts, "손절 판정 — 잔여 주문 취소")
        sim.position = None
        _close_trade(ts, "stop")
        sim.plan_state = None

    def _judge_4h(i: int, ts) -> None:
        """완결된 4h봉마다 1회 손절 판정 — 미완결 4h봉 판정 금지."""
        st = sim.plan_state
        while (
            sim.plan_state is not None
            and st.judge_idx < len(h4_close_ms)
            and h4_close_ms[st.judge_idx] <= open_ms_arr[i]
        ):
            c4 = h4_close[st.judge_idx]
            st.judge_idx += 1
            side = st.plan.side
            breached = c4 < st.plan.stop.price if side == "long" else c4 > st.plan.stop.price
            if not breached:
                continue
            if sim.position is not None:
                _stop_exit(i, ts)
            else:
                # 진입 전 무효화: 플랫 상태에서 4h 종가가 손절선 이탈.
                _cancel_open_orders(ts, "4h 종가 손절선 이탈 — 플랜 무효화 취소")
                sim.plan_state = None
            return

    def _fill_entry(i: int, ts, o: _RestingOrder, price: float) -> None:
        st = sim.plan_state
        plan = st.plan
        fee = cost.fee(price * o.qty, taker=False)
        _realize(-fee)
        st.fee_paid += fee
        sim.fee_paid_total += fee
        margin_add = o.qty * price / plan.leverage
        pos = sim.position
        if pos is None:
            new_qty, new_avg = o.qty, price
            new_margin = margin_add
        else:
            new_qty = pos.qty + o.qty
            new_avg = (pos.qty * pos.avg_entry + o.qty * price) / new_qty
            new_margin = pos.isolated_margin + margin_add
        # 매 체결 이벤트마다 avg_entry·마진·청산가 재계산 (스펙 §4).
        liq = liquidation_price(new_avg, plan.side, plan.leverage, new_qty * new_avg)
        sim.position = _Position(
            side=plan.side, qty=new_qty, avg_entry=new_avg,
            leverage=plan.leverage, isolated_margin=new_margin, liq_price=liq,
        )
        st.any_fill = True
        st.filled_qty += o.qty
        st.entry_px_qty += price * o.qty
        st.margin_alloc += margin_add
        if st.first_entry_ts is None:
            st.first_entry_ts = ts
        _record_fill(ts, "entry", o.side, price, o.qty, fee, "maker", o.leg_index)
        # TP 수량은 매 체결 이벤트마다 실제 체결 수량 기준 재계산 (스펙 §2).
        for other in st.orders:
            if other.kind == "tp":
                other.qty = st.filled_qty * other.fraction

    def _fill_tp(i: int, ts, o: _RestingOrder, price: float, *,
                 taker: bool = False) -> None:
        st = sim.plan_state
        pos = sim.position
        qty = pos.qty if o.final else min(o.qty, pos.qty)
        if qty <= _QTY_EPS:
            return
        realized = _sign(pos.side) * (price - pos.avg_entry) * qty
        fee = cost.fee(price * qty, taker=taker)
        _realize(realized - fee)
        st.realized_pnl += realized
        st.fee_paid += fee
        sim.fee_paid_total += fee
        st.exit_qty += qty
        st.exit_px_qty += price * qty
        remaining = pos.qty - qty
        if remaining <= _QTY_EPS:
            sim.position = None
        else:
            released = pos.isolated_margin * (qty / pos.qty)
            pos.qty = remaining
            pos.isolated_margin -= released
            pos.liq_price = liquidation_price(
                pos.avg_entry, pos.side, pos.leverage, remaining * pos.avg_entry
            )
        _record_fill(ts, "tp", o.side, price, qty, fee,
                     "taker" if taker else "maker", o.leg_index)
        if sim.position is None:
            # 최종 TP → plan_id 공유 미체결 자식 주문 전량 취소, 플랜 closed.
            _cancel_open_orders(ts, "최종 익절 — 잔여 주문 취소")
            _close_trade(ts, "tp")
            sim.plan_state = None

    def _activate_plan(i: int, ts, plan: TradePlan) -> None:
        sim.plan_seq += 1
        orders: list[_RestingOrder] = []
        entry_side = "buy" if plan.side == "long" else "sell"
        exit_side = "sell" if plan.side == "long" else "buy"
        for k, leg in enumerate(plan.entries):
            qty = plan.margin_usdt * plan.leverage * leg.fraction / leg.price
            orders.append(
                _RestingOrder("entry", k, entry_side, leg.price, qty, leg.fraction, i)
            )
        for k, leg in enumerate(plan.tps):
            orders.append(
                _RestingOrder(
                    "tp", k, exit_side, leg.price, 0.0, leg.fraction, i,
                    final=(k == len(plan.tps) - 1),
                )
            )
        close_ms = int(open_ms_arr[i]) + tf_ms
        judge_idx = int(np.searchsorted(h4_close_ms, close_ms, side="right"))
        sim.plan_state = _PlanState(
            plan=plan, plan_id=sim.plan_seq, created_bar=i,
            created_close_ms=close_ms, orders=orders, judge_idx=judge_idx,
        )
        for o in orders:
            sim.order_events.append(
                {
                    "ts": ts,
                    "event": "placed",
                    "kind": o.kind,
                    "leg_index": o.leg_index,
                    "price": o.price,
                    "attempt": 0,
                    "reason": "분할 진입 레그 발주" if o.kind == "entry" else "분할 익절 레그 발주",
                }
            )

    # ------------------------------------------------------------------ loop
    for i in range(n):
        ts = index[i]

        # UTC 일자 변경 시 일손실 누계 리셋 (서킷브레이커 기준).
        bar_date = ts.date()
        if bar_date != sim.daily_date:
            sim.daily_date = bar_date
            sim.daily_realized = 0.0

        # 1) 펀딩 정산 (정산 시각 = 봉 open).
        if sim.position is not None:
            _settle_funding(i, ts)

        # 2) 청산 — intrabar low/high, 모든 것(손절 포함)에 우선.
        if sim.position is not None:
            pos = sim.position
            hit = (
                low[i] <= pos.liq_price
                if pos.side == "long"
                else high[i] >= pos.liq_price
            )
            if hit:
                _liquidate(i, ts)

        # 3) 4h 종가 손절 판정 (마감 시각 ≥ open인 첫 봉의 시가에서 청산).
        if sim.plan_state is not None:
            _judge_4h(i, ts)

        st = sim.plan_state
        if st is not None:
            # 4) 진입 전 플랜 TTL — 래더 전량 취소 (abandoned).
            if not st.any_fill and i - st.created_bar >= settings.plan_ttl_bars:
                _cancel_open_orders(ts, "플랜 TTL 만료 — abandoned 취소")
                sim.plan_state = None
                st = None
            else:
                # 5) 주문 TTL — 원래 플랜 레그 가격 그대로만 재큐 (추격 금지).
                for o in st.orders:
                    if i - o.placed_bar > settings.order_ttl_bars:
                        o.placed_bar = i
                        o.attempt += 1
                        sim.order_events.append(
                            {
                                "ts": ts,
                                "event": "requeued",
                                "kind": o.kind,
                                "leg_index": o.leg_index,
                                "price": o.price,
                                "attempt": o.attempt,
                                "reason": "TTL 만료 — 원가격 재큐",
                            }
                        )

        # 6) 진입 체결 (관통해야 체결, 발주 봉 제외).
        #    블랙아웃 윈도 안에서는 신규 진입 체결 금지 (규칙 §2).
        bar_open_ms = int(open_ms_arr[i])
        in_blackout = any(lo <= bar_open_ms <= hi for lo, hi in blackout)
        entry_filled_this_bar = False
        if st is not None and not in_blackout:
            for o in [x for x in st.orders if x.kind == "entry"]:
                if o.placed_bar >= i:
                    continue
                if o.side == "buy":
                    if not (low[i] < o.price):
                        continue
                    price = min(open_[i], o.price)
                else:
                    if not (high[i] > o.price):
                        continue
                    price = max(open_[i], o.price)
                st.orders.remove(o)
                _fill_entry(i, ts, o, price)
                entry_filled_this_bar = True

        # 6b) 진입/증량 봉에서도 intrabar 청산 재확인 (adverse-first —
        #     같은 봉에서 포지션이 열리고 청산가까지 스윕된 경우).
        if entry_filled_this_bar and sim.position is not None:
            pos = sim.position
            hit = (
                low[i] <= pos.liq_price
                if pos.side == "long"
                else high[i] >= pos.liq_price
            )
            if hit:
                _liquidate(i, ts)

        # 7) TP 체결 — 같은 봉에 진입 체결 시 억제 (종가가 TP 초과면 예외적
        #    종가 체결 — 크로싱으로 보고 taker), 그 외엔 관통 규칙.
        if sim.plan_state is not None and sim.position is not None:
            for o in [x for x in sim.plan_state.orders if x.kind == "tp"]:
                if sim.position is None or sim.plan_state is None:
                    break
                if o.placed_bar >= i:
                    continue
                if entry_filled_this_bar:
                    beyond = (
                        close[i] > o.price if o.side == "sell" else close[i] < o.price
                    )
                    if not beyond:
                        continue
                    sim.plan_state.orders.remove(o)
                    _fill_tp(i, ts, o, close[i], taker=True)
                    continue
                else:
                    if o.side == "sell":
                        if not (high[i] > o.price):
                            continue
                        price = max(open_[i], o.price)
                    else:
                        if not (low[i] < o.price):
                            continue
                        price = min(open_[i], o.price)
                sim.plan_state.orders.remove(o)
                _fill_tp(i, ts, o, price)

        # 8) 마진 조정 에쿼티 = 실현 잔고 + 미실현 손익 (봉 종가 마크).
        unrealized = 0.0
        if sim.position is not None:
            pos = sim.position
            unrealized = _sign(pos.side) * (close[i] - pos.avg_entry) * pos.qty
        equity[i] = sim.cash + unrealized

        # 9) 새 플랜 후보 — 플랫 & 활성 플랜 없음일 때만 (봉 마감 시점 결정,
        #    발주는 다음 봉부터 매칭).
        if sim.plan_state is None and sim.position is None:
            candidate = plan_fn(ts)
            if candidate is not None:
                state = MarketState(
                    as_of_ts=int(open_ms_arr[i]) + tf_ms,
                    mark_price=float(close[i]),
                    open_positions=0,
                    daily_realized_pnl=sim.daily_realized,
                    blackout_windows=blackout,
                    open_plan_margin=0.0,
                    wallet_balance=sim.cash,
                )
                verdict = risk.review(candidate, settings, state)
                if verdict.approved:
                    _activate_plan(i, ts, candidate)
                else:
                    sim.rejections.append((ts, verdict.reason))

    # 데이터 끝 미청산 포지션 → 마지막 종가 마크-투-마켓 (open 트레이드).
    if sim.position is not None and n > 0:
        _close_trade(index[-1], "eod", still_open=True, mark=float(close[-1]))
        sim.plan_state = None

    equity_s = pd.Series(equity, index=index, name="equity", dtype=float)
    if n > 0:
        returns = equity_s.diff().fillna(equity_s.iloc[0] - seed) / seed
    else:
        returns = pd.Series(dtype=float, index=index)
    returns = returns.rename("returns")

    fills_df = (
        pd.DataFrame(sim.fills, columns=FILL_COLUMNS)
        if sim.fills
        else empty_fills_frame()
    )
    return BacktestResult(
        equity=equity_s,
        returns=returns,
        trades=build_trades_frame(sim.trade_records),
        fills=fills_df,
        order_events=sim.order_events,
        rejections=sim.rejections,
        funding_paid=float(sim.funding_paid_total),
        fee_paid=float(sim.fee_paid_total),
        liquidation_count=int(sim.liquidation_count),
        seed=seed,
        timeframe=tf,
    )
