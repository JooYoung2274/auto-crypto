import { useEffect, useState } from "react"

/** 온보딩 노출 여부를 기억하는 localStorage 키. 문구가 크게 바뀌면 v2로 올린다. */
export const ONBOARDING_KEY = "coinagent.onboarding.v1"

/** 면책 확인 시각(ISO). 이용자가 고지를 봤다는 기록으로 남긴다. */
export const CONSENT_KEY = "coinagent.consent.v1"

export interface OnboardingStep {
  title: string
  lines: string[]
  /** 강조 박스 — 오해가 잦은 지점(대기 시간, 무거래 정상)을 눈에 띄게. */
  callout?: string
}

/**
 * 비개발자 첫 사용자를 위한 3단계 안내.
 *
 * 실제 CS로 이어지는 오해를 먼저 막는 것이 목적이다:
 * ① 실제 돈이 나가는가 ② 왜 아무 거래도 안 하는가 ③ 연구가 왜 이렇게 오래 걸리는가.
 */
export const ONBOARDING_STEPS: OnboardingStep[] = [
  {
    title: "환영합니다 — 모의거래 전용입니다",
    lines: [
      "이 프로그램은 실시간 시세로 매매를 흉내 내지만, 주문은 전부 가상입니다.",
      "실제 자금이 움직이지 않고, 거래소 계정이나 API 키도 필요 없습니다.",
      "기본 전략이 이미 들어 있어 지금 바로 시작할 수 있습니다.",
    ],
    callout: "가상 자금 10,000 USDT로 시작합니다. 실제 돈은 한 푼도 나가지 않습니다.",
  },
  {
    title: "어떻게 돌아가나요",
    lines: [
      "7명의 에이전트가 15분마다 시장을 확인합니다.",
      "조건이 맞으면 분할 매수로 가상 주문을 내고, 손절·익절을 자동으로 걸어둡니다.",
      "탭 안내 — 팀: 에이전트 현황 · 활동 로그: 판단 근거 · 챔피언: 현재 전략 · 대기 주문: 체결 대기 · 포트폴리오: 손익.",
    ],
    callout:
      "조건이 맞지 않으면 아무 거래도 하지 않습니다. 하루 종일 거래가 없을 수 있고, 이는 고장이 아니라 정상 동작입니다.",
  },
  {
    title: "더 좋은 전략 찾기 (선택)",
    lines: [
      "상단 '🔬 전략 연구' 버튼을 누르면 수십 개 전략을 백테스트해 더 나은 전략을 찾습니다.",
      "완료되면 챔피언이 자동으로 교체됩니다. 지금 전략 그대로 두고 써도 됩니다.",
      "처음 실행할 때는 시세 데이터를 내려받으므로 인터넷 연결이 필요합니다.",
    ],
    callout:
      "전략 연구는 20~45분 걸립니다. 진행 중에는 창을 닫지 마세요. 멈춘 것처럼 보여도 정상입니다.",
  },
]

export const DISCLAIMER =
  "과거 데이터 백테스트 성과는 미래 수익을 보장하지 않습니다. 이 프로그램은 교육·연구용 시뮬레이터입니다."

/**
 * 자동 노출 여부.
 *
 * 모의거래 전용 빌드에서만, 그리고 아직 한 번도 닫지 않았을 때만 뜬다.
 * 실거래(paperOnly=false) UI는 이 오버레이를 절대 띄우지 않는다.
 */
export function shouldShowOnboarding(paperOnly: boolean, stored: string | null): boolean {
  return paperOnly && stored !== "done"
}

/** 마지막 단계에서 면책에 동의해야 '시작하기'가 열린다. */
export function canFinish(step: number, agreed: boolean): boolean {
  return step !== ONBOARDING_STEPS.length - 1 || agreed
}

interface Props {
  /** ``consented`` 는 면책 확인 체크 후 '시작하기'로 닫았는지 여부. */
  onClose: (consented: boolean) => void
}

/** 3단계 첫 실행 안내 오버레이. */
export function Onboarding({ onClose }: Props) {
  const [step, setStep] = useState(0)
  const [agreed, setAgreed] = useState(false)
  const current = ONBOARDING_STEPS[step]
  const last = step === ONBOARDING_STEPS.length - 1

  // Esc로도 닫히게 — 모달에 갇힌 느낌을 주지 않는다.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose(false)
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose])

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="사용 안내">
      <div className="modal onboarding">
        <div className="onboarding-progress">
          {ONBOARDING_STEPS.map((s, i) => (
            <span
              key={s.title}
              className={`onboarding-dot ${i === step ? "onboarding-dot-active" : ""}`}
            />
          ))}
          <span className="onboarding-count">
            {step + 1} / {ONBOARDING_STEPS.length}
          </span>
        </div>

        <h3 className="modal-title">{current.title}</h3>
        <ul className="onboarding-list">
          {current.lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
        {current.callout && <p className="onboarding-callout">{current.callout}</p>}

        <p className="onboarding-disclaimer">{DISCLAIMER}</p>

        {last && (
          <label className="onboarding-consent">
            <input
              type="checkbox"
              checked={agreed}
              onChange={(e) => setAgreed(e.target.checked)}
            />
            <span>위 안내와 면책 사항을 확인했습니다.</span>
          </label>
        )}

        <div className="modal-actions">
          {!last && (
            <button type="button" className="btn btn-ghost" onClick={() => onClose(false)}>
              건너뛰기
            </button>
          )}
          {step > 0 && (
            <button type="button" className="btn btn-ghost" onClick={() => setStep(step - 1)}>
              이전
            </button>
          )}
          <button
            type="button"
            className="btn btn-start"
            disabled={!canFinish(step, agreed)}
            onClick={() => (last ? onClose(true) : setStep(step + 1))}
          >
            {last ? "시작하기" : "다음"}
          </button>
        </div>
      </div>
    </div>
  )
}
