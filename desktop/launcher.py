"""Coin Agents Office — 모의거래 전용 데스크탑 런처.

FastAPI 백엔드를 백그라운드 스레드에서 띄우고, 빌드된 React UI를 네이티브
창(pywebview)으로 연다. 비개발자용: 더블클릭 실행, 터미널·키 불필요.

- 모의거래 전용: CA_PAPER_ONLY=true 강제 → 실거래 전환 UI 자체가 비활성화.
- 데이터/설정은 OS별 사용자 앱데이터 폴더에 저장 (읽기전용 .app/exe 안이 아님).
- 페이퍼 모드는 API 키가 필요 없다 (Binance 공개 시세만 사용).
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path


def _app_data_dir() -> Path:
    """OS별 쓰기 가능한 앱데이터 폴더 (DB·로그 저장)."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / "CoinAgentsOffice"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_up(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def main() -> None:
    data_dir = _app_data_dir()
    # 모의거래 전용 + 데이터 경로를 백엔드 Settings에 환경변수로 주입.
    os.environ.setdefault("CA_PAPER_ONLY", "true")
    os.environ.setdefault("CA_TRADING_MODE", "paper")
    os.environ.setdefault("CA_DB_PATH", str(data_dir / "coinagent.db"))
    os.environ.setdefault("CA_BAR_CLOSE_TRADE_ENABLED", "true")

    # PyInstaller 번들이면 backend가 sys.path에 포함돼 있다 (spec에서 처리).
    port = int(os.environ.get("COIN_PORT") or _free_port())

    import uvicorn

    from app.main import create_app

    app = create_app()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    if not _wait_until_up(port):
        raise RuntimeError("백엔드 서버가 시작되지 않았습니다")

    # 검증용: GUI 없이 서버만 잠시 유지 (CI/스모크 테스트).
    if os.environ.get("COIN_HEADLESS") == "1":
        print(f"HEADLESS OK — 서버 http://127.0.0.1:{port}")
        time.sleep(float(os.environ.get("COIN_HEADLESS_SECONDS", "6")))
        server.should_exit = True
        return

    import webview

    window = webview.create_window(
        "Coin Agents Office — 모의거래",
        f"http://127.0.0.1:{port}",
        width=1440,
        height=960,
        min_size=(1000, 700),
    )

    def _shutdown() -> None:
        server.should_exit = True

    window.events.closed += _shutdown
    webview.start()


if __name__ == "__main__":
    main()
