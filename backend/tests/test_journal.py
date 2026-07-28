"""매매일지 — 종결 거래의 설계 의도와 결과를 조립한다.

여기서 지키는 것:
1. 숫자는 거래 내역(trade_history_rows)과 **같은 롤업**을 쓴다 — 두 화면이
   같은 거래를 다른 금액으로 보여주면 안 된다.
2. 가격 자릿수가 래더 레그를 구분할 수 있어야 한다 (DOGE 0.0728 뭉침 버그).
3. 미체결 레그가 드러나야 한다 — 1/3만 채워진 진입은 다른 거래다.
4. 메모는 종결 거래에만 붙고, 저장·조회가 왕복한다.
"""
from __future__ import annotations

import json

import pytest  # noqa: F401 — asyncio_mode=auto

from app.journal import compose_entries, price_decimals, upsert_note
from app.trades import trade_history_rows


def _plan(db, *, symbol="SOLUSDT", side="short", status="closed", entries, stop, tps,
          leverage=3, evidence=None, reject_reason=""):
    payload = {
        "symbol": symbol,
        "side": side,
        "evidence": evidence or ["근거 1", "근거 2"],
        "entries": [{"kind": "entry", "price": p, "fraction": f} for p, f in entries],
        "stop": {"kind": "stop", "price": stop, "fraction": 1.0},
        "tps": [{"kind": "tp", "price": p, "fraction": f} for p, f in tps],
        "leverage": leverage,
        "margin_usdt": 600.0,
    }
    rows = db.execute(
        "INSERT INTO trade_plans (created_at, symbol, side, plan_json, status, "
        "reject_reason, filled_fraction) VALUES (?, ?, ?, ?, ?, ?, 1.0)",
        ("2026-07-27T00:00:00+00:00", symbol, side, json.dumps(payload), status,
         reject_reason),
    )
    return int(rows[0]["id"])


def _fill(db, plan_id, *, symbol, side, qty, price, ts, reduce_only, leg_kind, leg_index):
    db.execute(
        "INSERT INTO paper_orders (ts, symbol, side, qty, order_type, limit_price, "
        "filled_qty, avg_fill_price, reduce_only, leverage, plan_id, leg_kind, "
        "leg_index, client_order_id, status) "
        "VALUES (?, ?, ?, ?, 'limit', ?, ?, ?, ?, 3, ?, ?, ?, ?, 'filled')",
        (ts, symbol, side, qty, price, qty, price, 1 if reduce_only else 0,
         plan_id, leg_kind, leg_index, f"{plan_id}-{leg_kind}-{leg_index}-0"),
    )


def _sol_trade(db, status="closed"):
    """3레그 전부 체결 → 익절 2레그 전부 체결된 숏 거래."""
    pid = _plan(
        db,
        status=status,
        entries=[(76.8777, 0.5), (76.9642, 0.25), (77.0796, 0.25)],
        stop=77.3969,
        tps=[(75.7598, 0.5), (75.23, 0.5)],
        evidence=["4h 확정 피벗 박스 73.38~77.08", "박스 상단 분할 진입", "상단 이탈 = 손절"],
    )
    for i, (qty, px) in enumerate([(12.0, 76.88), (6.0, 76.96), (6.0, 77.08)]):
        _fill(db, pid, symbol="SOLUSDT", side="sell", qty=qty, price=px,
              ts="2026-07-27T12:45:07+00:00", reduce_only=False,
              leg_kind="entry", leg_index=i)
    for i, px in enumerate([75.76, 75.23]):
        _fill(db, pid, symbol="SOLUSDT", side="buy", qty=12.0, price=px,
              ts="2026-07-27T13:45:07+00:00", reduce_only=True,
              leg_kind="tp", leg_index=i)
    return pid


# ── 1. 거래 내역과 숫자가 일치한다 ──────────────────────────────────────
def test_numbers_match_trade_history(db):
    pid = _sol_trade(db)
    entry = compose_entries(db)[0]
    history = {r["plan_id"]: r for r in trade_history_rows(db)}[pid]

    assert entry["pnl_usdt"] == history["pnl_usdt"]
    assert entry["actual_avg_entry"] == history["avg_entry"]
    assert entry["avg_exit"] == history["avg_exit"]
    assert entry["qty"] == history["qty"]
    assert entry["ret_on_margin"] == history["ret_on_margin"]
    assert entry["exit_reason"] == history["exit_reason"]


# ── 2. 설계 의도가 실린다 ───────────────────────────────────────────────
def test_carries_entry_evidence(db):
    _sol_trade(db)
    entry = compose_entries(db)[0]
    assert len(entry["evidence"]) == 3
    assert "피벗 박스" in entry["evidence"][0]


def test_stop_and_rr_are_derived_from_the_plan(db):
    _sol_trade(db)
    entry = compose_entries(db)[0]
    # 숏: rr = (wEntry − wTP) / (stop − wEntry)
    w_entry = 76.8777 * 0.5 + 76.9642 * 0.25 + 77.0796 * 0.25
    w_tp = 75.7598 * 0.5 + 75.23 * 0.5
    assert entry["rr"] == pytest.approx((w_entry - w_tp) / (77.3969 - w_entry))
    assert entry["stop"]["distance_pct"] == pytest.approx(
        (77.3969 - w_entry) / w_entry
    )
    assert entry["rr_gate"] == 3.0  # 알트


def test_major_symbols_use_the_lower_rr_gate(db):
    pid = _plan(db, symbol="ETHUSDT", entries=[(1948.44, 1.0)], stop=1966.28,
                tps=[(1917.63, 0.5), (1901.88, 0.5)])
    _fill(db, pid, symbol="ETHUSDT", side="sell", qty=1.0, price=1948.44,
          ts="2026-07-27T00:00:00+00:00", reduce_only=False, leg_kind="entry", leg_index=0)
    _fill(db, pid, symbol="ETHUSDT", side="buy", qty=1.0, price=1910.0,
          ts="2026-07-27T02:00:00+00:00", reduce_only=True, leg_kind="tp", leg_index=0)
    assert compose_entries(db)[0]["rr_gate"] == 2.0


def test_long_and_short_rr_use_opposite_geometry(db):
    """롱/숏 부호를 뒤집으면 손익비가 음수가 되거나 뒤집힌다."""
    pid = _plan(db, symbol="BTCUSDT", side="long", entries=[(100.0, 1.0)],
                stop=90.0, tps=[(130.0, 1.0)])
    _fill(db, pid, symbol="BTCUSDT", side="buy", qty=1.0, price=100.0,
          ts="2026-07-27T00:00:00+00:00", reduce_only=False, leg_kind="entry", leg_index=0)
    _fill(db, pid, symbol="BTCUSDT", side="sell", qty=1.0, price=130.0,
          ts="2026-07-27T01:00:00+00:00", reduce_only=True, leg_kind="tp", leg_index=0)
    assert compose_entries(db)[0]["rr"] == pytest.approx(3.0)


# ── 3. 체결 현황 ────────────────────────────────────────────────────────
def test_unfilled_entry_legs_are_visible(db):
    """1/3만 채워진 진입은 전부 채워진 진입과 다른 거래다."""
    pid = _plan(db, entries=[(76.88, 0.5), (76.96, 0.25), (77.08, 0.25)],
                stop=77.40, tps=[(75.76, 0.5), (75.23, 0.5)])
    _fill(db, pid, symbol="SOLUSDT", side="sell", qty=12.0, price=76.88,
          ts="2026-07-27T00:00:00+00:00", reduce_only=False, leg_kind="entry", leg_index=0)
    _fill(db, pid, symbol="SOLUSDT", side="buy", qty=12.0, price=75.76,
          ts="2026-07-27T01:00:00+00:00", reduce_only=True, leg_kind="tp", leg_index=0)

    legs = compose_entries(db)[0]["entry_legs"]
    assert [l["filled"] for l in legs] == [True, False, False]
    assert legs[0]["fill_price"] == 76.88
    assert legs[1]["fill_price"] is None


def test_tp_leg_fill_state(db):
    _sol_trade(db)
    entry = compose_entries(db)[0]
    assert [t["filled"] for t in entry["tps"]] == [True, True]
    assert entry["outcome"] == "take_profit"


def test_stopped_trade_is_labelled(db):
    pid = _plan(db, status="stopped", entries=[(76.88, 1.0)], stop=77.40,
                tps=[(75.76, 1.0)])
    _fill(db, pid, symbol="SOLUSDT", side="sell", qty=12.0, price=76.88,
          ts="2026-07-27T00:00:00+00:00", reduce_only=False, leg_kind="entry", leg_index=0)
    _fill(db, pid, symbol="SOLUSDT", side="buy", qty=12.0, price=77.40,
          ts="2026-07-27T01:00:00+00:00", reduce_only=True, leg_kind="stop", leg_index=0)
    entry = compose_entries(db)[0]
    assert entry["outcome"] == "stop_loss"
    assert entry["pnl_usdt"] < 0
    assert [t["filled"] for t in entry["tps"]] == [False]


def test_holding_minutes(db):
    _sol_trade(db)
    assert compose_entries(db)[0]["holding_minutes"] == 60


# ── 4. 가격 자릿수 — DOGE 래더 뭉침 방지 ────────────────────────────────
def test_price_decimals_distinguishes_doge_ladder():
    """0.072841 / 0.072886 / 0.072947 이 서로 다르게 보여야 한다."""
    ladder = [0.07284053, 0.07288633, 0.07294739]
    d = price_decimals(ladder)
    assert len({f"{p:.{d}f}" for p in ladder}) == 3


def test_price_decimals_stays_readable_for_large_prices():
    assert price_decimals([1948.44, 1966.28]) == 2
    assert price_decimals([76.8777, 77.3969]) == 4


def test_price_decimals_handles_empty_and_zero():
    assert price_decimals([]) == 2
    assert price_decimals([0.0, 0.0]) == 2


def test_doge_entry_legs_render_distinctly(db):
    pid = _plan(db, symbol="DOGEUSDT",
                entries=[(0.07284053, 0.5), (0.07288633, 0.25), (0.07294739, 0.25)],
                stop=0.07311532, tps=[(0.07226137, 0.5), (0.071985, 0.5)])
    for i, (qty, px) in enumerate([(12500.0, 0.07284), (6250.0, 0.07288), (6250.0, 0.07294)]):
        _fill(db, pid, symbol="DOGEUSDT", side="sell", qty=qty, price=px,
              ts="2026-07-27T00:00:00+00:00", reduce_only=False, leg_kind="entry", leg_index=i)
    _fill(db, pid, symbol="DOGEUSDT", side="buy", qty=25000.0, price=0.0722,
          ts="2026-07-27T01:00:00+00:00", reduce_only=True, leg_kind="tp", leg_index=0)

    entry = compose_entries(db)[0]
    d = entry["price_decimals"]
    rendered = {f"{l['price']:.{d}f}" for l in entry["entry_legs"]}
    assert len(rendered) == 3, f"래더가 뭉쳤다: {rendered}"


# ── 5. 메모 ────────────────────────────────────────────────────────────
def test_note_roundtrip(db):
    pid = _sol_trade(db)
    assert compose_entries(db)[0]["note"] == ""

    saved = upsert_note(db, pid, "박스 상단에서 잘 잡혔다. 다음엔 손절을 더 넓게.")
    assert saved["plan_id"] == pid
    assert compose_entries(db)[0]["note"].startswith("박스 상단에서")

    upsert_note(db, pid, "수정됨")
    assert compose_entries(db)[0]["note"] == "수정됨"
    assert len(db.execute("SELECT 1 FROM journal_notes")) == 1


def test_note_rejects_unknown_plan(db):
    with pytest.raises(KeyError):
        upsert_note(db, 9999, "없는 거래")


def test_note_rejects_open_plan(db):
    """진행 중인 거래에는 일지 메모를 붙이지 않는다 (아직 결과가 없다)."""
    pid = _plan(db, status="active", entries=[(76.88, 1.0)], stop=77.4, tps=[(75.76, 1.0)])
    with pytest.raises(KeyError):
        upsert_note(db, pid, "진행 중")


# ── 6. 목록 동작 ────────────────────────────────────────────────────────
def test_empty_db_returns_empty_list(db):
    assert compose_entries(db) == []


def test_newest_first_and_limit(db):
    first = _sol_trade(db)
    second = _sol_trade(db)
    entries = compose_entries(db)
    assert [e["plan_id"] for e in entries] == [second, first]
    assert len(compose_entries(db, limit=1)) == 1


def test_survives_missing_plan_json(db):
    """설계 의도를 못 읽어도 숫자만으로 항목을 만든다 (레거시 행)."""
    pid = _sol_trade(db)
    db.execute("UPDATE trade_plans SET plan_json = '{}' WHERE id = ?", (pid,))
    entry = compose_entries(db)[0]
    assert entry["plan_id"] == pid
    assert entry["evidence"] == []
    assert entry["entry_legs"] == []
    assert entry["rr"] is None
    assert entry["stop"]["price"] is None
    assert entry["pnl_usdt"] != 0  # 숫자는 살아 있다
