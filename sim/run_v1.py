from sim.scenarios import make_standard_household_scenario
from sim.engine import run_simulation  # whatever your function is called

scenario = make_standard_household_scenario()

result = run_simulation(
    income=scenario["income"],
    obligations=scenario["obligations"],
    start_balance=scenario["start_balance"],
    start_day=scenario["start_day"],
    end_day=scenario["end_day"],
    verbose=True,
)

print(result)
