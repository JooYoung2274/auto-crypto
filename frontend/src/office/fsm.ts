// Per-character finite state machine. Pure TypeScript (no canvas/DOM) so the
// full transition table is unit-testable.
//
// Server events only set *intent*; the render loop owns positions. `arrived`
// is fed back by the engine when a walking character reaches its destination.

import type { MeetingId } from "../lib/types"

export type CharState = "IDLE" | "WORKING" | "WALK_TO_MEETING" | "MEETING" | "WALK_BACK"

export type FsmEvent =
  | { type: "agent_state"; state: "idle" | "working"; detail?: string }
  | { type: "meeting_start"; meetingId: MeetingId }
  | { type: "meeting_end"; meetingId: MeetingId }
  | { type: "arrived" }

export class CharacterFsm {
  state: CharState = "IDLE"
  /** State to resume once the character walks back to its desk. */
  pendingReturn: "idle" | "working" = "idle"
  /** Latest working detail text (shown while WORKING). */
  detail = ""
  /** Meeting currently walked-to / attended, if any. */
  meetingId: MeetingId | null = null

  send(ev: FsmEvent): void {
    switch (ev.type) {
      case "agent_state":
        this.onAgentState(ev.state, ev.detail ?? "")
        return
      case "meeting_start":
        this.onMeetingStart(ev.meetingId)
        return
      case "meeting_end":
        this.onMeetingEnd(ev.meetingId)
        return
      case "arrived":
        this.onArrived()
        return
    }
  }

  private onAgentState(state: "idle" | "working", detail: string): void {
    this.detail = state === "working" ? detail : ""
    switch (this.state) {
      case "IDLE":
      case "WORKING":
        this.state = state === "working" ? "WORKING" : "IDLE"
        return
      case "WALK_TO_MEETING":
      case "MEETING":
      case "WALK_BACK":
        // Remember what to resume after (or while walking back from) the meeting.
        this.pendingReturn = state
        return
    }
  }

  private onMeetingStart(meetingId: MeetingId): void {
    // Duplicate meeting_start for the meeting we already head to / attend: no-op.
    if (this.meetingId === meetingId && (this.state === "WALK_TO_MEETING" || this.state === "MEETING")) {
      return
    }
    if (this.state === "IDLE" || this.state === "WORKING") {
      this.pendingReturn = this.state === "WORKING" ? "working" : "idle"
    }
    // From WALK_BACK (or a retarget from another meeting) keep the existing
    // pendingReturn — that is still what the agent should resume at its desk.
    this.meetingId = meetingId
    this.state = "WALK_TO_MEETING"
  }

  private onMeetingEnd(meetingId: MeetingId): void {
    // Idempotent: ignore ends for meetings we are not part of (incl. duplicates).
    if (this.meetingId !== meetingId) return
    if (this.state === "MEETING" || this.state === "WALK_TO_MEETING") {
      // meeting_end may beat the walking character to the room: turn around.
      this.state = "WALK_BACK"
    }
    this.meetingId = null
  }

  private onArrived(): void {
    switch (this.state) {
      case "WALK_TO_MEETING":
        this.state = "MEETING"
        return
      case "WALK_BACK":
        this.state = this.pendingReturn === "working" ? "WORKING" : "IDLE"
        return
      default:
        // Arrival notifications are meaningless while not walking: ignore.
        return
    }
  }
}
