"""실험 — 6개 전략 템플릿을 같은 데이터로 비교.

현 챔피언(box_range)만 계속 자리를 지키고 있어, 다른 템플릿이 지금 시장에서
더 나은지 확인한다. 각 템플릿마다 파라미터 그리드에서 여러 조합을 샘플링해
최고 성적을 뽑고, 발동 자체가 없는 템플릿도 그대로 보고한다(=현 레짐에서
휴면이라는 사실이 정보다).

읽기 전용 — 반드시 **DB 복사본**을 지정한다.

    CA_DB_PATH=<복사본> backend/.venv/bin/python scripts/exp_template_review.py
"""
from __future__ import annotations

import json
import os
import random
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.agents.quant import evaluate_spec  # noqa: E402
from app.backtest.costs import PerpCostModel  # noqa: E402
from app.config import Settings  # noqa: E402
from app.data.loader import DataLoader  # noqa: E402
from app.data.regime import RegimeService  # noqa: E402
from app.db import Database  # noqa: E402
from app.strategies.base import StrategySpec  # noqa: E402
from app.strategies.registry import (  # noqa: E402
    TEMPLATES,
    _fix_constraints,
    _sample,
)

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LTCUSDT"]
TFS = ("1d", "4h", "15m")
SAMPLES = int(os.environ.get("SAMPLES", 8))  # 템플릿당 파라미터 조합 수
#: 평가 1회가 ~100초라 순차 실행은 비현실적이다. 실거래 서버가 15분마다
#: 7초만 쓰므로 코어를 나눠 써도 간섭이 없다 (nice로 우선순위도 낮춘다).
WORKERS = int(os.environ.get("WORKERS", 6))


_CTX = None


def _init(data, regimes, cost, settings):
    """워커 프로세스 초기화 — 데이터는 한 번만 전달받아 재사용한다."""
    global _CTX
    _CTX = (data, regimes, cost, settings)


def _evaluate(template: str, params: dict):
    data, regimes, cost, settings = _CTX
    agg, _per, _tr = evaluate_spec(
        StrategySpec(template, params), data, cost, settings, regimes=regimes
    )
    return agg


def load(db, loader, settings):
    data = {}
    for sym in SYMBOLS:
        frames = {}
        for tf in TFS:
            df = loader._read_cache(sym, tf, 4000)
            if df is not None and not df.empty:
                frames[tf] = df
        if "4h" in frames and settings.execution_timeframe in frames:
            data[sym] = frames
    svc = RegimeService(db, loader, settings)
    tf = settings.execution_timeframe
    return data, {s: svc.align_to(f[tf].index) for s, f in data.items()}


def score(m: dict) -> float:
    """랭킹용 간이 점수 — 샤프 위주, 청산은 즉시 탈락."""
    if (m.get("liquidation_count") or 0) > 0:
        return float("-inf")
    return (m.get("sharpe") or 0.0)


def main() -> None:
    db_path = os.environ.get("CA_DB_PATH")
    if not db_path:
        raise SystemExit("CA_DB_PATH에 **복사본** 경로를 지정하세요")
    settings = Settings(db_path=db_path, trading_mode="paper", _env_file=None)
    db = Database(db_path)
    loader = DataLoader(db, settings=settings)
    cost = PerpCostModel(
        maker_fee=settings.maker_fee, taker_fee=settings.taker_fee,
        slippage=settings.slippage,
    )
    data, regimes = load(db, loader, settings)
    print(f"심볼 {len(data)}개 · 4h 봉 {sum(len(f['4h']) for f in data.values()):,}개")
    print(f"템플릿당 파라미터 {SAMPLES}조합 샘플링\n")

    # 파라미터 조합을 먼저 전부 뽑아두고(결정론) 병렬로 평가한다.
    rng = random.Random(42)
    jobs = []
    for template in sorted(TEMPLATES):
        for _ in range(SAMPLES):
            jobs.append((template, _fix_constraints(
                template, {k: _sample(pr, rng) for k, pr in TEMPLATES[template].items()}
            )))

    global _CTX
    _CTX = (data, regimes, cost, settings)
    results = []
    with ProcessPoolExecutor(max_workers=WORKERS, initializer=_init,
                             initargs=(data, regimes, cost, settings)) as pool:
        futures = {pool.submit(_evaluate, t, p): (t, p) for t, p in jobs}
        for i, fut in enumerate(as_completed(futures), 1):
            template, params = futures[fut]
            try:
                agg = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  [{i}/{len(jobs)}] {template} 실패: {exc}", flush=True)
                continue
            results.append((template, agg, params))
            tc = (agg or {}).get("trade_count", 0)
            print(f"  [{i}/{len(jobs)}] {template:<18} 거래 {tc}", flush=True)

    print()
    rows = []
    for template in sorted(TEMPLATES):
        mine = [(a, p) for t, a, p in results if t == template and a and a.get("trade_count")]
        if not mine:
            rows.append((template, None, None, 0))
            print(f"{template:<18} 발동 0 — 현 레짐/데이터에서 셋업 없음")
            continue
        agg, params = max(mine, key=lambda x: score(x[0]))
        rows.append((template, agg, params, len(mine)))
        print(
            f"{template:<18} 거래{agg['trade_count']:>4} · 승률{agg['win_rate']:>6.1%} · "
            f"수익{agg['total_return']:>7.2%} · 샤프{agg['sharpe']:>6.2f} · "
            f"MDD{agg['mdd']:>6.2%} · 청산{agg.get('liquidation_count',0)}회 "
            f"({len(mine)}/{SAMPLES} 조합 발동)"
        )

    print("\n" + "=" * 78)
    ranked = [r for r in rows if r[1] is not None]
    ranked.sort(key=lambda r: score(r[1]), reverse=True)
    print("샤프 기준 순위 (청산 발생 템플릿은 제외)\n")
    for i, (t, agg, params, _f) in enumerate(ranked, 1):
        flag = " ⚠ 저신뢰(거래<10)" if agg["trade_count"] < settings.min_trades else ""
        print(f"  {i}. {t:<18} 샤프 {agg['sharpe']:>6.2f} · 거래 {agg['trade_count']:>3}{flag}")
        print(f"     {json.dumps(params, ensure_ascii=False)}")

    champ = db.execute("SELECT template, params_json FROM strategies WHERE status='champion'")
    if champ:
        print(f"\n현 챔피언: {champ[0]['template']} {champ[0]['params_json']}")
    db.close()


if __name__ == "__main__":
    main()
