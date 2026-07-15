import type {
  AppConfig,
  BacktestDetail,
  ChampionsResponse,
  CycleKind,
  EquityPoint,
  LeaderboardEntry,
  LogEntry,
  OpenPlanInfo,
  PlanInfo,
  PortfolioResponse,
  PositionInfo,
  RegimeInfo,
  Report,
  ReportSummary,
  StatusResponse,
  StrategyDetail,
  TradingMode,
  TradingModeResponse,
} from "./types"

/** REST client for the backend API (spec §7). Same-origin; Vite dev proxies /api. */

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  })
  if (!res.ok) {
    let detail = ""
    try {
      detail = await res.text()
    } catch {
      // keep empty
    }
    throw new ApiError(res.status, `${init?.method ?? "GET"} ${path} failed (${res.status})`, detail)
  }
  return (await res.json()) as T
}

export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, message: string, detail = "") {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.detail = detail
  }
}

/**
 * The backend serializes equity curves as [date, value] pairs
 * (backend/app/agents/quant.py downsample_equity, served verbatim by
 * GET /api/backtests/{id}); the UI works with EquityPoint objects, so
 * normalize at the API boundary.
 */
export type RawEquityPoint = EquityPoint | [string, number]

export function normalizeEquityCurve(curve: RawEquityPoint[]): EquityPoint[] {
  return curve.map((p) => (Array.isArray(p) ? { date: p[0], value: p[1] } : p))
}

export interface LogsQuery {
  agent?: string
  limit?: number
  before_id?: number
}

export const api = {
  status(): Promise<StatusResponse> {
    return request("/api/status")
  },
  startCycle(kind?: CycleKind): Promise<{ cycle_id: number; status: string; kind: CycleKind }> {
    // No body defaults to research on the backend; only send one when a kind is chosen.
    return request("/api/cycle/start", {
      method: "POST",
      ...(kind ? { body: JSON.stringify({ kind }) } : {}),
    })
  },
  stopCycle(): Promise<{ ok: boolean }> {
    return request("/api/cycle/stop", { method: "POST" })
  },
  goalStart(): Promise<{ status: string }> {
    return request("/api/goal/start", { method: "POST" })
  },
  goalStop(): Promise<{ status: string }> {
    return request("/api/goal/stop", { method: "POST" })
  },
  leaderboard(limit = 20): Promise<LeaderboardEntry[]> {
    return request(`/api/leaderboard?limit=${limit}`)
  },
  champions(): Promise<ChampionsResponse> {
    return request("/api/champions")
  },
  strategy(id: number): Promise<StrategyDetail> {
    return request(`/api/strategies/${id}`)
  },
  async backtest(id: number): Promise<BacktestDetail> {
    const raw = await request<Omit<BacktestDetail, "equity_curve"> & { equity_curve: RawEquityPoint[] }>(
      `/api/backtests/${id}`,
    )
    return { ...raw, equity_curve: normalizeEquityCurve(raw.equity_curve) }
  },
  reports(): Promise<ReportSummary[]> {
    return request("/api/reports")
  },
  report(id: number): Promise<Report> {
    return request(`/api/reports/${id}`)
  },
  logs(query: LogsQuery = {}): Promise<LogEntry[]> {
    const params = new URLSearchParams()
    if (query.agent) params.set("agent", query.agent)
    if (query.limit !== undefined) params.set("limit", String(query.limit))
    if (query.before_id !== undefined) params.set("before_id", String(query.before_id))
    const qs = params.toString()
    return request(`/api/logs${qs ? `?${qs}` : ""}`)
  },
  portfolio(): Promise<PortfolioResponse> {
    return request("/api/portfolio")
  },
  /** 오픈 포지션 목록 (청산가·마진비율·펀딩 카운트다운 포함, spec §7). */
  positions(): Promise<PositionInfo[]> {
    return request("/api/positions")
  },
  /** 최신 레짐 판정 (spec §3.1). */
  regime(): Promise<RegimeInfo> {
    return request("/api/regime")
  },
  /** TradePlan 상세 (래더 레그 + 체결 비율). */
  /** 대기·진행 중 플랜 목록 + 분할 레그 주문 (대기 주문 탭). */
  openPlans(): Promise<OpenPlanInfo[]> {
    return request("/api/plans")
  },
  plan(id: number): Promise<PlanInfo> {
    return request(`/api/plans/${id}`)
  },
  /**
   * 모드 전환 (spec §5): live 전환은 confirm:"LIVE" 타이핑 확인 필수.
   * 409 = flat-and-idle 게이트 거부 (사이클 실행 중 또는 오픈 주문/포지션 존재).
   */
  setTradingMode(mode: TradingMode, confirm?: string): Promise<TradingModeResponse> {
    return request("/api/trading-mode", {
      method: "POST",
      body: JSON.stringify(confirm !== undefined ? { mode, confirm } : { mode }),
    })
  },
  getConfig(): Promise<AppConfig> {
    return request("/api/config")
  },
  putConfig(config: Partial<AppConfig>): Promise<AppConfig> {
    return request("/api/config", { method: "PUT", body: JSON.stringify(config) })
  },
}
