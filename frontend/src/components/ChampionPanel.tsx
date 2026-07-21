import { useEffect, useState } from "react"
import { api } from "../lib/api"
import type { AvgMetrics, ChampionDetail, ChampionReign, ChampionsResponse, PlanInfo } from "../lib/types"
import { fmtParams, sideClass, sideLabel } from "../lib/format"
import { localStamp } from "./ReportView"
import { BacktestDetail } from "./BacktestDetail"

interface Props {
  /** Bumped by the parent on `leaderboard_update` WS events to trigger a refetch. */
  version: number
}

const fmtPct = (v: number | null | undefined) => (typeof v === "number" ? `${(v * 100).toFixed(1)}%` : "—")
const fmtSignedPct = (v: number | null | undefined) =>
  typeof v === "number" ? `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%` : "—"
const fmtNum = (v: number | null | undefined) => (typeof v === "number" ? v.toFixed(2) : "—")

/**
 * The 손절/익절 caption for the champion card, e.g. "손절 -8.9% / 익절 +26.6% (3:1)".
 * take_profit is always derived as stop × rr, so the ratio is recovered from
 * their quotient. Returns null when the strategy has no representative stop.
 */
export function stopTakeProfitLabel(
  stopPct: number | null | undefined,
  takeProfitPct: number | null | undefined,
): string | null {
  if (typeof stopPct !== "number" || typeof takeProfitPct !== "number") return null
  const ratio = stopPct !== 0 ? Math.round(takeProfitPct / stopPct) : null
  const ratioText = ratio !== null ? ` (${ratio}:1)` : ""
  return `손절 -${(stopPct * 100).toFixed(1)}% / 익절 +${(takeProfitPct * 100).toFixed(1)}%${ratioText}`
}

/**
 * 분할 진입 체결 현황, e.g. "3분할 진입 2/3 체결" (spec §8).
 * filled_fraction(0..1)을 진입 레그 fraction 누적합과 비교해 체결된 레그 수를
 * 복원한다 (기본 50/25/25 → 0.75 체결이면 2/3). 진입 레그가 없으면 null.
 */
export function splitFillStatus(plan: Pick<PlanInfo, "entries" | "filled_fraction">): string | null {
  const n = plan.entries.filter((leg) => leg.kind === "entry").length
  if (n === 0) return null
  let cum = 0
  let filled = 0
  for (const leg of plan.entries) {
    if (leg.kind !== "entry") continue
    cum += leg.fraction
    if (plan.filled_fraction >= cum - 1e-9) filled += 1
  }
  return `${n}분할 진입 ${filled}/${n} 체결`
}

const METRICS: [key: keyof AvgMetrics, label: string, kind: "pct" | "num" | "int"][] = [
  ["win_rate", "승률", "pct"],
  ["sharpe", "샤프", "num"],
  ["mdd", "MDD", "pct"],
  ["cagr", "CAGR", "pct"],
  ["trade_count", "거래수", "int"],
]

function MetricRow({ metrics }: { metrics: AvgMetrics }) {
  return (
    <div className="metric-row">
      {METRICS.map(([key, label, kind]) => {
        const v = metrics[key]
        return (
          <div key={key} className="metric-tile">
            <span className="metric-label">{label}</span>
            <span className={`metric-value ${key === "mdd" ? "neg" : ""}`}>
              {kind === "int"
                ? typeof v === "number"
                  ? String(v)
                  : "—"
                : kind === "num"
                  ? fmtNum(v as number | null)
                  : key === "cagr"
                    ? fmtSignedPct(v as number | null)
                    : fmtPct(v as number | null)}
            </span>
          </div>
        )
      })}
    </div>
  )
}

function CurrentChampion({ champ, onDrill }: { champ: ChampionDetail; onDrill: (id: number) => void }) {
  const stopLabel = stopTakeProfitLabel(champ.stop_pct, champ.take_profit_pct)
  const plan = champ.active_plan ?? null
  const fillLabel = plan ? splitFillStatus(plan) : null
  return (
    <div className="champion-card">
      <div className="champion-card-head">
        <span className="badge badge-champion">★ 현재 챔피언</span>
        <span className="strategy-name">{champ.template}</span>
        <span className="strategy-params">{fmtParams(champ.params)}</span>
        {champ.low_confidence && <span className="badge badge-warn">표본 부족</span>}
        {champ.low_activity && <span className="badge badge-warn">저활동</span>}
      </div>
      {stopLabel && <div className="champion-stop">{stopLabel}</div>}
      {plan && fillLabel && (
        <div className="champion-plan">
          <span className={`badge ${sideClass(plan.side)}`}>{sideLabel(plan.side)}</span>
          <span className="strategy-name">{plan.symbol}</span>
          {typeof plan.leverage === "number" && <span className="badge badge-lev">{plan.leverage}x</span>}
          <span className="champion-plan-fill">{fillLabel}</span>
        </div>
      )}
      <MetricRow metrics={champ.avg_metrics} />
      <div className="champion-meta">
        {champ.crowned_at && <span>등극: {localStamp(champ.crowned_at)}</span>}
      </div>
      <div className="champion-exec">💰 매매 사이클 실행 시 이 전략의 TradePlan으로 분할 진입합니다</div>
      {champ.backtests.length > 0 && (
        <div className="table-scroll">
          <table className="data-table row-clickable">
            <thead>
              <tr>
                <th>심볼</th>
                <th className="num">승률</th>
                <th className="num">샤프</th>
                <th className="num">MDD</th>
                <th className="num">거래수</th>
              </tr>
            </thead>
            <tbody>
              {champ.backtests.map((b) => {
                const m = b.metrics
                const asNum = (x: number | boolean | null | undefined) =>
                  typeof x === "number" ? x : null
                return (
                  <tr key={b.id} onClick={() => onDrill(champ.strategy_id)}>
                    <td>{b.symbol}</td>
                    <td className="num">{fmtPct(asNum(m.win_rate))}</td>
                    <td className="num">{fmtNum(asNum(m.sharpe))}</td>
                    <td className="num neg">{fmtPct(asNum(m.mdd))}</td>
                    <td className="num">{typeof m.trade_count === "number" ? m.trade_count : "—"}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function HistoryTable({ reigns }: { reigns: ChampionReign[] }) {
  return (
    <div className="champion-history">
      <h3 className="champion-history-title">역대 챔피언</h3>
      {reigns.length === 0 ? (
        <div className="panel-empty">아직 이전 챔피언이 없습니다</div>
      ) : (
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>전략</th>
                <th>등극</th>
                <th>강등</th>
                <th className="num">승률</th>
                <th className="num">샤프</th>
                <th className="num">MDD</th>
                <th className="num">CAGR</th>
              </tr>
            </thead>
            <tbody>
              {reigns.map((r) => (
                <tr key={`${r.strategy_id}-${r.crowned_at}`}>
                  <td>
                    <span className="strategy-name">{r.template}</span>
                    <span className="strategy-params">{fmtParams(r.params)}</span>
                  </td>
                  <td>{localStamp(r.crowned_at)}</td>
                  <td>{localStamp(r.demoted_at)}</td>
                  <td className="num">{fmtPct(r.avg_metrics.win_rate)}</td>
                  <td className="num">{fmtNum(r.avg_metrics.sharpe)}</td>
                  <td className="num neg">{fmtPct(r.avg_metrics.mdd)}</td>
                  <td
                    className={`num ${
                      typeof r.avg_metrics.cagr === "number" ? (r.avg_metrics.cagr >= 0 ? "pos" : "neg") : ""
                    }`}
                  >
                    {fmtPct(r.avg_metrics.cagr)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

/** 챔피언 탭: 현재 챔피언 카드(모의거래가 실행하는 전략) + 역대 챔피언 이력. */
export function ChampionPanel({ version }: Props) {
  const [data, setData] = useState<ChampionsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<number | null>(null)

  useEffect(() => {
    let alive = true
    setError(null)
    api
      .champions()
      .then((d) => {
        if (alive) setData(d)
      })
      .catch(() => {
        if (alive) setError("챔피언 정보를 불러오지 못했습니다 (백엔드 연결 확인)")
      })
    return () => {
      alive = false
    }
  }, [version])

  if (selected !== null) {
    return <BacktestDetail strategyId={selected} onBack={() => setSelected(null)} backLabel="챔피언" />
  }

  if (error) return <div className="panel-notice">{error}</div>
  if (data === null) return <div className="panel-empty">불러오는 중…</div>
  if (data.current === null && data.history.length === 0) {
    return <div className="panel-empty">아직 챔피언이 없습니다 — 전략 연구를 실행하세요.</div>
  }

  return (
    <div className="champion-panel">
      {data.current && <CurrentChampion champ={data.current} onDrill={setSelected} />}
      <HistoryTable reigns={data.history} />
    </div>
  )
}
