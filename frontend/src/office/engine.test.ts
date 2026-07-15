// Headless engine + waypoint tests: no canvas is ever created because
// start() is never called — construction, dispatch() and step() are DOM-free.

import { describe, expect, it } from "vitest"
import type { WsEvent } from "../lib/types"
import { OfficeEngine } from "./engine"
import type { AgentMeta } from "./engine"
import { BACK, FRONT, SIDE, SPRITE_H, SPRITE_W, validateMatrix, walkFrames } from "./sprites"
import {
  MEETING_SEATS,
  routeToMeetingSeat,
  routeToSeat,
  SEATS,
  stepAlongPath,
  toPx,
} from "./waypoints"
import type { Point } from "./waypoints"

const AGENTS: AgentMeta[] = [
  { id: "pm", name: "준", color: "#f2555a" },
  { id: "data", name: "다온", color: "#4cc38a" },
  { id: "strategist", name: "세라", color: "#f5a524" },
  { id: "quant", name: "민", color: "#52a9ff" },
  { id: "risk", name: "로건", color: "#d6409f" },
  { id: "analyst", name: "하나", color: "#b197fc" },
  { id: "trader", name: "태오", color: "#ffd43b" },
]

function makeEngine(): OfficeEngine {
  // start() is never called in tests, so the canvas is never touched.
  return new OfficeEngine({} as HTMLCanvasElement, AGENTS)
}

/** Run the simulation until a predicate holds (or fail after maxSteps). */
function simulate(engine: OfficeEngine, until: () => boolean, maxSteps = 5000): void {
  for (let i = 0; i < maxSteps; i += 1) {
    if (until()) return
    engine.step(50)
  }
  expect.fail(`condition not reached within ${maxSteps} steps`)
}

const meetingStart = (id: string, agents: [string, string]): WsEvent => ({
  type: "meeting_start",
  meeting_id: id,
  agents,
  topic: "인계",
})
const meetingEnd = (id: string): WsEvent => ({ type: "meeting_end", meeting_id: id })
const agentState = (agent_id: string, state: "idle" | "working", detail = ""): WsEvent => ({
  type: "agent_state",
  agent_id,
  state,
  detail,
})

describe("waypoint routing", () => {
  const isManhattan = (path: Point[]) => {
    for (let i = 1; i < path.length; i += 1) {
      const dx = Math.abs(path[i].x - path[i - 1].x)
      const dy = Math.abs(path[i].y - path[i - 1].y)
      expect(dx === 0 || dy === 0).toBe(true)
      expect(dx + dy).toBeGreaterThan(0) // no zero-length segments
    }
  }

  it("desk -> meeting seat routes are Manhattan and end at the seat", () => {
    for (const seat of SEATS) {
      for (const mseat of MEETING_SEATS) {
        const path = routeToMeetingSeat(toPx(seat), mseat)
        expect(path[0]).toEqual(toPx(seat))
        expect(path[path.length - 1]).toEqual(toPx(mseat))
        isManhattan(path)
      }
    }
  })

  it("meeting seat -> desk routes are Manhattan and end at the desk seat", () => {
    for (const mseat of MEETING_SEATS) {
      for (const seat of SEATS) {
        const path = routeToSeat(toPx(mseat), seat)
        expect(path[path.length - 1]).toEqual(toPx(seat))
        isManhattan(path)
      }
    }
  })

  it("routing from a mid-corridor position still reaches the target", () => {
    const midCorridor = { x: 230, y: toPx({ x: 0, y: 10 }).y } // on MAIN_Y between connector and door
    const path = routeToSeat(midCorridor, SEATS[0])
    expect(path[0]).toEqual(midCorridor)
    expect(path[path.length - 1]).toEqual(toPx(SEATS[0]))
    isManhattan(path)
  })

  it("routing to the seat you already occupy is a trivial path", () => {
    const path = routeToSeat(toPx(SEATS[2]), SEATS[2])
    expect(path).toHaveLength(1)
  })
})

describe("stepAlongPath (waypoint arrival)", () => {
  const path: Point[] = [
    { x: 0, y: 0 },
    { x: 10, y: 0 },
    { x: 10, y: 5 },
  ]

  it("advances along a segment without overshooting the waypoint", () => {
    const r = stepAlongPath({ x: 0, y: 0 }, path, 1, 4)
    expect(r.pos).toEqual({ x: 4, y: 0 })
    expect(r.idx).toBe(1)
    expect(r.arrived).toBe(false)
  })

  it("carries leftover distance around corners", () => {
    const r = stepAlongPath({ x: 8, y: 0 }, path, 1, 5)
    expect(r.pos).toEqual({ x: 10, y: 3 }) // 2 px to the corner + 3 px down
    expect(r.idx).toBe(2)
    expect(r.arrived).toBe(false)
  })

  it("lands exactly on the final waypoint and reports arrival", () => {
    const r = stepAlongPath({ x: 10, y: 3 }, path, 2, 999)
    expect(r.pos).toEqual({ x: 10, y: 5 })
    expect(r.arrived).toBe(true)
  })

  it("arrival at exactly-zero remaining distance is detected", () => {
    const r = stepAlongPath({ x: 10, y: 3 }, path, 2, 2)
    expect(r.pos).toEqual({ x: 10, y: 5 })
    expect(r.arrived).toBe(true)
  })
})

describe("sprite matrices", () => {
  it("all facings are valid 12x16 matrices", () => {
    for (const m of [FRONT, BACK, SIDE]) {
      expect(validateMatrix(m)).toHaveLength(SPRITE_H)
      for (const row of m) expect(row).toHaveLength(SPRITE_W)
    }
  })

  it("walk frames lift one leg each", () => {
    const [a, b] = walkFrames(FRONT)
    expect(a[SPRITE_H - 1].slice(2, 5)).toBe("...")
    expect(a[SPRITE_H - 2].slice(2, 5)).toBe("bbb")
    expect(b[SPRITE_H - 1].slice(7, 10)).toBe("...")
    expect(b[SPRITE_H - 2].slice(7, 10)).toBe("bbb")
  })
})

describe("OfficeEngine (headless)", () => {
  it("spawns every agent idle at its desk seat", () => {
    const engine = makeEngine()
    AGENTS.forEach((a, i) => {
      const d = engine.debug(a.id)!
      expect(d.state).toBe("IDLE")
      expect(d.pos).toEqual(toPx(SEATS[i]))
    })
  })

  it("agent_state working flips state without moving the character", () => {
    const engine = makeEngine()
    engine.dispatch(agentState("quant", "working", "백테스트 12/60"))
    const d = engine.debug("quant")!
    expect(d.state).toBe("WORKING")
    expect(d.detail).toBe("백테스트 12/60")
    expect(d.hasPath).toBe(false)
    expect(d.pos).toEqual(toPx(SEATS[3]))
  })

  it("meeting_start walks both participants to the meeting seats", () => {
    const engine = makeEngine()
    engine.dispatch(meetingStart("m1", ["pm", "strategist"]))
    expect(engine.debug("pm")!.state).toBe("WALK_TO_MEETING")
    expect(engine.debug("strategist")!.state).toBe("WALK_TO_MEETING")
    expect(engine.debug("quant")!.state).toBe("IDLE") // bystanders unaffected

    simulate(engine, () => engine.debug("pm")!.state === "MEETING" && engine.debug("strategist")!.state === "MEETING")
    expect(engine.debug("pm")!.pos).toEqual(toPx(MEETING_SEATS[0]))
    expect(engine.debug("strategist")!.pos).toEqual(toPx(MEETING_SEATS[1]))
  })

  it("dispatch is idempotent for duplicated meeting_start / meeting_end / agent_state", () => {
    const engine = makeEngine()
    engine.dispatch(meetingStart("m1", ["pm", "strategist"]))
    simulate(engine, () => engine.debug("pm")!.state === "MEETING")
    expect(engine.meetingCount()).toBe(1)

    engine.dispatch(meetingStart("m1", ["pm", "strategist"])) // duplicate
    expect(engine.meetingCount()).toBe(1)
    expect(engine.debug("pm")!.state).toBe("MEETING")
    expect(engine.debug("pm")!.hasPath).toBe(false)

    engine.dispatch(meetingEnd("m1"))
    engine.dispatch(meetingEnd("m1")) // duplicate
    engine.dispatch(meetingEnd("nope")) // unknown
    expect(engine.meetingCount()).toBe(0)
    expect(engine.debug("pm")!.state).toBe("WALK_BACK")

    engine.dispatch(agentState("data", "working", "x"))
    engine.dispatch(agentState("data", "working", "x"))
    expect(engine.debug("data")!.state).toBe("WORKING")
  })

  it("meeting_end before arrival turns walkers around mid-path", () => {
    const engine = makeEngine()
    engine.dispatch(meetingStart("m1", ["pm", "trader"]))
    for (let i = 0; i < 8; i += 1) engine.step(50) // partway there
    expect(engine.debug("pm")!.state).toBe("WALK_TO_MEETING")

    engine.dispatch(meetingEnd("m1"))
    expect(engine.debug("pm")!.state).toBe("WALK_BACK")
    simulate(engine, () => engine.debug("pm")!.state === "IDLE" && engine.debug("trader")!.state === "IDLE")
    expect(engine.debug("pm")!.pos).toEqual(toPx(SEATS[0]))
    expect(engine.debug("trader")!.pos).toEqual(toPx(SEATS[6]))
  })

  it("working event during WALK_BACK resumes WORKING at the desk", () => {
    const engine = makeEngine()
    engine.dispatch(meetingStart("m1", ["quant", "risk"]))
    simulate(engine, () => engine.debug("quant")!.state === "MEETING")
    engine.dispatch(meetingEnd("m1"))
    expect(engine.debug("quant")!.state).toBe("WALK_BACK")

    engine.dispatch(agentState("quant", "working", "다음 배치 백테스트"))
    expect(engine.debug("quant")!.state).toBe("WALK_BACK") // intent only, no teleport
    expect(engine.debug("quant")!.pendingReturn).toBe("working")

    simulate(engine, () => engine.debug("quant")!.state === "WORKING")
    expect(engine.debug("quant")!.pos).toEqual(toPx(SEATS[3]))
    expect(engine.debug("quant")!.detail).toBe("다음 배치 백테스트")
  })

  it("a WORKING participant returns to WORKING after the meeting", () => {
    const engine = makeEngine()
    engine.dispatch(agentState("strategist", "working", "후보 생성"))
    engine.dispatch(meetingStart("m1", ["strategist", "quant"]))
    expect(engine.debug("strategist")!.pendingReturn).toBe("working")
    simulate(engine, () => engine.debug("strategist")!.state === "MEETING")
    engine.dispatch(meetingEnd("m1"))
    simulate(engine, () => engine.debug("strategist")!.state === "WORKING")
    expect(engine.debug("quant")!.state).toBe("IDLE")
  })

  it("snapshot applies states and an active meeting, idempotently", () => {
    const engine = makeEngine()
    const snapshot: WsEvent = {
      type: "snapshot",
      agents: [
        { id: "pm", state: "working", detail: "사이클 진행" },
        { id: "data", state: "idle", detail: "" },
        { id: "strategist", state: "working", detail: "" },
        { id: "quant", state: "working", detail: "" },
        { id: "risk", state: "idle", detail: "" },
        { id: "analyst", state: "idle", detail: "" },
        { id: "trader", state: "idle", detail: "" },
      ],
      cycle: { id: 3, status: "running", step: "backtest" },
      meeting: { id: "m9", agents: ["strategist", "quant"] },
    }
    engine.dispatch(snapshot)
    engine.dispatch(snapshot) // replay must be harmless
    expect(engine.meetingCount()).toBe(1)
    expect(engine.debug("pm")!.state).toBe("WORKING")
    expect(engine.debug("strategist")!.state).toBe("WALK_TO_MEETING")
    simulate(engine, () => engine.debug("quant")!.state === "MEETING")
    // resync without the meeting ends it
    engine.dispatch({ ...snapshot, meeting: null })
    expect(engine.meetingCount()).toBe(0)
    simulate(engine, () => engine.debug("quant")!.state === "WORKING")
  })

  it("ignores events for unknown agents and non-office events", () => {
    const engine = makeEngine()
    engine.dispatch(agentState("ghost", "working", "?"))
    engine.dispatch({ type: "cycle_progress", cycle_id: 1, step: "backtest", pct: 50 })
    engine.dispatch({ type: "leaderboard_update", top: [] })
    engine.dispatch(meetingStart("m1", ["ghost", "phantom"]))
    engine.step(50)
    for (const a of AGENTS) expect(engine.debug(a.id)!.state).toBe("IDLE")
  })

  it("clamps dt so a huge frame cannot warp a character past its target", () => {
    const engine = makeEngine()
    engine.dispatch(meetingStart("m1", ["pm", "data"]))
    engine.step(60_000) // one giant frame == 50 ms of movement at most (2.8 px)
    const d = engine.debug("pm")!
    expect(d.state).toBe("WALK_TO_MEETING")
    const start = toPx(SEATS[0])
    const moved = Math.abs(d.pos.x - start.x) + Math.abs(d.pos.y - start.y)
    expect(moved).toBeLessThanOrEqual(56 * 0.05 + 1e-9)
  })
})
