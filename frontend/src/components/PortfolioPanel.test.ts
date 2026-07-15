import { describe, expect, it } from "vitest"
import { snapshotsToChartData } from "./PortfolioPanel"
import { buildChartGeometry } from "./EquityChart"
import type { PortfolioSnapshot } from "../lib/types"

const PAD = { top: 10, right: 10, bottom: 20, left: 40 }

// Exact shape returned by GET /api/portfolio snapshots (portfolio_snapshots
// columns, spec §6): {ts, wallet_balance, available, margin_used,
// unrealized_pnl, funding_cum, total_value} — USDT futures wallet.
const BACKEND_SNAPSHOTS: PortfolioSnapshot[] = [
  {
    ts: "2026-07-14 05:16:01",
    wallet_balance: 10_000.0,
    available: 8_500.0,
    margin_used: 1_500.0,
    unrealized_pnl: -12.5,
    funding_cum: -0.8,
    total_value: 9_987.5,
  },
  {
    ts: "2026-07-14 06:16:01",
    wallet_balance: 10_000.0,
    available: 8_500.0,
    margin_used: 1_500.0,
    unrealized_pnl: 116.0,
    funding_cum: -1.2,
    total_value: 10_116.0,
  },
  {
    ts: "2026-07-14 07:16:01",
    wallet_balance: 10_050.0,
    available: 8_550.0,
    margin_used: 1_500.0,
    unrealized_pnl: 16.0,
    funding_cum: -1.6,
    total_value: 10_066.0,
  },
]

describe("snapshotsToChartData", () => {
  it("maps futures snapshot rows to finite chart values from total_value", () => {
    const data = snapshotsToChartData(BACKEND_SNAPSHOTS)
    expect(data).toEqual([
      { date: "2026-07-14 05:16:01", value: 9_987.5 },
      { date: "2026-07-14 06:16:01", value: 10_116.0 },
      { date: "2026-07-14 07:16:01", value: 10_066.0 },
    ])
    for (const p of data) expect(Number.isFinite(p.value)).toBe(true)
  })

  it("produces drawable chart geometry from backend-shaped snapshots", () => {
    const geom = buildChartGeometry(snapshotsToChartData(BACKEND_SNAPSHOTS), 720, 220, PAD)
    expect(geom).not.toBeNull()
    expect(geom!.linePath).not.toContain("NaN")
    expect(geom!.points).toHaveLength(3)
  })

  it("keeps full timestamps in x tick labels (24/7 market — no date-only axis)", () => {
    const geom = buildChartGeometry(snapshotsToChartData(BACKEND_SNAPSHOTS), 720, 220, PAD)!
    expect(geom.xTicks[0].label).toBe("07.14 05:16")
    expect(geom.xTicks[geom.xTicks.length - 1].label).toBe("07.14 07:16")
  })
})
