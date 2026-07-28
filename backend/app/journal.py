"""매매일지 — 종결 거래를 "왜 들어갔고 / 어떻게 잡았고 / 어떻게 끝났는지"로 조립.

설계: ``docs/superpowers/specs/2026-07-27-trade-journal-design.md``

파생 뷰다. 자동 서술은 저장하지 않고 조회할 때마다 만든다 — 과거 거래에도
즉시 적용되고, 문구를 고치면 전부 반영된다. 사람이 쓴 메모만 ``journal_notes``에
남는다.

숫자는 ``trades.trade_history_rows``(거래 내역과 같은 롤업)를 그대로 쓴다.
여기서 따로 계산하면 두 화면이 같은 거래를 다른 숫자로 보여주게 된다.
"""
from __future__ import annotations

import datetime as dt
import json
import math

from .db import Database
from .trades import trade_history_rows

#: 손익비 게이트 — BTC·ETH는 1:2, 그 외 알트는 1:3 (규칙 §1).
MAJORS = ("BTCUSDT", "ETHUSDT")

#: 가격 표시 유효숫자. DOGE 래더 3개가 0.0728로 뭉개지던 문제를 막는다.
PRICE_SIGNIFICANT_DIGITS = 6


def price_decimals(prices: list[float]) -> int:
    """가격 목록을 구분해서 보여주는 데 필요한 소수 자릿수.

    심볼마다 가격대가 달라 고정 자릿수로는 안 된다 — ETH는 1,948.44면 충분하고
    DOGE는 0.072841/0.072886/0.072947을 구분해야 한다. 가장 큰 값 기준으로
    유효숫자 6자리를 맞춘다.
    """
    biggest = max((abs(p) for p in prices if p), default=0.0)
    if biggest <= 0:
        return 2
    magnitude = math.floor(math.log10(biggest))
    return max(2, min(8, PRICE_SIGNIFICANT_DIGITS - 1 - magnitude))


def _parse(ts: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _holding_minutes(entry_ts: str, exit_ts: str) -> int:
    return max(0, int((_parse(exit_ts) - _parse(entry_ts)).total_seconds() // 60))


def _rr(side: str, weighted_entry: float, weighted_tp: float, stop: float) -> float | None:
    """Side-aware 정규화 손익비 (스펙 §2). 위험이 0이면 정의되지 않는다."""
    risk = (weighted_entry - stop) if side == "long" else (stop - weighted_entry)
    if risk <= 0:
        return None
    reward = (weighted_tp - weighted_entry) if side == "long" else (weighted_entry - weighted_tp)
    return reward / risk


def _weighted(legs: list[dict]) -> float:
    return sum(float(l.get("price", 0.0)) * float(l.get("fraction", 0.0)) for l in legs)


def _entry_legs(planned: list[dict], fills: list[dict]) -> list[dict]:
    """계획한 래더 레그 + 실제 체결 여부.

    미체결 레그를 보여주는 것이 핵심이다 — '3개 중 1개만 채워진 진입'과
    '전부 채워진 진입'은 같은 플랜이라도 완전히 다른 거래다.
    """
    by_index = {int(f["leg_index"]): f for f in fills if f["leg_index"] is not None}
    legs = []
    for index, leg in enumerate(planned):
        fill = by_index.get(index)
        legs.append(
            {
                "index": index,
                "price": float(leg.get("price", 0.0)),
                "fraction": float(leg.get("fraction", 0.0)),
                "filled": fill is not None,
                "fill_price": float(fill["avg_fill_price"]) if fill else None,
                "fill_ts": fill["ts"] if fill else None,
            }
        )
    return legs


def _tp_legs(planned: list[dict], fills: list[dict]) -> list[dict]:
    """익절 레그 + 체결 여부.

    TTL 재발주로 같은 레그가 여러 번 기록될 수 있어(취소 후 재발주) 체결된
    행만 leg_index로 본다.
    """
    filled = {int(f["leg_index"]) for f in fills if f["leg_index"] is not None}
    return [
        {
            "index": index,
            "price": float(leg.get("price", 0.0)),
            "fraction": float(leg.get("fraction", 0.0)),
            "filled": index in filled,
        }
        for index, leg in enumerate(planned)
    ]


def _outcome(exit_reason: str) -> str:
    return {
        "익절": "take_profit",
        "손절": "stop_loss",
        "강제 청산": "liquidation",
    }.get(exit_reason, "closed")


def compose_entries(db: Database, limit: int | None = 50) -> list[dict]:
    """종결 거래를 일지 항목으로 조립한다 (최신순).

    설계 의도(plan_json)를 읽을 수 없는 거래는 숫자만으로도 항목을 만든다 —
    레거시 행 때문에 일지에 구멍이 나는 것보다 낫다.
    """
    rows = trade_history_rows(db, limit=limit)
    if not rows:
        return []

    notes = {
        int(r["plan_id"]): r["note"]
        for r in db.execute("SELECT plan_id, note FROM journal_notes")
    }

    entries: list[dict] = []
    for row in rows:
        plan_id = int(row["plan_id"])
        plan_rows = db.execute(
            "SELECT plan_json FROM trade_plans WHERE id = ?", (plan_id,)
        )
        payload = json.loads(plan_rows[0]["plan_json"] or "{}") if plan_rows else {}
        planned_entries = payload.get("entries") or []
        planned_tps = payload.get("tps") or []
        stop_price = float((payload.get("stop") or {}).get("price") or 0.0)

        orders = db.execute(
            "SELECT * FROM paper_orders WHERE plan_id = ? AND status = 'filled' "
            "ORDER BY id",
            (plan_id,),
        )
        entry_fills = [o for o in orders if not o["reduce_only"]]
        tp_fills = [o for o in orders if o["reduce_only"] and o["leg_kind"] == "tp"]

        weighted_entry = _weighted(planned_entries)
        weighted_tp = _weighted(planned_tps)
        side = row["side"]
        margin = (
            row["avg_entry"] * row["qty"] / row["leverage"] if row["leverage"] else 0.0
        )
        prices = [l.get("price", 0.0) for l in planned_entries + planned_tps]
        prices += [stop_price, row["avg_entry"], row["avg_exit"]]

        entries.append(
            {
                "plan_id": plan_id,
                "symbol": row["symbol"],
                "side": side,
                "leverage": row["leverage"],
                "outcome": _outcome(row["exit_reason"]),
                "exit_reason": row["exit_reason"],
                "entry_ts": row["entry_ts"],
                "exit_ts": row["exit_ts"],
                "holding_minutes": _holding_minutes(row["entry_ts"], row["exit_ts"]),
                "evidence": payload.get("evidence") or [],
                "entry_legs": _entry_legs(planned_entries, entry_fills),
                "planned_weighted_entry": weighted_entry or None,
                "actual_avg_entry": row["avg_entry"],
                "qty": row["qty"],
                "stop": {
                    "price": stop_price or None,
                    "distance_pct": (
                        abs(weighted_entry - stop_price) / weighted_entry
                        if weighted_entry and stop_price
                        else None
                    ),
                },
                "tps": _tp_legs(planned_tps, tp_fills),
                "rr": _rr(side, weighted_entry, weighted_tp, stop_price)
                if weighted_entry and stop_price and weighted_tp
                else None,
                "rr_gate": 2.0 if row["symbol"] in MAJORS else 3.0,
                "avg_exit": row["avg_exit"],
                "pnl_usdt": row["pnl_usdt"],
                "funding_paid": row["funding_paid"],
                "margin_usdt": margin,
                "ret_on_margin": row["ret_on_margin"],
                "price_decimals": price_decimals(prices),
                "note": notes.get(plan_id, ""),
            }
        )
    return entries


def upsert_note(db: Database, plan_id: int, note: str) -> dict:
    """메모 저장. 종결 거래가 아니면 ``KeyError``."""
    exists = db.execute(
        "SELECT 1 FROM trade_plans WHERE id = ? AND status IN ('closed', 'stopped')",
        (plan_id,),
    )
    if not exists:
        raise KeyError(plan_id)
    db.execute(
        "INSERT INTO journal_notes (plan_id, note, updated_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(plan_id) DO UPDATE SET note = excluded.note, "
        "updated_at = excluded.updated_at",
        (plan_id, note),
    )
    row = db.execute(
        "SELECT plan_id, note, updated_at FROM journal_notes WHERE plan_id = ?",
        (plan_id,),
    )[0]
    return {
        "plan_id": int(row["plan_id"]),
        "note": row["note"],
        "updated_at": row["updated_at"],
    }
