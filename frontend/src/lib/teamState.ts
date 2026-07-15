// Pure reducer tracking each agent's live status (state/detail/meeting) from
// WS events, for the 팀 tab. Kept framework-free so it is unit-testable.

import type { AgentStateValue, WsEvent } from "./types"

export interface TeamAgent {
  state: AgentStateValue
  detail: string
}

export interface TeamMeeting {
  agents: [string, string]
  topic: string
}

export interface TeamState {
  agents: Record<string, TeamAgent>
  /** Active meetings keyed by String(meeting_id). */
  meetings: Record<string, TeamMeeting>
}

export const emptyTeamState: TeamState = { agents: {}, meetings: {} }

/** Reduce a WS event into the team view. Returns the same reference for irrelevant events. */
export function reduceTeamEvent(s: TeamState, e: WsEvent): TeamState {
  switch (e.type) {
    case "snapshot": {
      const agents: Record<string, TeamAgent> = {}
      for (const a of e.agents) agents[a.id] = { state: a.state, detail: a.detail }
      const meetings: Record<string, TeamMeeting> = e.meeting
        ? { [String(e.meeting.id)]: { agents: e.meeting.agents, topic: "" } }
        : {}
      return { agents, meetings }
    }
    case "agent_state": {
      const prev = s.agents[e.agent_id]
      if (prev && prev.state === e.state && prev.detail === e.detail) return s
      return { ...s, agents: { ...s.agents, [e.agent_id]: { state: e.state, detail: e.detail } } }
    }
    case "meeting_start": {
      const key = String(e.meeting_id)
      if (s.meetings[key]) return s
      return { ...s, meetings: { ...s.meetings, [key]: { agents: e.agents, topic: e.topic } } }
    }
    case "meeting_end": {
      const key = String(e.meeting_id)
      if (!s.meetings[key]) return s
      const meetings = { ...s.meetings }
      delete meetings[key]
      return { ...s, meetings }
    }
    default:
      return s
  }
}

/** If the agent is in an active meeting, return its topic and the partner's id. */
export function agentMeeting(s: TeamState, agentId: string): { topic: string; partner: string } | null {
  for (const m of Object.values(s.meetings)) {
    const i = m.agents.indexOf(agentId)
    if (i !== -1) return { topic: m.topic, partner: m.agents[1 - i] }
  }
  return null
}
