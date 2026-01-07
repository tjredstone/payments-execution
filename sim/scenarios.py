import random
from typing import List, Tuple, Dict, Any

from sim.engine import IncomeEvent, Obligation


def make_standard_household_scenario(
    *,
    months: int = 6,
    days_per_month: int = 30,
    salary_amount: int = 2200,
    salary_day_in_month: int = 26,  # “pay day”
    salary_jitter_days: int = 2,  # pay day can move +/- this many days
    start_balance: int = 0,
) -> Dict[str, Any]:
    """
    Returns a dict of scenario inputs that BOTH engines can use.
    This contains only inputs (no execution logic).

    Notes:
    - We simulate 'months' each with 'days_per_month' days (simple model).
    - Salary arrives once per month with optional jitter.
    - Obligations are due on the last day of each month by default in this scenario.
    """

    income: List[IncomeEvent] = []
    obligations: List[Obligation] = []

    for m in range(months):
        base = m * days_per_month

        # Salary day can move a bit each month
        jitter = random.randint(-salary_jitter_days, salary_jitter_days)
        salary_day = base + salary_day_in_month + jitter
        income.append(IncomeEvent(day=salary_day, amount=salary_amount))

        # Example obligations (edit freely)
        due = base + days_per_month  # last day of the month
        obligations.extend(
            [
                Obligation(name="rent", due_day=due, amount=900),
                Obligation(name="council_tax", due_day=due, amount=140),
                Obligation(name="credit_card", due_day=due, amount=300),
            ]
        )

    return {
        "start_balance": start_balance,
        "start_day": 1,
        "end_day": months * days_per_month,
        "income": income,
        "obligations": obligations,
        # calendar config can live here later if you want
        "calendar": {
            "weekends_block_execution": True,
            "bank_holidays": [],  # we can add e.g. [30, 60, ...] later
        },
        "meta": {
            "months": months,
            "days_per_month": days_per_month,
            "salary_amount": salary_amount,
            "salary_day_in_month": salary_day_in_month,
            "salary_jitter_days": salary_jitter_days,
        },
    }
