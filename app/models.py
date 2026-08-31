from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


class Unreachable(Exception):
    pass


@dataclass
class Edge:
    """Одна операция: перевод, обмен, сделка, ввод или вывод."""

    id: str
    src: str
    dst: str
    kind: str = "transfer"
    rate_ref: Optional[str] = None      # "wallet_p2p:USDT/RUB:bid"
    rate_const: Optional[float] = None
    fee_pct: float = 0.0                # процент с результата
    fee_src: float = 0.0                # фикс в валюте источника
    fee_dst: float = 0.0                # фикс в валюте назначения (сеть)
    spread_pct: float = 0.0             # запас на проскальзывание
    min_amount: float = 0.0
    max_amount: float = float("inf")
    eta_min: int = 0
    note: str = ""

    # заполняется в рантайме
    rate: float = 1.0
    calib: float = 1.0                  # поправка по реальным сделкам
    rate_origin: str = "const"

    def usable(self, amount: float) -> bool:
        return self.min_amount <= amount <= self.max_amount

    def apply(self, amount: float) -> "StepResult":
        a = amount - self.fee_src
        if a <= 0:
            raise Unreachable(f"{self.id}: сумма меньше фиксированной комиссии")

        gross = a * self.rate * self.calib
        pct = (self.fee_pct + self.spread_pct) / 100.0
        after_pct = gross * (1.0 - pct)
        out = after_pct - self.fee_dst
        if out <= 0:
            raise Unreachable(f"{self.id}: комиссия сети съедает всю сумму")

        return StepResult(
            edge=self,
            amount_in=amount,
            amount_out=out,
            loss_pct=(1.0 - out / gross) * 100.0 if gross else 0.0,
            fee_breakdown={
                "fixed_src": self.fee_src,
                "percent": gross - after_pct,
                "fixed_dst": self.fee_dst,
            },
        )


@dataclass
class StepResult:
    edge: Edge
    amount_in: float
    amount_out: float
    loss_pct: float
    fee_breakdown: dict = field(default_factory=dict)


@dataclass
class Route:
    steps: list[StepResult]

    @property
    def key(self) -> str:
        return ">".join(s.edge.id for s in self.steps)

    @property
    def amount_in(self) -> float:
        return self.steps[0].amount_in

    @property
    def amount_out(self) -> float:
        return self.steps[-1].amount_out

    @property
    def eta_min(self) -> int:
        return sum(s.edge.eta_min for s in self.steps)

    @property
    def effective_rate(self) -> float:
        return self.amount_out / self.amount_in if self.amount_in else 0.0
