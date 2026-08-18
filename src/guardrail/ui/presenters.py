from __future__ import annotations

from typing import Any


def health_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "check": check.get("check_name"),
            "status": check.get("status"),
            "severity": check.get("severity"),
            "blocks analysis": check.get("blocks_analysis"),
            "observed": check.get("observed_value"),
            "expected": check.get("expected_range"),
            "action": check.get("recommended_action"),
            "message": check.get("message"),
        }
        for check in report.get("checks", [])
    ]


def metric_chart_rows(metric: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"variant": "control", "estimate": metric.get("control_estimate")},
        {"variant": "treatment", "estimate": metric.get("treatment_estimate")},
    ]


def decision_rows(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "created at": decision.get("created_at"),
            "decision": decision.get("decision"),
            "from": decision.get("previous_state"),
            "recommended": decision.get("recommended_state"),
            "analysis": decision.get("analysis_status"),
            "policy": decision.get("policy_version"),
            "reason": decision.get("decision_reason"),
        }
        for decision in decisions
    ]
