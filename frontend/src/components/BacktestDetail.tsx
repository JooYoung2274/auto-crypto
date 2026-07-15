import { useEffect, useState } from "react"
import { api } from "../lib/api"
import type { BacktestDetail as BacktestDetailData, StrategyDetail, TradeRow } from "../lib/types"
import { fmtParams, pnlClass, sideClass, sideLabel } from "../lib/format"
import { EquityChart } from "./EquityChart"
import { localStamp } from "./ReportView"

interface Props {
  strategyId: number
  onBack: () => void
  /** Label for the back button (defaults to the leaderboard, its original caller). */
  backLabel?: string
}

const fmtPct = (v: number | boolean | null | undefined) =>
  typeof v === "number" ? `${(v * 100).toFixed(1)}%` : "—"
const fmtNum = (v: number | boolean | null | undefined, digits = 2) =>
  typeof v === "number" ? v.toFixed(digits) : "—"
const fmtPrice = (v: number | null | undefined) =>
  typeof v === "number" ? v.toLocaleString("ko-KR", { maximumFractionDigits: 4 }) : "—"

const METRIC_LABELS: [key: string, label: string, kind: "pct" | "num" | "int"][] = [
  ["win_rate", "승률", "pct"],
  ["sharpe", "샤프", "num"],
  ["mdd", "MDD", "pct"],
  ["cagr", "CAGR", "pct"],
  ["profit_factor", "PF", "num"],
  ["trade_count", "거래수", "int"],
  ["avg_holding_hours", "평균 보유(h)", "num"],
  ["funding_paid", "펀딩 지불", "num"],
  ["liquidation_count", "청산 횟수", "int"],
]

/** Strategy drill-down: per-symbol backtests, equity curve, trade list. */
export function BacktestDetail({ strategyId, onBack, backLabel = "리더보드" }: Props) {
  const [strategy, setStrategy] = useState<StrategyDetail | null>(null)
  const [selectedBt, setSelectedBt] = useState<number | null>(null)
  const [detail, setDetail] = useState<BacktestDetailData | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    setStrategy(null)
    setSelectedBt(null)
    setError(null)
    api
      .strategy(strategyId)
      .then((s) => {
        if (!alive) return
        setStrategy(s)
        if (s.backtests.length > 0) setSelectedBt(s.backtests[0].id)
      })
      .catch(() => {
        if (alive) setError("전략 정보를 불러오지 못했습니다")
      })
    return () => {
      alive = false
    }
  }, [strategyId])

  useEffect(() => {
    if (selectedBt === null) return
    let alive = true
    setDetail(null)
    api
      .backtest(selectedBt)
      .then((d) => {
        if (alive) setDetail(d)
      })
      .catch(() => {
        if (alive) setError("백테스트 결과를 불러오지 못했습니다")
      })
    return () => {
      alive = false
    }
  }, [selectedBt])

  return (
    <div className="backtest-detail">
      <div className="detail-header">
        <button type="button" className="btn btn-ghost" onClick={onBack}>
          ← {backLabel}
        </button>
        {strategy && (
          <h3 className="detail-title">
            {strategy.template}
            <span className="detail-params">({fmtParams(strategy.params)})</span>
            {strategy.low_confidence && <span className="badge badge-warn">표본 부족</span>}
            {strategy.status === "champion" && <span className="badge badge-champion">★ 챔피언</span>}
          </h3>
        )}
      </div>
      {error && <div className="panel-notice">{error}</div>}
      {strategy && strategy.backtests.length === 0 && <div className="panel-empty">백테스트 결과가 없습니다</div>}
      {strategy && strategy.backtests.length > 0 && (
        <div className="chip-row">
          {strategy.backtests.map((bt) => (
            <button
              key={bt.id}
              type="button"
              className={`chip ${selectedBt === bt.id ? "chip-active" : ""}`}
              onClick={() => setSelectedBt(bt.id)}
            >
              {bt.symbol}
            </button>
          ))}
        </div>
      )}
      {detail && (
        <>
          <div className="metric-row">
            {METRIC_LABELS.map(([key, label, kind]) => (
              <div key={key} className="metric-tile">
                <span className="metric-label">{label}</span>
                <span className="metric-value">
                  {kind === "pct"
                    ? fmtPct(detail.metrics[key])
                    : kind === "int"
                      ? typeof detail.metrics[key] === "number"
                        ? String(detail.metrics[key])
                        : "—"
                      : fmtNum(detail.metrics[key], key === "avg_holding_hours" ? 1 : 2)}
                </span>
              </div>
            ))}
          </div>
          <EquityChart data={detail.equity_curve} />
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>진입 시각</th>
                  <th>청산 시각</th>
                  <th>방향</th>
                  <th className="num">진입가</th>
                  <th className="num">청산가</th>
                  <th className="num">수익률</th>
                  <th className="num">보유(h)</th>
                  <th className="num">펀딩</th>
                </tr>
              </thead>
              <tbody>
                {detail.trades.length === 0 && (
                  <tr>
                    <td colSpan={8} className="panel-empty">
                      트레이드 없음
                    </td>
                  </tr>
                )}
                {detail.trades.map((t: TradeRow, i) => (
                  <tr key={`${t.entry_ts}-${i}`}>
                    <td>{localStamp(t.entry_ts)}</td>
                    <td>{t.open || !t.exit_ts ? "보유중" : localStamp(t.exit_ts)}</td>
                    <td>
                      <span className={`badge ${sideClass(t.side)}`}>{sideLabel(t.side)}</span>
                      <span className="badge badge-lev">{t.leverage}x</span>
                    </td>
                    <td className="num">{fmtPrice(t.entry_price)}</td>
                    <td className="num">{fmtPrice(t.exit_price)}</td>
                    <td className={`num ${typeof t.net_ret === "number" ? pnlClass(t.net_ret) : ""}`}>
                      {fmtPct(t.net_ret)}
                    </td>
                    <td className="num">{typeof t.holding_hours === "number" ? t.holding_hours.toFixed(1) : "—"}</td>
                    <td className="num">{typeof t.funding_paid === "number" ? t.funding_paid.toFixed(2) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {strategy && selectedBt !== null && !detail && !error && <div className="panel-empty">불러오는 중…</div>}
    </div>
  )
}
