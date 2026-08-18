from __future__ import annotations

import argparse
import json

from guardrail.services.demo import DemoScenario, run_demo


def run_phase6_demo() -> None:
    parser = argparse.ArgumentParser(
        description="Run deterministic Guardrail Phase 6 portfolio scenarios."
    )
    parser.add_argument(
        "--scenario",
        choices=["all", *(scenario.value for scenario in DemoScenario)],
        default="all",
    )
    arguments = parser.parse_args()
    scenarios = (
        tuple(DemoScenario) if arguments.scenario == "all" else (DemoScenario(arguments.scenario),)
    )
    runs = tuple(run_demo(scenario) for scenario in scenarios)
    print(json.dumps([run.model_dump(mode="json") for run in runs], indent=2))
    if not all(run.passed for run in runs):
        raise SystemExit(1)
