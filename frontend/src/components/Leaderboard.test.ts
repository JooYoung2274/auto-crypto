import { describe, expect, it } from "vitest"
import { entryBadges } from "./Leaderboard"
import type { LeaderboardEntry } from "../lib/types"

const base: LeaderboardEntry = {
  strategy_id: 1,
  template: "topdown_pullback",
  params: { fast: 10, slow: 50, leverage: 5 },
  avg_metrics: {
    win_rate: 0.6,
    sharpe: 1.2,
    mdd: 0.1,
    cagr: 0.3,
    profit_factor: 1.8,
    trade_count: 24,
  },
  low_confidence: false,
  low_activity: false,
  status: "candidate",
}

describe("entryBadges (spec §8 — TF·side·펀딩·청산 뱃지)", () => {
  it("shows the timeframe badge from the top-level field", () => {
    const badges = entryBadges({ ...base, timeframe: "15m" })
    expect(badges).toContainEqual({ label: "15m", className: "badge-tf" })
  })

  it("falls back to params.timeframe when the field is absent", () => {
    const badges = entryBadges({ ...base, params: { ...base.params, timeframe: "4h" } })
    expect(badges).toContainEqual({ label: "4h", className: "badge-tf" })
  })

  it("shows the side badge (롱 = red-class, 숏 = blue-class)", () => {
    expect(entryBadges({ ...base, side: "long" })).toContainEqual({ label: "롱", className: "badge-long" })
    expect(entryBadges({ ...base, side: "short" })).toContainEqual({ label: "숏", className: "badge-short" })
  })

  it("flags liquidations as a danger badge with the count", () => {
    const badges = entryBadges({
      ...base,
      avg_metrics: { ...base.avg_metrics, liquidation_count: 2 },
    })
    expect(badges).toContainEqual({ label: "청산 2회", className: "badge-danger" })
  })

  it("shows funding drag as a signed PnL badge (지불 양수 → 음수 표기)", () => {
    const badges = entryBadges({
      ...base,
      avg_metrics: { ...base.avg_metrics, funding_paid: 12.34 },
    })
    expect(badges).toContainEqual({ label: "펀딩 -12.3", className: "badge-warn" })
  })

  it("shows received funding with a plus sign", () => {
    const badges = entryBadges({
      ...base,
      avg_metrics: { ...base.avg_metrics, funding_paid: -5.0 },
    })
    expect(badges).toContainEqual({ label: "펀딩 +5.0", className: "badge-warn" })
  })

  it("emits nothing extra for a plain entry (no TF/side/funding/liq data)", () => {
    expect(entryBadges(base)).toEqual([])
    // liquidation_count 0 / funding_paid 0 → 뱃지 없음
    expect(
      entryBadges({ ...base, avg_metrics: { ...base.avg_metrics, liquidation_count: 0, funding_paid: 0 } }),
    ).toEqual([])
  })
})
