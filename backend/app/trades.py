"""종결 거래의 실현 손익 롤업.

``/api/trade-history`` (포트폴리오 > 거래 내역)와 매매일지가 **같은 함수**를
쓴다. 두 화면이 같은 거래를 다른 숫자로 보여주면 어느 쪽을 믿어야 할지 알 수
없으므로, 롤업은 한 군데에만 둔다.

체결 기록(paper_orders 미러 — live도 동일 테이블)을 플랜 단위로 묶고, 펀딩은
보유 구간의 정산분 합(양수 = 지불 비용)으로 계산한다.
"""
from __future__ import annotations

import json


def trade_history_rows(db, limit: int | None = 50) -> list[dict]:
    """종결된 플랜의 실현 손익 내역 롤업 (get_trade_history + 누적 합계 공용).

    체결 기록(paper_orders 미러 — live도 동일 테이블)을 플랜 단위로 롤업한다.
    강제 청산 행은 plan_id 없이 기록되므로 심볼+사유+시각 윈도로 귀속시킨다.
    펀딩은 보유 구간의 정산분 합(양수 = 지불 비용). limit=None이면 전체."""
    rows: list[dict] = []
    sql = (
        "SELECT * FROM trade_plans WHERE status IN ('closed', 'stopped') "
        "AND filled_fraction > 0 ORDER BY id DESC"
    )
    query_params: tuple = ()
    if limit is not None:
        sql += " LIMIT ?"
        query_params = (int(limit),)
    for plan_row in db.execute(sql, query_params):
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
