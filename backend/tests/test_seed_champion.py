"""기본 챔피언 시드 — 첫 실행 편의 기능이자 **실거래 DB를 건드리면 안 되는**
쓰기 경로. 여기서 지키는 것:

1. paper_only=False(실거래 빌드)에서는 어떤 경우에도 쓰기가 없다.
2. 이미 사이클을 돌린 DB(전략/챔피언 이력 존재)에는 주입하지 않는다.
3. 주입되면 리더보드·/api/champions 에 실제로 챔피언으로 잡힌다
   (지표 없는 챔피언은 UI에서 '없음'으로 보이므로 백테스트까지 함께 넣는다).
4. 시드 파일이 없거나 깨져도 기동을 막지 않는다.
"""
from __future__ import annotations

import json

import pytest  # noqa: F401 — asyncio_mode=auto

from app.config import Settings
from app.orchestrator import compute_leaderboard
from app.seed import load_seed, seed_default_champion
from app.strategies.registry import TEMPLATES


def _paper_only(tmp_path, **kw) -> Settings:
    return Settings(paper_only=True, db_path=str(tmp_path / "x.db"), _env_file=None, **kw)


def _rows(db, table) -> list:
    return db.execute(f"SELECT * FROM {table}")


# ── 1. 실거래 보호 ───────────────────────────────────────────────────────
def test_never_seeds_when_not_paper_only(db, tmp_path):
    """실거래 빌드(paper_only=False)에서는 단 한 줄도 쓰지 않는다."""
    live = Settings(paper_only=False, db_path=str(tmp_path / "x.db"), _env_file=None)
    assert seed_default_champion(db, live) is None
    assert _rows(db, "strategies") == []
    assert _rows(db, "backtests") == []
    assert _rows(db, "champion_history") == []


def test_default_settings_are_not_paper_only():
    """기본값이 실거래 안전 쪽 — 설정을 잊어도 시드가 돌지 않는다."""
    assert Settings(_env_file=None).paper_only is False


# ── 2. 첫 실행에서만 ────────────────────────────────────────────────────
def test_skips_when_strategies_exist(db, tmp_path):
    db.execute(
        "INSERT INTO strategies (template, params_json, status) VALUES ('x', '{}', 'candidate')"
    )
    assert seed_default_champion(db, _paper_only(tmp_path)) is None
    assert len(_rows(db, "strategies")) == 1  # 기존 행만


def test_skips_when_champion_history_exists(db, tmp_path):
    """전략을 정리했더라도 챔피언 이력이 있으면 신규 설치가 아니다."""
    db.execute("INSERT INTO champion_history (strategy_id) VALUES (99)")
    assert seed_default_champion(db, _paper_only(tmp_path)) is None
    assert _rows(db, "strategies") == []


def test_seeding_is_idempotent(db, tmp_path):
    """두 번째 기동에서 중복 주입되지 않는다."""
    s = _paper_only(tmp_path)
    first = seed_default_champion(db, s)
    assert first is not None
    assert seed_default_champion(db, s) is None
    champs = db.execute("SELECT id FROM strategies WHERE status = 'champion'")
    assert len(champs) == 1


# ── 3. 주입 결과가 실제로 챔피언으로 잡힌다 ─────────────────────────────
def test_seeded_champion_appears_on_leaderboard(db, tmp_path):
    s = _paper_only(tmp_path)
    sid = seed_default_champion(db, s)
    assert sid is not None

    board = compute_leaderboard(db, 100, s.min_trades, s)
    champ = next((r for r in board if r["status"] == "champion"), None)
    assert champ is not None, "지표가 붙지 않으면 UI가 '챔피언 없음'을 띄운다"
    assert champ["strategy_id"] == sid
    assert champ["template"] in TEMPLATES
    assert champ["avg_metrics"].get("trade_count", 0) > 0


def test_seeded_champion_opens_a_reign(db, tmp_path):
    sid = seed_default_champion(db, _paper_only(tmp_path))
    reigns = db.execute("SELECT * FROM champion_history WHERE demoted_at IS NULL")
    assert len(reigns) == 1 and reigns[0]["strategy_id"] == sid


def test_seed_spec_is_loadable_by_orchestrator(db, tmp_path):
    """_load_champion_spec 이 읽을 수 있는 형태여야 trade 사이클이 돈다."""
    seed_default_champion(db, _paper_only(tmp_path))
    row = db.execute(
        "SELECT template, params_json FROM strategies WHERE status = 'champion'"
    )[0]
    params = json.loads(row["params_json"])
    assert row["template"] in TEMPLATES
    # 파라미터 키가 템플릿 그리드와 맞아야 백테스트/플랜 생성이 가능하다.
    assert set(params) == set(TEMPLATES[row["template"]])


# ── 4. 유니버스는 시드가 아니라 현재 설정을 따른다 ──────────────────────
def test_backtests_limited_to_configured_universe(db, tmp_path):
    """화이트리스트 밖 심볼을 챔피언 패널에 광고하지 않는다."""
    s = _paper_only(tmp_path, universe=["BTCUSDT", "ETHUSDT"])
    sid = seed_default_champion(db, s)
    assert sid is not None
    symbols = {r["symbol"] for r in db.execute("SELECT symbol FROM backtests")}
    assert symbols == {"BTCUSDT", "ETHUSDT"}
    stored = db.execute("SELECT universe_json FROM strategies")[0]["universe_json"]
    assert set(json.loads(stored)) == {"BTCUSDT", "ETHUSDT"}


def test_skips_when_universe_has_no_overlap(db, tmp_path):
    """겹치는 심볼이 없으면 지표 없는 챔피언 대신 아예 주입하지 않는다."""
    s = _paper_only(tmp_path, universe=["NOSUCHUSDT"])
    assert seed_default_champion(db, s) is None
    assert _rows(db, "strategies") == []


def test_seeded_metrics_are_tagged(db, tmp_path):
    """이번 설치에서 계산한 값이 아님을 지표에 표시한다."""
    seed_default_champion(db, _paper_only(tmp_path))
    for r in db.execute("SELECT metrics_json FROM backtests"):
        assert json.loads(r["metrics_json"])["seeded"] is True


# ── 5. 시드 파일 문제는 기동을 막지 않는다 ──────────────────────────────
def test_missing_seed_file_is_survivable(db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.seed._seed_path", lambda: tmp_path / "nope.json")
    assert seed_default_champion(db, _paper_only(tmp_path)) is None
    assert _rows(db, "strategies") == []


def test_corrupt_seed_file_is_survivable(db, tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr("app.seed._seed_path", lambda: bad)
    assert seed_default_champion(db, _paper_only(tmp_path)) is None


def test_unknown_template_is_rejected(db, tmp_path, monkeypatch):
    """시드가 코드보다 오래돼 템플릿이 사라진 경우."""
    stale = tmp_path / "stale.json"
    stale.write_text(
        json.dumps({"template": "gone_template", "params": {}, "backtests": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.seed._seed_path", lambda: stale)
    assert load_seed() is None
    assert seed_default_champion(db, _paper_only(tmp_path)) is None


# ── 6. 번들 파일 자체의 무결성 ──────────────────────────────────────────
def test_bundled_seed_is_valid():
    payload = load_seed()
    assert payload is not None, "번들 시드가 깨졌다 — 데스크탑 첫 실행이 무너진다"
    assert payload["template"] in TEMPLATES
    assert set(payload["params"]) == set(TEMPLATES[payload["template"]])
    assert payload["backtests"], "지표가 없으면 챔피언이 UI에 뜨지 않는다"
    for b in payload["backtests"]:
        assert b["symbol"].endswith("USDT")
        assert b["metrics"]["trade_count"] > 0
    # 과장 광고 방지 — 성과가 보장이 아님을 시드 자체가 명시한다.
    assert "보장하지 않습니다" in payload["note"]
