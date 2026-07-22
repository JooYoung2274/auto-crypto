"""오프라인 실험: 실행 TF 15m vs 5m 백테스트 비교 (실거래 무영향).

프로덕션 DB를 임시 복사본으로 떠서 5m 히스토리를 4,500봉(≈15.6일)으로
백필한 뒤, 현재 챔피언 파라미터를 동일 캘린더 구간에서 두 실행 TF로
돌려 비교한다. 5m 런은 TTL 의미 보존을 위해 order/plan TTL을 3배로 조정.

실행: .venv/bin/python scripts/exp_5m_vs_15m.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from app.agents.quant import aggregate_metrics, evaluate_spec
from app.backtest.costs import PerpCostModel
from app.config import Settings
from app.data.funding import FundingLoader
from app.data.loader import DataLoader
from app.data.regime import RegimeService
from app.db import Database
from app.strategies.base import StrategySpec

PROD_DB = Path(__file__).resolve().parents[1] / "coinagent.db"
EXP_DB = Path("/tmp/exp_5m_vs_15m.db")
CHAMPION_ID = 2412
BARS_5M = 4500  # ≈ 15.6일 — 15m 1,500봉과 동일 창
BARS_OTHER = 1500

shutil.copy(PROD_DB, EXP_DB)
for suffix in ("-wal", "-shm"):
    p = Path(str(PROD_DB) + suffix)
    if p.exists():
        shutil.copy(p, str(EXP_DB) + suffix)

base = dict(db_path=str(EXP_DB), _env_file=None, initial_seed_usdt=1900.0)
s15 = Settings(execution_timeframe="15m", **base)
s5 = Settings(
    execution_timeframe="5m", order_ttl_bars=24, plan_ttl_bars=288, **base
)

db = Database(str(EXP_DB))
loader = DataLoader(db, settings=s15)
funding_loader = FundingLoader(db, s15)
regime_service = RegimeService(db, loader, s15)

# -- 5m 백필 (임시 DB에서만) --------------------------------------------------------
for sym in s15.universe:
    db.execute(
        "DELETE FROM ohlcv_cache WHERE symbol = ? AND timeframe = '5m'", (sym,)
    )
    df = loader.get_ohlcv(sym, "5m", limit=BARS_5M)
    print(f"5m 백필 {sym}: {len(df)}봉", flush=True)

regime_service.refresh(limit=400)

# -- 데이터 조립 (양 런 공통, 캘린더 구간 통일) ---------------------------------------
data: dict[str, dict[str, pd.DataFrame]] = {}
for sym in s15.universe:
    frames = {
        "1d": loader.get_ohlcv(sym, "1d", limit=BARS_OTHER),
        "4h": loader.get_ohlcv(sym, "4h", limit=BARS_OTHER),
        "15m": loader.get_ohlcv(sym, "15m", limit=BARS_OTHER),
        "5m": loader.get_ohlcv(sym, "5m", limit=BARS_5M),
    }
    if any(f.empty for f in frames.values()):
        print(f"{sym}: 프레임 부족 — 제외")
        continue
    start = max(frames["15m"].index[0], frames["5m"].index[0])
    frames["15m"] = frames["15m"][frames["15m"].index >= start]
    frames["5m"] = frames["5m"][frames["5m"].index >= start]
    data[sym] = frames

row = db.execute(
    "SELECT template, params_json FROM strategies WHERE id = ?", (CHAMPION_ID,)
)[0]
spec = StrategySpec(row["template"], json.loads(row["params_json"]))
print(f"\n챔피언 #{CHAMPION_ID}: {row['template']} {row['params_json']}")


def run(settings: Settings, label: str) -> None:
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
    agg, per_symbol, trades = evaluate_spec(
        spec, data, cost, settings, regimes=regimes, fundings=fundings
    )
    print(f"\n===== {label} (실행 TF {tf}) =====")
    if agg is None:
        print("집계 불가 (거래 없음)")
        return
    print(
        f"총수익 {agg.get('total_return', 0):+.2%} | 승률 "
        f"{(agg.get('win_rate') or 0):.1%} | MDD {(agg.get('mdd') or 0):.2%} | "
        f"샤프 {agg.get('sharpe')} | 거래 {agg.get('trade_count')} | "
        f"펀딩 {agg.get('funding_paid', 0):+.2f} | 수수료 {agg.get('fee_paid', 0):.2f} | "
        f"청산 {agg.get('liquidation_count')}"
    )
    for r in per_symbol:
        print(
            f"  {r['symbol']:9s} 수익 {r['total_return']:+.2%} 승률 "
            f"{(r['win_rate'] or 0):.0%} MDD {(r['mdd'] or 0):.1%} "
            f"거래 {r['trade_count']}"
        )


run(s15, "베이스라인")
run(s5, "5분 실행")
db.close()
print("\n(임시 DB만 사용 — 프로덕션 무영향)")
