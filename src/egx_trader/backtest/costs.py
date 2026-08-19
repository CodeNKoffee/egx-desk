"""What a trade actually costs on EGX through Thndr.

Modelled explicitly because each of these quietly flatters a backtest if omitted:

**Fees are not linear.** Thndr Trader includes 50 commission-free executions a
month, then 2 EGP + 0.1%. A strategy firing 80 trades a month is meaningfully
worse than one firing 45, and a flat per-trade fee misses that entirely — as does
assuming everything is free.

**Capital gains tax is 10%**, on realised gains only.

**The subscription is a real drag.** 245 EGP a month whether you trade or not.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

FREE_TRADES_PER_MONTH = 50
COMMISSION_FLAT_EGP = 2.0
COMMISSION_RATE = 0.001
CGT_RATE = 0.10
SUBSCRIPTION_EGP_PER_MONTH = 245.0


@dataclass
class CostModel:
    """Tracks the monthly free-trade allowance across a backtest."""

    free_trades_per_month: int = FREE_TRADES_PER_MONTH
    subscription_egp: float = SUBSCRIPTION_EGP_PER_MONTH
    cgt_rate: float = CGT_RATE
    _executions: dict[tuple[int, int], int] = field(default_factory=dict)

    def commission(self, when: dt.date, notional: float) -> float:
        """Fee for one execution, consuming the month's allowance."""
        key = (when.year, when.month)
        used = self._executions.get(key, 0)
        self._executions[key] = used + 1
        if used < self.free_trades_per_month:
            return 0.0
        return COMMISSION_FLAT_EGP + notional * COMMISSION_RATE

    def executions_in(self, when: dt.date) -> int:
        return self._executions.get((when.year, when.month), 0)

    def months_spanned(self, start: dt.date, end: dt.date) -> int:
        return max(1, (end.year - start.year) * 12 + (end.month - start.month) + 1)

    def subscription_cost(self, start: dt.date, end: dt.date) -> float:
        return self.months_spanned(start, end) * self.subscription_egp

    def tax_on(self, realised_gain: float) -> float:
        """CGT applies to gains only. Losses do not generate a refund here."""
        return max(0.0, realised_gain) * self.cgt_rate

    def overrun_months(self) -> dict[tuple[int, int], int]:
        """Months that blew through the free allowance, and by how much."""
        return {
            k: v - self.free_trades_per_month
            for k, v in self._executions.items()
            if v > self.free_trades_per_month
        }
