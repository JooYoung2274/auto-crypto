# Coin Agents Office

AI 에이전트 팀이 협업해서 **크립토 무기한 선물(perp) 매매 전략을 발굴 → 백테스트 → 리스크 필터링 → 리포팅 → 페이퍼 매매**까지 수행하는 개인용 웹 서비스입니다. 역할이 나뉜 7명의 에이전트가 사이클마다 데이터 수집부터 발주까지 릴레이로 처리하며, 웹 UI에서 각 에이전트의 작업 현황·로그·리더보드·포트폴리오를 실시간으로 볼 수 있습니다.

- **Phase 1 (현재)**: 백테스팅 기반 전략 발굴 + 페이퍼(모의) 트레이딩 — 실데이터(Binance 공개 API, 키 불필요) 기반, LLM/유료 API 무의존
- **Phase 2 (버튼 하나로)**: Binance USDT-M / OKX 실전 매매 (`POST /api/trading-mode`, UI 전환 버튼)

## 매매 규칙이 코드로 강제됩니다

`docs/trading-rules.md`가 규범(normative) 문서이며, **RiskEngine**(순수 함수 게이트)과 **TradePlan**(시나리오 자료구조)이 이를 강제합니다:

| 규칙 | 강제 지점 |
|---|---|
| 레버리지 BTC ≤10x, 그 외 ≤5x, 최소 3x | RiskEngine 정적 게이트 |
| 격리(Isolated) 마진 고정 | 브로커가 진입 전 설정 |
| 지정가만 (진입·익절 = post-only maker) | Broker ABC — market 주문 경로 없음 |
| 분할 진입 50/25/25 · 분할 익절 (레그 합 = 100%) | TradePlan 구조 검증 |
| 손익비 미달 진입 금지 (BTC·ETH 1:2, 알트 1:3) | side-aware RR 게이트 |
| 진입 근거 2개 이상, 시나리오 없는 주문 거부 | evidence 검증 + **브로커 레벨** 플랜 게이트 |
| CPI·FOMC 블랙아웃 (±12h) | econ_events 런타임 게이트 |
| 손절 = 4h 종가 판정, 손절 미루기 금지 | PositionMonitor + 유리한 방향만 수정 허용 |
| 복리 금지 (시드 고정, 수익 출금) | 사이징 게이트 min(잔고, 시드) + withdrawal_ledger |
| 저시총 금지 | 유니버스 화이트리스트 (BTC/ETH/SOL/XRP/DOGE) |

손절/청산회피 청산만 예외적으로 taker(공격적 reduce-only 지정가)를 허용하며 백테스트도 동일 비용(0.05%)으로 모델링합니다.

## 에이전트 팀

| 에이전트 | 이름 | 역할 |
|---|---|---|
| PM | 준 (Jun) | 사이클 시작/종료, 작업 분배, 결과 취합 |
| Data Engineer | 다온 (Daon) | 멀티TF OHLCV·펀딩비·레짐 데이터 수집/캐싱 |
| Strategist | 세라 (Sera) | 레짐 판단(알트지수·도미넌스 프록시), 상대강도 심볼 선정, 전략 후보 |
| Quant | 민 (Min) | 플랜 구동 백테스트 (지정가 관통 체결·펀딩·청산 시뮬) |
| Risk Manager | 로건 (Rogan) | RiskEngine 게이트 + MDD·청산 0회·펀딩드래그 필터 |
| Analyst | 하나 (Hana) | 마크다운 리포트 (USDT, 펀딩/청산 섹션) |
| Trader | 태오 (Teo) | 지정가 래더 발주, TTL 원가격 재큐, 분할 익절 관리 |

## 전략 템플릿 5종

모든 전략은 시그널이 아니라 **완성된 시나리오(TradePlan)** 를 출력합니다 — 진입 근거 2개 이상 +
분할 진입 50/25/25(현재가 유리한 쪽 지지/저항 레벨에 패시브 지정가 3단) + 손절선(시나리오 붕괴
지점, 4h 종가 판정) + 분할 익절 2단(1차 부분 / 2차 잔량 전량). 익절 거리는 손절 거리의 R배수로
탐색(1차 2.2~4.0R, 2차 4.0~6.5R)되어 **가중 손익비가 구조적으로 1:3 이상**이며, 레버리지도
3~10 범위에서 탐색하되 심볼 캡(BTC 10x / 그 외 5x)에 잘립니다.

공통 상위 필터 = **레짐 게이팅**: 알트 지수(TOTAL2/3 프록시)의 50/200일선 관계 + 도미넌스
방향으로 `알트불장 / BTC장 / 숏장 / 현금`을 판정하고, 롱 전략은 롱 레짐에서만·숏은 숏 레짐에서만
플랜을 냅니다 (레짐 역행 진입 원천 차단). 스윙 피벗·VPVR 등 모든 구조 지표는 **우측 k봉 마감 후
확정된 값만** 사용하며, 룩어헤드는 TF별·지표별 오염 테스트로 회귀 방지됩니다.

### 1. `topdown_pullback` — 톱다운 눌림목 (기본기)
일봉 200선 위 확인 → 4h 골든크로스(fast 30~70선 ↑ slow 120~240선) → 15m에서 fast선 눌림
(±1~5% 밴드 접근) + 거래량 확인(평균의 1.0~2.5배) 후 지지 레벨에 분할 래더. "큰 흐름 읽고
작게 들어간다" — 손절은 눌림 지지 구조가 깨지는 지점.

### 2. `ma_confluence` — 이평선 황금타점
4h 50·100·200선이 한 지점에 수렴(허용 오차 0.4~3%)하는 자리에서 지지 시 눌림목 매수, 손절은
수렴 지점 바로 아래(0.5~3% 패드) — "지지되면 눌림목, 이탈하면 칼손절". 패닉 급락(8~30%) 시
400선 분할 매수 분기 포함.

### 3. `box_range` — 박스권 매매
4h 확정 피벗으로 박스를 긋고 **하단 15~30% 구간에서만 롱, 상단에서만 숏** — 중간(손익비 1:1
홀짝 자리)은 관망. 손절은 박스 이탈(높이의 2~10% 버퍼), 익절은 1차 부분 → **최종 미드포인트**
(반대편 끝까지 기다리지 않음).

### 4. `vwma_support` — VWMA·VPVR 보조 판단
VWMA 60~150선(기본 100선) 위 지지 확인 후 조정 시 그 선까지 기다렸다 매수. VPVR(80~200봉
거래량 프로파일) 매물대를 손절·익절 기준선으로 사용. VWMA+VPVR 동시 하향 이탈 = 숏 플랜.

### 5. `candle_breakout` — 돌파 후 리테스트
장대양봉(몸통 평균의 1.5~3배)+거래량 폭발 돌파를 확인하되 **추격 금지** — 돌파 레벨 ±0.5~3%
밴드로 되돌아오는 눌림 리테스트를 기다렸다 패시브 진입. 저항대 윗꼬리 음봉 = 숏, 지지대
아래꼬리 양봉 = 롱 분기 포함.

파라미터 탐색은 사이클마다 위 범위에서 랜덤/변이 샘플링(챔피언 파라미터 ±20% 변이 포함)으로
후보 60개를 만들고, 유니버스 × 멀티TF 백테스트 → 리스크 필터(청산 1회 = 즉시 탈락, MDD·최소
거래수) → 가중 랭킹(샤프 35%·수익 30%·MDD 20%·승률 15%)으로 챔피언을 선출합니다. 상대강도
로직은 심볼 선정(BTC 하락 대비 낙폭 비교)에 사용됩니다.

## 설치 · 실행

요구사항: Python 3.12+ / Node.js 18+

```bash
# 백엔드
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 프론트엔드 빌드
cd ../frontend
npm install && npm run build

# 실행 (단일 포트)
cd ../backend
.venv/bin/python -m uvicorn app.main:app --port 8000
```

`http://localhost:8000` 접속 — API·WebSocket·프론트가 한 포트로 서빙됩니다.

## 실전 매매 전환

기본은 **paper 모드**(초록 뱃지). 전환 조건: **키 설정 + flat-and-idle**(사이클 미실행·오픈 주문/포지션 0) — 아니면 409 거부.

```env
# backend/.env — Binance USDT-M
CA_BINANCE_API_KEY=...
CA_BINANCE_API_SECRET=...
CA_BINANCE_TESTNET=true          # 먼저 테스트넷으로 검증 권장

# 또는 OKX
CA_EXCHANGE=okx
CA_OKX_API_KEY=...
CA_OKX_API_SECRET=...
CA_OKX_API_PASSPHRASE=...
CA_OKX_DEMO=true                 # OKX 데모 트레이딩

# 안전장치 (기본값)
CA_LIVE_MAX_ORDER_USDT=100       # 1회 주문 노셔널 한도
CA_LIVE_DAILY_ORDER_LIMIT=20     # rolling 24h 주문 수 한도
CA_LIVE_MAX_LOSS_PCT=0.05        # 일손실 서킷브레이커
```

UI ControlBar의 **실거래 전환** 버튼(타이핑 확인 모달) 또는 `POST /api/trading-mode {"mode":"live","confirm":"LIVE"}`. 전환 시 키 검증 → 심볼별 isolated·레버리지 설정 → 포지션/주문 리컨실 후에만 활성화되며 헤더가 빨간 live 뱃지로 바뀝니다.

## 테스트

```bash
cd backend && .venv/bin/python -m pytest   # 오프라인 (합성 데이터 시드, 네트워크 무의존)
cd frontend && npx vitest run
```

핵심 불변식: 룩어헤드 금지(OHLCV TF별+레짐+펀딩+피벗/VPVR 독립 오염), 미완성 봉 제외, 관통 체결(터치 미체결), 동일봉 우선순위(청산>손절>진입>TP), 4h 종가 손절 판정, 청산 정확식(체결마다 재계산), 펀딩 부호, 복리 금지, 재기동 후 중복 주문 0건, 키 없이 live 기동 거부 등 — `docs/superpowers/specs/2026-07-14-coin-agents-design.md` §9.

## 프로젝트 구조

```
backend/app/
  main.py              # FastAPI 조립, broker provider(핫스왑), 봉마감 trade 트리거
  config.py            # pydantic-settings (접두사 CA_)
  monitor.py           # PositionMonitor — 4h 손절 판정·TTL·펀딩·청산 감시 (사이클 밖 상시 태스크)
  orchestrator.py      # research/validate/trade 사이클 상태머신
  agents/              # pm/data_engineer/strategist/quant/risk/analyst/trader
  risk/                # TradePlan + RiskEngine (규칙 게이트, 순수 함수)
  data/                # loader(Binance klines)/funding/regime(레짐 프록시)/indicators(VWMA·VPVR·피벗)
  data/sources/okx.py  # OKX 공개 시세 소스
  backtest/            # 플랜 구동 엔진 / PerpCostModel / metrics (비복리, TF별 연환산)
  strategies/          # 5 템플릿 + registry (그리드·변이)
  broker/              # base ABC / paper / binance / okx
docs/
  trading-rules.md     # 매매 규칙 (normative)
  superpowers/specs/   # 설계 스펙 v2
  superpowers/plans/   # 구현 플랜
```
