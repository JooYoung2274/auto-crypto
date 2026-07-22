"""Application settings (pydantic-settings, env prefix ``CA_``, .env support).

Coin Agents Office — Binance USDT-M perpetual futures. Spec §0/§4/§5 keys:
whitelist universe, fixed USDT seed (복리 금지), maker/taker fee split,
leverage caps (BTC 10x / alt 5x / min 3x), RR gates (BTC·ETH ≥2, 알트 ≥3),
multi-timeframe list + execution TF, split-entry fractions, TTLs,
liquidation buffer, funding, blackout, live safety limits.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CA_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    trading_mode: Literal["paper", "live"] = "paper"
    # 화이트리스트 = 저시총 금지 (규칙 §1) — 이 외 심볼은 RiskEngine이 거부.
    universe: list[str] = [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "DOGEUSDT",
    ]
    # 고정 시드 (USDT). 수익은 withdrawal_ledger로 분리 — 복리 금지 (규칙 §1).
    initial_seed_usdt: float = 10_000.0
    # 진입·TP 레그 = post-only maker, 손절/청산회피 exit = taker (스펙 §0).
    maker_fee: float = 0.00025
    taker_fee: float = 0.0005
    slippage: float = 0.0005
    # 레버리지 캡 — BTC 최대 10배, 그 외(ETH 포함) 최대 5배, 최소 3배 (규칙 §1).
    btc_max_leverage: int = 10
    alt_max_leverage: int = 5
    min_leverage: int = 3
    # 손익비 게이트 — BTC·ETH ≥ 1:2, 알트 ≥ 1:3 (규칙 §1).
    rr_min_major: float = 2.0
    rr_min_alt: float = 3.0
    # 멀티 타임프레임 (스펙 §1.2). 실행(주문) TF 기본 15m.
    timeframes: list[str] = ["1d", "4h", "15m", "5m"]
    execution_timeframe: str = "15m"
    # 분할 진입 비중 기본 50/25/25 (규칙 §1 몰빵 금지).
    entry_fractions: list[float] = [0.5, 0.25, 0.25]
    # 미체결 주문 TTL / 플랜 전체 TTL (실행 TF 봉 수 기준). TTL 재큐는 원가격만.
    order_ttl_bars: int = 8
    plan_ttl_bars: int = 96
    # 손절가는 청산가보다 markPrice 쪽으로 이 비율 이상 여유 필요 (스펙 §2).
    liq_buffer_pct: float = 0.10
    # 최소 손절 거리 — 가중 진입가 대비 손절 거리가 이 비율 미만이면 거부.
    # 진입가와 손절선이 붙어 RR이 뻥튀기되고 진입 즉시 무효화되는 플랜 차단.
    min_stop_distance_pct: float = 0.005
    # 펀딩 이력 부재 시 근사 요율 (0.01% / 8h).
    funding_default_rate: float = 0.0001
    # 런타임 포트폴리오 게이트 (스펙 §2).
    max_concurrent_positions: int = 3
    daily_max_loss_pct: float = 0.05
    # 지표 발표(CPI·FOMC 등) 전후 신규 진입 금지 시간 (±, 규칙 §2).
    blackout_hours: float = 12.0
    # Sharpe 연환산 계수 (스펙 §4).
    bars_per_year: dict[str, int] = {
        "1d": 365,
        "4h": 2190,
        "15m": 35040,
        "5m": 105120,
    }
    max_mdd: float = 0.30
    min_trades: int = 10
    # 활동성 필터: 유니버스 전체 연간 거래 횟수가 이 값 미만이면 전략 탈락.
    min_trades_per_year: float = 12.0
    # 랭킹 스코어 가중치 (sharpe/win_rate/mdd/cagr 순위 percent-rank 가중합).
    rank_w_sharpe: float = 0.35
    rank_w_win_rate: float = 0.15
    rank_w_mdd: float = 0.2
    rank_w_cagr: float = 0.3
    auto_cycle_minutes: int = 0  # 0=off
    # 자동/목표 사이클이 연구를 마치면 모의거래(trade) 사이클을 이어서 자동 실행.
    auto_trade_after_research: bool = False
    # 실행 TF 봉마감 정렬 trade 트리거 (스펙 §1.1, skip-not-queue). 기본 off.
    bar_close_trade_enabled: bool = False
    candidates_per_cycle: int = 60
    # 목표 탐색 모드(goal-seek): research/validate 사이클을 목표 달성까지 자동 반복.
    goal_win_rate: float = 0.65  # 목표 OOS 승률
    goal_validate_every: int = 5  # 검증 사이클 사이의 research 사이클 수
    goal_max_cycles: int = 200  # research 사이클 총 예산
    db_path: str = "coinagent.db"
    # 거래소 선택 — Binance가 주(primary), OKX 추가 지원 (사용자 요청 2026-07-14).
    # 시세 소스(DataLoader/FundingLoader)와 라이브 브로커를 이 값으로 고른다.
    exchange: Literal["binance", "okx"] = "binance"
    # Binance USDT-M (fapi). 키 없이 live 기동 거부, 테스트넷 플래그 지원.
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = False
    # OKX USDT 무기한(SWAP). API 키+시크릿+**패스프레이즈** 3종 필수 (base64 HMAC
    # 서명). okx_demo=True면 데모 트레이딩 헤더(x-simulated-trading: 1) 전송.
    okx_api_key: str = ""
    okx_api_secret: str = ""
    okx_api_passphrase: str = ""
    okx_demo: bool = False
    # 라이브 안전장치 — 주문 1건 노셔널 상한, rolling-24h 주문 수 한도.
    live_max_order_usdt: float = 100.0
    live_daily_order_limit: int = 20
    # 라이브 일손실 서킷브레이커 (reduce-only 킬스위치 진입 임계).
    live_max_loss_pct: float = 0.05


@lru_cache
def get_settings() -> Settings:
    return Settings()
