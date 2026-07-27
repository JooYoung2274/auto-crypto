# Coin Agents Office — 모의거래 데스크탑 앱

비개발자도 더블클릭으로 실행하는 **모의거래 전용** 데스크탑 빌드입니다.
터미널·Python·API 키가 필요 없습니다 (페이퍼 모드는 Binance 공개 시세만 사용).

## 특징

- **모의거래 전용** — `CA_PAPER_ONLY=true`로 실거래 전환이 백엔드·UI 양쪽에서 차단됩니다. 실제 자금이 절대 움직이지 않습니다.
- **키 불필요** — 페이퍼 모드는 공개 시세만 쓰므로 거래소 키가 필요 없습니다.
- **로컬 저장** — 매매 데이터·설정은 사용자 앱데이터 폴더에 저장됩니다:
  - macOS: `~/Library/Application Support/CoinAgentsOffice/`
  - Windows: `%APPDATA%\CoinAgentsOffice\`
  - Linux: `~/.local/share/CoinAgentsOffice/`

## 빌드 — GitHub Actions (권장)

**Windows PC 없이 `.exe`를 만드는 방법입니다.** PyInstaller는 크로스 컴파일이
불가능하므로 각 OS 러너에서 각자 빌드합니다.

GitHub 저장소 → **Actions** → **데스크탑 앱 빌드** → **Run workflow**

10~15분 뒤 실행 페이지 하단 Artifacts에서 Windows·macOS 산출물을 받습니다.
백엔드·프론트엔드 테스트가 먼저 돌고 실패하면 패키징하지 않습니다.

macOS 서명·공증 설정과 Windows SmartScreen 대응은 **[RELEASE.md](RELEASE.md)** 참고.

## 빌드 — 로컬 (현재 OS만)

프론트엔드 빌드 → 의존성 설치 → PyInstaller 패키징을 한 번에:

```bash
cd desktop
./build.sh          # macOS / Linux
```

또는 수동:

```bash
# 1. 프론트엔드
cd frontend && npm install && npm run build

# 2. 백엔드 + 데스크탑 의존성 (venv 권장)
cd ../backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r ../desktop/requirements.txt

# 3. 패키징
cd ../desktop && ../backend/.venv/bin/python -m PyInstaller --noconfirm coinagent.spec
```

**산출물**: `desktop/dist/CoinAgentsOffice.app` (macOS) / `desktop/dist/CoinAgentsOffice/` (Win·Linux)

## 실행 (최종 사용자)

- **macOS**: `CoinAgentsOffice.app` 더블클릭
- **Windows**: `CoinAgentsOffice.exe` 더블클릭
- **Linux**: `CoinAgentsOffice/CoinAgentsOffice` 실행

**첫 실행 시 바로 모의거래가 돌아갑니다** — 기본 전략(챔피언)이 번들돼 있어 대기 시간이 없습니다.
처음 켜면 3단계 사용 안내가 한 번 뜨고, 이후에는 헤더의 **❔ 사용 안내** 버튼으로 다시 볼 수 있습니다.

더 좋은 전략을 원하면 **🔬 전략 연구**를 돌리세요 (20~45분 소요, 완료 시 챔피언 자동 교체).

## 배포 시 주의

- **코드 서명/공증**: 절차와 GitHub Secrets 설정은 [RELEASE.md](RELEASE.md)에 정리했습니다. macOS는 Apple Developer 계정이 있으면 워크플로가 서명·공증까지 자동 처리하고, Windows는 인증서가 없으면 SmartScreen 경고가 남습니다 (압축 안 `사용안내.txt`가 통과 방법을 설명).
- **면책**: 투자 판단·결과에 대한 책임 고지를 반드시 포함하세요. 이 앱은 교육·연구용 시뮬레이터입니다.
- **문제 진단**: GUI 빌드는 콘솔이 없으므로 앱데이터 폴더의 `coinagent.log` / `coinagent-error.log`가 유일한 단서입니다. 문의 시 이 파일을 받으세요.
- **개발용 직접 실행** (패키징 없이 테스트):
  ```bash
  cd desktop && ../backend/.venv/bin/python launcher.py
  ```

## 개발자 참고

- `launcher.py` — uvicorn을 백그라운드 스레드로 띄우고 pywebview 네이티브 창을 연다. 페이퍼 전용 환경변수 주입 + 앱데이터 경로 설정. 소스 실행 시 `backend`를 `sys.path`에 넣는다.
- `coinagent.spec` — PyInstaller 스펙. 프론트엔드 `dist`를 `frontend_dist`로, 기본 챔피언 시드 JSON을 `app/seed`로 번들하고, pandas/numpy/uvicorn/fastapi/webview를 `collect_all`로 수집.
- `backend/app/seed/` — 첫 실행 챔피언 시드. **`paper_only=true` + 빈 DB** 두 조건을 모두 만족할 때만 주입되므로 실거래 DB에는 발동하지 않는다.
- `frontend/src/components/Onboarding.tsx` — 3단계 첫 실행 안내. `paper_only` 빌드에서만 자동 노출되고 `localStorage`로 1회만 뜬다.

### 스모크 테스트 (GUI 없이)

```bash
cd frontend && npm run build && cd ../desktop
COIN_HEADLESS=1 COIN_PORT=8891 ../backend/.venv/bin/python launcher.py &
curl -s localhost:8891/api/champions   # 기본 챔피언이 잡혀야 함
curl -s localhost:8891/api/config      # paper_only: true
```
