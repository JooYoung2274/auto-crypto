# Coin Agents Office — 구현 플랜 (2026-07-14)

전제: coinAgent는 stockAgents의 **포크 상태**에서 시작한다 (backend/, frontend/ 복사 완료).
각 태스크는 포크된 코드를 크립토 선물용으로 **변환**하며, 소유 파일이 겹치지 않는 태스크는 병렬 실행.
모든 태스크는 자기 모듈의 테스트를 함께 변환/추가하고 통과시켜야 완료.

참고 문서 (모든 구현자는 시작 전 필독):
- `docs/trading-rules.md` — 규범 (리스크 게이트·전략)
- `docs/superpowers/specs/2026-07-14-coin-agents-design.md` — 설계 스펙
- `docs/superpowers/specs/stockagents-contract-map.json` — 원본 계약 맵
- 원본 소스: `/Users/jooyoung/joo/stockAgents`

## Wave A — 기반 (병렬 2)

**A1. backend 코어**: `app/config.py`(CA_ 접두사, 스펙 §0·§5 설정키), `app/db.py`(스펙 §6 스키마),
`app/events.py`(신규 이벤트 타입), `app/ws.py`(스냅샷에 포지션 포함), `app/main.py`(타이틀,
봉마감 트리거 auto loop 뼈대), `tests/test_events.py`, `tests/test_main_lifespan.py` 갱신.

**A2. 데이터 레이어**: `app/data/loader.py` → Binance fapi klines 멀티TF + 미완성 봉 제외 +
SQLite 캐시 + 오프라인 합성 시드 경로 유지, `app/data/funding.py`(펀딩 이력),
`app/data/regime.py`(CoinGecko global → market_regime 캐시 → 레짐 판정), `app/data/indicators.py`
(SMA/EMA/VWMA/RSI/ATR/VPVR/스윙/박스/매물대 존), `tests/test_loader.py` 갱신 + indicators 테스트 신규.

## Wave B — 엔진 (병렬 2, A 완료 후)

**B1. 백테스트 엔진**: `app/backtest/engine.py`(양방향, 레버리지, 지정가 체결, 4h 종가 손절,
청산 우선, 분할 레그), `costs.py`(PerpCostModel+펀딩), `trades.py`, `metrics.py`(TF별 연환산,
funding_paid, liquidation_count), `tests/test_engine.py`, `test_metrics.py` 전면 개정.

**B2. 브로커 + 리스크엔진**: `app/broker/base.py`(ABC 확장), `app/broker/paper.py`(지정가 시뮬,
격리마진·청산·펀딩·TTL), `app/broker/binance.py`(신규 — HMAC REST, 테스트넷, 안전장치; toss.py 삭제),
`app/risk/engine.py`+`app/risk/plan.py`(TradePlan·게이트 — 신규), `tests/test_paper_broker.py`,
`test_binance_broker.py`(mock transport), `test_risk_engine.py` 신규.

## Wave C — 두뇌 (병렬 2, B 완료 후)

**C1. 전략**: `app/strategies/` 5 템플릿(topdown_pullback, ma_confluence, box_range, vwma_support,
candle_breakout) + base(generate_plan MTF) + registry(파라미터 그리드·변이 유지) — 기존 주식 전략 파일 삭제.
`tests/test_strategies.py` 전면 개정 (look-ahead TF별 오염 테스트 포함).

**C2. 에이전트 + 오케스트레이터**: `app/agents/*`(역할 크립토화, Strategist 레짐·상대강도,
Risk가 RiskEngine 호출, Trader 래더 발주·TTL·4h 손절 판정·출금 원장), `app/orchestrator.py`
(연속 trade 사이클, 스냅샷 확장), `app/reports/generator.py`(USDT·펀딩·청산 표기),
관련 테스트 갱신.

## Wave D — 표면 (병렬 2, C 완료 후)

**D1. API**: `app/api/routes.py` — 기존 + trading-mode 전환(핫스왑)·positions·regime·econ-events·plans,
`tests/test_api.py`, `test_orchestrator_e2e.py`, `test_e2e_live_server.py` 갱신 (오프라인 합성 시드 유지).

**D2. 프론트엔드**: `src/lib/types.ts`·`api.ts`(새 계약), PortfolioPanel(선물 타일·청산가),
Leaderboard(TF·side·펀딩 뱃지), ChampionPanel(분할 체결), ControlBar(모드 전환 버튼+확인 모달,
레짐 칩), 포맷터 USDT/타임스탬프, 오피스 캔버스 무변경, vitest 갱신.

## Wave F — OKX 어댑터 (Wave E 이후, 사용자 요청 2026-07-14)

Binance를 주 거래소로 유지하되 OKX를 추가 지원. `CA_EXCHANGE=binance|okx` 설정으로 선택.
- `app/data/sources/okx.py`: 공개 klines(`/api/v5/market/candles`)·펀딩 이력 — DataLoader 소스 추상화
- `app/broker/okx.py`: OKXBroker — API key+secret+**passphrase** base64 HMAC 서명, 데모 트레이딩
  (`x-simulated-trading: 1`), 심볼 매핑(`BTCUSDT`↔`BTC-USDT-SWAP`), 계약 단위(ctVal) 환산,
  isolated·레버리지 설정, Binance와 동일한 안전장치 세트
- 테스트: mock transport로 서명·심볼 매핑·계약 환산·안전장치 검증 (오프라인)

## Wave E — 통합 검증 (직렬)

1. `pytest` 전체 + `vitest` 전체 그린 (실패 시 디버그 루프)
2. `npm run build` → uvicorn 기동 → 오프라인 시드로 research→trade 사이클 e2e 스모크
3. 적대적 코드 리뷰 워크플로 (규칙 위반·룩어헤드·청산 수학) → 확인된 결함 수정
4. README.md 작성
