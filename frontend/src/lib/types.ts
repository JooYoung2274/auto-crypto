// Shared REST / WebSocket payload types — mirrors spec §7/§8
// (docs/superpowers/specs/2026-07-14-coin-agents-design.md).
// Backend is the source of truth; keep field names exactly as the spec states.

export type TradingMode = "paper" | "live"
export type AgentStateValue = "idle" | "working"
export type MeetingId = string | number

/** The three kinds of run a cycle can be (spec: research / validate / trade). */
export type CycleKind = "research" | "validate" | "trade"
/** Reports are only produced by research and validate runs (trade has no report step). */
export type ReportKind = "research" | "validation"

/** Perp position direction (paper_positions.side / trades.side). */
export type PositionSide = "long" | "short"

/** Market regime verdict (spec §3.1 — market_regime.regime). */
export type RegimeValue = "long_alt" | "long_btc" | "short" | "cash"

/**
 * Strategy params may nest (multi-timeframe legs, ladder fractions…), so the
 * value type is recursive instead of the stock version's flat number map.
 */
export type ParamValue = number | string | boolean | null | ParamValue[] | { [key: string]: ParamValue }
export type StrategyParams = Record<string, ParamValue>

// ---------------------------------------------------------------------------
// REST payloads
// ---------------------------------------------------------------------------

export interface AgentInfo {
  id: string
  name: string
  role: string
  state: AgentStateValue
  detail: string
}

export interface CycleInfo {
  id: number
  status: string
  step: string
  /** Which kind of run this is. Optional so older/partial payloads degrade gracefully (treated as research). */
  kind?: CycleKind
}

/** 목표 탐색 모드(goal-seek) 상태 — null until goal mode is started once this process. */
export interface GoalStatus {
  running: boolean
  cycles_done: number
  best_win_rate: number | null
  target_win_rate: number
  max_cycles: number
}

/** GET /api/status */
export interface StatusResponse {
  trading_mode: TradingMode
  cycle: CycleInfo | null
  agents: AgentInfo[]
  /** Optional so pre-goal-mode payloads degrade gracefully. */
  goal?: GoalStatus | null
}

export interface AvgMetrics {
  win_rate: number | null
  sharpe: number | null
  mdd: number | null
  cagr: number | null
  profit_factor: number | null
  trade_count: number
  /** 펀딩 순지불 합 (USDT, 양수 = 비용). Optional for legacy rows. */
  funding_paid?: number | null
  /** 강제 청산 횟수 — > 0 이면 리스크 즉시 탈락 대상 (스펙 §4). */
  liquidation_count?: number | null
}

/** GET /api/leaderboard row */
export interface LeaderboardEntry {
  strategy_id: number
  template: string
  params: StrategyParams
  avg_metrics: AvgMetrics
  low_confidence: boolean
  /** Annualized trade count below the activity threshold — champion-ineligible. */
  low_activity: boolean
  status: string
  /** 실행 타임프레임 (e.g. "15m"). Falls back to params.timeframe when absent. */
  timeframe?: string | null
  /** 전략의 방향 성향. Falls back to params.side when absent. */
  side?: PositionSide | "both" | null
}

export interface BacktestSummary {
  id: number
  /** Perp symbol, e.g. "BTCUSDT" (backtests.symbol — renamed from ticker). */
  symbol: string
  metrics: Record<string, number | boolean | null>
}

/** One leg of a TradePlan ladder (spec §2 — PlanLeg). */
export interface PlanLeg {
  kind: "entry" | "tp" | "stop"
  price: number
  fraction: number
}

/**
 * GET /api/plans/{id} — a TradePlan row (trade_plans + parsed plan_json).
 * Also embedded on the champion card as `active_plan` for split-fill status.
 */
export interface PlanInfo {
  id: number
  symbol: string
  side: PositionSide
  /** draft | approved | rejected | active | closed | stopped | abandoned */
  status: string
  entries: PlanLeg[]
  tps: PlanLeg[]
  stop: PlanLeg | null
  /** 진입 근거 (스펙 §2 — 근거 ≥ 2). */
  evidence?: string[]
  leverage?: number
  margin_usdt?: number
  /** 진입 래더 체결 비율 0..1 (trade_plans.filled_fraction). */
  filled_fraction: number
  reject_reason?: string
  created_at?: string
}

/** GET /api/champions `current` — the strategy 모의거래 executes. */
export interface ChampionDetail {
  strategy_id: number
  template: string
  params: StrategyParams
  avg_metrics: AvgMetrics
  low_confidence: boolean
  low_activity: boolean
  status: string
  /** When this reign began (SQLite UTC stamp, no tz suffix). */
  crowned_at: string | null
  /** Stop-loss distance as a price fraction; null when no representative stop. */
  stop_pct?: number | null
  /** Take-profit distance = stop_pct × rr; null when there is no stop. */
  take_profit_pct?: number | null
  /** 현재 진행 중인 TradePlan (분할 진입 체결 현황 표시용); 없으면 null/absent. */
  active_plan?: PlanInfo | null
  backtests: BacktestSummary[]
}

/** GET /api/champions `history` row — one past reign (newest first, current excluded). */
export interface ChampionReign {
  strategy_id: number
  template: string
  params: StrategyParams
  crowned_at: string
  demoted_at: string
  avg_metrics: AvgMetrics
}

/** GET /api/champions */
export interface ChampionsResponse {
  current: ChampionDetail | null
  history: ChampionReign[]
}

/** GET /api/strategies/{id} */
export interface StrategyDetail extends LeaderboardEntry {
  backtests: BacktestSummary[]
}

/** trades row — exact DB column names (spec §6, exact-shape test contract). */
export interface TradeRow {
  entry_ts: string
  exit_ts: string | null
  entry_price: number
  exit_price: number | null
  net_ret: number | null
  holding_hours: number | null
  side: PositionSide
  leverage: number
  timeframe?: string | null
  /** 펀딩 순지불 (USDT, 양수 = 비용). */
  funding_paid?: number | null
  /** 수수료 합 (maker + taker, USDT). */
  fee_paid?: number | null
  open?: boolean
}

export interface EquityPoint {
  date: string
  value: number
}

/** GET /api/backtests/{id} */
export interface BacktestDetail {
  id: number
  strategy_id: number
  symbol: string
  timeframe?: string | null
  metrics: Record<string, number | boolean | null>
  equity_curve: EquityPoint[]
  trades: TradeRow[]
}

export interface ReportSummary {
  id: number
  cycle_id: number
  created_at: string
  /** research (전략 연구) or validation (수익성 검증). Optional for pre-split rows (treated as research). */
  kind?: ReportKind
}

/** GET /api/reports/{id} */
export interface Report extends ReportSummary {
  markdown: string
}

/** activity_log row (GET /api/logs and the `log` WS event share this shape) */
export interface LogEntry {
  id: number
  ts: string
  agent: string | null
  level: string
  event_type: string
  message: string
  data?: Record<string, unknown> | null
}

/**
 * One isolated-margin perp position (paper_positions row + live mark-price
 * context). Served by GET /api/positions and embedded in GET /api/portfolio.
 */
export interface PositionInfo {
  symbol: string
  side: PositionSide
  qty: number
  avg_entry: number
  leverage: number
  isolated_margin: number
  liq_price: number
  /** 마크 가격 — 계산 불가 시 null/absent. */
  mark_price?: number | null
  unrealized_pnl?: number | null
  /** 유지마진 비율 0..1 — 청산 근접도. */
  margin_ratio?: number | null
  /** 이 포지션의 누적 펀딩 지불 (USDT, 양수 = 비용). */
  funding_paid?: number | null
  /** 다음 펀딩 정산 시각 (UTC ISO) — 카운트다운 표시용. */
  next_funding_ts?: string | null
  /** 걸려 있는 분할 익절 라인 (TP1이 먼저 — 진입가에서 가까운 순). */
  tp_lines?: { price: number; qty: number }[]
  /** 시나리오 손절선 (4h 종가 판정 기준). */
  stop_price?: number | null
}

/** Futures wallet summary (portfolio_snapshots latest row shape). */
export interface MarginSummary {
  wallet_balance: number
  available: number
  margin_used: number
  unrealized_pnl: number
  funding_cum?: number | null
}

export interface PortfolioSnapshot {
  ts: string
  wallet_balance: number
  available: number
  margin_used: number
  unrealized_pnl: number
  funding_cum?: number | null
  total_value: number
}

/** GET /api/portfolio */
export interface PortfolioResponse {
  wallet_balance: number
  available: number
  margin_used: number
  unrealized_pnl: number
  funding_cum?: number | null
  /** 복리 금지 스윕 누적 출금 수익 (withdrawal_ledger 합, USDT). */
  withdrawn_cum?: number | null
  positions: PositionInfo[]
  snapshots: PortfolioSnapshot[]
}

/** GET /api/regime (market_regime latest row, spec §3.1). */
export interface RegimeInfo {
  date: string
  regime: RegimeValue
  alt_index?: number | null
  dom_proxy?: number | null
}

/** POST /api/trading-mode response. */
export interface TradingModeResponse {
  trading_mode: TradingMode
}

/** GET/PUT /api/config (trading_mode is read-only on the server) */
export interface AppConfig {
  trading_mode: TradingMode
  universe: string[]
  timeframes?: string[]
  execution_timeframe?: string
  auto_cycle_minutes: number
  max_mdd: number
  min_trades: number
}

// ---------------------------------------------------------------------------
// WebSocket events (`type` field discriminator) — spec §7
// ---------------------------------------------------------------------------

export interface SnapshotAgent {
  id: string
  state: AgentStateValue
  detail: string
}

export interface MeetingInfo {
  id: MeetingId
  agents: [string, string]
}

export interface SnapshotEvent {
  type: "snapshot"
  agents: SnapshotAgent[]
  cycle: CycleInfo | null
  meeting: MeetingInfo | null
  /** 스냅샷에 포지션·마진 포함 (스펙 §7) — optional so partial payloads degrade. */
  positions?: PositionInfo[]
  margin?: MarginSummary | null
  trading_mode?: TradingMode
  regime?: RegimeValue | null
}

export interface AgentStateEvent {
  type: "agent_state"
  agent_id: string
  state: AgentStateValue
  detail: string
}

export interface MeetingStartEvent {
  type: "meeting_start"
  meeting_id: MeetingId
  agents: [string, string]
  topic: string
}

export interface MeetingEndEvent {
  type: "meeting_end"
  meeting_id: MeetingId
}

export interface LogEvent extends LogEntry {
  type: "log"
}

export interface CycleProgressEvent {
  type: "cycle_progress"
  cycle_id: number
  step: string
  pct: number
  kind?: CycleKind
}

export interface LeaderboardUpdateEvent {
  type: "leaderboard_update"
  top: LeaderboardEntry[]
}

/** 주문 체결 (레그 단위) — paper_orders 체결 이벤트. */
export interface OrderFilledEvent {
  type: "order_filled"
  symbol: string
  side: string
  qty: number
  price: number
  plan_id?: number | null
  leg_kind?: string | null
  leg_index?: number | null
  reduce_only?: boolean
}

export interface OrderCancelledEvent {
  type: "order_cancelled"
  symbol: string
  order_id?: number | null
  plan_id?: number | null
  reason?: string
}

/** 포지션 변경 텔레메트리 (유의미 변화 또는 ≥30s 스로틀, persist=False 경로). */
export interface PositionUpdateEvent {
  type: "position_update"
  positions: PositionInfo[]
  margin?: MarginSummary | null
}

/** 펀딩 정산 (funding_payments row; payment 양수 = 지불/비용). */
export interface FundingPaymentEvent {
  type: "funding_payment"
  symbol: string
  side: PositionSide
  rate: number
  payment: number
  ts?: string
}

/** 청산 경고 — 마크가가 청산가 버퍼 안으로 진입. */
export interface LiquidationWarningEvent {
  type: "liquidation_warning"
  symbol: string
  liq_price: number
  mark_price: number
  margin_ratio?: number | null
  message?: string
}

/** 레짐 판정 갱신 (spec §3.1). */
export interface RegimeUpdateEvent {
  type: "regime_update"
  regime: RegimeValue
  date?: string
  alt_index?: number | null
  dom_proxy?: number | null
}

export type WsEvent =
  | SnapshotEvent
  | AgentStateEvent
  | MeetingStartEvent
  | MeetingEndEvent
  | LogEvent
  | CycleProgressEvent
  | LeaderboardUpdateEvent
  | OrderFilledEvent
  | OrderCancelledEvent
  | PositionUpdateEvent
  | FundingPaymentEvent
  | LiquidationWarningEvent
  | RegimeUpdateEvent

/** GET /api/plans 의 플랜별 자식 주문 (분할 레그). */
export interface PlanOrderInfo {
  id: number
  side: "buy" | "sell"
  qty: number
  limit_price: number | null
  /** open | filled | cancelled | expired | rejected */
  status: string
  leg_kind: string | null
  leg_index: number | null
  filled_qty: number
  reduce_only: boolean
  ts: string
}

/** GET /api/plans — 대기·진행 중 플랜 + 자식 주문 (대기 주문 탭). */
export interface OpenPlanInfo extends PlanInfo {
  orders: PlanOrderInfo[]
}
