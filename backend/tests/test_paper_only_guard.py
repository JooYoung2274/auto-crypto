"""모의거래 전용 빌드(paper_only)는 어떤 경로로도 실거래로 뜨지 않는다.

데스크탑 앱은 구매자 PC에서 돌고, 실거래 주문이 나가면 실제 자금이 움직인다.
``paper_only``가 켜져 있으면 다음 세 경로가 전부 막혀야 한다:
  1. 환경변수/.env 에 CA_TRADING_MODE=live 가 있어도 기동은 paper
  2. 런타임 전환 API 거부
  3. live 브로커(거래소 어댑터) 자체가 생성되지 않음
"""
from __future__ import annotations

import pytest  # noqa: F401 — asyncio_mode=auto
from fastapi.testclient import TestClient

from app.broker.paper import PaperBroker
from app.config import Settings
from app.main import create_app


def test_env_live_is_downgraded_to_paper(tmp_path):
    """환경에 live 가 남아 있어도 paper 로 내려온다 (런처 setdefault 우회 방어)."""
    s = Settings(
        paper_only=True,
        trading_mode="live",
        db_path=str(tmp_path / "x.db"),
        _env_file=None,
    )
    assert s.trading_mode == "paper"


def test_live_build_is_untouched(tmp_path):
    """paper_only 가 꺼진 실거래 빌드는 그대로 live 로 뜬다."""
    s = Settings(
        paper_only=False,
        trading_mode="live",
        db_path=str(tmp_path / "x.db"),
        _env_file=None,
    )
    assert s.trading_mode == "live"


def test_env_file_cannot_flip_a_paper_only_build(tmp_path):
    """.env 에 live 와 거래소 키가 있어도 모의거래로 뜬다.

    데스크탑 앱을 저장소 폴더에서 실행하면 개발용 .env 가 읽히는 위치가 된다.
    """
    env = tmp_path / ".env"
    env.write_text(
        "CA_TRADING_MODE=live\n"
        "CA_EXCHANGE=okx\n"
        "CA_OKX_API_KEY=dummy\n"
        "CA_OKX_API_SECRET=dummy\n"
        "CA_OKX_API_PASSPHRASE=dummy\n",
        encoding="utf-8",
    )
    s = Settings(paper_only=True, db_path=str(tmp_path / "x.db"), _env_file=str(env))
    assert s.trading_mode == "paper"


def test_paper_only_app_boots_with_paper_broker(tmp_path):
    """기동 경로 전체 검증 — live 어댑터가 만들어지지 않는다.

    (live 였다면 OKX/Binance 브로커가 생성되고 reconcile 이 실제 계정에
    레버리지·마진 모드를 쓴다.)
    """
    s = Settings(
        paper_only=True,
        trading_mode="live",
        exchange="okx",
        db_path=str(tmp_path / "x.db"),
        _env_file=None,
    )
    with TestClient(create_app(s)) as client:
        assert isinstance(client.app.state.broker, PaperBroker)
        cfg = client.get("/api/config").json()
        assert cfg["trading_mode"] == "paper"
        assert cfg["paper_only"] is True


def test_paper_only_rejects_runtime_switch(tmp_path):
    s = Settings(paper_only=True, db_path=str(tmp_path / "x.db"), _env_file=None)
    with TestClient(create_app(s)) as client:
        r = client.post("/api/trading-mode", json={"mode": "live", "confirm": "LIVE"})
        assert r.status_code == 400
        assert client.get("/api/config").json()["trading_mode"] == "paper"
