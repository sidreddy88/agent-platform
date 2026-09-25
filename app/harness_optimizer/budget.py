"""
Hard spending cap, enforced in code, never left to the model.

Before an evaluation starts, the optimizer asks for a reservation sized by
the expected cost (per-case cost measured so far x cases x trials, with a
safety margin). If the reservation would take total spend past the cap, the
evaluation doesn't start and the run stops with a clear reason, rather than
discovering the overrun (or an empty credit balance, twice this week) halfway
through. Actual spend comes from the cost meter and is written into the run
state, so a resumed run knows what it has already spent.
"""
from __future__ import annotations

from dataclasses import dataclass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Budget:
    cap_usd: float
    spent_usd: float = 0.0
    margin: float = 1.25            # reserve 25% above the estimate

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.spent_usd)

    def reserve(self, estimate_usd: float, what: str) -> None:
        need = estimate_usd * self.margin
        if self.spent_usd + need > self.cap_usd:
            raise BudgetExceeded(
                f"{what}: needs ~${need:.2f} (estimate ${estimate_usd:.2f} x {self.margin} margin), "
                f"but only ${self.remaining_usd:.2f} of the ${self.cap_usd:.2f} cap remains"
            )

    def record(self, actual_usd: float) -> None:
        self.spent_usd += actual_usd


def estimate_eval_cost(n_cases: int, trials: int, per_case_usd: float) -> float:
    return n_cases * trials * per_case_usd
