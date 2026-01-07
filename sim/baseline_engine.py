from dataclasses import dataclass
from typing import List, Optional, Dict


# =========================
# Data models (mirrors engine.py)
# =========================


@dataclass
class IncomeEvent:
    day: int
    amount: int


@dataclass
class Obligation:
    name: str
    due_day: int
    amount: int
    paid: bool = False
    paid_day: Optional[int] = None
    failed: bool = False


@dataclass
class BaselineResult:
    on_time: int = 0
    late: int = 0
    failed: int = 0
    total_days_late: int = 0
    lowest_balance: int = 10**9
    trace: List[str] = None


# =========================
# Calendar helpers
# =========================


def is_weekend(day: int) -> bool:
    # Simple repeating 7-day model
    # Day 6 = Saturday, Day 0 = Sunday
    return day % 7 in (6, 0)


def next_working_day(day: int, bank_holidays: List[int]) -> int:
    d = day
    while is_weekend(d) or d in bank_holidays:
        d += 1
    return d


# =========================
# Baseline execution engine
# =========================


def run_baseline_simulation(
    *,
    income: List[IncomeEvent],
    obligations: List[Obligation],
    start_balance: int,
    start_day: int,
    end_day: int,
    bank_holidays: Optional[List[int]] = None,
    verbose: bool = False,
) -> BaselineResult:
    """
    Simulates how payments typically work today:
    - payments attempted on due date
    - if due date is non-working, deferred to next working day
    - no early payment
    - no foresight
    - no optimisation
    """

    if bank_holidays is None:
        bank_holidays = []

    balance = start_balance
    result = BaselineResult(trace=[])

    income_by_day: Dict[int, int] = {}
    for inc in income:
        income_by_day.setdefault(inc.day, 0)
        income_by_day[inc.day] += inc.amount

    # Precompute actual execution day for each obligation
    execution_days: Dict[int, List[Obligation]] = {}
    for ob in obligations:
        exec_day = next_working_day(ob.due_day, bank_holidays)
        execution_days.setdefault(exec_day, []).append(ob)

    for day in range(start_day, end_day + 1):

        # Income lands
        if day in income_by_day:
            balance += income_by_day[day]
            if verbose:
                result.trace.append(
                    f"Day {day}: salary +£{income_by_day[day]} → balance £{balance}"
                )

        # Attempt payments scheduled for today
        for ob in execution_days.get(day, []):

            if balance >= ob.amount:
                balance -= ob.amount
                ob.paid = True
                ob.paid_day = day

                if day == ob.due_day:
                    result.on_time += 1
                    status = "on due date"
                else:
                    result.late += 1
                    days_late = day - ob.due_day
                    result.total_days_late += days_late
                    status = f"{days_late} days late"

                if verbose:
                    result.trace.append(
                        f"Day {day}: {'⚠️' if day > ob.due_day else '✅'} "
                        f"PAID {ob.name} (£{ob.amount}, due {ob.due_day}, {status}) "
                        f"→ balance £{balance}"
                    )

            else:
                # Payment attempt fails (insufficient funds)
                ob.failed = True
                result.failed += 1

                if verbose:
                    result.trace.append(
                        f"Day {day}: ❌ FAILED {ob.name} (£{ob.amount}, due {ob.due_day}) "
                        f"→ balance £{balance}"
                    )

        result.lowest_balance = min(result.lowest_balance, balance)

    return result
