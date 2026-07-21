import type { CycleKind, ReportKind } from "./types"

// Korean display labels for the three run kinds and the two report kinds.
// Kept as pure helpers (no React) so they can be unit-tested directly and
// reused by both the ControlBar and the ReportView.

const CYCLE_KIND_LABELS: Record<CycleKind, string> = {
  research: "전략 연구",
  validate: "수익성 검증",
  trade: "모의거래",
}

/** Label for a cycle kind; unknown/missing kinds fall back to 전략 연구 (research).
 *  trade는 모드 인지형 — live 모드에서는 "실거래"로 표시한다. */
export function cycleKindLabel(
  kind: CycleKind | null | undefined,
  mode?: string | null,
): string {
  if (kind === "trade" && mode === "live") return "실거래"
  return (kind && CYCLE_KIND_LABELS[kind]) || CYCLE_KIND_LABELS.research
}

const REPORT_KIND_LABELS: Record<ReportKind, string> = {
  research: "연구",
  validation: "검증",
}

/** Short badge label for a report kind; unknown/missing kinds fall back to 연구 (research). */
export function reportKindLabel(kind: ReportKind | null | undefined): string {
  return kind === "validation" ? REPORT_KIND_LABELS.validation : REPORT_KIND_LABELS.research
}
