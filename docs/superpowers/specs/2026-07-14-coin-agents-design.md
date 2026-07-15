# Coin Agents Office — 설계 스펙 v2 (2026-07-14, 적대적 크리틱 반영)

crypto 무기한 선물(perp) 자동매매 시스템. `../stockAgents`의 "에이전트 오피스" 아키텍처를 포크하여
크립토 선물에 맞게 개조한다. 매매 규범은 `docs/trading-rules.md`(normative)를 따른다.
stockAgents의 정확한 계약은 `docs/superpowers/specs/stockagents-contract-map.json` 참조.
v1 스펙에 대한 3-렌즈 적대적 크리틱(규칙 충실도·아키텍처·퀀트 정합성)의 지적을 전부 반영했다.

## 0. 핵심 결정

| 항목 | 결정 |
|---|---|
| 거래소 | Binance USDT-M Perpetual (fapi). 공개 klines(키 불필요), 실 테스트넷 지원 |
| 데이터 | Binance 공개 REST klines + fundingRate. 레짐은 Binance 일봉 기반 프록시(§3.1) — 전부 키 프리·5년 백테스트 가능 |
| 라이브 | 네이티브 httpx HMAC 서명 `BinanceBroker`, `CA_BINANCE_TESTNET=true` 지원. 키 없이 live 기동 거부 |
| 유니버스 | `['BTCUSDT','ETHUSDT','SOLUSDT','XRPUSDT','DOGEUSDT']` (화이트리스트 = 저시총 금지) |
| 타임프레임 | `['1d','4h','15m','5m']`, 실행 TF 기본 `15m`. 페이퍼 체결 확인은 1m 종가 봉 |
| 레버리지 캡 | **BTC=10x, 그 외(ETH 포함)=5x, 최소 3x** — 규칙 §1 '알트 3~5배, BTC만 10배' |
| 마진 | Isolated 고정 |
| 주문 | **진입·TP 레그 = 패시브 지정가(post-only, maker 0.025%)**. **손절/청산회피 exit = 공격적 reduce-only 크로싱 리밋(taker 0.05% 모델링)** — 규칙 §1의 taker 0.05% 가정과 일치. 그 외 taker 경로 없음 |
| env 접두사 | `CA_`, DB `coinagent.db`, 통화 USDT (시드 기본 10,000) |

## 1. 시스템 구조

동일 유지: FastAPI 단일 프로세스 + SQLite(WAL) + EventBus→activity_log+WS + Orchestrator
사이클 상태머신(research/validate/trade) + 7 에이전트(준/다온/세라/민/로건/하나/태오) + React 픽셀
오피스(캔버스·FSM·웨이포인트 무변경) + frontend/dist 정적 서빙.

### 1.1 실행 토폴로지 (크리틱 반영 — 사이클 뮤텍스와 분리)

- **PositionMonitor = 사이클 상태머신 밖의 상시 asyncio 태스크.** 리스크 크리티컬 동작 전담:
  4h 종가 손절 판정, TTL 만료 취소/재큐, 펀딩 정산, 청산 경고, 스탑엑싯 체이스.
  research 사이클이 아무리 길어도 절대 블로킹되지 않는다.
- trade 사이클은 실행 TF 봉마감 트리거. **뮤텍스 점유 중이면 skip-not-queue** (드랍 후 로그, 재시도 큐 없음).
- trade 사이클과 PositionMonitor는 **공유 asyncio.Lock**으로 주문/포지션 변이를 직렬화
  (같은 심볼에 cancel-replace와 신규 진입이 인터리브 불가).
- SQLite: 백테스트 벌크 인서트는 500행 청크로 락 양보. 모니터는 인메모리 상태로 판단 후 사후 기록.

### 1.2 멀티 타임프레임

- `ohlcv_cache` PK `(symbol, timeframe, ts)` (ts=봉 open time, epoch ms). **미완성 봉 제외** (close_time > now 배제).
- 상위 TF → 하위 TF 정렬은 전용 헬퍼: **상위 TF에서 shift(1) 후 ffill** (교차 TF 룩어헤드 금지).
- 일 단위 시리즈(레짐·펀딩)도 같은 규칙: 인트라데이 봉 t는 **직전 완결 UTC 일자의 행만** 읽는다.
- look-ahead 오염 테스트는 OHLCV 각 TF + market_regime + funding_rates 시리즈 각각 독립 적용.

## 2. TradePlan + RiskEngine (규칙의 코드화)

```python
@dataclass(frozen=True)
class PlanLeg:
    kind: Literal['entry','tp','stop']
    price: float
    fraction: float

@dataclass(frozen=True)
class TradePlan:
    symbol: str; side: Literal['long','short']
    evidence: list[str]        # 근거 >= 2
    entries: list[PlanLeg]     # len>=2, fraction 합 == 1.0, 기본 50/25/25
    stop: PlanLeg              # 시나리오 붕괴 지점, 4h 종가 판정
    tps: list[PlanLeg]         # len>=2, fraction 합 == 1.0 (마지막 레그 = 잔량 전량)
    leverage: int; margin_usdt: float
```

**RR 공식 (side-aware, 정규화)**: wEntry = Σ(pᵢfᵢ) (fᵢ합=1), wTP = Σ(pⱼfⱼ) (fⱼ합=1).
long: rr = (wTP−wEntry)/(wEntry−stop), short: rr = (wEntry−wTP)/(stop−wEntry). 기하 검증 포함
(long: stop < entries < tps, short은 역순. 위반 시 거부).

**RiskEngine은 순수 함수**: `review(plan, settings, market_state) -> Approval|Rejection(reason)`.
`market_state`는 as_of_ts, mark_price, open_positions, daily_realized_pnl, blackout_windows를 담은
명시적 dataclass — DB/시계 접근 금지. 백테스트는 시뮬레이션 봉마다, 트레이더는 라이브 상태로 구성.

게이트 2계층:
- **정적 플랜 게이트 (백테스트·트레이더 동일 적용)**: 화이트리스트, 레버리지 캡(BTC 10 / 그 외 5, 최소 3),
  근거≥2, RR(BTC·ETH ≥2, 알트 ≥3), 분할 구조(진입≥2레그·합1.0, TP≥2레그·합1.0), 기하 검증,
  청산 버퍼(손절가가 청산가보다 markPrice 쪽으로 `liq_buffer_pct` 이상 여유), **진입 레그는 패시브 사이드만**
  (long 진입가 > mark 거부 — 돌파는 눌림 리테스트 형태로만, 규칙 §4-7과 일치).
- **런타임 포트폴리오 게이트 (트레이더/모니터 전용, 리포트에 명시)**: 최대 동시 포지션, 일손실 서킷브레이커,
  이벤트 블랙아웃(econ_events ±12h; 백테스트는 해당 기간 econ_events가 백필된 경우에만 결정론적으로 적용).

**복리 금지 (사이징 지점에서 강제)**: `effective_capital = min(wallet_balance, seed)`.
RiskEngine 체크: Σ(오픈 플랜 마진) + plan.margin_usdt ≤ effective_capital. 출금 스윕은
`withdrawable = max(0, realized_wallet − margin_used − seed)`를 **실현 잔고 기준, UTC 일 1회**,
미실현 손익 불포함, 사후 반전 없음 → `withdrawal_ledger`. 라이브에선 장부상 격리(실제 이체 아님)로
tradable capital에서 제외. 불변식 테스트: 수익 사이클 후 다음 플랜 마진 예산 불변.

**플랜 상태머신**: draft → approved → active(부분/전량 체결) → closed|stopped|abandoned.
- 손절 판정·최종 TP·시나리오 무효화 시 **plan_id 공유 미체결 자식 주문 전량 취소** 후 체결분 청산.
- TP/스탑 주문 수량은 **매 체결 이벤트마다 실제 체결 수량 기준 재계산** (reduce_only).
- 진입 전 무효화(플랫 상태에서 4h 종가가 손절선 이탈, 또는 plan_ttl 경과) → 래더 전량 취소(abandoned).
- **Broker ABC 레벨 강제**: reduce_only가 아닌 모든 주문은 status가 approved|active인 trade_plans 행을
  참조해야 하며 아니면 거부 — "시나리오 없는 주문은 브로커가 거부"(규칙 §2)를 어느 코드 경로도 우회 불가.
- TTL 재큐는 **원래 플랜 레그 가격 그대로만** (RR 불변). 가격 추격 재발주 없음. 시장이 떠나면 abandoned.
- 손절선 수정은 유리한 방향만 허용.

**멱등성/재기동**: client_order_id = `{plan_id}-{leg_kind}-{leg_index}-{attempt}` 결정론 생성.
타임아웃/재기동 후엔 client_order_id 조회 후 부재 시에만 제출. 부팅 시퀀스: active 플랜 로드 →
브로커 오픈주문·포지션 조회 → client_order_id/plan_id로 재부착 → 미부착 주문 취소 → 모니터 재개.
불변식: 재기동 후 중복 주문 0건. (PaperBroker는 SQLite 상태라 순수 DB 패스로 동일 수행.)

## 3. 전략 카탈로그

각 전략: `generate_plan(spec, frames: dict[tf, DataFrame], regime) -> TradePlan|None`.
`{-1,0,+1}`은 전략의 방향 의도 표현일 뿐, 엔진 입력은 항상 TradePlan.

### 3.1 레짐 모듈 (키 프리, 백테스트 가능한 프록시)

CoinGecko 무료 API는 히스토리를 제공하지 않으므로 **Binance 일봉 프록시**로 대체:
- `ALT_INDEX` = 유니버스 내 알트(비BTC) USDT 일봉 종가의 등가중 정규화 지수 → TOTAL2/3 프록시.
- `DOM_PROXY` = BTC 종가 / ALT_INDEX 비율 → 도미넌스 방향 프록시.
- 판정: ALT_INDEX 50SMA > 200SMA → 롱장, 아니면 숏/관망. 시장↑+DOM↓=알트 불장, 시장↓+DOM↑=현금/숏.
- 레짐 결과: `long_alt | long_btc | short | cash`. **히스토리 < 200봉이면 `cash`(진입 차단)**.
- 인트라데이 사용 시 직전 완결 일자 행만 (§1.2). market_regime 테이블에 캐시.

### 3.2 템플릿

| 템플릿 | 규칙 매핑 |
|---|---|
| `topdown_pullback` | 1d 200SMA 위 + 4h 골든크로스 + 15m 지지 리테스트·거래량 (§4-1) |
| `ma_confluence` | 4h 50/100/200 수렴 지지=롱/이탈=숏, 400선 패닉 분할매수 (§4-2) |
| `box_range` | 4h 확정 피벗 기반 박스: 하단25%=롱·상단25%=숏·중간=관망. **최종 TP = 박스 미드포인트** (규칙 §3) |
| `vwma_support` | VWMA100 지지 롱 / VWMA+VPVR 동시 이탈 숏 |
| `candle_breakout` | 장대양봉+거래량 돌파 **후 눌림 리테스트** 진입 / 꼬리 반전 캔들 (진입 레그는 항상 패시브 사이드) |

**피벗/존/VPVR 룩어헤드 차단**: 스윙 피벗은 우측 k봉(파라미터, 기본 3) 마감 후에만 확정,
확정 시각을 가진다. 봉 t에서 쓸 수 있는 박스/존 레벨은 t−1 이전 확정 피벗만. VPVR 윈도우 = [t−W, t−1]
마감 봉. 오염 테스트: 4h 마지막 k+1봉 오염 → 이전 봉들의 박스/VPVR 레벨 불변.

상대강도(§4-4)는 Strategist가 심볼 랭킹에 사용(BTC 하락 대비 상대 수익률). 순환매(§4-8)·호가창
회피(§4-9)는 Phase 2 (기본 off). 파라미터 그리드·변이·registry 구조는 stockAgents 그대로.

## 4. 백테스트 엔진 (플랜 구동, 상태 보존 루프)

벡터화 계층은 **지표 계산과 봉별 '플랜 후보 트리거'까지만**. 주문 수명주기(레스팅 레그, 체결,
TTL, 평단/마진/청산가 재계산, 부분 TP)는 실행 TF 봉 순차 루프가 소유. trades/metrics는 plan_id로 롤업.

**체결 모델 (규칙 §5 '관통해야 체결', 보수적)**:
- 진입(long): bar.low < P 일 때 체결, 체결가 = min(bar.open, P) (갭스루 오픈 처리). short 대칭.
- 주문은 **발주 이후에 open하는 봉**부터 매칭.
- 동일 봉 우선순위(보수적, 결정론): **청산 > 손절 exit > 진입 > TP**.
  진입과 TP가 같은 봉에서 닿으면 진입만 체결, TP는 다음 봉부터 (봉 종가가 TP 초과 시 예외적 종가 체결).
- 페이퍼/백테스트 모두 레그 단위 all-or-none (partially_filled는 라이브 전용 상태).

**손절 (4h 종가 판정)**: 완결된 4h봉마다 1회 판정. 4h 마감 시각 ≥ open인 **첫 15m봉의 시가**에서
taker 0.05%+슬리피지로 청산 (공격적 크로싱 리밋 모델). 미완결 4h봉으로는 절대 판정 금지(오염 테스트).

**청산**: intrabar low/high 기준, 손절보다 우선. 정확식 사용:
liq_long = avg_entry×(1−1/L)/(1−MMR), liq_short = avg_entry×(1+1/L)/(1+MMR). MMR은 노셔널 구간
테이블(tier-1이면 충분하나 테이블로). **매 체결 이벤트마다 avg_entry·마진·청산가 재계산** (백테스트·페이퍼 동일).
청산 시 격리마진 전액 손실. liquidation_count > 0 → Risk 즉시 탈락.

**펀딩**: 정산 시각마다 `cash_flow = −sign(pos) × rate × notional(마크가 기준)`
(long+양수 rate = 지불/차감). Binance fundingRate 이력 행을 각자의 ts에 적용(룩어헤드 없음).
이력 없으면 0.01%/8h 근사(설정). 부호 단위 테스트 필수.

**비용**: PerpCostModel(maker_fee=0.00025, taker_fee=0.0005, slippage) — 체결마다 어느 요율인지 태깅.
진입·TP=maker, 손절/청산회피 exit=taker. '비용은 성과를 항상 감소' 테스트 유지.

**지표**: 마진 조정 에쿼티 기준 per-bar 수익률. Sharpe = mean/std × √bars_per_year[tf]
(map: {1d:365, 4h:2190, 15m:35040, 5m:105120}). 총수익은 **비복리 합산 PnL/seed** (출금 규칙과 일치 —
챔피언 선정 목적함수가 라이브가 금지한 복리를 가정하지 않도록). 연환산 수익은 실제 타임스탬프 스팬 기준.
사이징은 항상 고정 시드 기준 margin_usdt. holding_hours, funding_paid, liquidation_count 포함.

## 5. 브로커

`Broker` ABC: `get_quote, get_balance, get_positions, place_order, cancel_order, get_open_orders,
set_leverage, set_margin_mode`. `OrderRequest`: symbol, side, qty(float), limit_price(필수),
reduce_only, aggressive(스탑엑싯 전용), leverage, client_order_id, plan_id.
ABC 공통 검증: 비-reduce_only 주문은 approved|active 플랜 필수. 심볼 필터(tickSize/stepSize/minNotional) 반올림.

- **PaperBroker**: 1m 마감 봉으로 지정가 체결 시뮬 (§4와 동일 관통 규칙 — 15m 백테스트 체결이면 그
  구성 1m봉에서도 체결됨을 패리티 테스트로 보증). **크로싱 주문은 post-only 거부** (aggressive 플래그 제외).
  aggressive reduce_only 주문은 다음 1m 시가에 taker로 체결. 격리마진·청산·펀딩 원장 시뮬.
- **BinanceBroker**: HMAC REST, 테스트넷 플래그. 기동 시 키 검증 + 심볼별 isolated/레버리지 설정 +
  리컨실(§2 부팅 시퀀스). 스탑엑싯 = reduce-only 리밋 체이스(1m마다 cancel-replace, K회 후 또는
  마진비율 임계 초과 시 reduce-only IOC 폴백 — 손절·청산회피 한정 taker 허용). 안전장치:
  `live_max_order_usdt`, rolling-24h 주문 수 한도, `live_max_loss_pct` 서킷브레이커, 레버리지 캡,
  reduce_only 킬스위치 모드. 429/418 백오프, recvWindow·서버시간 동기.
- **모드 전환**: `POST /api/trading-mode {mode, confirm:'LIVE'}`.
  Orchestrator/Monitor는 브로커를 직접 들지 않고 **`broker_provider()`로 매 사이클/틱 시작 시 조회**.
  전환 게이트: 사이클 실행 중이거나 현재 모드에 오픈 주문/포지션 존재 시 **409 거부** (flat-and-idle 필수).
  live 전환은 키 검증+리컨실 성공 후에만 활성화. UI 뱃지 paper(초록)↔live(빨강), 타이핑 확인 모달.

## 6. DB 스키마 (변경분)

- `ohlcv_cache(symbol, timeframe, ts, o,h,l,c, volume, quote_volume, PK(symbol,timeframe,ts))`
- `funding_rates(symbol, ts, rate, PK(symbol,ts))` / `funding_payments(id, ts, symbol, side, rate, payment)`
- `market_regime(date PK, alt_index, dom_proxy, regime)` (일봉 프록시 캐시)
- `trade_plans(id, created_at, symbol, side, plan_json, status[draft|approved|rejected|active|closed|stopped|abandoned], reject_reason, filled_fraction)`
- `paper_orders`: +order_type='limit', limit_price, filled_qty, avg_fill_price, reduce_only, aggressive,
  plan_id, leg_kind, leg_index, client_order_id UNIQUE, status[open|filled|cancelled|expired|rejected]
- `paper_positions(symbol PK, side, qty REAL, avg_entry, leverage, isolated_margin, liq_price)`
- `trades`: entry_ts/exit_ts (ISO datetime), holding_hours REAL, side, leverage, timeframe, funding_paid, fee_paid
  (frontend TradeRow와 정확 일치 — exact-shape 테스트 유지)
- `portfolio_snapshots`: +wallet_balance, available, margin_used, unrealized_pnl, funding_cum
- `withdrawal_ledger(id, ts, amount, reason)` · `econ_events(id, ts, name)`
- activity_log: 보존 정책(30일 또는 N행 초과 프루닝 잡). 기존 cycles/strategies/backtests/reports/champion_history 유지.

## 7. API / WS / 이벤트

- 추가 라우트: `POST /api/trading-mode`, `GET /api/positions`(liq·마진비율·펀딩 카운트다운),
  `GET /api/regime`, `GET/PUT /api/econ-events`, `GET /api/plans/{id}`.
- WS 이벤트 추가: `order_filled|order_cancelled|position_update|funding_payment|liquidation_warning|regime_update`.
- **이벤트 볼륨 제어**: position_update는 유의미 변화(체결·마진비율 밴드 교차·펀딩) 또는 ≥30s 스로틀.
  EventBus에 `persist=False` 텔레메트리 경로 추가(하트비트류는 activity_log 미기록). 스냅샷에 포지션·마진 포함.

## 8. 프론트엔드

오피스 캔버스·FSM·웨이포인트·스프라이트 무변경. 변경: USDT 포맷, 풀 타임스탬프, holding_hours,
중첩 파라미터 허용, PortfolioPanel 선물화(총자산/사용가능/포지션마진/미실현 + side·레버리지·청산가·
청산거리·펀딩), Leaderboard(TF·side·펀딩·청산 뱃지), ChampionPanel(분할 체결 "3분할 진입 2/3 체결"),
ControlBar(모드 뱃지+전환 버튼+확인 모달, 레짐 칩). 로그 vocabulary: '청산','펀딩','손절','분할 진입' 유지.

## 9. 테스트 불변식 (오프라인 합성 시드)

look-ahead 금지(OHLCV TF별+레짐+펀딩+피벗/VPVR 독립 오염), 미완성 봉 제외, 비용 단조 감소,
RR 게이트(롱·숏 각각), 레버리지 캡(BTC 10/ETH·알트 5), 화이트리스트, 블랙아웃, 분할 구조(레그 수·합),
기하 검증, 패시브 사이드 강제, 청산>손절>진입>TP 동일봉 우선순위, 관통 체결(터치 미체결), 갭스루 체결가,
4h 종가 손절 판정·첫 15m 시가 taker 청산, 미완결 4h봉 판정 금지, 청산 정확식·체결마다 재계산,
펀딩 부호, TTL 원가격 재큐·추격 금지, 플랜 종료 시 자식 주문 전량 취소, TP 수량=체결 수량 기준,
브로커 레벨 플랜 없는 주문 거부, 재기동 후 중복 주문 0건, 복리 금지(수익 후 마진 예산 불변),
실현 잔고만 출금, 손절 불리한 수정 거부, live 키 없이 기동 거부, 모드 전환 flat-and-idle 게이트,
페이퍼 크로싱 주문 post-only 거부, 15m↔1m 체결 패리티.

## 10. 단계

- **Phase 1 (이번 구현)**: 연구 사이클 + 페이퍼 트레이딩(실데이터) + 모드 전환 인프라 전체.
- **Phase 2**: 순환매·호가창 회피, 유저데이터 WS, VPVR 고도화.
