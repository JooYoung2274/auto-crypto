import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { connectWs } from "./ws"

class FakeWebSocket {
  static instances: FakeWebSocket[] = []
  url: string
  onopen: (() => void) | null = null
  onmessage: ((ev: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: (() => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeWebSocket.instances.push(this)
  }

  close(): void {
    // connectWs detaches onclose before closing; nothing to simulate here.
  }
}

beforeEach(() => {
  FakeWebSocket.instances = []
  vi.useFakeTimers()
  vi.stubGlobal("WebSocket", FakeWebSocket)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe("connectWs reconnect signal", () => {
  it("does not fire onReconnect on the first successful open", () => {
    const onReconnect = vi.fn()
    const conn = connectWs("ws://test/ws", () => {}, onReconnect)

    FakeWebSocket.instances[0].onopen!()

    expect(onReconnect).not.toHaveBeenCalled()
    conn.close()
  })

  it("fires onReconnect when the socket re-opens after a drop, and events still flow", () => {
    const events: unknown[] = []
    const onReconnect = vi.fn()
    const conn = connectWs("ws://test/ws", (e) => events.push(e), onReconnect)

    const first = FakeWebSocket.instances[0]
    first.onopen!()
    // Connection drops mid-session (laptop sleep / backend restart).
    first.onclose!()
    vi.advanceTimersByTime(500) // first backoff delay

    expect(FakeWebSocket.instances).toHaveLength(2)
    const second = FakeWebSocket.instances[1]
    second.onopen!()

    expect(onReconnect).toHaveBeenCalledTimes(1)

    // Live events keep flowing on the new socket.
    second.onmessage!({ data: JSON.stringify({ type: "meeting_end", meeting_id: 1 }) })
    expect(events).toEqual([{ type: "meeting_end", meeting_id: 1 }])
    conn.close()
  })

  it("does not treat failed pre-open attempts as reconnects", () => {
    const onReconnect = vi.fn()
    const conn = connectWs("ws://test/ws", () => {}, onReconnect)

    // First socket never opens (connection refused), then the retry succeeds.
    FakeWebSocket.instances[0].onclose!()
    vi.advanceTimersByTime(500)
    FakeWebSocket.instances[1].onopen!()

    expect(onReconnect).not.toHaveBeenCalled()
    conn.close()
  })

  it("does not reconnect after close()", () => {
    const conn = connectWs("ws://test/ws", () => {}, vi.fn())
    FakeWebSocket.instances[0].onopen!()
    conn.close()
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
  })
})
