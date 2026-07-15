import { useEffect, useMemo, useState } from "react"
import { api } from "../lib/api"
import type { LogEntry } from "../lib/types"

export interface AgentChip {
  id: string
  name: string
  color: string
}

interface Props {
  /** Logs received live over the WebSocket (newest last). */
  liveLogs: LogEntry[]
  agents: AgentChip[]
  /** true in ?demo=1 mode — skip REST fetches, show live logs only. */
  offline?: boolean
}

const PAGE_SIZE = 80

/** Backend log ts is UTC ISO (with offset) — render it in the viewer's local time. */
export function formatTs(ts: string): string {
  const d = new Date(ts)
  if (!Number.isNaN(d.getTime()))
    return d.toLocaleTimeString("ko-KR", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" })
  const t = ts.includes("T") ? ts.split("T")[1] : ts.split(" ")[1] ?? ts
  return (t ?? ts).slice(0, 8)
}

/** Live log stream with history pagination, per-agent filter chips, level colors. */
export function LogPanel({ liveLogs, agents, offline = false }: Props) {
  const [filter, setFilter] = useState<string | null>(null)
  const [older, setOlder] = useState<LogEntry[]>([])
  const [exhausted, setExhausted] = useState(offline)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Initial history page; refetch when the agent filter changes.
  useEffect(() => {
    if (offline) return
    let alive = true
    setLoading(true)
    setError(null)
    setOlder([])
    setExhausted(false)
    api
      .logs({ limit: PAGE_SIZE, agent: filter ?? undefined })
      .then((rows) => {
        if (!alive) return
        setOlder(rows)
        setExhausted(rows.length < PAGE_SIZE)
      })
      .catch(() => {
        if (alive) setError("과거 로그를 불러오지 못했습니다 (백엔드 연결 확인)")
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [filter, offline])

  const rows = useMemo(() => {
    const byId = new Map<number, LogEntry>()
    for (const l of older) byId.set(l.id, l)
    for (const l of liveLogs) byId.set(l.id, l)
    let all = [...byId.values()]
    if (filter) all = all.filter((l) => l.agent === filter)
    all.sort((a, b) => b.id - a.id)
    return all
  }, [older, liveLogs, filter])

  const loadMore = async () => {
    if (loading || exhausted || offline) return
    const oldest = rows.length > 0 ? rows[rows.length - 1].id : undefined
    setLoading(true)
    try {
      const page = await api.logs({ limit: PAGE_SIZE, agent: filter ?? undefined, before_id: oldest })
      setOlder((prev) => [...prev, ...page])
      if (page.length < PAGE_SIZE) setExhausted(true)
    } catch {
      setError("과거 로그를 불러오지 못했습니다")
    } finally {
      setLoading(false)
    }
  }

  const agentMeta = useMemo(() => new Map(agents.map((a) => [a.id, a])), [agents])
  const nameOf = (id: string | null) => (id ? agentMeta.get(id)?.name ?? id : "시스템")
  const colorOf = (id: string | null) => (id ? agentMeta.get(id)?.color ?? "#8f8aa8" : "#8f8aa8")

  return (
    <div className="log-panel">
      <div className="chip-row">
        <button
          type="button"
          className={`chip ${filter === null ? "chip-active" : ""}`}
          onClick={() => setFilter(null)}
        >
          전체
        </button>
        {agents.map((a) => (
          <button
            key={a.id}
            type="button"
            className={`chip ${filter === a.id ? "chip-active" : ""}`}
            style={filter === a.id ? { borderColor: a.color, color: a.color } : undefined}
            onClick={() => setFilter(filter === a.id ? null : a.id)}
          >
            <span className="chip-dot" style={{ background: a.color }} />
            {a.name}
          </button>
        ))}
      </div>
      {error && <div className="panel-notice">{error}</div>}
      <div className="log-list">
        {rows.length === 0 && !loading && <div className="panel-empty">로그가 없습니다</div>}
        {rows.map((log) => (
          <div key={log.id} className={`log-row log-level-${log.level}`}>
            <span className="log-ts">{formatTs(log.ts)}</span>
            <span className="log-agent" style={{ color: colorOf(log.agent) }}>
              {nameOf(log.agent)}
            </span>
            <span className="log-msg">{log.message}</span>
          </div>
        ))}
        {!exhausted && rows.length > 0 && (
          <button type="button" className="btn btn-ghost log-more" onClick={loadMore} disabled={loading}>
            {loading ? "불러오는 중…" : "이전 로그 더 보기"}
          </button>
        )}
      </div>
    </div>
  )
}
