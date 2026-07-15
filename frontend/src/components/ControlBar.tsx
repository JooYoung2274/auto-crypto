import { useEffect, useState } from "react"
import type { CycleInfo, CycleKind, CycleProgressEvent, GoalStatus, RegimeValue, TradingMode } from "../lib/types"
import { cycleKindLabel } from "../lib/cycleKind"
import { regimeLabel } from "../lib/format"

interface Props {
  mode: TradingMode | null
  cycle: CycleInfo | null
  progress: CycleProgressEvent | null
  busy: boolean
  goal: GoalStatus | null
  regime: RegimeValue | null
  onStart: (kind: CycleKind) => void
  onStop: () => void
  onGoalStart: () => void
  onGoalStop: () => void
  /** 모드 전환 요청 — live 전환은 confirm 타이핑 텍스트("LIVE")를 함께 넘긴다. */
  onSwitchMode: (mode: TradingMode, confirm?: string) => void
}

/** live 전환 확인 모달에서 정확히 타이핑해야 하는 문구 (spec §5 confirm:'LIVE'). */
export const LIVE_CONFIRM_TEXT = "LIVE"

/** 전환 버튼 라벨: paper → 실거래 전환, live → 모의 전환. */
export function modeSwitchLabel(mode: TradingMode | null): string {
  return mode === "live" ? "모의 전환" : "실거래 전환"
}

/** The three run kinds offered as start buttons, in display order. */
const RUN_BUTTONS: { kind: CycleKind; label: string }[] = [
  { kind: "research", label: "🔬 전략 연구" },
  { kind: "validate", label: "📊 수익성 검증" },
  { kind: "trade", label: "💰 모의거래 실행" },
]

/** Header control strip: mode badge + 전환 modal, regime chip, goal-seek toggle, run start/stop, live progress. */
export function ControlBar({
  mode,
  cycle,
  progress,
  busy,
  goal,
  regime,
  onStart,
  onStop,
  onGoalStart,
  onGoalStop,
  onSwitchMode,
}: Props) {
  // A run is active whenever a cycle object exists; start buttons are hidden then.
  const active = cycle !== null
  const goalRunning = goal?.running ?? false
  const goalBest = goal && goal.best_win_rate !== null ? `${Math.round(goal.best_win_rate * 100)}%` : null
  const rawPct = active && progress && progress.cycle_id === cycle.id ? progress.pct : null
  // Tolerate either a 0..1 fraction or a 0..100 percentage from the backend.
  const pct = rawPct === null ? null : Math.max(0, Math.min(100, rawPct <= 1 ? rawPct * 100 : rawPct))
  const step = (active && progress && progress.cycle_id === cycle.id ? progress.step : cycle?.step) || "진행 중"
  const kindLabel = cycleKindLabel(cycle?.kind)
  const regimeText = regimeLabel(regime)

  const [modalOpen, setModalOpen] = useState(false)
  const [confirmText, setConfirmText] = useState("")
  const toLive = mode !== "live"

  // Reset the typed confirmation whenever the modal (re)opens.
  useEffect(() => {
    if (modalOpen) setConfirmText("")
  }, [modalOpen])

  const confirmValid = !toLive || confirmText === LIVE_CONFIRM_TEXT
  const submitSwitch = () => {
    if (!confirmValid) return
    setModalOpen(false)
    if (toLive) onSwitchMode("live", confirmText)
    else onSwitchMode("paper")
  }

  return (
    <div className="control-bar">
      <span
        className={`mode-badge mode-${mode ?? "unknown"}`}
        title={mode === "live" ? "실거래 모드" : mode === "paper" ? "모의 투자 모드" : "백엔드 연결 대기"}
      >
        {mode === "live" ? "LIVE 실거래" : mode === "paper" ? "PAPER 모의" : "연결 대기"}
      </span>
      {mode !== null && (
        <button
          type="button"
          className={`btn ${toLive ? "btn-live" : "btn-start"}`}
          onClick={() => setModalOpen(true)}
          disabled={busy}
        >
          {modeSwitchLabel(mode)}
        </button>
      )}
      {regimeText && (
        <span className={`regime-chip regime-${regime}`} title="시장 레짐 판정 (일봉 프록시)">
          {regimeText}
        </span>
      )}
      {goalRunning ? (
        <button type="button" className="btn btn-goal-stop" onClick={onGoalStop} disabled={busy}>
          🎯 목표 탐색 중단
        </button>
      ) : (
        <button type="button" className="btn btn-goal" onClick={onGoalStart} disabled={busy}>
          🎯 목표 탐색
        </button>
      )}
      {!goalRunning &&
        (active ? (
          <button type="button" className="btn btn-stop" onClick={onStop} disabled={busy}>
            ■ 중지
          </button>
        ) : (
          RUN_BUTTONS.map((b) => (
            <button
              key={b.kind}
              type="button"
              className="btn btn-start"
              onClick={() => onStart(b.kind)}
              disabled={busy}
            >
              {b.label}
            </button>
          ))
        ))}
      {goalRunning && goal && (
        <span className="goal-progress" title="목표 탐색 진행 상황">
          목표 탐색 중 — 사이클 {goal.cycles_done}/{goal.max_cycles}
          {goalBest ? ` · 최고 OOS 승률 ${goalBest}` : ""}
        </span>
      )}
      {active && (
        <div className="cycle-progress" title={`사이클 #${cycle.id}`}>
          <span className="cycle-step">
            {kindLabel} 중 — {step}
          </span>
          <div className="progress-track" role="progressbar" aria-valuenow={pct ?? 0} aria-valuemin={0} aria-valuemax={100}>
            <div className="progress-fill" style={{ width: `${pct ?? 4}%` }} />
          </div>
          <span className="progress-pct">{pct !== null ? `${Math.round(pct)}%` : "…"}</span>
        </div>
      )}
      {modalOpen && (
        <div className="modal-backdrop" onClick={() => setModalOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            {toLive ? (
              <>
                <h3 className="modal-title">⚠️ 실거래 전환</h3>
                <p className="modal-body">
                  실제 자금으로 주문이 나갑니다. 전환하려면 <strong>{LIVE_CONFIRM_TEXT}</strong>를 정확히
                  입력하세요. (사이클 실행 중이거나 오픈 주문/포지션이 있으면 거부됩니다)
                </p>
                <input
                  className="modal-input"
                  value={confirmText}
                  onChange={(e) => setConfirmText(e.target.value)}
                  placeholder={LIVE_CONFIRM_TEXT}
                  autoFocus
                />
              </>
            ) : (
              <>
                <h3 className="modal-title">모의 전환</h3>
                <p className="modal-body">모의(paper) 모드로 전환합니다. 오픈 주문/포지션이 없어야 합니다.</p>
              </>
            )}
            <div className="modal-actions">
              <button type="button" className="btn btn-ghost" onClick={() => setModalOpen(false)}>
                취소
              </button>
              <button
                type="button"
                className={`btn ${toLive ? "btn-live" : "btn-start"}`}
                onClick={submitSwitch}
                disabled={!confirmValid || busy}
              >
                {toLive ? "실거래 전환" : "모의 전환"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
