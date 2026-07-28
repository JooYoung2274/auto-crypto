"""실험 — box_range 박스 이탈 가드의 영향 측정.

문제: `q = (mark - box.bottom) / box.height` 가 **음수**여도 (= 가격이 박스
하단 아래로 이미 이탈) `q <= entry_q` 를 만족해 '하단 지지 롱'으로 진입한다.
숏 쪽도 대칭으로 `q > 1` (상단 위로 이탈)에서 진입한다.

즉 "박스 하단 이탈 = 시나리오 붕괴 손절"이라고 근거에 적어놓고, 진입 시점에
이미 그 조건이 성립한 종목을 산다.

가드: 롱은 `0 <= q <= entry_q`, 숏은 `1-entry_q <= q <= 1` 일 때만 진입.

실행:
    CA_DB_PATH=<복사본> backend/.venv/bin/python scripts/exp_box_breakout_guard.py

실거래 DB를 직접 열지 말 것 — 반드시 복사본을 지정한다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import pandas as pd  # noqa: E402

from app.agents.quant import evaluate_spec  # noqa: E402
from app.backtest.costs import PerpCostModel  # noqa: E402
from app.config import Settings  # noqa: E402
from app.data.loader import DataLoader  # noqa: E402
from app.data.regime import RegimeService  # noqa: E402
from app.db import Database  # noqa: E402
from app.strategies import box_range  # noqa: E402
from app.strategies.base import StrategySpec  # noqa: E402

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LTCUSDT"]
TFS = ("1d", "4h", "15m")


def guarded_plan(frames, symbol, **params):
    """박스 안에 있을 때만 진입하도록 감싼 plan 함수.

    원본을 호출한 뒤, 만들어진 플랜이 '이탈 상태 진입'이면 버린다. 원본 로직을
    복제하지 않아 두 경로가 어긋날 일이 없다.
    """
    from app.data.indicators import build_box, swing_pivots
    from app.strategies.base import mark_price

    h4 = frames.get("4h")
    if h4 is None or len(h4) < box_range.MIN_BARS:
        return None
    mark = mark_price(frames)
    box = build_box(
        swing_pivots(h4, k=int(params.get("pivot_k", 3))),
        as_of=None,
        recent=box_range.RECENT_PIVOTS,
    )
    if box is None or box.height <= 0 or mark is None:
        return None
    q = (mark - box.bottom) / box.height
    if not (0.0 <= q <= 1.0):
        return None  # 박스 밖 — 시나리오가 이미 깨졌다
    return box_range.plan(frames, symbol, **params)


def load(db, loader, settings):
    """캐시만 읽는다 — 네트워크 호출 없음, 원본 DB 무접촉."""
    data = {}
    for sym in SYMBOLS:
        frames = {}
        for tf in TFS:
            df = loader._read_cache(sym, tf, 4000)
            if df is not None and not df.empty:
                frames[tf] = df
        if "4h" in frames and settings.execution_timeframe in frames:
            data[sym] = frames
    # 오케스트레이터와 동일하게 실행 TF 인덱스에 정렬 (1일 시프트).
    svc = RegimeService(db, loader, settings)
    tf = settings.execution_timeframe
    regimes = {sym: svc.align_to(f[tf].index) for sym, f in data.items()}
    return data, regimes


def run(label, spec, data, regimes, cost, settings):
    agg, per_symbol, trades = evaluate_spec(
        spec, data, cost, settings, regimes=regimes
    )
    if agg is None:
        print(f"{label:<12} 거래 없음")
        return None
    print(
        f"{label:<12} 거래 {agg['trade_count']:>3} · 승률 {agg['win_rate']:>6.1%} · "
        f"총수익 {agg['total_return']:>7.2%} · 샤프 {agg['sharpe']:>6.2f} · "
        f"MDD {agg['mdd']:>6.2%} · PF {agg.get('profit_factor') or 0:>5.2f} · "
        f"청산 {agg.get('liquidation_count', 0)}회"
    )
    return agg, trades


def main() -> None:
    db_path = os.environ.get("CA_DB_PATH")
    if not db_path:
        raise SystemExit("CA_DB_PATH에 **복사본** 경로를 지정하세요")
    settings = Settings(db_path=db_path, trading_mode="paper", _env_file=None)
    db = Database(db_path)
    loader = DataLoader(db, settings=settings)
    cost = PerpCostModel(
        maker_fee=settings.maker_fee,
        taker_fee=settings.taker_fee,
        slippage=settings.slippage,
    )

    data, regimes = load(db, loader, settings)
    print(f"심볼 {len(data)}개 · 4h 봉 {sum(len(f['4h']) for f in data.values())}개\n")

    # 실제 챔피언 파라미터
    import json

    row = db.execute("SELECT params_json FROM strategies WHERE status='champion'")
    params = json.loads(row[0]["params_json"]) if row else {
        "pivot_k": 3, "entry_q": 0.15, "stop_buf": 0.0857,
        "tp1_frac": 0.3568, "leverage": 3,
    }
    print(f"파라미터: {params}\n")

    spec = StrategySpec("box_range", params)
    before = run("현재 코드", spec, data, regimes, cost, settings)

    # 가드를 끼운 템플릿을 임시 등록해 같은 경로로 백테스트
    from app.strategies import registry

    original = registry.PLAN_FUNCS["box_range"]
    registry.PLAN_FUNCS["box_range"] = guarded_plan
    try:
        after = run("가드 적용", spec, data, regimes, cost, settings)
    finally:
        registry.PLAN_FUNCS["box_range"] = original

    if before and after:
        b, a = before[0], after[0]
        print("\n차이 (가드 − 현재)")
        for k, fmt in [
            ("trade_count", "{:+d}"), ("win_rate", "{:+.1%}"),
            ("total_return", "{:+.2%}"), ("sharpe", "{:+.2f}"), ("mdd", "{:+.2%}"),
        ]:
            bv, av = b.get(k) or 0, a.get(k) or 0
            print(f"  {k:<14} {fmt.format(av - bv)}")

        # 가드가 걸러낸 거래가 실제로 손실이었는지
        keys = lambda ts: {(t["symbol"], t["entry_ts"]) for t in ts}
        dropped = keys(before[1]) - keys(after[1])
        lost = [t for t in before[1] if (t["symbol"], t["entry_ts"]) in dropped]
        if lost:
            wins = sum(1 for t in lost if (t.get("net_ret") or 0) > 0)
            pnl = sum(t.get("pnl") or 0 for t in lost)
            print(f"\n가드가 걸러낸 거래 {len(lost)}건 — {wins}승 {len(lost)-wins}패 · "
                  f"합계 {pnl:+.2f} USDT")
    db.close()


if __name__ == "__main__":
    main()
