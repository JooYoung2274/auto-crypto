import { describe, expect, it } from "vitest"
import {
  CONSENT_KEY,
  DISCLAIMER,
  ONBOARDING_KEY,
  ONBOARDING_STEPS,
  canFinish,
  shouldShowOnboarding,
} from "./Onboarding"

describe("shouldShowOnboarding", () => {
  it("shows on a fresh paper-only install", () => {
    expect(shouldShowOnboarding(true, null)).toBe(true)
  })

  it("stays hidden once dismissed", () => {
    expect(shouldShowOnboarding(true, "done")).toBe(false)
  })

  it("never auto-opens on a live build", () => {
    // 실거래 UI에는 어떤 경우에도 오버레이가 뜨지 않아야 한다.
    expect(shouldShowOnboarding(false, null)).toBe(false)
    expect(shouldShowOnboarding(false, "done")).toBe(false)
  })

  it("treats an unrecognized stored value as not-yet-seen", () => {
    // 키 형식이 바뀐 경우 안내를 건너뛰기보다 다시 보여주는 쪽이 안전하다.
    expect(shouldShowOnboarding(true, "v0-garbage")).toBe(true)
  })
})

describe("ONBOARDING_STEPS", () => {
  it("has three steps with unique titles", () => {
    expect(ONBOARDING_STEPS).toHaveLength(3)
    const titles = ONBOARDING_STEPS.map((s) => s.title)
    expect(new Set(titles).size).toBe(3)
  })

  it("gives every step content and unique bullet keys", () => {
    for (const step of ONBOARDING_STEPS) {
      expect(step.title.length).toBeGreaterThan(0)
      expect(step.lines.length).toBeGreaterThan(0)
      // React key로 line 문자열을 쓰므로 중복이 있으면 안 된다.
      expect(new Set(step.lines).size).toBe(step.lines.length)
    }
  })

  it("states up front that no real money moves", () => {
    const first = ONBOARDING_STEPS[0]
    const text = [first.title, ...first.lines, first.callout ?? ""].join(" ")
    expect(text).toContain("가상")
    expect(text).toMatch(/실제 자금|실제 돈/)
  })

  it("warns that no-trade periods are normal", () => {
    // "고장난 것 같다"는 CS 문의를 막는 문구.
    const all = ONBOARDING_STEPS.map((s) => s.callout ?? "").join(" ")
    expect(all).toContain("정상")
  })

  it("states the research cycle wait time", () => {
    // 20~45분 대기를 미리 알리지 않으면 환불 문의로 이어진다.
    const all = ONBOARDING_STEPS.map((s) => `${s.callout ?? ""} ${s.lines.join(" ")}`).join(" ")
    expect(all).toMatch(/\d+~\d+분/)
  })
})

describe("DISCLAIMER", () => {
  it("disclaims future returns", () => {
    expect(DISCLAIMER).toContain("보장하지 않습니다")
  })
})

describe("ONBOARDING_KEY", () => {
  it("is versioned so copy changes can re-show the guide", () => {
    expect(ONBOARDING_KEY).toMatch(/\.v\d+$/)
  })

  it("does not collide with the consent record", () => {
    expect(CONSENT_KEY).not.toBe(ONBOARDING_KEY)
    expect(CONSENT_KEY).toMatch(/\.v\d+$/)
  })
})

describe("canFinish", () => {
  const lastStep = ONBOARDING_STEPS.length - 1

  it("gates the final button on the disclaimer checkbox", () => {
    expect(canFinish(lastStep, false)).toBe(false)
    expect(canFinish(lastStep, true)).toBe(true)
  })

  it("never blocks the 다음 button on earlier steps", () => {
    for (let i = 0; i < lastStep; i++) {
      expect(canFinish(i, false)).toBe(true)
    }
  })
})
