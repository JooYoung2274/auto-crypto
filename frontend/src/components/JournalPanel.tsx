import { useCallback, useEffect, useState } from "react"
import { api } from "../lib/api"
import type { JournalEntry, JournalOutcome } from "../lib/types"
import { localStamp } from "./ReportView"

interface Props {
  /** 새 거래가 종결되면 올라가는 카운터 — 재조회 트리거. */
  version?: number
}

/** 결과 배지 — 색과 라벨을 한 곳에서 정한다. */
export const OUTCOME_BADGE: Record<JournalOutcome, { label: string; cls: string }> = {
  take_profit: { label: "익절", cls: "journal-win" },
  stop_loss: { label: "손절", cls: "journal-loss" },
  liquidation: { label: "강제 청산", cls: "journal-liq" },
  closed: { label: "종료", cls: "journal-flat" },
}

/** 보유 시간 표기. 60분 배수에서 '1시간 0분'이 되지 않게 한다. */
export function holdingLabel(minutes: number): string {
  if (minutes < 60) return `${minutes}분`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours}시간 ${rest}분` : `${hours}시간`
}

/** 가격 — 백엔드가 심볼 가격대에 맞춰 내려준 자릿수를 그대로 쓴다. */
export function price(value: number | null | undefined, decimals: number): string {
  if (value === null || value === undefined) return "–"
  return value.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })
}

const pct = (v: number | null | undefined, digits = 2) =>
  v === null || v === undefined ? "–" : `${(v * 100).toFixed(digits)}%`

const usdt = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(2)} USDT`

function NoteEditor({ entry, onSaved }: { entry: JournalEntry; onSaved: (n: string) => void }) {
  const [text, setText] = useState(entry.note)
  const [state, setState] = useState<"idle" | "saving" | "saved" | "error">("idle")

  // 다른 거래 카드로 재사용될 때 편집 중이던 내용이 새지 않게 한다.
  useEffect(() => {
    setText(entry.note)
    setState("idle")
  }, [entry.plan_id, entry.note])

  const save = async () => {
    setState("saving")
    try {
      await api.saveJournalNote(entry.plan_id, text)
      setState("saved")
      onSaved(text)
    } catch {
      setState("error")
    }
  }

  return (
    <div className="journal-note">
      <label className="journal-note-label">내 메모</label>
      <textarea
        className="journal-note-input"
        value={text}
        rows={3}
        placeholder="이 거래에서 배운 것, 다음에 바꿀 것을 적어두세요. 숫자는 위에 이미 정리돼 있습니다."
        onChange={(e) => {
          setText(e.target.value)
          setState("idle")
        }}
      />
      <div className="journal-note-actions">
        {state === "saved" && <span className="journal-note-ok">저장됨</span>}
        {state === "error" && <span className="journal-note-err">저장 실패 — 다시 시도해주세요</span>}
        <button
          type="button"
          className="btn btn-start"
          onClick={save}
          disabled={state === "saving" || text === entry.note}
        >
          {state === "saving" ? "저장 중…" : "저장"}
        </button>
      </div>
    </div>
  )
}

function JournalCard({ entry, onNoteSaved }: { entry: JournalEntry; onNoteSaved: (id: number, n: string) => void }) {
  const d = entry.price_decimals
  const badge = OUTCOME_BADGE[entry.outcome] ?? OUTCOME_BADGE.closed
  const filledLegs = entry.entry_legs.filter((l) => l.filled).length

  return (
    <article className="journal-card">
      <header className="journal-head">
        <span className={`journal-badge ${badge.cls}`}>{badge.label}</span>
        <span className="journal-symbol">{entry.symbol}</span>
        <span className={`pos-side pos-${entry.side}`}>{entry.side === "short" ? "숏" : "롱"}</span>
        <span className="journal-lev">{entry.leverage}배</span>
        <span className="journal-pnl">
          <span className={entry.pnl_usdt >= 0 ? "pos" : "neg"}>{usdt(entry.pnl_usdt)}</span>
          <span className="journal-ret">마진 대비 {pct(entry.ret_on_margin, 1)}</span>
        </span>
      </header>

      <p className="journal-when">
        {localStamp(entry.entry_ts)} 진입 → {localStamp(entry.exit_ts)} 청산 · 보유{" "}
        {holdingLabel(entry.holding_minutes)}
      </p>

      {entry.evidence.length > 0 && (
        <section className="journal-section">
          <h4>왜 들어갔나</h4>
          <ul className="journal-evidence">
            {entry.evidence.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </section>
      )}

      {entry.entry_legs.length > 0 && (
        <section className="journal-section">
          <h4>
            진입 설계 — 분할 매수 <span className="journal-hint">{filledLegs}/{entry.entry_legs.length} 체결</span>
          </h4>
          <table className="data-table journal-table">
            <thead>
              <tr>
                <th>레그</th>
                <th className="num">지정가</th>
                <th className="num">비중</th>
                <th>체결</th>
              </tr>
            </thead>
            <tbody>
              {entry.entry_legs.map((leg) => (
                <tr key={leg.index} className={leg.filled ? "" : "journal-unfilled"}>
                  <td>{leg.index + 1}</td>
                  <td className="num">{price(leg.price, d)}</td>
                  <td className="num">{Math.round(leg.fraction * 100)}%</td>
                  <td>
                    {leg.filled
                      ? `✅ ${leg.fill_ts ? localStamp(leg.fill_ts) : ""} @ ${price(leg.fill_price, d)}`
                      : "❌ 미체결"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="journal-note-line">
            계획 가중 진입가 <strong>{price(entry.planned_weighted_entry, d)}</strong> → 실제 평균{" "}
            <strong>{price(entry.actual_avg_entry, d)}</strong> (수량 {entry.qty.toLocaleString()})
          </p>
        </section>
      )}

      <section className="journal-section">
        <h4>손절 · 익절을 어떻게 잡았나</h4>
        <ul className="journal-evidence">
          <li>
            <strong>손절 {price(entry.stop.price, d)}</strong>
            {entry.stop.distance_pct !== null && ` — 진입가 대비 ${pct(entry.stop.distance_pct)}`}. 시나리오
            붕괴 지점이며, 4h <em>종가</em>가 넘을 때만 판정합니다 (꼬리 사냥 방지).
          </li>
          {entry.tps.length > 0 && (
            <li>
              <strong>
                익절{" "}
                {entry.tps
                  .map((t) => `${price(t.price, d)} (${Math.round(t.fraction * 100)}%)`)
                  .join(" / ")}
              </strong>{" "}
              — 분할 익절. 체결: {entry.tps.filter((t) => t.filled).length}/{entry.tps.length}
            </li>
          )}
          {entry.rr !== null && (
            <li>
              <strong>손익비 1:{entry.rr.toFixed(2)}</strong> — 최소 1:{entry.rr_gate} 게이트를 통과해
              승인됐습니다.
            </li>
          )}
        </ul>
      </section>

      <section className="journal-section">
        <h4>결과</h4>
        <ul className="journal-evidence">
          <li>
            평균 청산 <strong>{price(entry.avg_exit, d)}</strong> · 실현{" "}
            <strong className={entry.pnl_usdt >= 0 ? "pos" : "neg"}>{usdt(entry.pnl_usdt)}</strong>{" "}
            (마진 {entry.margin_usdt.toFixed(0)} 대비 {pct(entry.ret_on_margin, 1)})
          </li>
          <li>
            펀딩 {usdt(-entry.funding_paid)} · 종료 사유 {entry.exit_reason}
          </li>
          {entry.outcome === "take_profit" && entry.tps.every((t) => t.filled) && entry.tps.length > 0 && (
            <li>익절 레그가 계획대로 전부 체결됐습니다 — 시나리오가 그대로 실현된 거래입니다.</li>
          )}
          {entry.outcome === "stop_loss" && (
            <li>손절선이 지켜졌습니다. 계획된 최대 손실 범위 안에서 종료됐습니다.</li>
          )}
        </ul>
      </section>

      <NoteEditor entry={entry} onSaved={(n) => onNoteSaved(entry.plan_id, n)} />
    </article>
  )
}

/** 매매일지 탭 — 종결 거래별 진입 근거·설계·결과 + 메모. */
export function JournalPanel({ version = 0 }: Props) {
  const [entries, setEntries] = useState<JournalEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    api
      .journal()
      .then((rows) => alive && setEntries(rows))
      .catch(() => alive && setError("일지를 불러오지 못했습니다"))
    return () => {
      alive = false
    }
  }, [version])

  const onNoteSaved = useCallback((planId: number, note: string) => {
    setEntries((prev) =>
      prev ? prev.map((e) => (e.plan_id === planId ? { ...e, note } : e)) : prev,
    )
  }, [])

  if (error) return <div className="panel-empty">{error}</div>
  if (entries === null) return <div className="panel-empty">불러오는 중…</div>
  if (entries.length === 0)
    return (
      <div className="panel-empty">
        아직 종결된 거래가 없습니다 — 거래가 익절·손절로 끝나면 여기에 일지가 쌓입니다.
      </div>
    )

  return (
    <div className="journal-panel">
      {entries.map((e) => (
        <JournalCard key={e.plan_id} entry={e} onNoteSaved={onNoteSaved} />
      ))}
    </div>
  )
}
