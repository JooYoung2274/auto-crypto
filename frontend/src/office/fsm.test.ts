import { describe, expect, it } from "vitest"
import { CharacterFsm } from "./fsm"

const working = (detail = "백테스트 중") => ({ type: "agent_state", state: "working", detail }) as const
const idle = () => ({ type: "agent_state", state: "idle" }) as const
const start = (meetingId: string | number = "m1") => ({ type: "meeting_start", meetingId }) as const
const end = (meetingId: string | number = "m1") => ({ type: "meeting_end", meetingId }) as const
const arrived = () => ({ type: "arrived" }) as const

describe("CharacterFsm basic transitions", () => {
  it("starts IDLE with idle pendingReturn", () => {
    const fsm = new CharacterFsm()
    expect(fsm.state).toBe("IDLE")
    expect(fsm.pendingReturn).toBe("idle")
  })

  it("IDLE -> WORKING on working agent_state, storing detail", () => {
    const fsm = new CharacterFsm()
    fsm.send(working("삼성전자 백테스트 중 (34/60)"))
    expect(fsm.state).toBe("WORKING")
    expect(fsm.detail).toBe("삼성전자 백테스트 중 (34/60)")
  })

  it("WORKING -> IDLE on idle agent_state, clearing detail", () => {
    const fsm = new CharacterFsm()
    fsm.send(working())
    fsm.send(idle())
    expect(fsm.state).toBe("IDLE")
    expect(fsm.detail).toBe("")
  })

  it("meeting_start from IDLE walks to meeting with pendingReturn=idle", () => {
    const fsm = new CharacterFsm()
    fsm.send(start())
    expect(fsm.state).toBe("WALK_TO_MEETING")
    expect(fsm.pendingReturn).toBe("idle")
    expect(fsm.meetingId).toBe("m1")
  })

  it("meeting_start from WORKING walks to meeting with pendingReturn=working", () => {
    const fsm = new CharacterFsm()
    fsm.send(working())
    fsm.send(start())
    expect(fsm.state).toBe("WALK_TO_MEETING")
    expect(fsm.pendingReturn).toBe("working")
  })

  it("arrival while WALK_TO_MEETING enters MEETING", () => {
    const fsm = new CharacterFsm()
    fsm.send(start())
    fsm.send(arrived())
    expect(fsm.state).toBe("MEETING")
  })

  it("meeting_end while MEETING walks back; arrival resumes pendingReturn=idle", () => {
    const fsm = new CharacterFsm()
    fsm.send(start())
    fsm.send(arrived())
    fsm.send(end())
    expect(fsm.state).toBe("WALK_BACK")
    expect(fsm.meetingId).toBeNull()
    fsm.send(arrived())
    expect(fsm.state).toBe("IDLE")
  })

  it("full round trip resumes WORKING when the agent was working", () => {
    const fsm = new CharacterFsm()
    fsm.send(working())
    fsm.send(start())
    fsm.send(arrived())
    fsm.send(end())
    fsm.send(arrived())
    expect(fsm.state).toBe("WORKING")
  })
})

describe("CharacterFsm edge cases", () => {
  it("meeting_end arriving BEFORE the character reaches the room turns it around", () => {
    const fsm = new CharacterFsm()
    fsm.send(start())
    expect(fsm.state).toBe("WALK_TO_MEETING")
    fsm.send(end()) // meeting over before arrival
    expect(fsm.state).toBe("WALK_BACK")
    fsm.send(arrived())
    expect(fsm.state).toBe("IDLE")
  })

  it("duplicate meeting_start for the same meeting is a no-op", () => {
    const fsm = new CharacterFsm()
    fsm.send(working())
    fsm.send(start())
    const pending = fsm.pendingReturn
    fsm.send(start()) // duplicate while walking
    expect(fsm.state).toBe("WALK_TO_MEETING")
    expect(fsm.pendingReturn).toBe(pending)
    fsm.send(arrived())
    fsm.send(start()) // duplicate while in the meeting
    expect(fsm.state).toBe("MEETING")
  })

  it("duplicate / unknown meeting_end is a no-op", () => {
    const fsm = new CharacterFsm()
    fsm.send(start())
    fsm.send(arrived())
    fsm.send(end("other-meeting")) // not our meeting
    expect(fsm.state).toBe("MEETING")
    fsm.send(end())
    expect(fsm.state).toBe("WALK_BACK")
    fsm.send(end()) // duplicate end after we already left
    expect(fsm.state).toBe("WALK_BACK")
  })

  it("duplicate agent_state events are idempotent", () => {
    const fsm = new CharacterFsm()
    fsm.send(working("a"))
    fsm.send(working("b"))
    expect(fsm.state).toBe("WORKING")
    expect(fsm.detail).toBe("b")
    fsm.send(idle())
    fsm.send(idle())
    expect(fsm.state).toBe("IDLE")
  })

  it("working event during WALK_BACK sets pendingReturn and resumes WORKING on arrival", () => {
    const fsm = new CharacterFsm()
    fsm.send(start())
    fsm.send(arrived())
    fsm.send(end())
    expect(fsm.state).toBe("WALK_BACK")
    fsm.send(working("새 작업"))
    expect(fsm.state).toBe("WALK_BACK") // no teleport into WORKING mid-walk
    expect(fsm.pendingReturn).toBe("working")
    fsm.send(arrived())
    expect(fsm.state).toBe("WORKING")
    expect(fsm.detail).toBe("새 작업")
  })

  it("working then idle during MEETING resolves to the LAST intent", () => {
    const fsm = new CharacterFsm()
    fsm.send(start())
    fsm.send(arrived())
    fsm.send(working())
    expect(fsm.state).toBe("MEETING")
    expect(fsm.pendingReturn).toBe("working")
    fsm.send(idle())
    expect(fsm.pendingReturn).toBe("idle")
    fsm.send(end())
    fsm.send(arrived())
    expect(fsm.state).toBe("IDLE")
  })

  it("meeting_start during WALK_BACK re-routes to the new meeting keeping pendingReturn", () => {
    const fsm = new CharacterFsm()
    fsm.send(working())
    fsm.send(start("m1"))
    fsm.send(arrived())
    fsm.send(end("m1"))
    expect(fsm.state).toBe("WALK_BACK")
    fsm.send(start("m2"))
    expect(fsm.state).toBe("WALK_TO_MEETING")
    expect(fsm.meetingId).toBe("m2")
    expect(fsm.pendingReturn).toBe("working")
    fsm.send(arrived())
    fsm.send(end("m2"))
    fsm.send(arrived())
    expect(fsm.state).toBe("WORKING")
  })

  it("a new meeting_start while already MEETING retargets to the new meeting", () => {
    const fsm = new CharacterFsm()
    fsm.send(start("m1"))
    fsm.send(arrived())
    fsm.send(start("m2"))
    expect(fsm.state).toBe("WALK_TO_MEETING")
    expect(fsm.meetingId).toBe("m2")
    fsm.send(end("m1")) // stale end for the abandoned meeting: ignored
    expect(fsm.state).toBe("WALK_TO_MEETING")
  })

  it("arrived while not walking is ignored", () => {
    const fsm = new CharacterFsm()
    fsm.send(arrived())
    expect(fsm.state).toBe("IDLE")
    fsm.send(working())
    fsm.send(arrived())
    expect(fsm.state).toBe("WORKING")
  })

  it("numeric meeting ids work end to end", () => {
    const fsm = new CharacterFsm()
    fsm.send(start(7))
    fsm.send(arrived())
    expect(fsm.meetingId).toBe(7)
    fsm.send(end(7))
    expect(fsm.state).toBe("WALK_BACK")
  })
})
