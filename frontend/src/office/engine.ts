// Framework-free 2D office engine. A single <canvas> is driven by rAF; WS
// events only set intent (via each character's FSM) and the render loop owns
// all positions — characters never teleport.
//
// Headless-testable: the constructor, dispatch() and step() never touch the
// DOM. Canvas work (sprite baking, background render, rAF) happens in start().

import type { MeetingId, WsEvent } from "../lib/types"
import { CharacterFsm } from "./fsm"
import type { CharState } from "./fsm"
import { bakeAgentSprites, SPRITE_H, SPRITE_W } from "./sprites"
import type { Facing, SpriteSheet } from "./sprites"
import { monitorScreenRect, renderBackground } from "./tilemap"
import {
  MEETING_SEATS,
  routeToMeetingSeat,
  routeToSeat,
  SCALE,
  SEATS,
  stepAlongPath,
  toPx,
  WORLD_H,
  WORLD_W,
} from "./waypoints"
import type { Point } from "./waypoints"

export interface AgentMeta {
  id: string
  name: string
  color: string
  /** Job title shown under the name label (e.g. "전략가"). */
  role?: string
}

/** Read-only view of one character, for tests and debugging. */
export interface CharDebug {
  state: CharState
  pendingReturn: "idle" | "working"
  detail: string
  pos: Point
  hasPath: boolean
}

interface Character {
  meta: AgentMeta
  deskIndex: number
  fsm: CharacterFsm
  pos: Point
  path: Point[] | null
  pathIdx: number
  facing: Facing
  meetingSeat: 0 | 1 | null
  walkTime: number
  sprites: SpriteSheet | null
}

interface ActiveMeeting {
  agents: [string, string]
  topic: string
}

const WALK_SPEED = 56 // unscaled px per second
const MAX_DT_MS = 50 // dt clamp — long tab-away frames must not warp characters

export class OfficeEngine {
  private readonly canvas: HTMLCanvasElement
  private readonly chars = new Map<string, Character>()
  private readonly order: string[] = []
  private readonly meetings = new Map<string, ActiveMeeting>()
  private ctx: CanvasRenderingContext2D | null = null
  private background: HTMLCanvasElement | null = null
  private raf: number | null = null
  private lastTs: number | null = null
  private clock = 0 // seconds, for animations

  constructor(canvas: HTMLCanvasElement, agents: AgentMeta[]) {
    this.canvas = canvas
    agents.forEach((meta, i) => {
      const deskIndex = i % SEATS.length
      this.chars.set(meta.id, {
        meta,
        deskIndex,
        fsm: new CharacterFsm(),
        pos: toPx(SEATS[deskIndex]),
        path: null,
        pathIdx: 1,
        facing: "down",
        meetingSeat: null,
        walkTime: 0,
        sprites: null,
      })
      this.order.push(meta.id)
    })
  }

  // -------------------------------------------------------------- lifecycle

  /** Begin rendering. Safe to call twice (StrictMode) — the second call is a no-op. */
  start(): void {
    if (this.raf !== null) return
    const ctx = this.canvas.getContext("2d")
    if (!ctx) return
    this.ctx = ctx
    this.canvas.width = WORLD_W * SCALE
    this.canvas.height = WORLD_H * SCALE
    if (!this.background) this.background = renderBackground()
    for (const c of this.chars.values()) {
      if (!c.sprites) c.sprites = bakeAgentSprites(c.meta.id, c.meta.color)
    }
    this.lastTs = null
    this.raf = requestAnimationFrame(this.loop)
  }

  /** Stop rendering and release the rAF handle. Safe to call twice. */
  destroy(): void {
    if (this.raf !== null) {
      cancelAnimationFrame(this.raf)
      this.raf = null
    }
    this.lastTs = null
    this.ctx = null
  }

  private loop = (ts: number): void => {
    const dt = this.lastTs === null ? 16 : ts - this.lastTs
    this.lastTs = ts
    this.step(dt)
    this.render()
    this.raf = requestAnimationFrame(this.loop)
  }

  // -------------------------------------------------------------- events

  /**
   * Feed a WS event. Only snapshot / agent_state / meeting_start / meeting_end
   * are consumed; everything else is ignored. Idempotent: replaying the same
   * event is a no-op.
   */
  dispatch(e: WsEvent): void {
    switch (e.type) {
      case "snapshot": {
        for (const a of e.agents) {
          this.applyAgentState(a.id, a.state, a.detail)
        }
        // End meetings the snapshot no longer knows about.
        const liveId = e.meeting ? String(e.meeting.id) : null
        for (const id of [...this.meetings.keys()]) {
          if (id !== liveId) this.endMeeting(id)
        }
        if (e.meeting) this.startMeeting(e.meeting.id, e.meeting.agents, "")
        return
      }
      case "agent_state":
        this.applyAgentState(e.agent_id, e.state, e.detail)
        return
      case "meeting_start":
        this.startMeeting(e.meeting_id, e.agents, e.topic)
        return
      case "meeting_end":
        this.endMeeting(String(e.meeting_id))
        return
      default:
        return // log / cycle_progress / leaderboard_update are not ours
    }
  }

  private applyAgentState(agentId: string, state: "idle" | "working", detail: string): void {
    const c = this.chars.get(agentId)
    if (!c) return
    c.fsm.send({ type: "agent_state", state, detail })
    // IDLE<->WORKING flips only change pose, never position/path.
    if (c.fsm.state === "WORKING") c.facing = "up"
    else if (c.fsm.state === "IDLE") c.facing = "down"
  }

  private startMeeting(meetingId: MeetingId, agents: [string, string], topic: string): void {
    const key = String(meetingId)
    if (this.meetings.has(key)) return // duplicate meeting_start: idempotent
    this.meetings.set(key, { agents, topic })
    agents.forEach((agentId, i) => {
      const c = this.chars.get(agentId)
      if (!c) return
      c.meetingSeat = i === 0 ? 0 : 1
      c.fsm.send({ type: "meeting_start", meetingId })
      if (c.fsm.state === "WALK_TO_MEETING") {
        this.setPath(c, routeToMeetingSeat(c.pos, MEETING_SEATS[c.meetingSeat]))
      }
    })
  }

  private endMeeting(key: string): void {
    const meeting = this.meetings.get(key)
    if (!meeting) return // unknown or already-ended meeting: idempotent
    this.meetings.delete(key)
    for (const agentId of meeting.agents) {
      const c = this.chars.get(agentId)
      if (!c) continue
      const before = c.fsm.state
      c.fsm.send({ type: "meeting_end", meetingId: this.parseMeetingId(key, c) })
      if (c.fsm.state === "WALK_BACK" && before !== "WALK_BACK") {
        c.meetingSeat = null
        this.setPath(c, routeToSeat(c.pos, SEATS[c.deskIndex]))
      }
    }
  }

  /** FSM stores the original MeetingId; recover it so string/number ids both match. */
  private parseMeetingId(key: string, c: Character): MeetingId {
    return c.fsm.meetingId !== null && String(c.fsm.meetingId) === key ? c.fsm.meetingId : key
  }

  private setPath(c: Character, path: Point[]): void {
    c.path = path
    c.pathIdx = 1
    if (path.length <= 1) this.finishWalk(c) // already at the destination
  }

  // -------------------------------------------------------------- simulation

  /** Advance the simulation by dtMs (clamped to 50 ms). Public for headless tests. */
  step(dtMs: number): void {
    const dt = Math.min(dtMs, MAX_DT_MS) / 1000
    this.clock += dt
    for (const c of this.chars.values()) {
      if (!c.path) {
        c.walkTime = 0
        continue
      }
      const before = c.pos
      const { pos, idx, arrived } = stepAlongPath(c.pos, c.path, c.pathIdx, WALK_SPEED * dt)
      c.pos = pos
      c.pathIdx = idx
      c.walkTime += dt
      const dx = pos.x - before.x
      const dy = pos.y - before.y
      if (Math.abs(dx) > Math.abs(dy)) c.facing = dx > 0 ? "right" : "left"
      else if (Math.abs(dy) > 0) c.facing = dy > 0 ? "down" : "up"
      if (arrived) this.finishWalk(c)
    }
  }

  private finishWalk(c: Character): void {
    c.path = null
    c.pathIdx = 1
    c.walkTime = 0
    c.fsm.send({ type: "arrived" })
    switch (c.fsm.state) {
      case "MEETING":
        c.facing = c.meetingSeat === 1 ? "up" : "down" // face each other across the table
        return
      case "WORKING":
        c.facing = "up" // face the monitor
        return
      case "IDLE":
        c.facing = "down"
        return
      case "WALK_BACK":
        // meeting ended exactly on arrival: turn around immediately
        this.setPath(c, routeToSeat(c.pos, SEATS[c.deskIndex]))
        return
      case "WALK_TO_MEETING":
        if (c.meetingSeat !== null) {
          this.setPath(c, routeToMeetingSeat(c.pos, MEETING_SEATS[c.meetingSeat]))
        }
        return
    }
  }

  // -------------------------------------------------------------- inspection

  debug(agentId: string): CharDebug | null {
    const c = this.chars.get(agentId)
    if (!c) return null
    return {
      state: c.fsm.state,
      pendingReturn: c.fsm.pendingReturn,
      detail: c.fsm.detail,
      pos: { ...c.pos },
      hasPath: c.path !== null,
    }
  }

  /** Number of currently tracked meetings (for tests). */
  meetingCount(): number {
    return this.meetings.size
  }

  // -------------------------------------------------------------- rendering

  private render(): void {
    const ctx = this.ctx
    const bg = this.background
    if (!ctx || !bg) return
    ctx.imageSmoothingEnabled = false
    ctx.setTransform(SCALE, 0, 0, SCALE, 0, 0)
    ctx.drawImage(bg, 0, 0)

    this.renderMonitorGlow(ctx)

    // y-sorted painter: lower characters draw over higher ones
    const sorted = this.order
      .map((id) => this.chars.get(id))
      .filter((c): c is Character => c !== undefined)
      .sort((a, b) => a.pos.y - b.pos.y)
    for (const c of sorted) this.renderCharacter(ctx, c)

    // UI pass at device resolution for crisp text
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    for (const c of sorted) this.renderLabel(ctx, c)
    for (const c of sorted) this.renderStateIcon(ctx, c)
    for (const c of sorted) this.renderWorkingBubble(ctx, c)
    this.renderMeetingBubbles(ctx)
  }

  private renderMonitorGlow(ctx: CanvasRenderingContext2D): void {
    for (const c of this.chars.values()) {
      if (c.fsm.state !== "WORKING") continue
      const scr = monitorScreenRect(c.deskIndex)
      const pulse = 0.35 + 0.2 * Math.sin(this.clock * 5 + c.deskIndex)
      ctx.fillStyle = `rgba(120, 200, 255, ${pulse.toFixed(3)})`
      ctx.fillRect(scr.x, scr.y, scr.w, scr.h)
      ctx.fillStyle = "rgba(120, 200, 255, 0.12)"
      ctx.fillRect(scr.x - 2, scr.y - 2, scr.w + 4, scr.h + 4)
    }
  }

  private renderCharacter(ctx: CanvasRenderingContext2D, c: Character): void {
    if (!c.sprites) return
    const frames = c.sprites[c.facing]
    let frame = 0
    if (c.path) {
      frame = Math.floor(c.walkTime / 0.14) % 2 === 0 ? 1 : 2
    }
    // gentle idle/working bob
    let bob = 0
    if (!c.path && (c.fsm.state === "IDLE" || c.fsm.state === "MEETING")) {
      bob = Math.floor(this.clock * 1.6 + c.deskIndex) % 2 === 0 ? 0 : -1
    }
    const x = Math.round(c.pos.x - SPRITE_W / 2)
    const y = Math.round(c.pos.y - SPRITE_H + 2 + bob)
    // soft shadow
    ctx.fillStyle = "rgba(0,0,0,0.28)"
    ctx.fillRect(Math.round(c.pos.x - 4), Math.round(c.pos.y), 8, 2)
    ctx.drawImage(frames[frame], x, y)
  }

  private renderLabel(ctx: CanvasRenderingContext2D, c: Character): void {
    const sx = c.pos.x * SCALE
    const sy = (c.pos.y + 4) * SCALE
    ctx.textAlign = "center"
    ctx.textBaseline = "top"
    ctx.font = "700 11px ui-monospace, monospace"
    const nameW = ctx.measureText(c.meta.name).width
    const role = c.meta.role ?? ""
    ctx.font = "9px ui-monospace, monospace"
    const roleW = role ? ctx.measureText(role).width : 0
    const w = Math.max(nameW, roleW)
    const h = role ? 25 : 14
    ctx.fillStyle = "rgba(10, 8, 18, 0.6)"
    ctx.fillRect(sx - w / 2 - 4, sy - 1, w + 8, h)
    ctx.font = "700 11px ui-monospace, monospace"
    ctx.fillStyle = c.meta.color
    ctx.fillText(c.meta.name, sx, sy)
    if (role) {
      ctx.font = "9px ui-monospace, monospace"
      ctx.fillStyle = "rgba(200, 196, 214, 0.9)"
      ctx.fillText(role, sx, sy + 12)
    }
  }

  private renderStateIcon(ctx: CanvasRenderingContext2D, c: Character): void {
    const sx = c.pos.x * SCALE
    const sy = (c.pos.y - SPRITE_H) * SCALE
    if (c.fsm.state === "IDLE") {
      // drowsy "z"
      ctx.font = "700 12px ui-monospace, monospace"
      ctx.textAlign = "center"
      ctx.textBaseline = "alphabetic"
      ctx.fillStyle = "rgba(200, 200, 220, 0.75)"
      const drift = Math.floor(this.clock * 2 + c.deskIndex) % 3
      ctx.fillText("z", sx + 10 + drift, sy - drift * 2)
    } else if (c.fsm.state === "WORKING") {
      // little yellow spark
      const blink = Math.floor(this.clock * 4 + c.deskIndex) % 2 === 0
      ctx.fillStyle = blink ? "#ffd43b" : "#f5a524"
      ctx.fillRect(sx + 9, sy - 8, 3, 6)
      ctx.fillRect(sx + 6, sy - 4, 6, 3)
    }
  }

  private truncate(text: string, max = 26): string {
    return text.length > max ? `${text.slice(0, max - 1)}…` : text
  }

  private drawBubble(ctx: CanvasRenderingContext2D, sx: number, sy: number, text: string, accent: string): void {
    ctx.font = "11px ui-monospace, monospace"
    ctx.textAlign = "center"
    ctx.textBaseline = "middle"
    const w = ctx.measureText(text).width + 14
    const h = 20
    let bx = sx - w / 2
    bx = Math.max(4, Math.min(bx, this.canvas.width - w - 4))
    const by = Math.max(4, sy - h - 8)
    ctx.fillStyle = "rgba(244, 242, 250, 0.95)"
    ctx.fillRect(bx, by, w, h)
    ctx.fillStyle = accent
    ctx.fillRect(bx, by + h - 2, w, 2)
    // tail
    ctx.fillStyle = "rgba(244, 242, 250, 0.95)"
    ctx.beginPath()
    ctx.moveTo(sx - 4, by + h)
    ctx.lineTo(sx + 4, by + h)
    ctx.lineTo(sx, by + h + 5)
    ctx.closePath()
    ctx.fill()
    ctx.fillStyle = "#17151f"
    ctx.fillText(text, bx + w / 2, by + h / 2)
  }

  private renderWorkingBubble(ctx: CanvasRenderingContext2D, c: Character): void {
    if (c.fsm.state !== "WORKING" || !c.fsm.detail) return
    const sx = c.pos.x * SCALE
    const sy = (c.pos.y - SPRITE_H - 2) * SCALE
    this.drawBubble(ctx, sx, sy, this.truncate(c.fsm.detail), c.meta.color)
  }

  private renderMeetingBubbles(ctx: CanvasRenderingContext2D): void {
    for (const meeting of this.meetings.values()) {
      const [aId, bId] = meeting.agents
      const a = this.chars.get(aId)
      const b = this.chars.get(bId)
      if (!a || !b) continue
      if (a.fsm.state !== "MEETING" || b.fsm.state !== "MEETING") continue
      // alternate the speaker every 1.4 s
      const speaker = Math.floor(this.clock / 1.4) % 2 === 0 ? a : b
      const dots = ".".repeat(1 + (Math.floor(this.clock * 2.5) % 3))
      const text = meeting.topic ? `${this.truncate(meeting.topic, 18)} ${dots}` : dots
      const sx = speaker.pos.x * SCALE
      const sy = (speaker.pos.y - SPRITE_H - 2) * SCALE
      this.drawBubble(ctx, sx, sy, text, speaker.meta.color)
    }
  }
}
