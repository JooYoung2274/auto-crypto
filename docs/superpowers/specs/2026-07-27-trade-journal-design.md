# 매매일지 (Trade Journal) — 설계

**작성일** 2026-07-27 · **상태** 승인됨

## 목적

종결된 거래마다 **왜 들어갔고 / 손절·익절을 어떻게 잡았고 / 결과가 어땠는지**를
기록된 데이터만으로 서술한다. 사용자는 여기에 사람만 쓸 수 있는 메모(소감,
개선점)를 덧붙인다.

기존 `포트폴리오 > 거래 내역` 표는 "무엇이 일어났는가"(숫자)를 보여준다.
일지는 "왜 그렇게 했는가"(설계 의도)를 붙여 되짚어볼 수 있게 한다.

## 원칙

리포트 계층과 같은 규칙을 따른다.

- **숫자와 근거는 코드가 쓴다** — 진입 근거(`evidence`), 래더 지정가, 손절
  거리, 손익비, 실현 손익은 전부 이미 DB에 있다. 사람이 옮겨 적지 않는다.
- **사람은 판단만 쓴다** — 자동 서술로 표현할 수 없는 것만 메모로 남긴다.
- **매매 판단에 영향 없음** — 읽기 전용 파생 뷰. 메모는 어떤 로직도 읽지 않는다.

## 저장 방식 — C안 (파생 뷰 + 메모 테이블)

| 구성 | 저장 | 이유 |
|---|---|---|
| 자동 서술 | **저장 안 함** — 조회 시 DB에서 조립 | 과거 거래에 즉시 소급 적용되고, 서술 문구를 고치면 전부 반영된다 |
| 사용자 메모 | `journal_notes` 테이블 | 사람이 쓴 것은 유일본이라 반드시 보존 |

```sql
CREATE TABLE IF NOT EXISTS journal_notes (
    plan_id    INTEGER PRIMARY KEY,
    note       TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

`plan_id`는 `trade_plans.id` — 종결 거래의 자연키이며 이미 `거래 내역`이
쓰고 있다.

## 구성 요소

### `backend/app/trades.py` (신규 — 기존 코드 이동)

`api/routes.py`의 `_trade_history_rows`를 그대로 옮긴다. 일지와 거래 내역이
**같은 롤업을 공유**해야 두 화면의 숫자가 어긋나지 않는다. routes는 재-export만
남긴다. 기존 회귀 테스트 3건이 이동을 검증한다.

### `backend/app/journal.py` (신규)

```python
def compose_entries(db, limit=None) -> list[dict]
```

거래 내역 롤업(숫자) + `plan_json`(설계 의도) + `paper_orders`(실제 체결)를
합쳐 구조화된 dict를 만든다. **마크다운이 아니라 JSON을 돌려준다** — 프론트가
표·배지로 직접 렌더하고 메모 편집기를 붙일 수 있어야 한다.

항목: `plan_id, symbol, side, leverage, outcome, entry_ts, exit_ts,
holding_minutes, evidence[], entry_legs[], planned_weighted_entry,
actual_avg_entry, qty, stop{price,distance_pct}, tps[], rr, rr_gate,
avg_exit, pnl_usdt, funding_paid, margin_usdt, ret_on_margin, price_decimals,
note`.

### 가격 자릿수

DOGE 래더가 `0.0728 / 0.0729 / 0.0729`로 보이는 잘림 버그가 있었다(실제
0.072841 / 0.072886 / 0.072947). **유효숫자 6자리** 기준으로 심볼별 자릿수를
계산해 `price_decimals`로 내려보낸다.

### API

| 메서드 | 경로 | 동작 |
|---|---|---|
| `GET` | `/api/journal?limit=50` | 일지 목록 (최신순) |
| `PUT` | `/api/journal/{plan_id}/note` | 메모 upsert (`{"note": "..."}`) |

메모 대상 `plan_id`가 종결 거래가 아니면 404.

### 프론트엔드

- 탭 **매매일지** 추가 (`JournalPanel.tsx`)
- 거래 1건 = 카드 1장: 헤더(결과 배지·심볼·방향·레버리지) → 진입 근거 →
  분할 진입 표(체결 여부 포함) → 손절·익절 설계 → 결과 → 메모 편집기
- 메모는 textarea + 저장 버튼. 저장 성공/실패를 인라인 표시.

## 테스트

- `trades.py` 이동 후 기존 `/api/trade-history` 회귀 3건 통과
- `journal.py`: 익절/손절/강제청산 각 경로, 미체결 레그가 섞인 래더,
  손익비 side-aware 계산, 가격 자릿수(DOGE 래더 3개가 서로 다르게 표시)
- API: 목록 정렬·limit, 메모 upsert·조회 왕복, 미존재 plan_id 404
- 프론트: 보유시간 포맷(`1시간 0분` → `1시간`), 결과 배지 매핑

## 범위 밖 (YAGNI)

- 일지 검색·필터·태그
- 날짜별 회고(거래 단위만)
- LLM 자동 코멘트 — 기존 해설 계층이 있으므로 필요해지면 그때 붙인다
- 내보내기(PDF/CSV)
