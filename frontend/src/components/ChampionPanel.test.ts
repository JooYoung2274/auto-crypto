import { describe, expect, it } from "vitest"
import { splitFillStatus, stopTakeProfitLabel } from "./ChampionPanel"
import type { PlanLeg } from "../lib/types"

describe("stopTakeProfitLabel", () => {
  it("formats stop/take-profit with the recovered 3:1 ratio", () => {
    expect(stopTakeProfitLabel(0.05, 0.15)).toBe("손절 -5.0% / 익절 +15.0% (3:1)")
  })

  it("rounds the ratio recovered from take_profit / stop", () => {
    // 0.089 × 3 = 0.267 → ratio round(0.267/0.089) = 3
    expect(stopTakeProfitLabel(0.089, 0.267)).toBe("손절 -8.9% / 익절 +26.7% (3:1)")
  })

  it("returns null when the strategy has no stop", () => {
    expect(stopTakeProfitLabel(null, null)).toBeNull()
    expect(stopTakeProfitLabel(null, 0.2)).toBeNull()
    expect(stopTakeProfitLabel(0.05, null)).toBeNull()
    // coin backend may omit the fields entirely (TradePlan carries the stop)
    expect(stopTakeProfitLabel(undefined, undefined)).toBeNull()
  })

  it("omits the ratio when stop is zero (no division)", () => {
    expect(stopTakeProfitLabel(0, 0)).toBe("손절 -0.0% / 익절 +0.0%")
  })
})

const entry = (fraction: number): PlanLeg => ({ kind: "entry", price: 100, fraction })

describe("splitFillStatus (분할 진입 체결 현황, spec §8)", () => {
  const ladder = { entries: [entry(0.5), entry(0.25), entry(0.25)] }

  it("reports partial ladder fills: 기본 50/25/25에서 0.75 체결 = 2/3", () => {
    expect(splitFillStatus({ ...ladder, filled_fraction: 0.75 })).toBe("3분할 진입 2/3 체결")
  })

  it("reports 0 and full fills", () => {
    expect(splitFillStatus({ ...ladder, filled_fraction: 0 })).toBe("3분할 진입 0/3 체결")
    expect(splitFillStatus({ ...ladder, filled_fraction: 1.0 })).toBe("3분할 진입 3/3 체결")
  })

  it("counts only fully-filled legs (레그 단위 all-or-none)", () => {
    // 0.6 체결: 첫 레그(0.5)만 완결 — 두 번째 레그는 아직
    expect(splitFillStatus({ ...ladder, filled_fraction: 0.6 })).toBe("3분할 진입 1/3 체결")
  })

  it("tolerates float dust at leg boundaries", () => {
    expect(splitFillStatus({ ...ladder, filled_fraction: 0.7499999999 })).toBe("3분할 진입 2/3 체결")
  })

  it("handles a 2-leg ladder", () => {
    const two = { entries: [entry(0.5), entry(0.5)], filled_fraction: 0.5 }
    expect(splitFillStatus(two)).toBe("2분할 진입 1/2 체결")
  })

  it("ignores non-entry legs and returns null without entries", () => {
    const stop: PlanLeg = { kind: "stop", price: 90, fraction: 1 }
    expect(splitFillStatus({ entries: [stop], filled_fraction: 1 })).toBeNull()
    expect(splitFillStatus({ entries: [], filled_fraction: 0 })).toBeNull()
  })
})
