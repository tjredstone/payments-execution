from sim.scenarios import make_standard_household_scenario
from sim.engine_v1_1 import run_simulation as run_execution_aware
from sim.baseline_engine import run_baseline_simulation


def run_comparison(
    salary: int = 2200,
    rent: int = 900,
    council_tax: int = 140,
    credit_card: int = 300,
    months: int = 6,
    verbose: bool = True,
):
    """
    Runs the same scenario through:
    - the baseline (current system)
    - the execution-aware system (v1)

    Parameters are explicit so this can be driven by:
    - CLI
    - Flask UI
    - tests later

    Returns a dict suitable for:
    - CLI inspection
    - Flask rendering
    - JSON output later
    """

    # Build shared scenario
    scenario = make_standard_household_scenario(
        salary=salary,
        rent=rent,
        council_tax=council_tax,
        credit_card=credit_card,
        months=months,
    )

    # --- Baseline ---
    baseline_result = run_baseline_simulation(
        income=scenario["income"],
        obligations=[
            # fresh copies so engines don't interfere
            o.__class__(**vars(o))
            for o in scenario["obligations"]
        ],
        start_balance=scenario["start_balance"],
        start_day=scenario["start_day"],
        end_day=scenario["end_day"],
        bank_holidays=scenario["calendar"]["bank_holidays"],
        verbose=verbose,
    )

    # --- Execution-aware ---
    execution_result = run_execution_aware(
        income=scenario["income"],
        obligations=[o.__class__(**vars(o)) for o in scenario["obligations"]],
        start_balance=scenario["start_balance"],
        start_day=scenario["start_day"],
        end_day=scenario["end_day"],
        verbose=verbose,
    )

    return {
        "scenario_meta": scenario["meta"],
        "baseline": baseline_result,
        "execution_aware": execution_result,
    }


def print_summary(comparison):
    b = comparison["baseline"]
    e = comparison["execution_aware"]

    print("\n=== Comparison Summary ===\n")

    print("Baseline (current system):")
    print(f"  On-time payments: {b.on_time}")
    print(f"  Late payments: {b.late}")
    print(f"  Failed payments: {b.failed}")
    print(f"  Total days late: {b.total_days_late}")
    print(f"  Lowest balance: £{b.lowest_balance}")

    print("\nExecution-aware system (v1):")
    print(f"  On-time payments: {e.on_time}")
    print(f"  Failed payments: {e.failed}")
    print(f"  Lowest balance: £{e.lowest_balance}")

    print("\nNet difference:")
    print(f"  Late payments avoided: {b.late}")
    print(f"  Days late avoided: {b.total_days_late}")
    print(f"  Failures avoided: {b.failed - e.failed}")


def main():
    # Default CLI run
    comparison = run_comparison(verbose=True)

    print("\n--- Baseline trace ---\n")
    for line in comparison["baseline"].trace:
        print(line)

    print("\n--- Execution-aware trace ---\n")
    for line in comparison["execution_aware"].trace:
        print(line)

    print_summary(comparison)


if __name__ == "__main__":
    main()
