from __future__ import annotations

import pytest

from guardrail.domain.experiments import RolloutState
from guardrail.domain.rollout import RolloutDecision
from guardrail.services.demo import DemoScenario, run_demo


@pytest.mark.parametrize(
    ("scenario", "decision", "state"),
    [
        (DemoScenario.HEALTHY_ROLLOUT, RolloutDecision.RAMP, RolloutState.RUNNING_25),
        (DemoScenario.TELEMETRY_CORRUPTION, RolloutDecision.PAUSE, RolloutState.PAUSED),
        (DemoScenario.HARMFUL_GUARDRAIL, RolloutDecision.PAUSE, RolloutState.PAUSED),
    ],
)
def test_phase6_demo_scenarios_are_reproducible(
    scenario: DemoScenario,
    decision: RolloutDecision,
    state: RolloutState,
) -> None:
    first = run_demo(scenario)
    second = run_demo(scenario)

    assert first.passed is True
    assert first.outcome.decision.decision == decision
    assert first.outcome.experiment.state == state
    assert first.outcome.decision.decision_id == second.outcome.decision.decision_id
    assert first.outcome.transition.transition_id == second.outcome.transition.transition_id


def test_harmful_demo_records_all_failed_product_guardrails() -> None:
    run = run_demo(DemoScenario.HARMFUL_GUARDRAIL)

    failed = {
        check.check_name
        for check in run.outcome.decision.guardrail_checks
        if check.status == "FAIL"
    }
    assert failed == {
        "cancellation_rate",
        "payment_failure_rate",
        "average_delivery_minutes",
    }
