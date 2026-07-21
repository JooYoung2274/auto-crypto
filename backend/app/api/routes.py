"""REST API — spec §7 contract, verbatim shapes.

Coin Agents Office 라우트: 기존 사이클/리더보드/리포트/로그 계약 유지 +
크립토 선물 확장 — 모드 전환(핫스왑), 포지션(청산가·마진비율·펀딩 카운트다운),
레짐, 경제 이벤트(블랙아웃 소스), TradePlan 상세. 프론트엔드 계약은
frontend/src/lib/types.ts 와 정확히 일치해야 한다.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..agents.quant import aggregate_metrics
from ..agents.risk import trades_per_year
from ..broker.base import Position
from ..broker.paper import FUNDING_INTERVAL_MS, PaperBroker
from ..db import Database
from ..orchestrator import (
    CYCLE_KINDS,
    CycleInProgressError,
    GoalInProgressError,
    Orchestrator,
    compute_leaderboard,
)
from ..risk.plan import TradePlan, maintenance_margin_rate

router = APIRouter(prefix="/api")


def _orch(request: Request) -> Orchestrator:
    return request.app.state.orchestrator


# -- status / cycle --------------------------------------------------------------
@router.get("/status")
async def get_status(request: Request) -> dict:
    return _orch(request).status()


@router.post("/cycle/start")
async def start_cycle(request: Request) -> dict:
    # Optional JSON body {"kind": research|validate|trade}; missing/empty → research.
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — no/invalid body is allowed
        body = {}
    kind = (body or {}).get("kind") or "research"
    if kind not in CYCLE_KINDS:
        raise HTTPException(status_code=400, detail=f"unknown cycle kind: {kind}")
    try:
        cycle_id = await _orch(request).start_cycle(kind)
    except CycleInProgressError:
        raise HTTPException(status_code=409, detail="cycle already in progress")
    return {"cycle_id": cycle_id, "status": "running", "kind": kind}


@router.post("/cycle/stop")
async def stop_cycle(request: Request) -> dict:
    await _orch(request).stop_cycle()
    return {"status": "stopped"}


# -- goal-seek (목표 탐색 모드) ---------------------------------------------------
@router.post("/goal/start")
async def start_goal(request: Request) -> dict:
    try:
        _orch(request).start_goal()
    except GoalInProgressError:
        raise HTTPException(status_code=409, detail="goal mode already running")
    return {"status": "running"}


@router.post("/goal/stop")
async def stop_goal(request: Request) -> dict:
    await _orch(request).stop_goal()
    return {"status": "stopped"}


# -- leaderboard / strategies / backtests ----------------------------------------
@router.get("/leaderboard")
async def get_leaderboard(request: Request, limit: int = 20) -> list[dict]:
    db = request.app.state.db
    settings = request.app.state.settings
    return await asyncio.to_thread(
        compute_leaderboard, db, limit, settings.min_trades, settings
    )


# -- plans (TradePlan 상세 — 래더 레그 + 분할 체결 현황) ----------------------------
def _plan_info(row: dict) -> dict:
    """trade_plans 행 + plan_json → PlanInfo (frontend types.ts 계약)."""
    payload = json.loads(row["plan_json"] or "{}")
    return {
        "id": int(row["id"]),
        "symbol": row["symbol"],
        "side": row["side"],
        "status": row["status"],
        "entries": payload.get("entries", []),
        "tps": payload.get("tps", []),
        "stop": payload.get("stop"),
        "evidence": payload.get("evidence", []),
        "leverage": payload.get("leverage"),
        "margin_usdt": payload.get("margin_usdt"),
        "filled_fraction": float(row["filled_fraction"] or 0.0),
        "reject_reason": row["reject_reason"] or "",
        "created_at": row["created_at"],
    }


@router.get("/plans")
async def list_open_plans(request: Request) -> list[dict]:
    """대기·진행 중(approved|active) 플랜 목록 + 자식 주문 — 대기 주문 탭용.

    각 플랜은 PlanInfo에 ``orders``(분할 레그별 주문: 지정가·수량·상태·
    체결 수량)를 덧붙인 형태다."""
    db = request.app.state.db
    plans: list[dict] = []
    for row in db.execute(
        "SELECT * FROM trade_plans WHERE status IN ('approved', 'active') "
        "ORDER BY id DESC"
    ):
        info = _plan_info(row)
        info["orders"] = [
            {
                "id": int(o["id"]),
                "side": o["side"],
                "qty": float(o["qty"]),
                "limit_price": (
                    float(o["limit_price"])
                    if o["limit_price"] is not None
                    else None
                ),
                "status": o["status"],
                "leg_kind": o["leg_kind"],
                "leg_index": o["leg_index"],
                "filled_qty": float(o["filled_qty"] or 0.0),
                "reduce_only": bool(o["reduce_only"]),
                "ts": o["ts"],
            }
            for o in db.execute(
                "SELECT * FROM paper_orders WHERE plan_id = ? ORDER BY id",
                (int(row["id"]),),
            )
        ]
        plans.append(info)
    return plans


@router.get("/plans/{plan_id}")
async def get_plan(request: Request, plan_id: int) -> dict:
    db = request.app.state.db
    rows = db.execute("SELECT * FROM trade_plans WHERE id = ?", (plan_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="plan not found")
    return _plan_info(rows[0])


# -- champions -------------------------------------------------------------------
@router.get("/champions")
async def get_champions(request: Request) -> dict:
    """The strategy the trade cycle (모의거래) runs, plus every past reign.

    ``current`` is the reigning champion with per-symbol backtests and — when a
    TradePlan is open — its active plan (분할 진입 체결 현황) plus stop/TP
    distances derived from that plan; ``history`` lists demoted reigns
    newest-first (the open reign is excluded)."""
    db = request.app.state.db
    settings = request.app.state.settings

    board = await asyncio.to_thread(
        compute_leaderboard, db, 100, settings.min_trades, settings
    )
    champ = next((r for r in board if r["status"] == "champion"), None)
    current = None
    if champ is not None:
        sid = champ["strategy_id"]
        crowned = db.execute(
            "SELECT crowned_at FROM champion_history "
            "WHERE strategy_id = ? AND demoted_at IS NULL ORDER BY id DESC LIMIT 1",
            (sid,),
        )
        backtests = db.execute(
            "SELECT id, symbol, metrics_json FROM backtests "
            "WHERE strategy_id = ? ORDER BY id",
            (sid,),
        )
        # 현재 열려 있는 TradePlan (분할 진입 체결 현황 표시용).
        plan_rows = db.execute(
            "SELECT * FROM trade_plans WHERE status IN ('approved', 'active') "
            "ORDER BY id DESC LIMIT 1"
        )
        active_plan = _plan_info(plan_rows[0]) if plan_rows else None
        stop_pct = None
        take_profit_pct = None
        if plan_rows:
            with contextlib.suppress(Exception):
                plan = TradePlan.from_json(plan_rows[0]["plan_json"])
                entry = plan.weighted_entry
                if entry > 0:
                    stop_pct = abs(entry - plan.stop.price) / entry
                    take_profit_pct = stop_pct * plan.rr
        current = {
            "strategy_id": sid,
            "template": champ["template"],
            "params": champ["params"],
            "avg_metrics": champ["avg_metrics"],
            "low_confidence": champ["low_confidence"],
            "low_activity": champ["low_activity"],
            "status": champ["status"],
            "crowned_at": crowned[0]["crowned_at"] if crowned else None,
            "stop_pct": stop_pct,
            "take_profit_pct": take_profit_pct,
            "active_plan": active_plan,
            "backtests": [
                {
                    "id": int(b["id"]),
                    "symbol": b["symbol"],
                    "metrics": json.loads(b["metrics_json"]),
                }
                for b in backtests
            ],
        }

    reigns = db.execute(
        "SELECT ch.strategy_id, ch.crowned_at, ch.demoted_at, s.template, s.params_json "
        "FROM champion_history ch JOIN strategies s ON s.id = ch.strategy_id "
        "WHERE ch.demoted_at IS NOT NULL ORDER BY ch.id DESC"
    )
    history = []
    for r in reigns:
        bts = db.execute(
            "SELECT metrics_json FROM backtests WHERE strategy_id = ?",
            (r["strategy_id"],),
        )
        avg, _ = aggregate_metrics(
            [json.loads(b["metrics_json"]) for b in bts], settings.min_trades
        )
        history.append(
            {
                "strategy_id": int(r["strategy_id"]),
                "template": r["template"],
                "params": json.loads(r["params_json"]),
                "crowned_at": r["crowned_at"],
                "demoted_at": r["demoted_at"],
                "avg_metrics": avg,
            }
        )

    return {"current": current, "history": history}


@router.get("/strategies/{strategy_id}")
async def get_strategy(request: Request, strategy_id: int) -> dict:
    db = request.app.state.db
    settings = request.app.state.settings
    rows = db.execute(
        "SELECT id, cycle_id, template, params_json, universe_json, status "
        "FROM strategies WHERE id = ?",
        (strategy_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="strategy not found")
    s = rows[0]
    backtests = db.execute(
        "SELECT id, symbol, metrics_json, created_at FROM backtests "
        "WHERE strategy_id = ? ORDER BY id",
        (strategy_id,),
    )
    # StrategyDetail extends LeaderboardEntry (frontend types.ts): avg_metrics +
    # low_confidence/low_activity를 리더보드와 동일 계산으로 채운다.
    metrics_list = [json.loads(b["metrics_json"]) for b in backtests]
    avg, low_conf = aggregate_metrics(metrics_list, settings.min_trades)
    low_activity = trades_per_year(avg) < settings.min_trades_per_year
    return {
        "strategy_id": int(s["id"]),
        "cycle_id": s["cycle_id"],
        "template": s["template"],
        "params": json.loads(s["params_json"]),
        "universe": json.loads(s["universe_json"]),
        "status": s["status"],
        "avg_metrics": avg,
        "low_confidence": low_conf,
        "low_activity": low_activity,
        "backtests": [
            {
                "id": int(b["id"]),
                "symbol": b["symbol"],
                "metrics": json.loads(b["metrics_json"]),
                "created_at": b["created_at"],
            }
            for b in backtests
        ],
    }


@router.get("/backtests/{backtest_id}")
async def get_backtest(request: Request, backtest_id: int) -> dict:
    db = request.app.state.db
    rows = db.execute(
        "SELECT id, strategy_id, symbol, metrics_json, equity_curve_json, created_at "
        "FROM backtests WHERE id = ?",
        (backtest_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="backtest not found")
    b = rows[0]
    metrics = json.loads(b["metrics_json"])
    trades = db.execute(
        "SELECT entry_ts, exit_ts, entry_price, exit_price, net_ret, holding_hours, "
        "side, leverage, timeframe, funding_paid, fee_paid "
        "FROM trades WHERE backtest_id = ? ORDER BY id",
        (backtest_id,),
    )
    return {
        "id": int(b["id"]),
        "strategy_id": int(b["strategy_id"]),
        "symbol": b["symbol"],
        "timeframe": metrics.get("timeframe"),
        "metrics": metrics,
        "equity_curve": json.loads(b["equity_curve_json"]),
        "trades": trades,
        "created_at": b["created_at"],
    }


# -- reports ----------------------------------------------------------------------
@router.get("/reports")
async def list_reports(request: Request, limit: int = 50) -> list[dict]:
    db = request.app.state.db
    return db.execute(
        "SELECT id, cycle_id, kind, created_at FROM reports ORDER BY id DESC LIMIT ?",
        (limit,),
    )


@router.get("/reports/{report_id}")
async def get_report(request: Request, report_id: int) -> dict:
    db = request.app.state.db
    rows = db.execute(
        "SELECT id, cycle_id, kind, markdown, created_at FROM reports WHERE id = ?",
        (report_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="report not found")
    return rows[0]


# -- logs ------------------------------------------------------------------------
@router.get("/logs")
async def get_logs(
    request: Request,
    agent: str | None = None,
    limit: int = 50,
    before_id: int | None = None,
) -> list[dict]:
    db = request.app.state.db
    sql = "SELECT id, ts, agent, level, event_type, message, data_json FROM activity_log WHERE event_type = 'log'"
    params: list = []
    if agent:
        sql += " AND agent = ?"
        params.append(agent)
    if before_id is not None:
        sql += " AND id < ?"
        params.append(before_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = db.execute(sql, tuple(params))
    return [
        {
            "id": r["id"],
            "ts": r["ts"],
            "agent": r["agent"],
            "level": r["level"],
            "event_type": r["event_type"],
            "message": r["message"],
            "data": json.loads(r["data_json"] or "{}"),
        }
        for r in rows
    ]


# -- positions / portfolio ---------------------------------------------------------
def _next_funding_iso(now_ms: int) -> str:
    """다음 8h UTC 펀딩 정산 시각 (카운트다운 표시용, UTC-naive ISO)."""
    next_ms = (now_ms // FUNDING_INTERVAL_MS + 1) * FUNDING_INTERVAL_MS
    return dt.datetime.fromtimestamp(next_ms / 1000, tz=dt.timezone.utc).replace(
        tzinfo=None
    ).isoformat()


def _position_info(pos: Position, db: Database, now_ms: int) -> dict:
    """Position → PositionInfo (청산가 + 마진비율 + 펀딩 카운트다운, 스펙 §7)."""
    mark = pos.mark_price or pos.avg_entry
    notional = mark * pos.qty
    maint = notional * maintenance_margin_rate(notional)
    margin_balance = pos.isolated_margin + (pos.unrealized_pnl or 0.0)
    margin_ratio = maint / margin_balance if margin_balance > 0 else None
    # funding_paid는 **현재 포지션**에 귀속된 펀딩만 합산한다 (심볼 전체 이력이
    # 아니라). 현재 열린 플랜의 최초 진입 체결 시각 이후 정산분만 — 없으면 플랜
    # created_at 폴백. ts 형식 혼용(‘T’+offset vs SQLite 공백)은 substr 정규화로 흡수.
    # 'stopped'도 포함 — 4h 손절 판정 후 스탑엑싯이 아직 체이스 중인(수 분,
    # 미체결이면 더 긴) 포지션도 올바른 펀딩 윈도·tp_lines·stop_price를
    # 유지하게 (finding #4). 방향도 매칭 — 헤지 모드 반대 방향 포지션에 이
    # 플랜의 라인을 붙이지 않게 (finding #5).
    plan_rows = db.execute(
        "SELECT id, created_at, plan_json FROM trade_plans WHERE symbol = ? "
        "AND side = ? AND status IN ('approved', 'active', 'stopped') "
        "ORDER BY id DESC LIMIT 1",
        (pos.symbol, pos.side),
    )
    # 익절/손절 라인 (포트폴리오 표시용): 실제로 걸려 있는 reduce-only TP
    # 주문을 우선 사용하고, 아직 발주 전이면 플랜의 TP 레그로 폴백.
    tp_lines: list[dict] = []
    stop_price: float | None = None
    if plan_rows:
        payload = json.loads(plan_rows[0]["plan_json"] or "{}")
        stop_leg = payload.get("stop") or {}
        stop_price = stop_leg.get("price")
        open_tps = db.execute(
            "SELECT limit_price, qty FROM paper_orders "
            "WHERE plan_id = ? AND reduce_only = 1 AND aggressive = 0 "
            "AND status = 'open'",
            (int(plan_rows[0]["id"]),),
        )
        if open_tps:
            tp_lines = [
                {"price": float(r["limit_price"]), "qty": float(r["qty"])}
                for r in open_tps
            ]
        else:
            tp_lines = [
                {"price": float(leg["price"]), "qty": pos.qty * float(leg["fraction"])}
                for leg in payload.get("tps", [])
            ]
        # TP1 = 진입가에서 가까운 순 (long 오름차순, short 내림차순).
        tp_lines.sort(key=lambda t: t["price"], reverse=(pos.side == "short"))
    if plan_rows:
        entry_fill = db.execute(
            "SELECT MIN(substr(replace(ts, 'T', ' '), 1, 19)) AS t FROM paper_orders "
            "WHERE plan_id = ? AND status = 'filled' "
            "AND client_order_id LIKE '%-entry-%'",
            (int(plan_rows[0]["id"]),),
        )
        threshold = entry_fill[0]["t"] or db.execute(
            "SELECT substr(replace(?, 'T', ' '), 1, 19) AS t",
            (plan_rows[0]["created_at"],),
        )[0]["t"]
        funding_rows = db.execute(
            "SELECT COALESCE(SUM(payment), 0) AS total FROM funding_payments "
            "WHERE symbol = ? AND substr(replace(ts, 'T', ' '), 1, 19) >= ?",
            (pos.symbol, threshold),
        )
    else:
        # 소유 플랜이 approved/active/stopped 중 없어도 심볼 전체 이력을 무한
        # 합산하지 않는다 (finding #4): rejected/abandoned를 제외한 최신 플랜의
        # created_at을 펀딩 임계로 쓰고, 그런 플랜도 없을 때만 전체 합산.
        fallback = db.execute(
            "SELECT created_at FROM trade_plans WHERE symbol = ? AND side = ? "
            "AND status NOT IN ('rejected', 'abandoned') ORDER BY id DESC LIMIT 1",
            (pos.symbol, pos.side),
        )
        if fallback:
            threshold = db.execute(
                "SELECT substr(replace(?, 'T', ' '), 1, 19) AS t",
                (fallback[0]["created_at"],),
            )[0]["t"]
            funding_rows = db.execute(
                "SELECT COALESCE(SUM(payment), 0) AS total FROM funding_payments "
                "WHERE symbol = ? AND substr(replace(ts, 'T', ' '), 1, 19) >= ?",
                (pos.symbol, threshold),
            )
        else:
            funding_rows = db.execute(
                "SELECT COALESCE(SUM(payment), 0) AS total FROM funding_payments "
                "WHERE symbol = ?",
                (pos.symbol,),
            )
    return {
        "symbol": pos.symbol,
        "side": pos.side,
        "qty": pos.qty,
        "avg_entry": pos.avg_entry,
        "leverage": pos.leverage,
        "isolated_margin": pos.isolated_margin,
        "liq_price": pos.liq_price,
        "mark_price": pos.mark_price or None,
        "unrealized_pnl": pos.unrealized_pnl,
        "margin_ratio": margin_ratio,
        # funding_payments.payment는 지갑 현금흐름(+ = 수취)이고, PositionInfo
        # 계약은 '지불액'(+ = 비용)이므로 부호를 뒤집는다 — UI는 이 값을 다시
        # 손익 관점(-)으로 표기한다.
        "funding_paid": -float(funding_rows[0]["total"]),
        "next_funding_ts": _next_funding_iso(now_ms),
        "tp_lines": tp_lines,
        "stop_price": stop_price,
    }


@router.get("/positions")
async def get_positions(request: Request) -> list[dict]:
    """오픈 포지션 목록 — 청산가·마진비율·펀딩 카운트다운 포함 (스펙 §7)."""
    broker = request.app.state.broker
    db = request.app.state.db
    now_ms = int(time.time() * 1000)
    positions = await broker.get_positions()
    return [_position_info(pos, db, now_ms) for pos in positions]


@router.get("/trade-history")
async def get_trade_history(request: Request, limit: int = 50) -> list[dict]:
    """종결된 플랜의 실현 손익 내역 (손절/익절/강제 청산 라벨 포함).

    체결 기록(paper_orders 미러 — live도 동일 테이블)을 플랜 단위로 롤업한다.
    강제 청산 행은 plan_id 없이 기록되므로 심볼+사유+시각 윈도로 귀속시킨다.
    펀딩은 보유 구간의 정산분 합(양수 = 지불 비용)."""
    db = request.app.state.db
    rows: list[dict] = []
    for plan_row in db.execute(
        "SELECT * FROM trade_plans WHERE status IN ('closed', 'stopped') "
        "AND filled_fraction > 0 ORDER BY id DESC LIMIT ?",
        (int(limit),),
    ):
        plan_id = int(plan_row["id"])
        payload = json.loads(plan_row["plan_json"] or "{}")
        side = plan_row["side"]
        sign = 1.0 if side == "long" else -1.0
        fills = db.execute(
            "SELECT * FROM paper_orders WHERE plan_id = ? AND status = 'filled' "
            "ORDER BY id",
            (plan_id,),
        )
        entries = [f for f in fills if not f["reduce_only"]]
        exits = [f for f in fills if f["reduce_only"]]
        liquidated = plan_row["reject_reason"] == "강제 청산"
        if liquidated:
            # plan_id가 스탬핑된 청산 행은 이미 위 exits(plan_id = ?)에 포함된다.
            # 레거시 plan_id-NULL 청산 행만, 다음 플랜 생성 시각을 상한으로 두고
            # 귀속한다 — 재진입 심볼의 나중 청산을 이 플랜에 이중계상하지 않게
            # (finding #3/#13).
            next_created = db.execute(
                "SELECT MIN(created_at) AS c FROM trade_plans "
                "WHERE symbol = ? AND id > ?",
                (plan_row["symbol"], plan_id),
            )[0]["c"]
            params: list = [plan_row["symbol"], plan_row["created_at"]]
            upper = ""
            if next_created is not None:
                upper = (
                    "AND substr(replace(ts, 'T', ' '), 1, 19) < "
                    "substr(replace(?, 'T', ' '), 1, 19) "
                )
                params.append(next_created)
            exits += db.execute(
                "SELECT * FROM paper_orders WHERE symbol = ? AND status = 'filled' "
                "AND plan_id IS NULL AND reason LIKE '%강제 청산%' "
                "AND substr(replace(ts, 'T', ' '), 1, 19) >= "
                "substr(replace(?, 'T', ' '), 1, 19) " + upper + "ORDER BY id",
                tuple(params),
            )
        if not entries or not exits:
            continue

        def _avg(fs: list[dict]) -> tuple[float, float]:
            qty = sum(float(f["filled_qty"] or f["qty"]) for f in fs)
            px = sum(
                float(f["avg_fill_price"] or f["limit_price"] or 0.0)
                * float(f["filled_qty"] or f["qty"])
                for f in fs
            )
            return (px / qty if qty > 0 else 0.0, qty)

        avg_entry, entry_qty = _avg(entries)
        avg_exit, exit_qty = _avg(exits)
        matched = min(entry_qty, exit_qty)
        pnl = sign * (avg_exit - avg_entry) * matched
        leverage = int(payload.get("leverage") or 1)
        margin = avg_entry * entry_qty / max(1, leverage)
        first_entry = min(f["ts"] for f in entries)
        last_exit = max(f["ts"] for f in exits)
        funding = db.execute(
            "SELECT COALESCE(SUM(payment), 0.0) AS t FROM funding_payments "
            "WHERE symbol = ? "
            "AND substr(replace(ts, 'T', ' '), 1, 19) >= "
            "substr(replace(?, 'T', ' '), 1, 19) "
            "AND substr(replace(ts, 'T', ' '), 1, 19) <= "
            "substr(replace(?, 'T', ' '), 1, 19)",
            (plan_row["symbol"], first_entry, last_exit),
        )
        funding_paid = -float(funding[0]["t"])  # + = 지불 비용
        if liquidated:
            exit_reason = "강제 청산"
        elif plan_row["status"] == "stopped":
            exit_reason = "손절"
        else:
            exit_reason = "익절"
        rows.append(
            {
                "plan_id": plan_id,
                "symbol": plan_row["symbol"],
                "side": side,
                "leverage": leverage,
                "entry_ts": first_entry,
                "exit_ts": last_exit,
                "avg_entry": avg_entry,
                "avg_exit": avg_exit,
                "qty": matched,
                "pnl_usdt": pnl - funding_paid,
                "funding_paid": funding_paid,
                "ret_on_margin": (pnl - funding_paid) / margin if margin > 0 else 0.0,
                "exit_reason": exit_reason,
            }
        )
    return rows


@router.get("/portfolio")
async def get_portfolio(request: Request) -> dict:
    """선물 지갑 요약 (총자산/사용가능/포지션마진/미실현/펀딩) + 포지션 +
    포트폴리오 스냅샷 히스토리."""
    broker = request.app.state.broker
    db = request.app.state.db
    balance = await broker.get_balance()
    now_ms = int(time.time() * 1000)
    positions = [
        _position_info(pos, db, now_ms) for pos in await broker.get_positions()
    ]
    snapshots = db.execute(
        "SELECT ts, wallet_balance, available, margin_used, unrealized_pnl, "
        "funding_cum, total_value FROM "
        "(SELECT id, ts, wallet_balance, available, margin_used, unrealized_pnl, "
        "funding_cum, total_value FROM portfolio_snapshots "
        "ORDER BY id DESC LIMIT 100) ORDER BY id"
    )
    funding_cum = snapshots[-1]["funding_cum"] if snapshots else 0.0
    return {
        "wallet_balance": balance.wallet_balance,
        "available": balance.available,
        "margin_used": balance.margin_used,
        "unrealized_pnl": balance.unrealized_pnl,
        "funding_cum": funding_cum,
        # 복리 금지 스윕 누적액 — 시드 초과 실현 수익의 장부상 금고 (규칙 §1).
        "withdrawn_cum": float(
            db.execute(
                "SELECT COALESCE(SUM(amount), 0.0) AS total FROM withdrawal_ledger"
            )[0]["total"]
        ),
        "positions": positions,
        "snapshots": snapshots,
    }


# -- regime ------------------------------------------------------------------------
@router.get("/regime")
async def get_regime(request: Request) -> dict:
    """최신 레짐 판정 (market_regime 캐시, 스펙 §3.1). 히스토리 부재 시 'cash'."""
    db = request.app.state.db
    rows = db.execute(
        "SELECT date, alt_index, dom_proxy, regime FROM market_regime "
        "ORDER BY date DESC LIMIT 1"
    )
    if not rows:
        return {"date": None, "regime": "cash", "alt_index": None, "dom_proxy": None}
    r = rows[0]
    return {
        "date": r["date"],
        "regime": r["regime"],
        "alt_index": r["alt_index"],
        "dom_proxy": r["dom_proxy"],
    }


# -- econ events (이벤트 블랙아웃 소스) ----------------------------------------------
class EconEventIn(BaseModel):
    ts: str  # ISO datetime (UTC)
    name: str


@router.get("/econ-events")
async def get_econ_events(request: Request) -> list[dict]:
    db = request.app.state.db
    return db.execute("SELECT id, ts, name FROM econ_events ORDER BY ts")


@router.put("/econ-events")
async def put_econ_events(request: Request, events: list[EconEventIn]) -> list[dict]:
    """경제 이벤트 목록 전체 교체 — ±blackout_hours 신규 진입 금지 윈도의 소스."""
    for event in events:
        try:
            dt.datetime.fromisoformat(event.ts)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"잘못된 이벤트 시각(ISO 필요): {event.ts}"
            )
    db = request.app.state.db
    db.execute("DELETE FROM econ_events")
    for event in events:
        db.execute(
            "INSERT INTO econ_events (ts, name) VALUES (?, ?)",
            (event.ts, event.name),
        )
    return db.execute("SELECT id, ts, name FROM econ_events ORDER BY ts")


# -- trading mode (paper ↔ live 핫스왑, 스펙 §5) -------------------------------------
class TradingModeRequest(BaseModel):
    mode: Literal["paper", "live"]
    confirm: str | None = None


@router.post("/trading-mode")
async def set_trading_mode(request: Request, body: TradingModeRequest) -> dict:
    """모드 전환 — flat-and-idle 게이트(409) 후 app.state.broker 핫스왑.

    broker_provider()가 매 사이클/틱 시작 시 app.state.broker를 조회하므로
    여기서의 교체가 즉시 반영된다. live 전환은 confirm='LIVE' 타이핑 확인 +
    키 검증/리컨실 성공 후에만 활성화 (키 미설정 시 400)."""
    app = request.app
    settings = app.state.settings
    db = app.state.db
    if body.mode == settings.trading_mode:
        return {"trading_mode": settings.trading_mode}

    # live 전환은 타이핑 확인 우선 검사 (락 잡기 전 빠른 400).
    if body.mode == "live" and body.confirm != "LIVE":
        raise HTTPException(
            status_code=400,
            detail="live 전환은 confirm='LIVE' 타이핑 확인이 필요합니다",
        )

    # 공유 trade_lock으로 flat-and-idle 검사~핫스왑~old broker aclose 전체를
    # 원자화한다 (스펙 §5): 락을 잡으면 trade 사이클/PositionMonitor 틱이 진행
    # 중일 수 없어 TOCTOU와 '사용 중 aclose' 경합이 모두 사라진다.
    async with app.state.orchestrator.trade_lock:
        # flat-and-idle 게이트: 사이클 실행 중이거나, 미결 플랜, 또는 현재 모드에
        # 오픈 주문/포지션이 있으면 409 거부.
        if _orch(request).running:
            raise HTTPException(
                status_code=409,
                detail="사이클 실행 중 — 모드 전환 불가 (flat-and-idle 필수)",
            )
        broker = app.state.broker
        open_orders = await broker.get_open_orders()
        positions = await broker.get_positions()
        if open_orders or positions:
            raise HTTPException(
                status_code=409,
                detail="오픈 주문/포지션 존재 — 모드 전환 불가 (flat-and-idle 필수)",
            )
        # 미결 플랜 게이트 (belt-and-suspenders): 오픈 주문/포지션이 없더라도
        # approved/active 플랜이 남아 있으면 전환 거부 (스펙 §5 flat-and-idle).
        if db.execute(
            "SELECT id FROM trade_plans WHERE status IN ('approved', 'active') LIMIT 1"
        ):
            raise HTTPException(
                status_code=409,
                detail="진행 중 플랜(approved/active) 존재 — 모드 전환 불가 (flat-and-idle 필수)",
            )

        if body.mode == "live":
            # Lazy import: paper 모드는 라이브 어댑터를 절대 건드리지 않는다.
            # 거래소 선택 — Binance 주(primary), OKX 추가 (CA_EXCHANGE).
            from ..broker.binance import BinanceBroker, BinanceConfigError
            from ..broker.okx import OKXBroker, OKXConfigError

            try:
                if settings.exchange == "okx":
                    new_broker = OKXBroker(settings, db=db)
                else:
                    new_broker = BinanceBroker(settings, db=db)
            except (BinanceConfigError, OKXConfigError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"거래소 API 키 미설정 — live 전환 불가: {exc}",
                )
            try:
                # 키 검증 + isolated/레버리지 설정 + 부팅 리컨실 (스펙 §2/§5).
                await new_broker.reconcile()
            except Exception as exc:  # noqa: BLE001 — 어떤 실패든 live 전환 거부
                with contextlib.suppress(Exception):
                    await new_broker.aclose()
                raise HTTPException(
                    status_code=400,
                    detail=f"live 전환 실패 — 키 검증/리컨실 오류: {exc}",
                )
        else:
            new_broker = PaperBroker(db, app.state.loader, settings)

        old_broker = app.state.broker
        app.state.broker = new_broker  # 핫스왑: broker_provider()가 즉시 따라온다
        settings.trading_mode = body.mode
        # aclose는 락 안에서 수행 — 이 시점엔 어떤 사이클/틱도 old_broker의 HTTP
        # 클라이언트를 사용 중이지 않다 (사용 중 close 경합 제거).
        aclose = getattr(old_broker, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()
    return {"trading_mode": settings.trading_mode}


# -- config -----------------------------------------------------------------------
class ConfigUpdate(BaseModel):
    universe: list[str] | None = None
    auto_cycle_minutes: int | None = None
    auto_trade_after_research: bool | None = None
    bar_close_trade_enabled: bool | None = None
    max_mdd: float | None = None
    min_trades: int | None = None
    min_trades_per_year: float | None = None
    candidates_per_cycle: int | None = None
    max_concurrent_positions: int | None = None
    daily_max_loss_pct: float | None = None
    blackout_hours: float | None = None
    rank_w_sharpe: float | None = None
    rank_w_win_rate: float | None = None
    rank_w_mdd: float | None = None
    rank_w_cagr: float | None = None


def _config_view(settings) -> dict:
    return {
        # read-only — 모드 변경은 POST /api/trading-mode 로만 (config PUT 무시).
        "trading_mode": settings.trading_mode,
        "universe": settings.universe,
        "timeframes": settings.timeframes,
        "execution_timeframe": settings.execution_timeframe,
        "auto_cycle_minutes": settings.auto_cycle_minutes,
        "auto_trade_after_research": settings.auto_trade_after_research,
        "bar_close_trade_enabled": settings.bar_close_trade_enabled,
        "max_mdd": settings.max_mdd,
        "min_trades": settings.min_trades,
        "min_trades_per_year": settings.min_trades_per_year,
        "candidates_per_cycle": settings.candidates_per_cycle,
        "max_concurrent_positions": settings.max_concurrent_positions,
        "daily_max_loss_pct": settings.daily_max_loss_pct,
        "blackout_hours": settings.blackout_hours,
        "rank_w_sharpe": settings.rank_w_sharpe,
        "rank_w_win_rate": settings.rank_w_win_rate,
        "rank_w_mdd": settings.rank_w_mdd,
        "rank_w_cagr": settings.rank_w_cagr,
    }


@router.get("/config")
async def get_config(request: Request) -> dict:
    return _config_view(request.app.state.settings)


@router.put("/config")
async def put_config(request: Request, update: ConfigUpdate) -> dict:
    settings = request.app.state.settings
    for key, value in update.model_dump(exclude_none=True).items():
        setattr(settings, key, value)
    return _config_view(settings)
