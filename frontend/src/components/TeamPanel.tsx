import { useEffect, useState } from "react"
import type { AgentMeta } from "../office/engine"
import type { TeamState } from "../lib/teamState"
import type { AppConfig } from "../lib/types"
import { agentMeeting } from "../lib/teamState"
import { nextJudgment } from "../lib/schedule"

/** What each role does, shown on the card regardless of live state. */
const ROLE_DESCRIPTIONS: Record<string, string> = {
  pm: "사이클 총괄 · 팀 작업 분배와 결과 취합",
  data: "Binance 멀티TF 시세·펀딩비 수집 · 캐싱 · 검증",
  strategist: "레짐·상대강도 기반 전략 후보 생성 — 챔피언 변이 + 랜덤 탐색",
  quant: "후보 전략 × 유니버스 선물 백테스트, 지표 산출",
  risk: "리스크 게이트 — RR·레버리지 캡·청산 버퍼·MDD 차단",
  analyst: "사이클 결과를 마크다운 리포트로 정리",
  trader: "챔피언 TradePlan 분할 진입 래더 발주 · 손절/펀딩/청산 관리",
}

/** 다음 판단 시각 갱신 주기. 분 단위 표기라 20초면 충분하다. */
const TICK_MS = 20_000

interface Props {
  agents: AgentMeta[]
  team: TeamState
  config?: AppConfig | null
}

export function TeamPanel({ agents, team, config = null }: Props) {
  const nameOf = (id: string) => agents.find((a) => a.id === id)?.name ?? id
  const [now, setNow] = useState(() => new Date())

  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), TICK_MS)
    return () => clearInterval(t)
  }, [])

  const busy = agents.some(
    (a) => team.agents[a.id]?.state === "working" || agentMeeting(team, a.id) !== null,
  )
  const schedule = nextJudgment(config, now)

  return (
    <div className="team-panel">
      <div className={`team-schedule ${busy ? "team-schedule-busy" : ""}`}>
        <span className="team-schedule-icon">{busy ? "⚙️" : "⏱"}</span>
        <span>{busy ? "팀이 작업 중입니다" : schedule.text}</span>
      </div>
      <div className="team-grid">
        {agents.map((a) => {
          const live = team.agents[a.id]
          const meeting = agentMeeting(team, a.id)
          const status: "meeting" | "working" | "idle" = meeting
            ? "meeting"
            : live?.state === "working"
              ? "working"
              : "idle"
          const statusLabel =
            status === "meeting" ? "회의 중" : status === "working" ? "작업 중" : "대기"
          // 대기 중이면 활동 줄을 비운다 — '다음 업무 대기 중'을 카드마다 7번
          // 반복하는 대신 위 스트립이 그 정보를 한 번만, 더 정확하게 보여준다.
          const activity = meeting
            ? `${nameOf(meeting.partner)}와(과) 회의${meeting.topic ? ` — ${meeting.topic}` : ""}`
            : live?.state === "working"
              ? live.detail || "작업 처리 중"
              : null
          return (
            <div key={a.id} className={`team-card team-${status}`}>
              <div className="team-card-head">
                <span className="team-avatar" style={{ background: a.color }}>
                  {a.name.slice(0, 1)}
                </span>
                <div className="team-who">
                  <span className="team-name" style={{ color: a.color }}>
                    {a.name}
                  </span>
                  <span className="team-role">{a.role}</span>
                </div>
                <span className={`team-status team-status-${status}`}>{statusLabel}</span>
              </div>
              {activity && <p className="team-activity">{activity}</p>}
              <p className="team-desc">{ROLE_DESCRIPTIONS[a.id] ?? ""}</p>
            </div>
          )
        })}
      </div>
    </div>
  )
}
