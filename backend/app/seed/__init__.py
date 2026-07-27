"""기본 챔피언 시드 — 첫 실행에서 바로 모의거래가 돌아가게 한다.

비개발자 구매자가 앱을 켜면 챔피언이 없어 30~45분짜리 '전략 연구'를 먼저
돌려야 했다. 번들된 기본 전략을 첫 실행에 한 번만 주입해 그 대기를 없앤다.

**실거래 안전장치 (2중 가드)** — 아래 조건을 모두 만족할 때만 주입한다:

1. ``settings.paper_only`` 가 True — 모의거래 전용 데스크탑 빌드에서만.
   실거래 서버는 ``paper_only=False`` 라 이 함수가 절대 쓰기를 하지 않는다.
2. ``strategies`` / ``champion_history`` 테이블이 완전히 비어 있음 — 즉
   한 번이라도 사이클을 돌린 DB에는 손대지 않는다.

두 조건 중 하나라도 어긋나면 즉시 ``None``을 돌려주고 아무것도 쓰지 않는다.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from ..config import Settings
from ..db import Database
from ..strategies.registry import TEMPLATES

log = logging.getLogger(__name__)

SEED_PATH = Path(__file__).with_name("default_champion.json")


def _seed_path() -> Path:
    """소스 실행이면 패키지 옆, PyInstaller 번들이면 _MEIPASS 하위."""
    if SEED_PATH.is_file():
        return SEED_PATH
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "app" / "seed" / "default_champion.json"
        if bundled.is_file():
            return bundled
    return SEED_PATH


def load_seed() -> dict | None:
    """번들 JSON을 읽는다. 파일이 없거나 깨졌으면 None (기동은 계속)."""
    try:
        payload = json.loads(_seed_path().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("기본 챔피언 시드를 읽지 못했습니다 (%s) — 건너뜁니다", exc)
        return None
    if not payload.get("template") or not isinstance(payload.get("params"), dict):
        log.warning("기본 챔피언 시드 형식 오류 — 건너뜁니다")
        return None
    if payload["template"] not in TEMPLATES:
        # 템플릿이 제거·개명된 경우 (시드가 코드보다 오래됨).
        log.warning("기본 챔피언 템플릿 '%s' 미등록 — 건너뜁니다", payload["template"])
        return None
    return payload


def _is_fresh_install(db: Database) -> bool:
    """사이클을 한 번도 돌리지 않은 DB인지."""
    return not (
        db.execute("SELECT 1 FROM strategies LIMIT 1")
        or db.execute("SELECT 1 FROM champion_history LIMIT 1")
    )


def seed_default_champion(db: Database, settings: Settings) -> int | None:
    """첫 실행이면 기본 챔피언을 주입하고 strategy id를 돌려준다.

    조건 미충족·시드 부재·쓰기 실패는 모두 ``None`` — 기동을 막지 않는다.
    """
    # 가드 1: 모의거래 전용 빌드에서만. 실거래 DB에는 어떤 경우에도 쓰지 않는다.
    if not getattr(settings, "paper_only", False):
        return None
    # 가드 2: 완전 신규 설치에서만.
    if not _is_fresh_install(db):
        return None

    payload = load_seed()
    if payload is None:
        return None

    # 유니버스는 시드가 아니라 현재 설정을 따른다 — 화이트리스트 밖 심볼이
    # 챔피언 패널에 뜨면 RiskEngine이 거부하는 심볼을 광고하는 꼴이 된다.
    allowed = set(settings.universe)
    backtests = [
        b
        for b in payload.get("backtests", [])
        if isinstance(b, dict) and b.get("symbol") in allowed
    ]
    if not backtests:
        # 지표가 하나도 없으면 리더보드에서 걸러져 UI가 '챔피언 없음'을 띄운다.
        log.warning("기본 챔피언에 현재 유니버스와 겹치는 백테스트가 없어 건너뜁니다")
        return None

    try:
        with db.transaction():
            rows = db.execute(
                "INSERT INTO strategies "
                "(cycle_id, template, params_json, universe_json, status) "
                "VALUES (NULL, ?, ?, ?, 'champion')",
                (
                    payload["template"],
                    json.dumps(payload["params"]),
                    json.dumps([b["symbol"] for b in backtests]),
                ),
            )
            strategy_id = int(rows[0]["id"])
            for b in backtests:
                metrics = dict(b.get("metrics") or {})
                # 번들 지표임을 표시 — 이번 설치에서 계산한 값이 아니다.
                metrics["seeded"] = True
                db.execute(
                    "INSERT INTO backtests (strategy_id, symbol, metrics_json) "
                    "VALUES (?, ?, ?)",
                    (strategy_id, b["symbol"], json.dumps(metrics)),
                )
            db.execute(
                "INSERT INTO champion_history (strategy_id, crowned_at) "
                "VALUES (?, datetime('now'))",
                (strategy_id,),
            )
    except Exception as exc:  # noqa: BLE001 — 시드 실패로 앱이 안 뜨면 안 된다.
        log.warning("기본 챔피언 주입 실패 (%s) — 챔피언 없이 시작합니다", exc)
        return None

    log.info(
        "기본 챔피언 주입 — #%d %s (%d개 심볼)",
        strategy_id,
        payload["template"],
        len(backtests),
    )
    return strategy_id
