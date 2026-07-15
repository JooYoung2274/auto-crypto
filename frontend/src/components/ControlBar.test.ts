import { describe, expect, it } from "vitest"
import { LIVE_CONFIRM_TEXT, modeSwitchLabel } from "./ControlBar"

describe("mode switch button (spec §5 — 타이핑 확인 모달)", () => {
  it("labels the switch by the CURRENT mode", () => {
    expect(modeSwitchLabel("paper")).toBe("실거래 전환")
    expect(modeSwitchLabel("live")).toBe("모의 전환")
    // unknown/connecting still offers the (gated) live switch label
    expect(modeSwitchLabel(null)).toBe("실거래 전환")
  })

  it("requires typing exactly LIVE for the live confirmation", () => {
    expect(LIVE_CONFIRM_TEXT).toBe("LIVE")
  })
})
