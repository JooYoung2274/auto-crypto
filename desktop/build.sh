#!/usr/bin/env bash
# Coin Agents Office — 모의거래 데스크탑 앱 빌드 (macOS / Linux).
# 사용: cd desktop && ./build.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> [1/4] 프론트엔드 빌드"
(cd frontend && npm install && npm run build)

echo "==> [2/4] 백엔드 + 데스크탑 의존성 설치 (venv 권장)"
PY="${PYTHON:-python3}"
$PY -m pip install -r backend/requirements.txt
$PY -m pip install -r desktop/requirements.txt

echo "==> [3/4] PyInstaller 패키징"
cd desktop
$PY -m PyInstaller --noconfirm coinagent.spec

echo "==> [4/4] 완료"
echo "산출물: desktop/dist/CoinAgentsOffice.app (macOS) 또는 desktop/dist/CoinAgentsOffice/ (Linux)"
echo "실행: open dist/CoinAgentsOffice.app   (또는 dist/CoinAgentsOffice/CoinAgentsOffice)"
