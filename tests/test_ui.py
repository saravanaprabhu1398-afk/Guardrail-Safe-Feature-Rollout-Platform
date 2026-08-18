from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
from streamlit.testing.v1 import AppTest

from guardrail.ui.client import GuardrailApiClient, GuardrailApiError
from guardrail.ui.presenters import decision_rows, health_rows, metric_chart_rows

ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def test_ui_client_builds_encoded_requests_and_payloads() -> None:
    requests = []

    def opener(request, *, timeout: float):
        requests.append((request, timeout))
        return FakeResponse({"experiment_id": "ranking test"})

    client = GuardrailApiClient("http://api:8000/", opener=opener)

    client.apply_transition(
        "ranking test",
        requested_by="guardrail-ui",
        approved_by="product-owner",
    )

    request, timeout = requests[0]
    assert request.full_url == "http://api:8000/experiments/ranking%20test/transitions"
    assert request.method == "POST"
    assert json.loads(request.data) == {
        "requested_by": "guardrail-ui",
        "approved_by": "product-owner",
    }
    assert timeout == 30.0


def test_ui_client_surfaces_safe_api_error_detail() -> None:
    def opener(request, *, timeout: float):
        raise HTTPError(
            request.full_url,
            409,
            "Conflict",
            {},
            io.BytesIO(json.dumps({"detail": "Cannot ramp from PAUSED."}).encode()),
        )

    with pytest.raises(GuardrailApiError, match="Cannot ramp from PAUSED") as error:
        GuardrailApiClient("http://api", opener=opener).launch_experiment("ranking")

    assert error.value.status_code == 409


def test_ui_presenters_shape_health_metrics_and_decision_history() -> None:
    health = health_rows(
        {
            "checks": [
                {
                    "check_name": "event_loss",
                    "status": "FAIL",
                    "severity": "CRITICAL",
                    "blocks_analysis": True,
                    "observed_value": 0.2,
                    "expected_range": "<= 0.05",
                    "recommended_action": "PAUSE",
                    "message": "Event loss exceeds policy.",
                }
            ]
        }
    )
    chart = metric_chart_rows({"control_estimate": 0.4, "treatment_estimate": 0.5})
    decisions = decision_rows(
        [
            {
                "created_at": "2026-08-18T10:00:00Z",
                "decision": "RAMP",
                "previous_state": "RUNNING_5",
                "recommended_state": "RUNNING_25",
                "analysis_status": "COMPLETE",
                "policy_version": "phase5-rollout-v1",
                "decision_reason": "Evidence passed.",
            }
        ]
    )

    assert health[0]["check"] == "event_loss"
    assert health[0]["blocks analysis"] is True
    assert chart == [
        {"variant": "control", "estimate": 0.4},
        {"variant": "treatment", "estimate": 0.5},
    ]
    assert decisions[0]["recommended"] == "RUNNING_25"


def test_streamlit_control_app_renders_all_phase6_workspaces() -> None:
    app = AppTest.from_file(str(ROOT / "src" / "guardrail" / "ui" / "app.py"))

    app.run(timeout=15)

    assert not app.exception
    assert [title.value for title in app.title] == ["Guardrail Control"]
    assert [tab.label for tab in app.tabs] == ["Experiment", "Health", "Results", "Decisions"]
    assert {button.label for button in app.button} >= {
        "Create draft",
        "Run health checks",
        "Analyze experiment",
        "Evaluate and apply",
    }
