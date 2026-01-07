import random
from sim.engine_v1_1 import IncomeEvent, Obligation


def make_standard_household_scenario(
    salary: int = 2200,
    rent: int = 900,
    council_tax: int = 140,
    credit_card: int = 300,
    months: int = 6,
):
    """
    Builds a simple, realistic household scenario.

    - Salary arrives once per month with small timing variance
    - Obligations fall at the end of each month
    - No behavioural assumptions
    """

    DAYS_PER_MONTH = 30

    income = []
    obligations = []

    start_balance = 0
    start_day = 1
    end_day = months * DAYS_PER_MONTH

    for m in range(months):
        base = m * DAYS_PER_MONTH

        # Salary day jitter (realistic payroll variance)
        salary_day = base + 25 + random.choice([-2, -1, 0, 1, 2])
        income.append(IncomeEvent(day=salary_day, amount=salary))

        # Fixed obligations
        obligations.extend(
            [
                Obligation("rent", base + 30, rent),
                Obligation("council_tax", base + 30, council_tax),
                Obligation("credit_card", base + 30, credit_card),
            ]
        )

    scenario = {
        "income": income,
        "obligations": obligations,
        "start_balance": start_balance,
        "start_day": start_day,
        "end_day": end_day,
        "calendar": {"bank_holidays": []},  # placeholder for future realism
        "meta": {
            "months": months,
            "salary": salary,
            "rent": rent,
            "council_tax": council_tax,
            "credit_card": credit_card,
        },
    }

    return scenario
