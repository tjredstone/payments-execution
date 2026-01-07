from dataclasses import dataclass, field
from typing import List, Optional


# =========================
# Models
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
class Result:
    on_time: int = 0
    failed: int = 0
    lowest_balance: int = field(default_factory=lambda: 10**9)
    trace: List[str] = field(default_factory=list)


# =========================
# Calendar abstraction
# =========================


def can_execute(day: int) -> bool:
    """
    v1.1 calendar rules:
    - weekends unavailable
    - no bank holidays yet (handled in baseline engine)
    """
    return day % 7 not in (6, 0)


# =========================
# Execution-aware engine (v1.1)
# =========================


def run_simulation(
    *,
    income: List[IncomeEvent],
    obligations: List[Obligation],
    start_balance: int,
    start_day: int,
    end_day: int,
    verbose: bool = False,
) -> Result:
    """
    Execution-aware payment scheduler.

    Guarantees:
    - no late payments when funds exist
    - no negative balances
    - early payment allowed
    - execution only when necessary (last safe moment)
    """

    balance = start_balance
    result = Result()

    # Aggregate income by day
    income_by_day = {}
    for inc in income:
        income_by_day.setdefault(inc.day, 0)
        income_by_day[inc.day] += inc.amount

    for day in range(start_day, end_day + 1):

        # Income lands
        if day in income_by_day:
            balance += income_by_day[day]
            if verbose:
                result.trace.append(
                    f"Day {day}: salary +£{income_by_day[day]} → balance £{balance}"
                )

        # Obligations processed in due-date order
        for ob in sorted(obligations, key=lambda o: o.due_day):

            if ob.paid or ob.failed:
                continue

            # If we've passed the due date without paying, this is a failure
            if day > ob.due_day:
                ob.failed = True
                result.failed += 1
                if verbose:
                    result.trace.append(
                        f"Day {day}: ❌ FAILED {ob.name} "
                        f"(due {ob.due_day}, £{ob.amount})"
                    )
                continue

            # Cannot execute today (weekend)
            if not can_execute(day):
                continue

            # Find remaining executable days up to due date
            future_exec_days = [
                d for d in range(day + 1, ob.due_day + 1) if can_execute(d)
            ]

            # Determine whether today is the last safe execution opportunity
            if future_exec_days:
                last_exec_day = future_exec_days[-1]
                future_income = sum(
                    i.amount for i in income if day < i.day <= last_exec_day
                )
                last_safe = (balance + future_income - ob.amount) < 0
            else:
                # No future execution opportunities
                last_safe = True

            # If today is the last safe moment, act
            if last_safe:
                if balance - ob.amount < 0:
                    ob.failed = True
                    result.failed += 1
                    if verbose:
                        result.trace.append(
                            f"Day {day}: ❌ FAILED {ob.name} "
                            f"(£{ob.amount}, balance £{balance})"
                        )
                else:
                    balance -= ob.amount
                    ob.paid = True
                    ob.paid_day = day
                    result.on_time += 1

                    if verbose:
                        delta = ob.due_day - day
                        timing = "on due date" if delta == 0 else f"{delta} days early"
                        result.trace.append(
                            f"Day {day}: ✅ PAID {ob.name} "
                            f"(£{ob.amount}, due {ob.due_day}, {timing}) "
                            f"→ balance £{balance}"
                        )

        result.lowest_balance = min(result.lowest_balance, balance)

    return result
