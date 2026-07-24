import { useEffect, useState } from "react"
import { api } from "../lib/api"
import type { EquityPoint, PortfolioResponse, PortfolioSnapshot, TradeHistoryRow } from "../lib/types"
import { fmtSignedUsdt, fmtUsdt, liqDistancePct, pnlClass, sideClass, sideLabel } from "../lib/format"
import { EquityChart, shortDate } from "./EquityChart"

/** 종결 사유 → 뱃지 클래스 (익절 = 수익색, 손절/강제 청산 = 손실색). */
export function exitReasonClass(reason: string): string {
  if (reason === "익절") return "badge-long"
  if (reason === "강제 청산") return "badge-liq"
  return "badge-short"
}

/** Map backend snapshot rows ({ts, wallet_balance, …, total_value}) to chart points. */
export function snapshotsToChartData(snapshots: PortfolioSnapshot[]): EquityPoint[] {
  return snapshots.map((s) => ({ date: s.ts, value: s.total_value }))
}

const fmtPrice = (v: number | null | undefined) =>
  typeof v === "number" ? v.toLocaleString("ko-KR", { maximumFractionDigits: 4 }) : "—"

interface Props {
  /** Bumped by the parent on position/funding WS events to trigger a refetch. */
  version?: number
}

/** 선물 포트폴리오: 지갑 타일(총 자산/사용 가능/포지션 마진/미실현 손익) + 포지션 테이블. */
export function PortfolioPanel({ version = 0 }: Props) {
  const [pf, setPf] = useState<PortfolioResponse | null>(null)
  const [history, setHistory] = useState<TradeHistoryRow[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    api
      .portfolio()
      .then((p) => {
        if (alive) setPf(p)
      })
      .catch(() => {
        if (alive) setError("포트폴리오를 불러오지 못했습니다 (백엔드 연결 확인)")
      })
    api
      .tradeHistory()
      .then((rows) => {
        if (alive) setHistory(rows)
      })
      .catch(() => {
        // 내역 로드 실패는 패널 전체를 막지 않는다 — 빈 목록 유지.
      })
    return () => {
      alive = false
    }
  }, [version])

  if (error) return <div className="panel-notice">{error}</div>
  if (!pf) return <div className="panel-empty">불러오는 중…</div>

  const chartData = snapshotsToChartData(pf.snapshots)

  return (
    <div className="portfolio-panel">
      <div className="metric-row">
        <div className="metric-tile">
          <span className="metric-label">총 자산</span>
          <span className="metric-value">{fmtUsdt(pf.wallet_balance)}</span>
        </div>
        <div className="metric-tile">
          <span className="metric-label">사용 가능</span>
          <span className="metric-value">{fmtUsdt(pf.available)}</span>
        </div>
        <div className="metric-tile">
          <span className="metric-label">포지션 마진</span>
          <span className="metric-value">{fmtUsdt(pf.margin_used)}</span>
        </div>
        <div className="metric-tile">
          <span className="metric-label">미실현 손익</span>
          <span className={`metric-value ${pnlClass(pf.unrealized_pnl)}`}>{fmtSignedUsdt(pf.unrealized_pnl)}</span>
        </div>
        <div className="metric-tile">
          <span className="metric-label">
            매매 가용 자금
            {typeof pf.seed === "number" && (
              <span className="metric-sub"> 시드 {fmtUsdt(pf.seed)}</span>
            )}
          </span>
          <span
            className="metric-value"
            title="복리 금지 — 매매에는 min(지갑, 시드)만 사용합니다. 초과 수익은 매매에 재투입되지 않고 시드로 고정됩니다"
          >
            {fmtUsdt(pf.trading_capital ?? pf.seed ?? 0)}
          </span>
        </div>
        <div className="metric-tile">
          <span className="metric-label">누적 출금 수익</span>
          <span
            className={`metric-value ${typeof pf.withdrawn_cum === "number" && pf.withdrawn_cum > 0 ? "pos" : ""}`}
            title="복리 금지 규칙 — 시드 초과 실현 수익은 자동으로 출금 원장에 격리됩니다"
          >
            {fmtUsdt(pf.withdrawn_cum ?? 0)}
          </span>
        </div>
        <div className="metric-tile">
          <span className="metric-label">
            누적 실현 손익
            {typeof pf.closed_trades === "number" && pf.closed_trades > 0 && (
              <span className="metric-sub">
                {" "}
                {pf.win_trades}승 {pf.closed_trades - (pf.win_trades ?? 0)}패
              </span>
            )}
          </span>
          <span
            className={`metric-value ${pnlClass(pf.realized_pnl_cum ?? 0)}`}
            title="종결된 모든 거래(익절·손절·청산)의 순손익 합 — 매매로 번/잃은 금액"
          >
            {fmtSignedUsdt(pf.realized_pnl_cum ?? 0)}
          </span>
        </div>
      </div>
      {chartData.length > 0 && <EquityChart data={chartData} color="#52a9ff" />}
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>포지션</th>
              <th className="num">수량</th>
              <th className="num">평균 진입가</th>
              <th className="num">마크가</th>
              <th className="num">익절 라인</th>
              <th className="num">손절선</th>
              <th className="num">청산가</th>
              <th className="num">청산거리</th>
              <th className="num">펀딩</th>
              <th className="num">미실현 손익</th>
            </tr>
          </thead>
          <tbody>
            {pf.positions.length === 0 && (
              <tr>
                <td colSpan={10} className="panel-empty">
                  오픈 포지션 없음
                </td>
              </tr>
            )}
            {pf.positions.map((p) => {
              const liqDist = liqDistancePct(p.mark_price, p.liq_price)
              // funding_paid 양수 = 지불(비용) → 손익 관점 부호 반전 표기
              const fundingPnl = typeof p.funding_paid === "number" ? -p.funding_paid : null
              return (
                <tr key={p.symbol}>
                  <td>
                    <span className="strategy-name">{p.symbol}</span>
                    <span className={`badge ${sideClass(p.side)}`}>{sideLabel(p.side)}</span>
                    <span className="badge badge-lev">{p.leverage}x</span>
                    <span className="strategy-params">격리마진 {fmtUsdt(p.isolated_margin)}</span>
                  </td>
                  <td className="num">{p.qty.toLocaleString("ko-KR", { maximumFractionDigits: 6 })}</td>
                  <td className="num">{fmtPrice(p.avg_entry)}</td>
                  <td className="num">{fmtPrice(p.mark_price)}</td>
                  <td className="num pos">
                    {p.tp_lines && p.tp_lines.length > 0
                      ? p.tp_lines.map((t, i) => (
                          <div key={i}>
                            {fmtPrice(t.price)}
                            <span className="strategy-params">
                              {" "}
                              ({t.qty.toLocaleString("ko-KR", { maximumFractionDigits: 6 })})
                            </span>
                          </div>
                        ))
                      : "—"}
                  </td>
                  <td className="num neg">
                    {typeof p.stop_price === "number" ? fmtPrice(p.stop_price) : "—"}
                  </td>
                  <td className="num neg">{fmtPrice(p.liq_price)}</td>
                  <td className={`num ${liqDist !== null && liqDist < 10 ? "neg" : ""}`}>
                    {liqDist !== null ? `${liqDist.toFixed(1)}%` : "—"}
                  </td>
                  <td className={`num ${fundingPnl !== null ? pnlClass(fundingPnl) : ""}`}>
                    {fundingPnl !== null ? fmtSignedUsdt(fundingPnl) : "—"}
                  </td>
                  <td className={`num ${typeof p.unrealized_pnl === "number" ? pnlClass(p.unrealized_pnl) : ""}`}>
                    {typeof p.unrealized_pnl === "number" ? fmtSignedUsdt(p.unrealized_pnl) : "—"}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <h3 className="section-title">거래 내역 (손절·익절)</h3>
      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              <th>종목</th>
              <th>결과</th>
              <th className="num">수량</th>
              <th className="num">평균 진입</th>
              <th className="num">평균 청산</th>
              <th className="num">실현 손익</th>
              <th className="num">수익률(마진)</th>
              <th className="num">펀딩</th>
              <th>진입 시각</th>
              <th>청산 시각</th>
            </tr>
          </thead>
          <tbody>
            {history.length === 0 && (
              <tr>
                <td colSpan={10} className="panel-empty">
                  아직 종결된 거래가 없습니다
                </td>
              </tr>
            )}
            {history.map((t) => (
              <tr key={t.plan_id}>
                <td>
                  <span className="strategy-name">{t.symbol}</span>
                  <span className={`badge ${sideClass(t.side)}`}>{sideLabel(t.side)}</span>
                  <span className="badge badge-lev">{t.leverage}x</span>
                </td>
                <td>
                  <span className={`badge ${exitReasonClass(t.exit_reason)}`}>{t.exit_reason}</span>
                </td>
                <td className="num">{t.qty.toLocaleString("ko-KR", { maximumFractionDigits: 6 })}</td>
                <td className="num">{fmtPrice(t.avg_entry)}</td>
                <td className="num">{fmtPrice(t.avg_exit)}</td>
                <td className={`num ${pnlClass(t.pnl_usdt)}`}>{fmtSignedUsdt(t.pnl_usdt)}</td>
                <td className={`num ${pnlClass(t.ret_on_margin)}`}>
                  {(t.ret_on_margin * 100).toFixed(1)}%
                </td>
                <td className={`num ${pnlClass(-t.funding_paid)}`}>{fmtSignedUsdt(-t.funding_paid)}</td>
                <td>{shortDate(t.entry_ts)}</td>
                <td>{shortDate(t.exit_ts)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
