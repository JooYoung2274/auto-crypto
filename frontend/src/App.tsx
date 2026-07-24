import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react"
import "./App.css"
import { ControlBar } from "./components/ControlBar"
import { LogPanel } from "./components/LogPanel"
import { Leaderboard } from "./components/Leaderboard"
import { ChampionPanel } from "./components/ChampionPanel"
import { PendingPanel } from "./components/PendingPanel"
import { ReportView } from "./components/ReportView"
import { PortfolioPanel } from "./components/PortfolioPanel"
import { TeamPanel } from "./components/TeamPanel"
import { emptyTeamState, reduceTeamEvent } from "./lib/teamState"
import type { AgentMeta } from "./office/engine"
import { api, ApiError } from "./lib/api"
import { connectWs, defaultWsUrl } from "./lib/ws"
import type {
  CycleInfo,
  CycleKind,
  CycleProgressEvent,
  GoalStatus,
  LogEntry,
  RegimeValue,
  TradingMode,
  WsEvent,
} from "./lib/types"

/** The seven agents (spec §3); desk order follows this array. */
const AGENTS: AgentMeta[] = [
  { id: "pm", name: "준", role: "PM", color: "#f2555a" },
  { id: "data", name: "다온", role: "데이터", color: "#4cc38a" },
  { id: "strategist", name: "세라", role: "전략가", color: "#f5a524" },
  { id: "quant", name: "민", role: "퀀트", color: "#52a9ff" },
  { id: "risk", name: "로건", role: "리스크", color: "#d6409f" },
  { id: "analyst", name: "하나", role: "애널리스트", color: "#b197fc" },
  { id: "trader", name: "태오", role: "트레이더", color: "#ffd43b" },
]

type TimedEvent = [atMs: number, event: WsEvent]

/** One scripted discovery cycle for ?demo=1 — loops forever, no backend needed. */
function demoScript(loop: number): TimedEvent[] {
  const m = (n: number) => `demo-${loop}-${n}`
  const work = (agent_id: string, detail: string): WsEvent => ({ type: "agent_state", agent_id, state: "working", detail })
  const idle = (agent_id: string): WsEvent => ({ type: "agent_state", agent_id, state: "idle", detail: "" })
  return [
    [500, work("pm", "사이클 시작 — 작업 분배")],
    [1500, { type: "meeting_start", meeting_id: m(1), agents: ["pm", "strategist"], topic: "탐색 방향 결정" }],
    [9500, { type: "meeting_end", meeting_id: m(1) }],
    [10000, work("pm", "진행 상황 모니터링")],
    [10500, work("data", "OHLCV·펀딩비 갱신 (BTCUSDT 15m)")],
    [12000, work("strategist", "레짐 판정 → 후보 전략 60개 생성 중")],
    [15500, { type: "meeting_start", meeting_id: m(2), agents: ["strategist", "quant"], topic: "후보 인계" }],
    [23500, { type: "meeting_end", meeting_id: m(2) }],
    [24000, work("quant", "백테스트 34/60 (ETHUSDT 15m)")],
    [26000, idle("data")],
    [30000, { type: "meeting_start", meeting_id: m(3), agents: ["quant", "risk"], topic: "결과 인계" }],
    [38000, { type: "meeting_end", meeting_id: m(3) }],
    [38500, work("risk", "게이트: RR·레버리지 캡·청산 버퍼·MDD")],
    [41000, idle("quant")],
    [43000, work("analyst", "리포트 작성 중")],
    [44000, idle("risk")],
    [46000, { type: "meeting_start", meeting_id: m(4), agents: ["analyst", "pm"], topic: "사이클 보고" }],
    [54000, { type: "meeting_end", meeting_id: m(4) }],
    [55000, work("trader", "챔피언 플랜 → 분할 진입 래더 발주")],
    [56000, idle("analyst")],
    [56500, idle("strategist")],
    [60000, idle("trader")],
    [60500, idle("pm")],
  ]
}

const DEMO_LOOP_MS = 63_000
const MAX_LIVE_LOGS = 500

/** Synthesize a LogPanel entry from a demo office event so the log tab stays alive offline. */
function demoLogFromEvent(ev: WsEvent, id: number): LogEntry | null {
  const base = { id, ts: new Date().toISOString(), level: "info", data: null }
  if (ev.type === "agent_state" && ev.state === "working") {
    return { ...base, agent: ev.agent_id, event_type: "agent_state", message: ev.detail }
  }
  if (ev.type === "meeting_start") {
    return { ...base, agent: ev.agents[0], event_type: "meeting", message: `회의 시작 — ${ev.topic}` }
  }
  return null
}

const TABS = [
  { id: "team", label: "팀" },
  { id: "logs", label: "로그" },
  { id: "leaderboard", label: "리더보드" },
  { id: "champion", label: "챔피언" },
  { id: "pending", label: "대기 주문" },
  { id: "reports", label: "리포트" },
  { id: "portfolio", label: "포트폴리오" },
] as const

type TabId = (typeof TABS)[number]["id"]

export default function App() {
  const demo = useMemo(() => new URLSearchParams(window.location.search).get("demo") === "1", [])

  const [mode, setMode] = useState<TradingMode | null>(null)
  const [cycle, setCycle] = useState<CycleInfo | null>(null)
  const [progress, setProgress] = useState<CycleProgressEvent | null>(null)
  const [goal, setGoal] = useState<GoalStatus | null>(null)
  const [regime, setRegime] = useState<RegimeValue | null>(null)
  const [liveLogs, setLiveLogs] = useState<LogEntry[]>([])
  const [wsGeneration, setWsGeneration] = useState(0)
  const [lbVersion, setLbVersion] = useState(0)
  const [pfVersion, setPfVersion] = useState(0)
  const [tab, setTab] = useState<TabId>("team")
  const [team, dispatchTeam] = useReducer(reduceTeamEvent, emptyTeamState)
  const [busy, setBusy] = useState(false)
  const [paperOnly, setPaperOnly] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  const appendLog = useCallback((log: LogEntry) => {
    setLiveLogs((prev) => (prev.length >= MAX_LIVE_LOGS ? [...prev.slice(1), log] : [...prev, log]))
  }, [])

  /** Fan a WS event out to the panel states. */
  const handleEvent = useCallback(
    (e: WsEvent) => {
      dispatchTeam(e)
      switch (e.type) {
        case "snapshot":
          setCycle(e.cycle)
          if (!e.cycle) setProgress(null)
          if (e.trading_mode) setMode(e.trading_mode)
          if (e.regime !== undefined) setRegime(e.regime)
          break
        case "log":
          appendLog(e)
          break
        case "cycle_progress":
          setProgress(e)
          setCycle((c) => (c && c.id === e.cycle_id ? { ...c, step: e.step } : c))
          break
        case "leaderboard_update":
          setLbVersion((v) => v + 1)
          break
        case "regime_update":
          setRegime(e.regime)
          break
        // 포지션/체결/펀딩 텔레메트리 → 포트폴리오 탭 리페치 트리거
        case "position_update":
        case "order_filled":
        case "order_cancelled":
        case "funding_payment":
        case "liquidation_warning":
          setPfVersion((v) => v + 1)
          break
        default:
          break
      }
    },
    [appendLog],
  )

  // ?demo=1: replay the scripted cycle in a loop, no backend required.
  // 모의거래 전용 빌드 여부 — 실거래 전환 버튼 숨김 (데스크탑 앱).
  useEffect(() => {
    if (demo) return
    api
      .getConfig()
      .then((c) => setPaperOnly(Boolean((c as { paper_only?: boolean }).paper_only)))
      .catch(() => {
        /* config 로드 실패 시 기본값(false) 유지 */
      })
  }, [demo])

  const demoLogId = useRef(1)
  useEffect(() => {
    if (!demo) return
    setMode("paper")
    let timers: ReturnType<typeof setTimeout>[] = []
    let loop = 0
    const run = () => {
      timers = demoScript(loop).map(([at, ev]) =>
        setTimeout(() => {
          handleEvent(ev)
          const log = demoLogFromEvent(ev, demoLogId.current)
          if (log) {
            demoLogId.current += 1
            appendLog(log)
          }
        }, at),
      )
      timers.push(
        setTimeout(() => {
          loop += 1
          run()
        }, DEMO_LOOP_MS),
      )
    }
    run()
    return () => {
      for (const t of timers) clearTimeout(t)
    }
  }, [demo, appendLog, handleEvent])

  // A reconnected socket has missed every log emitted during the outage; stale
  // liveLogs would leave a silent, unfillable id gap in LogPanel (loadMore only
  // paginates below the global oldest id). Clear them and remount LogPanel via
  // key so it refetches the newest history page and paginates contiguously.
  const handleReconnect = useCallback(() => {
    setLiveLogs([])
    setWsGeneration((g) => g + 1)
  }, [])

  // Live mode: stream backend events into the engine + panels (reconnects with backoff).
  useEffect(() => {
    if (demo) return
    const conn = connectWs(defaultWsUrl(), handleEvent, handleReconnect)
    return () => conn.close()
  }, [demo, handleEvent, handleReconnect])

  // Live mode: keep /api/status fresh (mode badge + cycle state survive WS gaps).
  const refreshStatus = useCallback(async () => {
    try {
      const s = await api.status()
      setMode(s.trading_mode)
      setCycle(s.cycle)
      setGoal(s.goal ?? null)
      if (!s.cycle) setProgress(null)
    } catch {
      // backend unreachable — keep last known state
    }
  }, [])

  // 레짐 칩: 초기 1회 + 10분 폴링 (regime_update WS가 주 갱신 경로).
  useEffect(() => {
    if (demo) return
    let alive = true
    const fetchRegime = async () => {
      try {
        const r = await api.regime()
        if (alive) setRegime(r.regime)
      } catch {
        // 레짐 미산출 — 칩 숨김 유지
      }
    }
    fetchRegime()
    const t = setInterval(fetchRegime, 600_000)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [demo])

  useEffect(() => {
    if (demo) return
    refreshStatus()
    const t = setInterval(refreshStatus, 10_000)
    return () => clearInterval(t)
  }, [demo, refreshStatus])

  // Transient notice auto-dismiss.
  useEffect(() => {
    if (notice === null) return
    const t = setTimeout(() => setNotice(null), 4000)
    return () => clearTimeout(t)
  }, [notice])

  const onStart = useCallback(async (kind: CycleKind) => {
    if (demo) {
      setNotice("데모 모드에서는 사이클을 시작할 수 없습니다")
      return
    }
    setBusy(true)
    try {
      await api.startCycle(kind)
      await refreshStatus()
    } catch (e) {
      setNotice(
        e instanceof ApiError && e.status === 409
          ? "이미 실행 중인 사이클이 있습니다"
          : "사이클 시작 실패 — 백엔드 연결을 확인하세요",
      )
    } finally {
      setBusy(false)
    }
  }, [demo, refreshStatus])

  const onStop = useCallback(async () => {
    if (demo) return
    setBusy(true)
    try {
      await api.stopCycle()
      await refreshStatus()
    } catch {
      setNotice("사이클 중지 실패 — 백엔드 연결을 확인하세요")
    } finally {
      setBusy(false)
    }
  }, [demo, refreshStatus])

  const onGoalStart = useCallback(async () => {
    if (demo) {
      setNotice("데모 모드에서는 목표 탐색을 시작할 수 없습니다")
      return
    }
    setBusy(true)
    try {
      await api.goalStart()
      await refreshStatus()
    } catch (e) {
      setNotice(
        e instanceof ApiError && e.status === 409
          ? "이미 목표 탐색이 실행 중입니다"
          : "목표 탐색 시작 실패 — 백엔드 연결을 확인하세요",
      )
    } finally {
      setBusy(false)
    }
  }, [demo, refreshStatus])

  const onGoalStop = useCallback(async () => {
    if (demo) return
    setBusy(true)
    try {
      await api.goalStop()
      await refreshStatus()
    } catch {
      setNotice("목표 탐색 중지 실패 — 백엔드 연결을 확인하세요")
    } finally {
      setBusy(false)
    }
  }, [demo, refreshStatus])

  // 모드 전환 (spec §5): live는 타이핑 확인 문자열과 함께, 409 = flat-and-idle 거부.
  const onSwitchMode = useCallback(
    async (target: TradingMode, confirm?: string) => {
      if (demo) {
        setNotice("데모 모드에서는 전환할 수 없습니다")
        return
      }
      setBusy(true)
      try {
        await api.setTradingMode(target, confirm)
        await refreshStatus()
        setNotice(target === "live" ? "실거래 모드로 전환되었습니다" : "모의 모드로 전환되었습니다")
      } catch (e) {
        if (e instanceof ApiError) {
          // 400(키 미설정·리컨실 실패 등)은 서버의 detail 메시지를 그대로
          // 보여준다 — 연결 오류로 뭉뚱그리면 원인을 알 수 없다.
          let serverMsg = ""
          try {
            serverMsg = (JSON.parse(e.detail) as { detail?: string }).detail ?? ""
          } catch {
            serverMsg = e.detail
          }
          setNotice(
            e.status === 409
              ? "전환 거부 — 사이클 실행 중이거나 오픈 주문/포지션이 있습니다 (flat-and-idle 필수)"
              : serverMsg || "모드 전환 실패 — 백엔드 연결을 확인하세요",
          )
        } else {
          setNotice("모드 전환 실패 — 백엔드 연결을 확인하세요")
        }
      } finally {
        setBusy(false)
      }
    },
    [demo, refreshStatus],
  )

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-title">
          <h1>Coin Agents Office</h1>
          {demo && <span className="demo-badge">DEMO REPLAY</span>}
        </div>
        <ControlBar
          mode={mode}
          cycle={cycle}
          progress={progress}
          busy={busy}
          goal={goal}
          regime={regime}
          onStart={onStart}
          onStop={onStop}
          onGoalStart={onGoalStart}
          onGoalStop={onGoalStop}
          onSwitchMode={onSwitchMode}
          paperOnly={paperOnly}
        />
      </header>
      {notice && <div className="app-notice">{notice}</div>}
      <section className="panel-area">
        <nav className="tabs">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              className={`tab ${tab === t.id ? "tab-active" : ""}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <div className="tab-body">
          {tab === "team" && <TeamPanel agents={AGENTS} team={team} />}
          {tab === "logs" && <LogPanel key={wsGeneration} liveLogs={liveLogs} agents={AGENTS} offline={demo} />}
          {tab === "leaderboard" && <Leaderboard version={lbVersion} />}
          {tab === "champion" && <ChampionPanel version={lbVersion} />}
          {tab === "pending" && <PendingPanel version={pfVersion} />}
          {tab === "reports" && <ReportView />}
          {tab === "portfolio" && <PortfolioPanel version={pfVersion} />}
        </div>
      </section>
    </div>
  )
}
