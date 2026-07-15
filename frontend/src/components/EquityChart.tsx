import { useId, useMemo } from "react"
import type { EquityPoint } from "../lib/types"

/**
 * Hand-rolled SVG line chart (no chart libraries) — dark-theme friendly,
 * subtle horizontal grid, single accent line with a soft area fill, and
 * compact axis labels. Geometry building is a pure function so it can be
 * unit-tested without a DOM.
 */

export interface ChartPadding {
  top: number
  right: number
  bottom: number
  left: number
}

export interface XTick {
  x: number
  label: string
}

export interface YTick {
  y: number
  value: number
  label: string
}

export interface ChartGeometry {
  points: { x: number; y: number }[]
  linePath: string
  areaPath: string
  xTicks: XTick[]
  yTicks: YTick[]
  baseY: number
}

/** "Nice" axis tick values covering [min, max] with roughly `count` steps. */
export function niceTicks(min: number, max: number, count = 4): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || count < 1) return []
  if (min === max) return [min]
  if (min > max) [min, max] = [max, min]
  const rough = (max - min) / count
  const pow = 10 ** Math.floor(Math.log10(rough))
  const norm = rough / pow
  const eps = 1e-9
  const mult = norm <= 1 + eps ? 1 : norm <= 2 + eps ? 2 : norm <= 5 + eps ? 5 : 10
  const step = mult * pow
  const start = Math.ceil(min / step) * step
  const ticks: number[] = []
  for (let i = 0; ; i++) {
    const v = start + i * step
    if (v > max + step * 1e-6) break
    ticks.push(Number(v.toFixed(10)))
  }
  return ticks
}

/** Compact USDT axis label: 1.2M / 10.5k / 250 / 1.05 (억/만 대신 k/M 경로). */
export function formatCompact(v: number): string {
  const trim = (n: number) => {
    const s = n.toFixed(1)
    return s.endsWith(".0") ? s.slice(0, -2) : s
  }
  const abs = Math.abs(v)
  if (abs >= 1e6) return `${trim(v / 1e6)}M`
  if (abs >= 1e3) return `${trim(v / 1e3)}k`
  if (abs >= 100) return String(Math.round(v))
  return String(Math.round(v * 100) / 100)
}

/**
 * 24/7 크립토 축 라벨은 풀 타임스탬프가 필요하다:
 * "2026-07-14T09:30:00" → "07.14 09:30", 시각이 없으면 "26.07.14".
 */
export function shortDate(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(iso)
  if (!m) return iso.slice(0, 10)
  if (m[4]) return `${m[2]}.${m[3]} ${m[4]}:${m[5]}`
  return `${m[1].slice(2)}.${m[2]}.${m[3]}`
}

const round2 = (n: number) => Math.round(n * 100) / 100

/**
 * Pure geometry builder: maps an equity series onto SVG coordinates.
 * Returns null when there is nothing to draw.
 */
export function buildChartGeometry(
  data: EquityPoint[],
  width: number,
  height: number,
  padding: ChartPadding,
): ChartGeometry | null {
  const innerW = width - padding.left - padding.right
  const innerH = height - padding.top - padding.bottom
  if (data.length === 0 || innerW <= 0 || innerH <= 0) return null

  const values = data.map((d) => d.value)
  let lo = Math.min(...values)
  let hi = Math.max(...values)
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null
  if (lo === hi) {
    const pad = Math.abs(lo) * 0.05 || 1
    lo -= pad
    hi += pad
  } else {
    const pad = (hi - lo) * 0.06
    lo -= pad
    hi += pad
  }

  const n = data.length
  const xAt = (i: number) => padding.left + (n === 1 ? innerW / 2 : (i / (n - 1)) * innerW)
  const yAt = (v: number) => padding.top + (1 - (v - lo) / (hi - lo)) * innerH

  const points = data.map((d, i) => ({ x: round2(xAt(i)), y: round2(yAt(d.value)) }))
  const linePath = points.map((p, i) => `${i === 0 ? "M" : "L"}${p.x},${p.y}`).join(" ")
  const baseY = round2(padding.top + innerH)
  const first = points[0]
  const last = points[points.length - 1]
  const areaPath = `${linePath} L${last.x},${baseY} L${first.x},${baseY} Z`

  const yTicks: YTick[] = niceTicks(lo, hi, 4).map((v) => ({
    y: round2(yAt(v)),
    value: v,
    label: formatCompact(v),
  }))

  const tickCount = Math.min(4, n)
  const xTicks: XTick[] = []
  for (let t = 0; t < tickCount; t++) {
    const i = tickCount === 1 ? 0 : Math.round((t / (tickCount - 1)) * (n - 1))
    xTicks.push({ x: round2(xAt(i)), label: shortDate(data[i].date) })
  }

  return { points, linePath, areaPath, xTicks, yTicks, baseY }
}

const PAD: ChartPadding = { top: 12, right: 14, bottom: 22, left: 52 }
const WIDTH = 720

interface Props {
  data: EquityPoint[]
  height?: number
  color?: string
}

export function EquityChart({ data, height = 220, color = "#4cc38a" }: Props) {
  const gradId = useId()
  const geom = useMemo(() => buildChartGeometry(data, WIDTH, height, PAD), [data, height])

  if (!geom) return <div className="chart-empty">차트 데이터 없음</div>

  const last = geom.points[geom.points.length - 1]
  return (
    <svg
      viewBox={`0 0 ${WIDTH} ${height}`}
      className="equity-chart"
      role="img"
      aria-label="자산 곡선 차트"
      preserveAspectRatio="none"
    >
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.26" />
          <stop offset="100%" stopColor={color} stopOpacity="0.02" />
        </linearGradient>
      </defs>
      {geom.yTicks.map((t) => (
        <g key={`y${t.value}`}>
          <line x1={PAD.left} x2={WIDTH - PAD.right} y1={t.y} y2={t.y} className="chart-grid" />
          <text x={PAD.left - 7} y={t.y + 3} textAnchor="end" className="chart-label">
            {t.label}
          </text>
        </g>
      ))}
      <path d={geom.areaPath} fill={`url(#${gradId})`} stroke="none" />
      <path d={geom.linePath} fill="none" stroke={color} strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={last.x} cy={last.y} r="2.6" fill={color} />
      {geom.xTicks.map((t, i) => (
        <text
          key={`x${i}`}
          x={t.x}
          y={height - 6}
          textAnchor={i === 0 ? "start" : i === geom.xTicks.length - 1 ? "end" : "middle"}
          className="chart-label"
        >
          {t.label}
        </text>
      ))}
    </svg>
  )
}
