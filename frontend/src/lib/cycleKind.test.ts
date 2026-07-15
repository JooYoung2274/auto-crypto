import { describe, expect, it } from "vitest"
import { cycleKindLabel, reportKindLabel } from "./cycleKind"

describe("cycleKindLabel", () => {
  it("maps each run kind to its Korean label", () => {
    expect(cycleKindLabel("research")).toBe("전략 연구")
    expect(cycleKindLabel("validate")).toBe("수익성 검증")
    expect(cycleKindLabel("trade")).toBe("모의거래")
  })

  it("falls back to 전략 연구 for missing/unknown kinds", () => {
    expect(cycleKindLabel(undefined)).toBe("전략 연구")
    expect(cycleKindLabel(null)).toBe("전략 연구")
  })
})

describe("reportKindLabel", () => {
  it("labels research and validation reports", () => {
    expect(reportKindLabel("research")).toBe("연구")
    expect(reportKindLabel("validation")).toBe("검증")
  })

  it("falls back to 연구 for missing kinds (pre-split rows)", () => {
    expect(reportKindLabel(undefined)).toBe("연구")
    expect(reportKindLabel(null)).toBe("연구")
  })
})
