import type { AppConfig } from "./types"

/** 타임프레임 → 밀리초. 백엔드 `_TF_SECONDS`와 같은 표. */
export const TF_MS: Record<string, number> = {
  "1m": 60_000,
  "5m": 300_000,
  "15m": 900_000,
  "1h": 3_600_000,
  "4h": 14_400_000,
  "1d": 86_400_000,
}

/**
 * 다음 봉마감 시각.
 *
 * 거래소 봉 경계는 UTC epoch 기준으로 정렬돼 있으므로 로컬 시간대와 무관하게
 * epoch를 봉 길이로 올림하면 된다 (4h·1d에서 특히 중요 — 로컬 자정 기준으로
 * 계산하면 UTC 오프셋만큼 어긋난다).
 */
export function nextBarClose(timeframe: string, now: Date): Date | null {
  const ms = TF_MS[timeframe]
  if (!ms) return null
  return new Date((Math.floor(now.getTime() / ms) + 1) * ms)
}

/** 남은 시간을 사람이 읽는 표기로. 1분 미만은 '곧'. */
export function formatCountdown(fromMs: number): string {
  if (fromMs <= 0) return "곧"
  const minutes = Math.floor(fromMs / 60_000)
  if (minutes < 1) return "곧"
  if (minutes < 60) return `${minutes}분 후`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest ? `${hours}시간 ${rest}분 후` : `${hours}시간 후`
}

/** 로컬 시:분 (2자리). */
export function clockLabel(d: Date): string {
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`
}

export interface NextJudgment {
  /** 상단 스트립에 그대로 출력하는 문장. */
  text: string
  /** 시각이 확정된 경우에만 채워진다 (테스트·툴팁용). */
  at: Date | null
}

/**
 * 팀 탭 상단에 표시할 "다음에 무슨 일이 일어나는가".
 *
 * 에이전트 카드마다 '다음 업무 대기 중'을 7번 반복하는 대신, 실제로 유용한
 * 정보 한 줄을 패널 머리에 둔다. 우선순위:
 *   ① 봉마감 트리거가 켜져 있으면 다음 봉마감 시각
 *   ② 자동 사이클 간격이 설정돼 있으면 그 간격
 *   ③ 둘 다 없으면 수동 실행 안내
 */
export function nextJudgment(config: AppConfig | null, now: Date): NextJudgment {
  if (config?.bar_close_trade_enabled && config.execution_timeframe) {
    const at = nextBarClose(config.execution_timeframe, now)
    if (at) {
      return {
        at,
        text:
          `다음 판단 ${clockLabel(at)} (${formatCountdown(at.getTime() - now.getTime())})` +
          ` · ${config.execution_timeframe} 봉마감 기준`,
      }
    }
  }
  if (config && config.auto_cycle_minutes > 0) {
    return { at: null, text: `자동 사이클 ${config.auto_cycle_minutes}분 간격으로 실행 중` }
  }
  return { at: null, text: "대기 중 — 상단 버튼으로 실행하면 팀이 움직입니다" }
}
