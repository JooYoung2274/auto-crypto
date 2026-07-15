import { describe, expect, it } from "vitest"
import { buildChartGeometry, formatCompact, niceTicks, shortDate } from "./EquityChart"
import { normalizeEquityCurve } from "../lib/api"
import type { EquityPoint } from "../lib/types"

const PAD = { top: 10, right: 10, bottom: 20, left: 40 }

const pt = (date: string, value: number): EquityPoint => ({ date, value })

describe("niceTicks", () => {
  it("covers the range with round steps", () => {
    const ticks = niceTicks(0, 100, 4)
    expect(ticks[0]).toBe(0)
    expect(ticks[ticks.length - 1]).toBe(100)
    const step = ticks[1] - ticks[0]
    for (let i = 1; i < ticks.length; i++) {
      expect(ticks[i] - ticks[i - 1]).toBeCloseTo(step, 8)
    }
    for (const t of ticks) {
      expect(t).toBeGreaterThanOrEqual(0)
      expect(t).toBeLessThanOrEqual(100)
    }
  })

  it("handles fractional ranges without float dust", () => {
    const ticks = niceTicks(0.95, 1.35, 4)
    expect(ticks.length).toBeGreaterThanOrEqual(3)
    for (const t of ticks) {
      expect(t).toBeGreaterThanOrEqual(0.95)
      expect(t).toBeLessThanOrEqual(1.35 + 1e-9)
      // values should be short decimals (nice numbers), not 1.0000000000004
      expect(String(t).length).toBeLessThanOrEqual(6)
    }
  })

  it("degenerate inputs", () => {
    expect(niceTicks(5, 5)).toEqual([5])
    expect(niceTicks(Number.NaN, 10)).toEqual([])
    expect(niceTicks(0, 10, 0)).toEqual([])
  })

  it("swaps inverted bounds", () => {
    const ticks = niceTicks(100, 0, 4)
    expect(ticks[0]).toBe(0)
    expect(ticks[ticks.length - 1]).toBe(100)
  })
})

describe("formatCompact (USDT k/M path — 억/만 제거)", () => {
  it("formats k/M units", () => {
    expect(formatCompact(1_234_567)).toBe("1.2M")
    expect(formatCompact(1_000_000)).toBe("1M")
    expect(formatCompact(15_000)).toBe("15k")
    expect(formatCompact(10_500)).toBe("10.5k")
    expect(formatCompact(1_500)).toBe("1.5k")
  })
  it("formats small numbers", () => {
    expect(formatCompact(250)).toBe("250")
    expect(formatCompact(1.234)).toBe("1.23")
    expect(formatCompact(-15_000)).toBe("-15k")
  })
})

describe("shortDate (24/7 크립토 — 풀 타임스탬프 축 라벨)", () => {
  it("keeps the time when the point has one", () => {
    expect(shortDate("2026-07-14T09:30:00")).toBe("07.14 09:30")
    expect(shortDate("2026-07-14 09:30:00")).toBe("07.14 09:30")
  })
  it("shortens date-only strings like before", () => {
    expect(shortDate("2024-03-15")).toBe("24.03.15")
  })
  it("falls back for non-ISO strings", () => {
    expect(shortDate("day-1")).toBe("day-1")
  })
})

describe("buildChartGeometry", () => {
  it("returns null for empty data or degenerate size", () => {
    expect(buildChartGeometry([], 720, 220, PAD)).toBeNull()
    expect(buildChartGeometry([pt("2024-01-01", 1)], 40, 220, PAD)).toBeNull()
  })

  it("builds a valid line and closed area path", () => {
    const data = [pt("2024-01-01", 1.0), pt("2024-01-02", 1.1), pt("2024-01-03", 0.9)]
    const geom = buildChartGeometry(data, 720, 220, PAD)
    expect(geom).not.toBeNull()
    expect(geom!.points).toHaveLength(3)
    expect(geom!.linePath.startsWith("M")).toBe(true)
    expect(geom!.linePath.split("L")).toHaveLength(3) // M + 2 L segments
    expect(geom!.areaPath.endsWith("Z")).toBe(true)
    expect(geom!.linePath).not.toContain("NaN")
    expect(geom!.areaPath).not.toContain("NaN")
  })

  it("maps higher values to smaller y (SVG axis is inverted)", () => {
    const data = [pt("2024-01-01", 1.0), pt("2024-01-02", 2.0)]
    const geom = buildChartGeometry(data, 720, 220, PAD)!
    expect(geom.points[1].y).toBeLessThan(geom.points[0].y)
  })

  it("keeps points inside the padded plot area", () => {
    const data = Array.from({ length: 50 }, (_, i) => pt(`2024-01-${String((i % 28) + 1).padStart(2, "0")}`, 1 + Math.sin(i / 5) * 0.2))
    const geom = buildChartGeometry(data, 720, 220, PAD)!
    for (const p of geom.points) {
      expect(p.x).toBeGreaterThanOrEqual(PAD.left)
      expect(p.x).toBeLessThanOrEqual(720 - PAD.right)
      expect(p.y).toBeGreaterThanOrEqual(PAD.top)
      expect(p.y).toBeLessThanOrEqual(220 - PAD.bottom)
    }
    expect(geom.points[0].x).toBe(PAD.left)
    expect(geom.points[geom.points.length - 1].x).toBe(720 - PAD.right)
  })

  it("handles flat series without NaN and with usable ticks", () => {
    const data = [pt("2024-01-01", 1.0), pt("2024-01-02", 1.0), pt("2024-01-03", 1.0)]
    const geom = buildChartGeometry(data, 720, 220, PAD)!
    expect(geom.linePath).not.toContain("NaN")
    expect(geom.yTicks.length).toBeGreaterThan(0)
    // all points on one horizontal line
    const ys = new Set(geom.points.map((p) => p.y))
    expect(ys.size).toBe(1)
  })

  it("handles a single point", () => {
    const geom = buildChartGeometry([pt("2024-01-01", 1_000_000)], 720, 220, PAD)!
    expect(geom.points).toHaveLength(1)
    expect(geom.linePath).not.toContain("NaN")
    expect(geom.xTicks).toHaveLength(1)
  })

  it("renders backend-shaped [date, value] pair data once normalized at the API boundary", () => {
    // GET /api/backtests/{id} serves equity_curve as PAIRS (quant.py
    // downsample_equity); api.backtest() must normalize them into objects
    // or the chart geometry degenerates to null ("차트 데이터 없음").
    const wirePairs: [string, number][] = [
      ["2023-01-02", 1.0],
      ["2023-01-03", 1.05],
      ["2023-01-04", 0.97],
    ]
    const geom = buildChartGeometry(normalizeEquityCurve(wirePairs), 720, 220, PAD)
    expect(geom).not.toBeNull()
    expect(geom!.points).toHaveLength(3)
    expect(geom!.linePath).not.toContain("NaN")
    expect(geom!.xTicks[0].label).toBe("23.01.02")
  })

  it("produces x tick labels from the data dates", () => {
    const data = [pt("2024-01-01", 1), pt("2024-06-01", 2), pt("2024-12-31", 3)]
    const geom = buildChartGeometry(data, 720, 220, PAD)!
    expect(geom.xTicks[0].label).toBe("24.01.01")
    expect(geom.xTicks[geom.xTicks.length - 1].label).toBe("24.12.31")
  })
})
