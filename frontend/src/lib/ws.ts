import type { WsEvent } from "./types"

export interface WsConnection {
  close(): void
}

const BASE_DELAY_MS = 500
const MAX_DELAY_MS = 10_000

/**
 * Connect to the backend WebSocket with automatic exponential-backoff
 * reconnection. The server sends a `snapshot` event right after each
 * (re)connect, so consumers recover full state for free.
 *
 * `onReconnect` fires on every successful re-open after the socket has been
 * open at least once before — events published during the outage were never
 * delivered, so consumers must resync anything the snapshot does not carry
 * (e.g. logs).
 */
export function connectWs(url: string, onEvent: (e: WsEvent) => void, onReconnect?: () => void): WsConnection {
  let ws: WebSocket | null = null
  let closed = false
  let attempt = 0
  let hadOpen = false
  let timer: ReturnType<typeof setTimeout> | null = null

  const scheduleReconnect = () => {
    if (closed) return
    const delay = Math.min(MAX_DELAY_MS, BASE_DELAY_MS * 2 ** attempt)
    attempt += 1
    timer = setTimeout(open, delay)
  }

  const open = () => {
    if (closed) return
    try {
      ws = new WebSocket(url)
    } catch {
      scheduleReconnect()
      return
    }
    ws.onopen = () => {
      attempt = 0
      if (hadOpen) onReconnect?.()
      hadOpen = true
    }
    ws.onmessage = (ev: MessageEvent) => {
      let parsed: WsEvent
      try {
        parsed = JSON.parse(String(ev.data)) as WsEvent
      } catch {
        return
      }
      onEvent(parsed)
    }
    ws.onclose = () => {
      ws = null
      scheduleReconnect()
    }
    ws.onerror = () => {
      // onclose follows; nothing to do here.
    }
  }

  open()

  return {
    close() {
      closed = true
      if (timer !== null) clearTimeout(timer)
      if (ws !== null) {
        ws.onclose = null
        ws.close()
        ws = null
      }
    },
  }
}

/** Default WebSocket URL for the current origin (works behind the Vite proxy too). */
export function defaultWsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws"
  return `${proto}://${window.location.host}/ws`
}
