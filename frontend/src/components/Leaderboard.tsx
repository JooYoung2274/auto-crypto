import { useEffect, useState } from "react"
import { api } from "../lib/api"
import type { LeaderboardEntry } from "../lib/types"
import { fmtParams, sideClass, sideLabel } from "../lib/format"
import { BacktestDetail } from "./BacktestDetail"

interface Props {
  /** Bumped by the parent on `leaderboard_update` WS events to trigger a refetch. */
  version: number
}

const fmtPct = (v: number | null | undefined) => (typeof v === "number" ? `${(v * 100).toFixed(1)}%` : "—")
const fmtNum = (v: number | null | undefined) => (typeof v === "number" ? v.toFixed(2) : "—")

export interface EntryBadge {
  label: string
  className: string
}

/**
 * TF·side·펀딩·청산 뱃지 (spec §8). Pure so it is unit-testable:
 * - TF: entry.timeframe (params.timeframe fallback), e.g. "15m"
 * - side: 롱/숏/양방향
 * - 청산: liquidation_count > 0 → 챔피언 결격 위험 신호
 * - 펀딩: 펀딩 순지불이 0이 아니면 손익 부호로 표시 (양수 지불 = 비용 → 음수 표기)
 */
export function entryBadges(e: LeaderboardEntry): EntryBadge[] {
  const badges: EntryBadge[] = []
  const tf = e.timeframe ?? (typeof e.params.timeframe === "string" ? e.params.timeframe : null)
  if (tf) badges.push({ label: String(tf), className: "badge-tf" })
  const side = e.side ?? (typeof e.params.side === "string" ? (e.params.side as string) : null)
  if (side === "long" || side === "short" || side === "both") {
    badges.push({ label: sideLabel(side), className: side === "both" ? "badge-tf" : sideClass(side) })
  }
  const m = e.avg_metrics
  if (typeof m.liquidation_count === "number" && m.liquidation_count > 0) {
    badges.push({ label: `청산 ${m.liquidation_count}회`, className: "badge-danger" })
  }
  if (typeof m.funding_paid === "number" && m.funding_paid !== 0) {
    const pnl = -m.funding_paid
    badges.push({ label: `펀딩 ${pnl > 0 ? "+" : ""}${pnl.toFixed(1)}`, className: "badge-warn" })
  }
  return badges
}

/** Strategy ranking table; row click drills into per-symbol backtests. */
export function Leaderboard({ version }: Props) {
  const [entries, setEntries] = useState<LeaderboardEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<number | null>(null)

  useEffect(() => {
    let alive = true
    setError(null)
    api
      .leaderboard(20)
      .then((rows) => {
        if (alive) setEntries(rows)
      })
      .catch(() => {
        if (alive) setError("리더보드를 불러오지 못했습니다 (백엔드 연결 확인)")
      })
    return () => {
      alive = false
    }
  }, [version])

  if (selected !== null) {
    return <BacktestDetail strategyId={selected} onBack={() => setSelected(null)} />
  }

  return (
    <div className="leaderboard">
      {error && <div className="panel-notice">{error}</div>}
      {entries !== null && entries.length === 0 && (
        <div className="panel-empty">아직 전략이 없습니다 — 사이클을 시작해 보세요</div>
      )}
      {entries !== null && entries.length > 0 && (
        <div className="table-scroll">
          <table className="data-table row-clickable">
            <thead>
              <tr>
                <th className="num">#</th>
                <th>전략</th>
                <th className="num">승률</th>
                <th className="num">샤프</th>
                <th className="num">MDD</th>
                <th className="num">CAGR</th>
                <th className="num">PF</th>
                <th className="num">거래수</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e, i) => (
                <tr key={e.strategy_id} onClick={() => setSelected(e.strategy_id)}>
                  <td className="num rank">{i + 1}</td>
                  <td>
                    <span className="strategy-name">{e.template}</span>
                    <span className="strategy-params">{fmtParams(e.params)}</span>
                    {e.status === "champion" && <span className="badge badge-champion">★ 챔피언</span>}
                    {e.low_confidence && <span className="badge badge-warn">표본 부족</span>}
                    {e.low_activity && <span className="badge badge-warn">저활동</span>}
                    {entryBadges(e).map((b) => (
                      <span key={b.label} className={`badge ${b.className}`}>
                        {b.label}
                      </span>
                    ))}
                  </td>
                  <td className="num">{fmtPct(e.avg_metrics.win_rate)}</td>
                  <td className="num">{fmtNum(e.avg_metrics.sharpe)}</td>
                  <td className="num neg">{fmtPct(e.avg_metrics.mdd)}</td>
                  <td className={`num ${typeof e.avg_metrics.cagr === "number" ? (e.avg_metrics.cagr >= 0 ? "pos" : "neg") : ""}`}>
                    {fmtPct(e.avg_metrics.cagr)}
                  </td>
                  <td className="num">{fmtNum(e.avg_metrics.profit_factor)}</td>
                  <td className="num">{e.avg_metrics.trade_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {entries === null && !error && <div className="panel-empty">불러오는 중…</div>}
    </div>
  )
}
