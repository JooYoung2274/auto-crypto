"""Perp 거래 비용 모델 (스펙 §4, 규칙 §1).

- 진입·TP 레그 = 패시브 지정가(post-only) → maker 요율, 슬리피지 없음
  (지정가 그대로 체결되므로).
- 손절/청산회피 exit = 공격적 reduce-only 크로싱 리밋 → taker 요율 + 슬리피지.

체결마다 어느 요율인지 태깅한다 ('maker' | 'taker').
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PerpCostModel:
    maker_fee: float = 0.00025  # post-only 지정가 (진입·TP)
    taker_fee: float = 0.0005  # 공격적 exit (손절/청산회피)
    slippage: float = 0.0005  # taker 경로에만 적용

    @property
    def maker_cost(self) -> float:
        """패시브 지정가 체결의 노셔널 대비 비용 비율."""
        return self.maker_fee

    @property
    def taker_cost(self) -> float:
        """공격적 크로싱 exit의 노셔널 대비 비용 비율 (taker + 슬리피지)."""
        return self.taker_fee + self.slippage

    def fee(self, notional: float, taker: bool) -> float:
        """체결 노셔널(USDT)에 대한 수수료 금액."""
        rate = self.taker_cost if taker else self.maker_cost
        return abs(notional) * rate
