import type { AgentMeta } from "../office/engine"
import type { TeamState } from "../lib/teamState"
import { agentMeeting } from "../lib/teamState"

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

interface Props {
  agents: AgentMeta[]
  team: TeamState
}

export function TeamPanel({ agents, team }: Props) {
  const nameOf = (id: string) => agents.find((a) => a.id === id)?.name ?? id
  return (
    <div className="team-grid">
      {agents.map((a) => {
        const live = team.agents[a.id]
        const meeting = agentMeeting(team, a.id)
        const status: "meeting" | "working" | "idle" = meeting ? "meeting" : live?.state === "working" ? "working" : "idle"
        const statusLabel = status === "meeting" ? "회의 중" : status === "working" ? "작업 중" : "대기"
        const activity = meeting
          ? `${nameOf(meeting.partner)}와(과) 회의${meeting.topic ? ` — ${meeting.topic}` : ""}`
          : live?.state === "working" && live.detail
            ? live.detail
            : live?.state === "working"
              ? "작업 처리 중"
              : "다음 업무 대기 중"
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
            <p className="team-activity">{activity}</p>
            <p className="team-desc">{ROLE_DESCRIPTIONS[a.id] ?? ""}</p>
          </div>
        )
      })}
    </div>
  )
}
