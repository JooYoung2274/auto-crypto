// USDT/perp display helpers — pure functions, unit-tested (format.test.ts).
// Korean convention kept: ko-KR digit grouping, up = red(.pos) / down = blue(.neg).

import type { ParamValue, PositionSide, RegimeValue, StrategyParams } from "./types"

/** "12,345.67 USDT" — ko-KR grouping, max 2 decimals. */
export function fmtUsdt(v: number): string {
  return `${v.toLocaleString("ko-KR", { maximumFractionDigits: 2 })} USDT`
}

/** "+12.34 USDT" / "-5.6 USDT" — explicit sign for PnL amounts. */
export function fmtSignedUsdt(v: number): string {
  return `${v > 0 ? "+" : ""}${v.toLocaleString("ko-KR", { maximumFractionDigits: 2 })} USDT`
}

/** 한국 관례: 상승 빨강(.pos) / 하락 파랑(.neg). */
export function pnlClass(v: number): string {
  return v > 0 ? "pos" : v < 0 ? "neg" : ""
}

/** "롱" / "숏" side badge label. */
export function sideLabel(side: PositionSide | "both" | string): string {
  return side === "long" ? "롱" : side === "short" ? "숏" : side === "both" ? "양방향" : String(side)
}

/** CSS suffix for side badges: badge-long / badge-short. */
export function sideClass(side: PositionSide | string): string {
  return side === "short" ? "badge-short" : "badge-long"
}

const REGIME_LABELS: Record<RegimeValue, string> = {
  long_btc: "롱장",
  long_alt: "알트불장",
  short: "숏장",
  cash: "현금",
}

/** 레짐 칩 라벨 (spec §8 — 롱장/알트불장/숏장/현금). Unknown values pass through. */
export function regimeLabel(regime: RegimeValue | string | null | undefined): string | null {
  if (!regime) return null
  return REGIME_LABELS[regime as RegimeValue] ?? String(regime)
}

/**
 * 청산까지의 거리 (마크가 대비 %). Returns null when inputs are unusable —
 * the UI then shows "—" instead of a misleading 0%.
 */
export function liqDistancePct(markPrice: number | null | undefined, liqPrice: number | null | undefined): number | null {
  if (typeof markPrice !== "number" || typeof liqPrice !== "number") return null
  if (!Number.isFinite(markPrice) || !Number.isFinite(liqPrice) || markPrice <= 0 || liqPrice <= 0) return null
  return (Math.abs(markPrice - liqPrice) / markPrice) * 100
}

function fmtParamValue(v: ParamValue): string {
  if (v === null) return "null"
  if (Array.isArray(v)) return `[${v.map(fmtParamValue).join(",")}]`
  if (typeof v === "object") {
    return `{${Object.entries(v)
      .map(([k, x]) => `${k}:${fmtParamValue(x)}`)
      .join(",")}}`
  }
  return String(v)
}

/**
 * "fast=10, slow=50, legs={tf:15m,fracs:[0.5,0.25,0.25]}" — unlike the stock
 * version, params may nest (multi-TF legs / ladder fractions), so objects and
 * arrays are rendered inline instead of "[object Object]".
 */
export function fmtParams(params: StrategyParams | null | undefined): string {
  if (!params) return ""
  return Object.entries(params)
    .map(([k, v]) => `${k}=${fmtParamValue(v)}`)
    .join(", ")
}
