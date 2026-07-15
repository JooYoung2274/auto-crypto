import { describe, expect, it } from "vitest"
import { agentMeeting, emptyTeamState, reduceTeamEvent } from "./teamState"
import type { WsEvent } from "./types"

const snapshot: WsEvent = {
  type: "snapshot",
  agents: [
    { id: "pm", state: "idle", detail: "" },
    { id: "quant", state: "working", detail: "백테스트 3/60" },
  ],
  cycle: null,
  meeting: null,
}

describe("reduceTeamEvent", () => {
  it("populates agents from a snapshot", () => {
    const s = reduceTeamEvent(emptyTeamState, snapshot)
    expect(s.agents.pm).toEqual({ state: "idle", detail: "" })
    expect(s.agents.quant).toEqual({ state: "working", detail: "백테스트 3/60" })
  })

  it("restores an active meeting from a snapshot", () => {
    const s = reduceTeamEvent(emptyTeamState, {
      ...snapshot,
      meeting: { id: 7, agents: ["pm", "quant"] },
    } as WsEvent)
    expect(agentMeeting(s, "pm")).toEqual({ topic: "", partner: "quant" })
  })

  it("updates a single agent on agent_state and is idempotent", () => {
    const s0 = reduceTeamEvent(emptyTeamState, snapshot)
    const ev: WsEvent = { type: "agent_state", agent_id: "pm", state: "working", detail: "분배 중" }
    const s1 = reduceTeamEvent(s0, ev)
    expect(s1.agents.pm).toEqual({ state: "working", detail: "분배 중" })
    expect(s1.agents.quant).toBe(s0.agents.quant)
    expect(reduceTeamEvent(s1, ev)).toBe(s1) // duplicate event: same reference
  })

  it("tracks meeting_start/meeting_end and exposes partner + topic", () => {
    const s0 = reduceTeamEvent(emptyTeamState, snapshot)
    const s1 = reduceTeamEvent(s0, { type: "meeting_start", meeting_id: "m1", agents: ["pm", "quant"], topic: "보고" })
    expect(agentMeeting(s1, "quant")).toEqual({ topic: "보고", partner: "pm" })
    expect(agentMeeting(s1, "risk")).toBeNull()
    const s2 = reduceTeamEvent(s1, { type: "meeting_end", meeting_id: "m1" })
    expect(agentMeeting(s2, "pm")).toBeNull()
    expect(reduceTeamEvent(s2, { type: "meeting_end", meeting_id: "m1" })).toBe(s2) // idempotent
  })

  it("matches string and numeric meeting ids like the office engine does", () => {
    const s1 = reduceTeamEvent(emptyTeamState, { type: "meeting_start", meeting_id: 3, agents: ["pm", "risk"], topic: "" })
    const s2 = reduceTeamEvent(s1, { type: "meeting_end", meeting_id: "3" })
    expect(Object.keys(s2.meetings)).toHaveLength(0)
  })

  it("ignores unrelated events without changing the reference", () => {
    const s0 = reduceTeamEvent(emptyTeamState, snapshot)
    const log: WsEvent = { type: "log", id: 1, ts: "", agent: "pm", level: "info", event_type: "log", message: "x", data: null }
    expect(reduceTeamEvent(s0, log)).toBe(s0)
  })
})
