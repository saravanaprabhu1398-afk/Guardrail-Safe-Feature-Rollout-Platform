from __future__ import annotations

import os
from typing import Any

import streamlit as st

from guardrail.ui.client import GuardrailApiClient, GuardrailApiError
from guardrail.ui.presenters import decision_rows, health_rows, metric_chart_rows

DEFAULT_API_URL = os.getenv("GUARDRAIL_API_URL", "http://localhost:8000")


def _client() -> GuardrailApiClient:
    return GuardrailApiClient(st.session_state.get("api_url", DEFAULT_API_URL))


def _remember_experiment(experiment: dict[str, Any]) -> None:
    st.session_state["experiment_id"] = experiment["experiment_id"]
    st.session_state["experiment"] = experiment


def _show_error(exc: GuardrailApiError) -> None:
    if exc.status_code == 404:
        st.warning(str(exc))
    else:
        st.error(str(exc))


def _experiment_tab(experiment_id: str) -> None:
    st.subheader("Experiment setup")
    st.caption("Create a draft, launch the initial 5% allocation, and inspect its state.")
    with st.form("create_experiment"):
        identifier = st.text_input("Experiment ID", value=experiment_id)
        name = st.text_input("Name", value="Personalized restaurant ranking")
        hypothesis = st.text_area(
            "Hypothesis",
            value="Personalized ranking increases completed orders per exposed user.",
        )
        primary_metric = st.text_input("Primary metric", value="order_conversion_rate")
        submitted = st.form_submit_button("Create draft", type="primary")
    if submitted:
        try:
            experiment = _client().create_experiment(
                {
                    "experiment_id": identifier,
                    "name": name,
                    "hypothesis": hypothesis,
                    "primary_metric": primary_metric,
                }
            )
            _remember_experiment(experiment)
            st.success(f"Created {identifier} in DRAFT.")
        except GuardrailApiError as exc:
            _show_error(exc)

    left, right = st.columns(2)
    if left.button("Load experiment", use_container_width=True):
        try:
            _remember_experiment(_client().get_experiment(identifier))
        except GuardrailApiError as exc:
            _show_error(exc)
    if right.button("Launch at 5%", type="primary", use_container_width=True):
        try:
            experiment = _client().launch_experiment(identifier)
            _remember_experiment(experiment)
            st.success(f"{identifier} is now {experiment['state']}.")
        except GuardrailApiError as exc:
            _show_error(exc)

    experiment = st.session_state.get("experiment")
    if experiment:
        columns = st.columns(4)
        columns[0].metric("State", experiment["state"])
        columns[1].metric("Treatment", f"{experiment['treatment_percentage']}%")
        columns[2].metric("Allocation version", experiment["allocation_version"])
        columns[3].metric("Primary metric", experiment["primary_metric"])


def _health_tab(experiment_id: str) -> None:
    st.subheader("Integrity and freshness")
    st.caption("Seven version-scoped checks gate every statistical and rollout decision.")
    if st.button("Run health checks", type="primary"):
        try:
            st.session_state["health_report"] = _client().get_health(experiment_id)
        except GuardrailApiError as exc:
            _show_error(exc)
    report = st.session_state.get("health_report")
    if not report:
        st.info("Run the checks to load Snowflake-backed integrity evidence.")
        return
    status = "BLOCKED" if report["analysis_blocked"] else "HEALTHY"
    if report["analysis_blocked"]:
        st.error(f"Analysis status: {status}")
    else:
        st.success(f"Analysis status: {status}")
    first, second = st.columns(2)
    first.metric("Allocation version", report["allocation_version"])
    second.metric("Expected treatment", f"{report['expected_treatment_percentage']}%")
    st.dataframe(health_rows(report), hide_index=True, use_container_width=True)


def _results_tab(experiment_id: str) -> None:
    st.subheader("Metrics and statistical evidence")
    st.caption("Descriptive estimates stay visible; integrity and power determine readiness.")
    if st.button("Analyze experiment", type="primary"):
        try:
            st.session_state["analysis_report"] = _client().get_results(experiment_id)
        except GuardrailApiError as exc:
            _show_error(exc)
    report = st.session_state.get("analysis_report")
    if not report:
        st.info("Analyze the experiment to load its immutable snapshot-backed result.")
        return
    top = st.columns(4)
    top[0].metric("Status", report["status"])
    top[1].metric("Allocation version", report["allocation_version"])
    top[2].metric("Metrics", len(report["metrics"]))
    top[3].metric("Blocking checks", len(report["blocking_checks"]))
    st.caption(
        f"Health snapshot: {report.get('health_snapshot_id') or 'not persisted'} · "
        f"Analysis snapshot: {report.get('analysis_snapshot_id') or 'not persisted'}"
    )
    if report["analysis_blocked"]:
        st.error(
            "Statistical decision is blocked; metric evidence was not allowed to drive rollout."
        )
    for metric in report["metrics"]:
        with st.container(border=True):
            st.markdown(f"#### {metric['metric_name']}")
            summary = st.columns(4)
            summary[0].metric("Status", metric["status"])
            summary[1].metric("Absolute lift", _number(metric.get("absolute_lift")))
            summary[2].metric("p-value", _number(metric.get("p_value"), digits=4))
            summary[3].metric("Estimated power", _percent(metric.get("estimated_power")))
            st.bar_chart(metric_chart_rows(metric), x="variant", y="estimate")
            st.caption(metric["message"])


def _decisions_tab(experiment_id: str) -> None:
    st.subheader("Rollout decision history")
    st.caption("Every recommendation retains policy, evidence, approver, and snapshot lineage.")
    if st.button("Refresh decision history"):
        try:
            st.session_state["decisions"] = _client().get_decisions(experiment_id)
        except GuardrailApiError as exc:
            _show_error(exc)
    decisions = st.session_state.get("decisions", [])
    if decisions:
        st.dataframe(decision_rows(decisions), hide_index=True, use_container_width=True)
    else:
        st.info("No persisted rollout decisions have been loaded.")

    with st.form("evaluate_transition"):
        requested_by = st.text_input("Requested by", value="guardrail-ui")
        approved_by = st.text_input("Approved by", value="product-owner")
        submitted = st.form_submit_button("Evaluate and apply", type="primary")
    if submitted:
        try:
            outcome = _client().apply_transition(
                experiment_id,
                requested_by=requested_by,
                approved_by=approved_by,
            )
            st.session_state["experiment"] = outcome["experiment"]
            st.session_state["decisions"] = _client().get_decisions(experiment_id)
            decision = outcome["decision"]
            st.success(
                f"{decision['decision']}: {decision['previous_state']} → "
                f"{decision['recommended_state']}"
            )
            st.caption(decision["decision_reason"])
        except GuardrailApiError as exc:
            _show_error(exc)


def _number(value: float | None, *, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def main() -> None:
    st.set_page_config(page_title="Guardrail Control", page_icon="🛡️", layout="wide")
    st.title("Guardrail Control")
    st.caption("Explainable experimentation and progressive-delivery decisions")

    with st.sidebar:
        st.header("Connection")
        st.session_state["api_url"] = st.text_input("Guardrail API", value=DEFAULT_API_URL)
        experiment_id = st.text_input(
            "Active experiment",
            value=st.session_state.get("experiment_id", "new_ranking_algorithm"),
        )
        st.session_state["experiment_id"] = experiment_id
        st.divider()
        st.caption("Decision flow")
        st.markdown("Telemetry → integrity → statistics → guardrails → rollout")

    tabs = st.tabs(["Experiment", "Health", "Results", "Decisions"])
    with tabs[0]:
        _experiment_tab(experiment_id)
    with tabs[1]:
        _health_tab(experiment_id)
    with tabs[2]:
        _results_tab(experiment_id)
    with tabs[3]:
        _decisions_tab(experiment_id)


if __name__ == "__main__":
    main()
