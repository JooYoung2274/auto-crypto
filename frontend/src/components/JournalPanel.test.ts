import { describe, expect, it } from "vitest"
import { OUTCOME_BADGE, holdingLabel, price } from "./JournalPanel"
import type { JournalOutcome } from "../lib/types"

describe("holdingLabel", () => {
  it("uses minutes under an hour", () => {
    expect(holdingLabel(0)).toBe("0분")
    expect(holdingLabel(59)).toBe("59분")
  })

  it("drops the trailing 0분 on whole hours", () => {
    // '1시간 0분' 은 사람이 쓰지 않는 표기다.
    expect(holdingLabel(60)).toBe("1시간")
    expect(holdingLabel(120)).toBe("2시간")
  })

  it("keeps the remainder when there is one", () => {
    expect(holdingLabel(153)).toBe("2시간 33분")
  })
})

describe("price", () => {
  it("renders with the decimals the backend chose for the symbol", () => {
    expect(price(1948.44, 2)).toBe("1,948.44")
    expect(price(76.8777, 4)).toBe("76.8777")
  })

  it("keeps a DOGE ladder distinguishable", () => {
    // 백엔드가 6자리를 내려주면 세 레그가 서로 다르게 보여야 한다.
    const rendered = [0.07284053, 0.07288633, 0.07294739].map((p) => price(p, 6))
    expect(new Set(rendered).size).toBe(3)
  })

  it("pads to the requested precision", () => {
    expect(price(75.23, 4)).toBe("75.2300")
  })

  it("renders a dash for missing values", () => {
    // 레거시 플랜은 손절가가 없을 수 있다 — NaN 을 그리면 안 된다.
    expect(price(null, 2)).toBe("–")
    expect(price(undefined, 2)).toBe("–")
  })

  it("does not treat zero as missing", () => {
    expect(price(0, 2)).toBe("0.00")
  })
})

describe("OUTCOME_BADGE", () => {
  it("covers every outcome the backend can emit", () => {
    const outcomes: JournalOutcome[] = ["take_profit", "stop_loss", "liquidation", "closed"]
    for (const o of outcomes) {
      expect(OUTCOME_BADGE[o]).toBeDefined()
      expect(OUTCOME_BADGE[o].label.length).toBeGreaterThan(0)
      expect(OUTCOME_BADGE[o].cls).toMatch(/^journal-/)
    }
  })

  it("gives wins and losses distinct styling", () => {
    expect(OUTCOME_BADGE.take_profit.cls).not.toBe(OUTCOME_BADGE.stop_loss.cls)
  })
})
