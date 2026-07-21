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

describe("cycleKindLabel — 모드 인지형 trade 라벨", () => {
  it("live 모드에서는 trade가 실거래로 표시된다", () => {
    expect(cycleKindLabel("trade", "live")).toBe("실거래")
  })
  it("paper/미지정 모드에서는 기존 모의거래 유지", () => {
    expect(cycleKindLabel("trade", "paper")).toBe("모의거래")
    expect(cycleKindLabel("trade")).toBe("모의거래")
  })
  it("trade 외 kind는 모드와 무관", () => {
    expect(cycleKindLabel("research", "live")).toBe("전략 연구")
  })
})
