from sim.scenarios import make_standard_household_scenario
from sim.baseline_engine import run_baseline_simulation


def main():
    # Build the shared scenario
    scenario = make_standard_household_scenario()

    # Run the baseline (current system) simulation
    result = run_baseline_simulation(
        income=scenario["income"],
        obligations=scenario["obligations"],
        start_balance=scenario["start_balance"],
        start_day=scenario["start_day"],
        end_day=scenario["end_day"],
        bank_holidays=scenario["calendar"]["bank_holidays"],
        verbose=True,
    )

    # Print trace
    for line in result.trace:
        print(line)

    # Print summary
    print("\n=== Baseline System Simulation ===")
    print(f"On-time payments: {result.on_time}")
    print(f"Late payments: {result.late}")
    print(f"Failed payments: {result.failed}")
    print(f"Total days late: {result.total_days_late}")
    print(f"Lowest balance observed: £{result.lowest_balance}")


if __name__ == "__main__":
    main()
