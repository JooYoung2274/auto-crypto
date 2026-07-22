"""오프라인 평가: vpvr_accum 신규 템플릿 (실거래 무영향, 임시 DB).

두 개 창에서 평가한다:
- A) 15m 실행 최근 ~15일 (현행 운영 조건) — 숏 레짐이면 0거래가 정상
- B) 4h 실행 ~250일 (롱 레짐 구간 포함) — 템플릿 엣지의 실질 검증

파라미터는 레지스트리 그리드에서 시드 고정 랜덤 40개 샘플 + 현행 챔피언
(box_range #2412)을 같은 창에서 참조 성적으로 병기한다.

실행: .venv/bin/python scripts/exp_vpvr_accum.py
"""
from __future__ import annotations

import json
import random
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from app.agents.quant import evaluate_spec
from app.backtest.costs import PerpCostModel
from app.config import Settings
from app.data.funding import FundingLoader
from app.data.loader import DataLoader
from app.data.regime import RegimeService
from app.db import Database
from app.strategies.base import StrategySpec
from app.strategies.registry import TEMPLATES, _fix_constraints, _sample

PROD_DB = Path(__file__).resolve().parents[1] / "coinagent.db"
EXP_DB = Path("/tmp/exp_vpvr.db")
CHAMPION_ID = 2412
N_SAMPLES = 40

shutil.copy(PROD_DB, EXP_DB)
for suffix in ("-wal", "-shm"):
    p = Path(str(PROD_DB) + suffix)
    if p.exists():
        shutil.copy(p, str(EXP_DB) + suffix)

base = dict(db_path=str(EXP_DB), _env_file=None, initial_seed_usdt=1900.0)
s15 = Settings(execution_timeframe="15m", **base)
s4h = Settings(execution_timeframe="4h", order_ttl_bars=2, plan_ttl_bars=12, **base)

db = Database(str(EXP_DB))
loader = DataLoader(db, settings=s15)
funding_loader = FundingLoader(db, s15)
regime_service = RegimeService(db, loader, s15)
regime_service.refresh(limit=400)

data: dict[str, dict[str, pd.DataFrame]] = {}
for sym in s15.universe:
    frames = {
        "1d": loader.get_ohlcv(sym, "1d", limit=1500),
        "4h": loader.get_ohlcv(sym, "4h", limit=1500),
        "15m": loader.get_ohlcv(sym, "15m", limit=1500),
        "5m": loader.get_ohlcv(sym, "5m", limit=1500),
    }
    if any(f.empty for f in frames.values()):
        continue
    data[sym] = frames

# 후보: 레지스트리 그리드에서 vpvr_accum만 시드 고정 샘플.
rng = random.Random(42)
grid = TEMPLATES["vpvr_accum"]
specs = [
    StrategySpec(
        "vpvr_accum",
        _fix_constraints(
            "vpvr_accum", {k: _sample(pr, rng) for k, pr in grid.items()}
        ),
    )
    for _ in range(N_SAMPLES)
]
champ_row = db.execute(
    "SELECT template, params_json FROM strategies WHERE id = ?", (CHAMPION_ID,)
)[0]
champion = StrategySpec(champ_row["template"], json.loads(champ_row["params_json"]))


def run_window(settings: Settings, label: str) -> None:
    tf = settings.execution_timeframe
    regimes = {
        sym: regime_service.align_to(frames[tf].index)
        for sym, frames in data.items()
    }
    fundings = {}
    for sym, frames in data.items():
        idx = frames[tf].index
        fundings[sym] = funding_loader.get_funding(
            sym, int(idx[0].value // 1_000_000), int(idx[-1].value // 1_000_000)
        )
    cost = PerpCostModel(settings.maker_fee, settings.taker_fee, settings.slippage)
    idx0 = next(iter(data.values()))[tf].index
    print(f"\n===== {label} — 창: {idx0[0]} ~ {idx0[-1]} ({len(idx0)}봉 {tf})")

    rows = []
    for i, spec in enumerate(specs):
        agg, _, _ = evaluate_spec(
            spec, data, cost, settings, regimes=regimes, fundings=fundings
        )
        if agg:
            rows.append((spec, agg))
    traded = [(s, a) for s, a in rows if (a.get("trade_count") or 0) > 0]
    print(f"vpvr_accum 후보 {N_SAMPLES}개 중 거래 발생: {len(traded)}개")
    traded.sort(key=lambda x: (x[1].get("total_return") or 0), reverse=True)
    for spec, a in traded[:5]:
        print(
            f"  수익 {a['total_return']:+.2%} 승률 {(a['win_rate'] or 0):.0%} "
            f"MDD {(a['mdd'] or 0):.1%} 샤프 {a['sharpe'] and round(a['sharpe'],2)} "
            f"거래 {a['trade_count']} 청산 {a['liquidation_count']} | "
            + ", ".join(f"{k}={round(v,3) if isinstance(v,float) else v}"
                        for k, v in spec.params.items()
                        if k in ("rise_min", "consol_band", "conc_min", "leverage"))
        )
    if traded:
        rets = [a["total_return"] or 0 for _, a in traded]
        wins = [a["win_rate"] or 0 for _, a in traded]
        print(f"  (중앙값: 수익 {sorted(rets)[len(rets)//2]:+.2%}, 승률 {sorted(wins)[len(wins)//2]:.0%})")

    agg, _, _ = evaluate_spec(
        champion, data, cost, settings, regimes=regimes, fundings=fundings
    )
    if agg:
        print(
            f"[참조] 챔피언 box_range: 수익 {agg['total_return']:+.2%} 승률 "
            f"{(agg['win_rate'] or 0):.0%} MDD {(agg['mdd'] or 0):.1%} "
            f"거래 {agg['trade_count']}"
        )


run_window(s15, "A) 15m 실행 · 최근 창")
run_window(s4h, "B) 4h 실행 · 장기 창")
db.close()
print("\n(임시 DB만 사용 — 프로덕션 무영향)")
