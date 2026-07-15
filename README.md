# Coin Agents Office

AI 에이전트 팀이 협업해서 **크립토 무기한 선물(perp) 매매 전략을 발굴 → 백테스트 → 리스크 필터링 → 리포팅 → 페이퍼 매매**까지 수행하는 개인용 웹 서비스입니다. `stockAgents`의 "에이전트 오피스" 아키텍처를 크립토 선물에 맞게 개조했습니다. 7명의 에이전트는 2D 픽셀 오피스에서 캐릭터로 시각화되며, 업무 인계 시점마다 회의실로 걸어가 소통하는 모습이 실시간으로 보입니다.

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

| 에이전트 | 캐릭터 | 역할 |
|---|---|---|
| PM | 준 (Jun) | 사이클 시작/종료, 작업 분배, 결과 취합 |
| Data Engineer | 다온 (Daon) | 멀티TF OHLCV·펀딩비·레짐 데이터 수집/캐싱 |
| Strategist | 세라 (Sera) | 레짐 판단(알트지수·도미넌스 프록시), 상대강도 심볼 선정, 전략 후보 |
| Quant | 민 (Min) | 플랜 구동 백테스트 (지정가 관통 체결·펀딩·청산 시뮬) |
| Risk Manager | 로건 (Rogan) | RiskEngine 게이트 + MDD·청산 0회·펀딩드래그 필터 |
| Analyst | 하나 (Hana) | 마크다운 리포트 (USDT, 펀딩/청산 섹션) |
| Trader | 태오 (Teo) | 지정가 래더 발주, TTL 원가격 재큐, 분할 익절 관리 |

전략 템플릿 5종: 톱다운 눌림목(`topdown_pullback`), 이평선 황금타점(`ma_confluence`), 박스권(`box_range`), VWMA 지지(`vwma_support`), 돌파 후 리테스트(`candle_breakout`). 모든 전략은 TOTAL2/3·도미넌스 프록시 레짐 필터를 통과해야 하며, 룩어헤드는 TF별 오염 테스트로 차단됩니다.

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
  superpowers/specs/   # 설계 스펙 v2, stockAgents 계약 맵
  superpowers/plans/   # 구현 플랜
```
