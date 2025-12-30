import random
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


# =========================
# Calendar abstraction
# =========================


def can_execute(day: int) -> bool:
    # v1: weekends unavailable
    return day % 7 not in (6, 0)


# =========================
# Engine
# =========================


def run_simulation(
    income: List[IncomeEvent],
    obligations: List[Obligation],
    start_balance: int,
    start_day: int,
    end_day: int,
    verbose: bool = False,
) -> Result:

    balance = start_balance
    result = Result()

    income_by_day = {}
    for inc in income:
        income_by_day.setdefault(inc.day, 0)
        income_by_day[inc.day] += inc.amount

    for day in range(start_day, end_day + 1):

        # Income lands
        if day in income_by_day:
            balance += income_by_day[day]
            if verbose:
                print(f"Day {day}: salary +£{income_by_day[day]} → balance £{balance}")

        for ob in sorted(obligations, key=lambda o: o.due_day):
            if ob.paid or ob.failed:
                continue

            if day > ob.due_day:
                # missed deadline
                ob.failed = True
                result.failed += 1
                if verbose:
                    print(
                        f"Day {day}: ❌ FAILED {ob.name} "
                        f"(due {ob.due_day}, £{ob.amount})"
                    )
                continue

            if not can_execute(day):
                continue

            # find future executable days
            future_exec_days = [
                d for d in range(day + 1, ob.due_day + 1) if can_execute(d)
            ]

            # determine if today is last safe chance
            if future_exec_days:
                future_income = sum(
                    i.amount for i in income if day < i.day <= future_exec_days[-1]
                )
                last_safe = (balance + future_income - ob.amount) < 0
            else:
                last_safe = True  # no more chances

            if last_safe:
                if balance - ob.amount < 0:
                    ob.failed = True
                    result.failed += 1
                    if verbose:
                        print(
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
                        print(
                            f"Day {day}: ✅ PAID {ob.name} "
                            f"(£{ob.amount}, due {ob.due_day}, {timing}) "
                            f"→ balance £{balance}"
                        )

        result.lowest_balance = min(result.lowest_balance, balance)

    return result


# =========================
# Scenario
# =========================


def run():

    DAYS = 30
    MONTHS = 6
    SALARY = 2200

    balance = 0

    income = []
    obligations = []

    for m in range(MONTHS):
        base = m * DAYS
        salary_day = base + 25 + random.choice([-2, -1, 0, 1, 2])
        income.append(IncomeEvent(salary_day, SALARY))

        obligations.extend(
            [
                Obligation("rent", base + 30, 900),
                Obligation("council_tax", base + 30, 140),
                Obligation("credit_card", base + 30, 300),
            ]
        )

    result = run_simulation(
        income=income,
        obligations=obligations,
        start_balance=balance,
        start_day=1,
        end_day=MONTHS * DAYS,
        verbose=True,
    )

    print("\n=== Optimal Execution Simulation ===")
    print(f"On-time payments: {result.on_time}")
    print(f"Failures: {result.failed}")
    print(f"Lowest balance: £{result.lowest_balance}")


if __name__ == "__main__":
    run()
