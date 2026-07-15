import { afterEach, describe, expect, it, vi } from "vitest"
import { api, ApiError, normalizeEquityCurve } from "./api"
import type { RawEquityPoint } from "./api"

// Backend wire format: downsample_equity (backend/app/agents/quant.py) emits
// [date, value] PAIRS, served verbatim by GET /api/backtests/{id}.
const BACKEND_BACKTEST_PAYLOAD = {
  id: 7,
  strategy_id: 3,
  symbol: "BTCUSDT",
  timeframe: "15m",
  metrics: { cagr: 0.12, mdd: 0.08, funding_paid: 3.2, liquidation_count: 0 },
  equity_curve: [
    ["2026-01-02T00:00:00", 1.0],
    ["2026-01-02T04:00:00", 1.01],
    ["2026-01-02T08:00:00", 0.99],
  ],
  trades: [],
}

describe("normalizeEquityCurve", () => {
  it("converts backend [date, value] pairs to EquityPoint objects", () => {
    const pairs: RawEquityPoint[] = [
      ["2023-01-02", 1.0],
      ["2023-01-03", 1.05],
    ]
    expect(normalizeEquityCurve(pairs)).toEqual([
      { date: "2023-01-02", value: 1.0 },
      { date: "2023-01-03", value: 1.05 },
    ])
  })

  it("passes already-normalized objects through unchanged", () => {
    const objs: RawEquityPoint[] = [{ date: "2023-01-02", value: 1.0 }]
    expect(normalizeEquityCurve(objs)).toEqual([{ date: "2023-01-02", value: 1.0 }])
  })
})

describe("api.startCycle", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  const stubFetch = () => {
    const fetchMock = vi.fn(
      async (_path: string, _init?: RequestInit) =>
        new Response(JSON.stringify({ cycle_id: 1, status: "running", kind: "research" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    )
    vi.stubGlobal("fetch", fetchMock)
    return fetchMock
  }

  it("posts a JSON body with the chosen kind", async () => {
    const fetchMock = stubFetch()
    await api.startCycle("validate")
    expect(fetchMock).toHaveBeenCalledWith("/api/cycle/start", expect.objectContaining({ method: "POST" }))
    const init = fetchMock.mock.calls[0][1]
    expect(JSON.parse(init?.body as string)).toEqual({ kind: "validate" })
  })

  it("sends no body when no kind is given (backend defaults to research)", async () => {
    const fetchMock = stubFetch()
    await api.startCycle()
    const init = fetchMock.mock.calls[0][1]
    expect(init?.body).toBeUndefined()
  })
})

describe("api goal-seek", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  const stubFetch = () => {
    const fetchMock = vi.fn(
      async (_path: string, _init?: RequestInit) =>
        new Response(JSON.stringify({ status: "running" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    )
    vi.stubGlobal("fetch", fetchMock)
    return fetchMock
  }

  it("goalStart POSTs to /api/goal/start with no body", async () => {
    const fetchMock = stubFetch()
    await api.goalStart()
    expect(fetchMock).toHaveBeenCalledWith("/api/goal/start", expect.objectContaining({ method: "POST" }))
    expect(fetchMock.mock.calls[0][1]?.body).toBeUndefined()
  })

  it("goalStop POSTs to /api/goal/stop", async () => {
    const fetchMock = stubFetch()
    await api.goalStop()
    expect(fetchMock).toHaveBeenCalledWith("/api/goal/stop", expect.objectContaining({ method: "POST" }))
  })
})

describe("api.setTradingMode (spec §5 — 모드 전환)", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  const stubFetch = (status = 200, body: unknown = { trading_mode: "live" }) => {
    const fetchMock = vi.fn(
      async (_path: string, _init?: RequestInit) =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
    )
    vi.stubGlobal("fetch", fetchMock)
    return fetchMock
  }

  it("POSTs mode + typed confirm text for live", async () => {
    const fetchMock = stubFetch()
    await api.setTradingMode("live", "LIVE")
    expect(fetchMock).toHaveBeenCalledWith("/api/trading-mode", expect.objectContaining({ method: "POST" }))
    const init = fetchMock.mock.calls[0][1]
    expect(JSON.parse(init?.body as string)).toEqual({ mode: "live", confirm: "LIVE" })
  })

  it("POSTs mode only when switching back to paper", async () => {
    const fetchMock = stubFetch(200, { trading_mode: "paper" })
    await api.setTradingMode("paper")
    const init = fetchMock.mock.calls[0][1]
    expect(JSON.parse(init?.body as string)).toEqual({ mode: "paper" })
  })

  it("throws ApiError(409) on the flat-and-idle gate rejection", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("open positions", { status: 409 })),
    )
    const err = await api.setTradingMode("live", "LIVE").catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(409)
  })
})

describe("api.positions / api.regime / api.plan (new coin routes)", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("GETs the new endpoints at their spec §7 paths", async () => {
    const fetchMock = vi.fn(
      async (_path: string) =>
        new Response(JSON.stringify([]), { status: 200, headers: { "Content-Type": "application/json" } }),
    )
    vi.stubGlobal("fetch", fetchMock)
    await api.positions()
    await api.regime().catch(() => null)
    await api.plan(3).catch(() => null)
    const paths = fetchMock.mock.calls.map((c) => c[0])
    expect(paths).toContain("/api/positions")
    expect(paths).toContain("/api/regime")
    expect(paths).toContain("/api/plans/3")
  })
})

describe("api.backtest", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("normalizes the backend's equity_curve pairs so every point has a finite value", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(JSON.stringify(BACKEND_BACKTEST_PAYLOAD), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      ),
    )

    const detail = await api.backtest(7)

    expect(detail.symbol).toBe("BTCUSDT")
    expect(detail.equity_curve).toEqual([
      { date: "2026-01-02T00:00:00", value: 1.0 },
      { date: "2026-01-02T04:00:00", value: 1.01 },
      { date: "2026-01-02T08:00:00", value: 0.99 },
    ])
    // The old untransformed shape made d.value undefined for every point,
    // which broke buildChartGeometry (Math.min(...) → NaN → null geometry).
    for (const p of detail.equity_curve) {
      expect(Number.isFinite(p.value)).toBe(true)
      expect(typeof p.date).toBe("string")
    }
  })
})
