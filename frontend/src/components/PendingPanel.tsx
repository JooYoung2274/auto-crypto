import { useEffect, useState } from "react"
import { api } from "../lib/api"
import type { OpenPlanInfo, PlanOrderInfo } from "../lib/types"
import { fmtUsdt, sideClass, sideLabel } from "../lib/format"
import { shortDate } from "./EquityChart"

const fmtPrice = (v: number | null | undefined) =>
  typeof v === "number" ? v.toLocaleString("ko-KR", { maximumFractionDigits: 4 }) : "—"

/** 주문 상태 → 한국어 라벨 (paper_orders.status). */
export function orderStatusLabel(status: string): string {
  switch (status) {
    case "open":
      return "대기"
    case "filled":
      return "체결"
    case "cancelled":
      return "취소"
    case "expired":
      return "TTL 만료"
    case "rejected":
      return "거부"
    default:
      return status
  }
}

/** 주문 행의 종류 라벨 — leg_kind 우선, 없으면 reduce_only로 추정. */
export function orderKindLabel(o: PlanOrderInfo): string {
  if (o.leg_kind === "entry") return `진입 ${(o.leg_index ?? 0) + 1}`
  if (o.leg_kind === "tp") return `익절 ${(o.leg_index ?? 0) + 1}`
  if (o.leg_kind === "stop-exit") return "손절 청산"
  return o.reduce_only ? "청산" : "진입"
}

/** 플랜 상태 → 한국어 라벨 (trade_plans.status). */
export function planStatusLabel(status: string): string {
  switch (status) {
    case "approved":
      return "승인 — 체결 대기"
    case "active":
      return "진행 중 (부분 체결)"
    default:
      return status
  }
}

interface Props {
  /** Bumped by the parent on order/position WS events to trigger a refetch. */
  version?: number
}

/** 대기 주문 탭: 오픈 플랜(시나리오)별 분할 레그 주문 현황. */
export function PendingPanel({ version = 0 }: Props) {
  const [plans, setPlans] = useState<OpenPlanInfo[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    api
      .openPlans()
      .then((p) => {
        if (alive) setPlans(p)
      })
      .catch(() => {
        if (alive) setError("대기 주문을 불러오지 못했습니다 (백엔드 연결 확인)")
      })
    return () => {
      alive = false
    }
  }, [version])

  if (error) return <div className="panel-notice">{error}</div>
  if (!plans) return <div className="panel-empty">불러오는 중…</div>
  if (plans.length === 0)
    return <div className="panel-empty">대기 중인 플랜이 없습니다 — 조건이 맞으면 트레이더가 시나리오를 냅니다</div>

  return (
    <div className="pending-panel">
      {plans.map((plan) => (
        <div key={plan.id} className="pending-plan">
          <div className="pending-head">
            <strong>{plan.symbol}</strong>
            <span className={sideClass(plan.side)}>{sideLabel(plan.side)}</span>
            <span className="pending-status">{planStatusLabel(plan.status)}</span>
            {typeof plan.leverage === "number" && <span>{plan.leverage}x 격리</span>}
            {typeof plan.margin_usdt === "number" && <span>마진 {fmtUsdt(plan.margin_usdt)}</span>}
            <span>진입 체결 {Math.round(plan.filled_fraction * 100)}%</span>
            {plan.created_at && <span className="pending-ts">{shortDate(plan.created_at)}</span>}
          </div>
          <div className="pending-scenario">
            {plan.stop && <span>손절 {fmtPrice(plan.stop.price)} (4h 종가 판정)</span>}
            {plan.tps.length > 0 && (
              <span>
                익절 {plan.tps.map((t) => `${fmtPrice(t.price)} (${Math.round(t.fraction * 100)}%)`).join(" → ")}
              </span>
            )}
            {plan.evidence && plan.evidence.length > 0 && <span>근거: {plan.evidence.join(", ")}</span>}
          </div>
          <table className="data-table">
            <thead>
              <tr>
                <th>레그</th>
                <th>방향</th>
                <th>수량</th>
                <th>지정가</th>
                <th>체결 수량</th>
                <th>상태</th>
                <th>발주 시각</th>
              </tr>
            </thead>
            <tbody>
              {plan.orders.map((o) => (
                <tr key={o.id}>
                  <td>{orderKindLabel(o)}</td>
                  <td>
                    <span className={sideClass(o.side === "buy" ? "long" : "short")}>
                      {o.side === "buy" ? "매수" : "매도"}
                    </span>
                  </td>
                  <td>{o.qty.toLocaleString("ko-KR", { maximumFractionDigits: 6 })}</td>
                  <td>{fmtPrice(o.limit_price)}</td>
                  <td>{o.filled_qty > 0 ? o.filled_qty.toLocaleString("ko-KR", { maximumFractionDigits: 6 }) : "—"}</td>
                  <td>{orderStatusLabel(o.status)}</td>
                  <td>{shortDate(o.ts)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  )
}
