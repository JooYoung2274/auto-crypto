import { describe, expect, it } from "vitest"
import {
  fmtParams,
  fmtSignedUsdt,
  fmtUsdt,
  liqDistancePct,
  pnlClass,
  regimeLabel,
  sideClass,
  sideLabel,
} from "./format"

describe("fmtUsdt / fmtSignedUsdt", () => {
  it("formats USDT amounts with ko-KR grouping (원화 표기 제거)", () => {
    expect(fmtUsdt(10_000)).toBe("10,000 USDT")
    expect(fmtUsdt(1_234.567)).toBe("1,234.57 USDT")
    expect(fmtUsdt(0)).toBe("0 USDT")
  })

  it("adds an explicit + for gains", () => {
    expect(fmtSignedUsdt(12.3)).toBe("+12.3 USDT")
    expect(fmtSignedUsdt(-8.25)).toBe("-8.25 USDT")
    expect(fmtSignedUsdt(0)).toBe("0 USDT")
  })
})

describe("pnlClass (한국 관례: 상승 빨강 / 하락 파랑)", () => {
  it("maps sign to pos/neg/empty", () => {
    expect(pnlClass(1)).toBe("pos")
    expect(pnlClass(-1)).toBe("neg")
    expect(pnlClass(0)).toBe("")
  })
})

describe("sideLabel / sideClass", () => {
  it("labels long/short/both in Korean", () => {
    expect(sideLabel("long")).toBe("롱")
    expect(sideLabel("short")).toBe("숏")
    expect(sideLabel("both")).toBe("양방향")
  })

  it("maps sides to badge classes", () => {
    expect(sideClass("long")).toBe("badge-long")
    expect(sideClass("short")).toBe("badge-short")
  })
})

describe("regimeLabel (spec §8 — 롱장/알트불장/숏장/현금)", () => {
  it("maps the four regimes", () => {
    expect(regimeLabel("long_btc")).toBe("롱장")
    expect(regimeLabel("long_alt")).toBe("알트불장")
    expect(regimeLabel("short")).toBe("숏장")
    expect(regimeLabel("cash")).toBe("현금")
  })

  it("returns null for missing regime (chip hidden)", () => {
    expect(regimeLabel(null)).toBeNull()
    expect(regimeLabel(undefined)).toBeNull()
  })
})

describe("liqDistancePct (청산거리 %)", () => {
  it("computes |mark - liq| / mark for a long", () => {
    // long 10x: 진입 100, 청산 ≈ 90 → 마크 100에서 10% 거리
    expect(liqDistancePct(100, 90)).toBeCloseTo(10, 8)
  })

  it("computes the distance for a short (liq above mark)", () => {
    expect(liqDistancePct(100, 110)).toBeCloseTo(10, 8)
  })

  it("returns null for unusable inputs instead of a misleading 0%", () => {
    expect(liqDistancePct(null, 90)).toBeNull()
    expect(liqDistancePct(100, undefined)).toBeNull()
    expect(liqDistancePct(0, 90)).toBeNull()
    expect(liqDistancePct(100, 0)).toBeNull()
    expect(liqDistancePct(Number.NaN, 90)).toBeNull()
  })
})

describe("fmtParams (중첩 파라미터 허용)", () => {
  it("formats flat params like the stock version", () => {
    expect(fmtParams({ fast: 10, slow: 50 })).toBe("fast=10, slow=50")
  })

  it("renders nested objects and arrays instead of [object Object]", () => {
    expect(fmtParams({ tf: "15m", fracs: [0.5, 0.25, 0.25], legs: { entry: 3, tp: 2 } })).toBe(
      "tf=15m, fracs=[0.5,0.25,0.25], legs={entry:3,tp:2}",
    )
  })

  it("handles null values and empty/missing params", () => {
    expect(fmtParams({ a: null })).toBe("a=null")
    expect(fmtParams({})).toBe("")
    expect(fmtParams(null)).toBe("")
    expect(fmtParams(undefined)).toBe("")
  })
})
