import { describe, expect, it } from "vitest"
import { TF_MS, clockLabel, formatCountdown, nextBarClose, nextJudgment } from "./schedule"
import type { AppConfig } from "./types"

const cfg = (over: Partial<AppConfig> = {}): AppConfig => ({
  trading_mode: "paper",
  universe: ["BTCUSDT"],
  auto_cycle_minutes: 0,
  max_mdd: 0.3,
  min_trades: 10,
  ...over,
})

describe("nextBarClose", () => {
  it("rounds up to the next boundary", () => {
    const now = new Date("2026-07-27T10:07:30Z")
    expect(nextBarClose("15m", now)?.toISOString()).toBe("2026-07-27T10:15:00.000Z")
  })

  it("advances to the following bar when exactly on a boundary", () => {
    // 경계에 정확히 걸렸을 때 '지금'을 돌려주면 카운트다운이 0에 멈춘다.
    const now = new Date("2026-07-27T10:15:00Z")
    expect(nextBarClose("15m", now)?.toISOString()).toBe("2026-07-27T10:30:00.000Z")
  })

  it("aligns 4h and 1d bars to UTC, not local midnight", () => {
    // 로컬 자정 기준으로 계산하면 UTC 오프셋만큼 어긋난다 (KST는 +9시간).
    const now = new Date("2026-07-27T21:30:00Z")
    expect(nextBarClose("4h", now)?.toISOString()).toBe("2026-07-28T00:00:00.000Z")
    expect(nextBarClose("1d", now)?.toISOString()).toBe("2026-07-28T00:00:00.000Z")
  })

  it("returns null for an unknown timeframe", () => {
    expect(nextBarClose("7m", new Date())).toBeNull()
    expect(nextBarClose("", new Date())).toBeNull()
  })

  it("covers every timeframe the backend can be configured with", () => {
    for (const tf of Object.keys(TF_MS)) {
      expect(nextBarClose(tf, new Date("2026-07-27T10:07:30Z"))).not.toBeNull()
    }
  })
})

describe("formatCountdown", () => {
  it("collapses sub-minute and past times to 곧", () => {
    expect(formatCountdown(0)).toBe("곧")
    expect(formatCountdown(-5_000)).toBe("곧")
    expect(formatCountdown(45_000)).toBe("곧")
  })

  it("uses minutes under an hour", () => {
    expect(formatCountdown(12 * 60_000)).toBe("12분 후")
    expect(formatCountdown(59 * 60_000)).toBe("59분 후")
  })

  it("switches to hours past an hour", () => {
    expect(formatCountdown(60 * 60_000)).toBe("1시간 후")
    expect(formatCountdown(95 * 60_000)).toBe("1시간 35분 후")
  })
})

describe("clockLabel", () => {
  it("zero-pads to HH:MM", () => {
    const d = new Date(2026, 6, 27, 9, 5)
    expect(clockLabel(d)).toBe("09:05")
  })
})

describe("nextJudgment", () => {
  const now = new Date("2026-07-27T10:07:30Z")

  it("prefers the bar-close trigger when enabled", () => {
    const r = nextJudgment(
      cfg({ bar_close_trade_enabled: true, execution_timeframe: "15m" }),
      now,
    )
    expect(r.at?.toISOString()).toBe("2026-07-27T10:15:00.000Z")
    expect(r.text).toContain("다음 판단")
    expect(r.text).toContain("15m 봉마감 기준")
  })

  it("falls back to the auto-cycle interval", () => {
    const r = nextJudgment(cfg({ auto_cycle_minutes: 30 }), now)
    expect(r.at).toBeNull()
    expect(r.text).toContain("30분 간격")
  })

  it("does not claim a bar-close schedule when the trigger is off", () => {
    // bar_close_trade_enabled 가 꺼져 있으면 봉마감에 아무 일도 일어나지 않는다.
    const r = nextJudgment(cfg({ execution_timeframe: "15m" }), now)
    expect(r.text).not.toContain("봉마감")
  })

  it("falls through when the timeframe is unknown", () => {
    const r = nextJudgment(
      cfg({ bar_close_trade_enabled: true, execution_timeframe: "7m", auto_cycle_minutes: 30 }),
      now,
    )
    expect(r.text).toContain("30분 간격")
  })

  it("guides the user to the run buttons when nothing is scheduled", () => {
    expect(nextJudgment(cfg(), now).text).toContain("상단 버튼")
    expect(nextJudgment(null, now).text).toContain("상단 버튼")
  })
})
