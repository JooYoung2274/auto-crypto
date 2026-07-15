import { describe, expect, it } from "vitest"
import { formatTs } from "./LogPanel"
import { localStamp } from "./ReportView"

// Pin the test timezone so UTC→local conversion is deterministic. The app's
// users are Korean crypto traders, so Asia/Seoul (UTC+9) is the realistic wall clock.
declare const process: { env: Record<string, string | undefined> }
process.env.TZ = "Asia/Seoul"

describe("formatTs (activity log timestamps)", () => {
  it("converts backend UTC ISO timestamps to local wall time", () => {
    // backend/app/events.py: datetime.now(timezone.utc).isoformat(timespec="seconds")
    // The old slice-based formatter displayed the raw UTC time "04:12:33".
    expect(formatTs("2026-07-13T04:12:33+00:00")).toMatch(/13:12:33/)
  })

  it("handles Z-suffixed UTC timestamps too", () => {
    expect(formatTs("2026-07-13T04:12:33Z")).toMatch(/13:12:33/)
  })

  it("falls back to the raw string for unparsable input", () => {
    expect(formatTs("??")).toBe("??")
  })
})

describe("localStamp (report created_at)", () => {
  it("treats bare SQLite datetime('now') strings as UTC, not local", () => {
    // reports.created_at defaults to SQLite datetime('now') — UTC with NO tz
    // suffix. It must render identically to the same instant tagged +00:00.
    expect(localStamp("2026-07-13 04:12:33")).toBe(localStamp("2026-07-13T04:12:33+00:00"))
  })

  it("renders local (Seoul) date and time, rolling the date past midnight", () => {
    // 2026-07-13 16:30 UTC is 2026-07-14 01:30 KST — the displayed *date*
    // must be the 14th, which the old raw slice got wrong.
    const out = localStamp("2026-07-13 16:30:00")
    expect(out).toMatch(/0?1:30/)
    expect(out).toContain("14")
    expect(out).not.toContain("16:30")
  })

  it("falls back to a trimmed raw string for unparsable input", () => {
    expect(localStamp("not-a-timestamp")).toBe("not-a-timestamp")
  })
})
