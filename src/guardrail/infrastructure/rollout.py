from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC
from typing import Any

from guardrail.config import SnowflakeSettings
from guardrail.domain.analysis import (
    AnalysisEvidenceError,
    AnalysisSnapshotError,
    ExperimentAnalysisStatus,
)
from guardrail.domain.experiments import ExperimentCreate, RolloutState
from guardrail.domain.integrity import IntegrityEvidenceError
from guardrail.domain.metrics import MetricArtifactPersistenceError
from guardrail.domain.rollout import (
    DecisionEvidenceError,
    DecisionPersistenceError,
    GuardrailCheckResult,
    InvalidRolloutTransitionError,
    RolloutDecision,
    RolloutDecisionRecord,
    RolloutGuardrailEvidence,
    RolloutPolicyError,
    StateTransitionRecord,
    TransitionRequest,
)
from guardrail.infrastructure.analysis import SnowflakeAnalysisRepository
from guardrail.infrastructure.integrity import SnowflakeIntegrityRepository
from guardrail.infrastructure.metrics import SnowflakeMetricArtifactRepository
from guardrail.infrastructure.snapshots import SnowflakeAnalysisSnapshotRepository
from guardrail.infrastructure.snowflake import connect
from guardrail.services.analysis import ExperimentAnalysisService
from guardrail.services.integrity import IntegrityService
from guardrail.services.rollout import RolloutPolicy, RolloutPolicyEvaluator, RolloutService
from guardrail.services.store import InMemoryExperimentStore

DECISION_EVIDENCE_QUERY = """
SELECT
    MAX(IFF(variant_id = 'control', cancellation_user_rate, NULL)),
    MAX(IFF(variant_id = 'treatment', cancellation_user_rate, NULL)),
    MAX(IFF(variant_id = 'control', payment_failure_user_rate, NULL)),
    MAX(IFF(variant_id = 'treatment', payment_failure_user_rate, NULL)),
    MAX(IFF(variant_id = 'control', average_delivery_minutes, NULL)),
    MAX(IFF(variant_id = 'treatment', average_delivery_minutes, NULL))
FROM GUARDRAIL.METRICS.AGG_EXPERIMENT_RESULTS
WHERE experiment_id = %s
  AND allocation_version = %s
"""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _variant(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _aware(value: Any) -> Any:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class SnowflakeDecisionEvidenceRepository:
    def __init__(self, settings: SnowflakeSettings) -> None:
        self._settings = settings.model_copy(
            update={
                "snowflake_warehouse": settings.snowflake_transform_warehouse,
                "snowflake_schema": "METRICS",
            }
        )

    def get_evidence(self, experiment_id: str, allocation_version: int) -> RolloutGuardrailEvidence:
        try:
            with connect(self._settings) as connection:
                row = (
                    connection.cursor()
                    .execute(DECISION_EVIDENCE_QUERY, (experiment_id, allocation_version))
                    .fetchone()
                )
        except Exception as exc:
            raise DecisionEvidenceError("Rollout guardrail evidence is unavailable.") from exc
        if row is None:
            raise DecisionEvidenceError("Rollout guardrail evidence query returned no row.")
        try:
            return RolloutGuardrailEvidence(
                experiment_id=experiment_id,
                allocation_version=allocation_version,
                control_cancellation_rate=row[0],
                treatment_cancellation_rate=row[1],
                control_payment_failure_rate=row[2],
                treatment_payment_failure_rate=row[3],
                control_delivery_minutes=row[4],
                treatment_delivery_minutes=row[5],
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise DecisionEvidenceError(
                "Rollout guardrail evidence returned an invalid row."
            ) from exc


class SnowflakeDecisionRepository:
    def __init__(self, settings: SnowflakeSettings) -> None:
        self._settings = settings.model_copy(
            update={
                "snowflake_warehouse": settings.snowflake_transform_warehouse,
                "snowflake_schema": "CONTROL",
            }
        )

    def persist_outcome(
        self,
        decision: RolloutDecisionRecord,
        transition: StateTransitionRecord,
    ) -> tuple[RolloutDecisionRecord, StateTransitionRecord]:
        try:
            with connect(self._settings) as connection:
                connection.autocommit(False)
                try:
                    connection.cursor().execute(
                        """
                        MERGE INTO CONTROL.ROLLOUT_DECISIONS AS target
                        USING (
                          SELECT %s AS DECISION_ID, %s AS EXPERIMENT_ID,
                                 %s AS ALLOCATION_VERSION, %s AS PREVIOUS_STATE,
                                 %s AS RECOMMENDED_STATE, %s AS DECISION,
                                 %s AS DECISION_REASON, %s AS ANALYSIS_STATUS,
                                 %s AS ANALYSIS_SNAPSHOT_ID, %s AS HEALTH_SNAPSHOT_ID,
                                 %s AS POLICY_VERSION, PARSE_JSON(%s) AS POLICY,
                                 PARSE_JSON(%s) AS GUARDRAIL_EVIDENCE,
                                 PARSE_JSON(%s) AS GUARDRAIL_CHECKS, %s AS CREATED_AT
                        ) AS source
                        ON target.DECISION_ID = source.DECISION_ID
                        WHEN NOT MATCHED THEN INSERT (
                          DECISION_ID, EXPERIMENT_ID, ALLOCATION_VERSION, PREVIOUS_STATE,
                          RECOMMENDED_STATE, DECISION, DECISION_REASON, ANALYSIS_STATUS,
                          ANALYSIS_SNAPSHOT_ID, HEALTH_SNAPSHOT_ID, POLICY_VERSION, POLICY,
                          GUARDRAIL_EVIDENCE, GUARDRAIL_CHECKS, CREATED_AT
                        ) VALUES (
                          source.DECISION_ID, source.EXPERIMENT_ID, source.ALLOCATION_VERSION,
                          source.PREVIOUS_STATE, source.RECOMMENDED_STATE, source.DECISION,
                          source.DECISION_REASON, source.ANALYSIS_STATUS,
                          source.ANALYSIS_SNAPSHOT_ID, source.HEALTH_SNAPSHOT_ID,
                          source.POLICY_VERSION, source.POLICY, source.GUARDRAIL_EVIDENCE,
                          source.GUARDRAIL_CHECKS, source.CREATED_AT
                        )
                        """,
                        (
                            decision.decision_id,
                            decision.experiment_id,
                            decision.allocation_version,
                            decision.previous_state.value,
                            decision.recommended_state.value,
                            decision.decision.value,
                            decision.decision_reason,
                            decision.analysis_status.value,
                            decision.analysis_snapshot_id,
                            decision.health_snapshot_id,
                            decision.policy_version,
                            _canonical_json(decision.policy),
                            _canonical_json(decision.guardrail_evidence.model_dump(mode="json")),
                            _canonical_json(
                                [
                                    check.model_dump(mode="json")
                                    for check in decision.guardrail_checks
                                ]
                            ),
                            decision.created_at,
                        ),
                    )
                    connection.cursor().execute(
                        """
                        MERGE INTO CONTROL.STATE_TRANSITIONS AS target
                        USING (
                          SELECT %s AS TRANSITION_ID, %s AS DECISION_ID,
                                 %s AS EXPERIMENT_ID, %s AS ALLOCATION_VERSION,
                                 %s AS PREVIOUS_STATE, %s AS NEW_STATE, %s AS DECISION,
                                 %s AS DECISION_REASON, %s AS ANALYSIS_SNAPSHOT_ID,
                                 %s AS HEALTH_SNAPSHOT_ID, %s AS POLICY_VERSION,
                                 %s AS REQUESTED_BY, %s AS APPROVED_BY, %s AS CREATED_AT
                        ) AS source
                        ON target.TRANSITION_ID = source.TRANSITION_ID
                        WHEN NOT MATCHED THEN INSERT (
                          TRANSITION_ID, DECISION_ID, EXPERIMENT_ID, ALLOCATION_VERSION,
                          PREVIOUS_STATE, NEW_STATE, DECISION, DECISION_REASON,
                          ANALYSIS_SNAPSHOT_ID, HEALTH_SNAPSHOT_ID, POLICY_VERSION,
                          REQUESTED_BY, APPROVED_BY, CREATED_AT
                        ) VALUES (
                          source.TRANSITION_ID, source.DECISION_ID, source.EXPERIMENT_ID,
                          source.ALLOCATION_VERSION, source.PREVIOUS_STATE, source.NEW_STATE,
                          source.DECISION, source.DECISION_REASON, source.ANALYSIS_SNAPSHOT_ID,
                          source.HEALTH_SNAPSHOT_ID, source.POLICY_VERSION,
                          source.REQUESTED_BY, source.APPROVED_BY, source.CREATED_AT
                        )
                        """,
                        (
                            transition.transition_id,
                            transition.decision_id,
                            transition.experiment_id,
                            transition.allocation_version,
                            transition.previous_state.value,
                            transition.new_state.value,
                            transition.decision.value,
                            transition.decision_reason,
                            transition.analysis_snapshot_id,
                            transition.health_snapshot_id,
                            transition.policy_version,
                            transition.requested_by,
                            transition.approved_by,
                            transition.created_at,
                        ),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                persisted_decision = self._get_decision(connection, decision.decision_id)
                persisted_transition = self._get_transition(connection, transition.transition_id)
        except Exception as exc:
            raise DecisionPersistenceError("Rollout decision could not be persisted.") from exc
        if persisted_decision is None or persisted_transition is None:
            raise DecisionPersistenceError("Persisted rollout outcome could not be read.")
        return persisted_decision, persisted_transition

    def list_decisions(self, experiment_id: str) -> tuple[RolloutDecisionRecord, ...]:
        try:
            with connect(self._settings) as connection:
                rows = (
                    connection.cursor()
                    .execute(
                        """
                        SELECT DECISION_ID, EXPERIMENT_ID, ALLOCATION_VERSION,
                               PREVIOUS_STATE, RECOMMENDED_STATE, DECISION,
                               DECISION_REASON, ANALYSIS_STATUS, ANALYSIS_SNAPSHOT_ID,
                               HEALTH_SNAPSHOT_ID, POLICY_VERSION, POLICY,
                               GUARDRAIL_EVIDENCE, GUARDRAIL_CHECKS, CREATED_AT
                        FROM CONTROL.ROLLOUT_DECISIONS
                        WHERE EXPERIMENT_ID = %s
                        ORDER BY CREATED_AT DESC, DECISION_ID DESC
                        """,
                        (experiment_id,),
                    )
                    .fetchall()
                )
        except Exception as exc:
            raise DecisionPersistenceError("Rollout decision history is unavailable.") from exc
        return tuple(self._decision(row) for row in rows)

    def _get_decision(self, connection: Any, decision_id: str) -> RolloutDecisionRecord | None:
        row = (
            connection.cursor()
            .execute(
                """
                SELECT DECISION_ID, EXPERIMENT_ID, ALLOCATION_VERSION,
                       PREVIOUS_STATE, RECOMMENDED_STATE, DECISION,
                       DECISION_REASON, ANALYSIS_STATUS, ANALYSIS_SNAPSHOT_ID,
                       HEALTH_SNAPSHOT_ID, POLICY_VERSION, POLICY,
                       GUARDRAIL_EVIDENCE, GUARDRAIL_CHECKS, CREATED_AT
                FROM CONTROL.ROLLOUT_DECISIONS
                WHERE DECISION_ID = %s
                """,
                (decision_id,),
            )
            .fetchone()
        )
        return self._decision(row) if row is not None else None

    def _get_transition(self, connection: Any, transition_id: str) -> StateTransitionRecord | None:
        row = (
            connection.cursor()
            .execute(
                """
                SELECT TRANSITION_ID, DECISION_ID, EXPERIMENT_ID, ALLOCATION_VERSION,
                       PREVIOUS_STATE, NEW_STATE, DECISION, DECISION_REASON,
                       ANALYSIS_SNAPSHOT_ID, HEALTH_SNAPSHOT_ID, POLICY_VERSION,
                       REQUESTED_BY, APPROVED_BY, CREATED_AT
                FROM CONTROL.STATE_TRANSITIONS
                WHERE TRANSITION_ID = %s
                """,
                (transition_id,),
            )
            .fetchone()
        )
        if row is None:
            return None
        return StateTransitionRecord(
            transition_id=row[0],
            decision_id=row[1],
            experiment_id=row[2],
            allocation_version=row[3],
            previous_state=RolloutState(row[4]),
            new_state=RolloutState(row[5]),
            decision=RolloutDecision(row[6]),
            decision_reason=row[7],
            analysis_snapshot_id=row[8],
            health_snapshot_id=row[9],
            policy_version=row[10],
            requested_by=row[11],
            approved_by=row[12],
            created_at=_aware(row[13]),
        )

    @staticmethod
    def _decision(row: tuple[Any, ...]) -> RolloutDecisionRecord:
        return RolloutDecisionRecord(
            decision_id=row[0],
            experiment_id=row[1],
            allocation_version=row[2],
            previous_state=RolloutState(row[3]),
            recommended_state=RolloutState(row[4]),
            decision=RolloutDecision(row[5]),
            decision_reason=row[6],
            analysis_status=ExperimentAnalysisStatus(row[7]),
            analysis_snapshot_id=row[8],
            health_snapshot_id=row[9],
            policy_version=row[10],
            policy=_variant(row[11]),
            guardrail_evidence=RolloutGuardrailEvidence.model_validate(_variant(row[12])),
            guardrail_checks=tuple(
                GuardrailCheckResult.model_validate(check) for check in _variant(row[13])
            ),
            created_at=_aware(row[14]),
        )


def run_phase5_decision() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate and persist a Phase 5 rollout decision for a running 5% experiment."
    )
    parser.add_argument("experiment_id")
    parser.add_argument("--requested-by", default="guardrail-cli")
    parser.add_argument("--approved-by", required=True)
    arguments = parser.parse_args()

    settings = SnowflakeSettings()  # type: ignore[call-arg]
    store = InMemoryExperimentStore()
    store.create_experiment(
        ExperimentCreate(
            experiment_id=arguments.experiment_id,
            name=f"Rollout evaluation for {arguments.experiment_id}",
            hypothesis="The treatment improves the primary metric without harming guardrails.",
            primary_metric="order_conversion_rate",
        )
    )
    store.launch_experiment(arguments.experiment_id)
    analysis_service = ExperimentAnalysisService(
        IntegrityService(SnowflakeIntegrityRepository(settings)),
        SnowflakeAnalysisRepository(settings),
        snapshot_repository=SnowflakeAnalysisSnapshotRepository(settings),
    )
    try:
        active_metrics = SnowflakeMetricArtifactRepository(settings).list_active()
        service = RolloutService(
            store,
            analysis_service,
            SnowflakeDecisionEvidenceRepository(settings),
            SnowflakeDecisionRepository(settings),
            evaluator=RolloutPolicyEvaluator(
                RolloutPolicy.from_metric_definitions(
                    artifact.definition for artifact in active_metrics
                )
            ),
        )
        outcome = service.evaluate_and_transition(
            arguments.experiment_id,
            TransitionRequest(
                requested_by=arguments.requested_by,
                approved_by=arguments.approved_by,
            ),
        )
    except (
        IntegrityEvidenceError,
        AnalysisEvidenceError,
        AnalysisSnapshotError,
        DecisionEvidenceError,
        DecisionPersistenceError,
        InvalidRolloutTransitionError,
        MetricArtifactPersistenceError,
        RolloutPolicyError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None

    print(json.dumps(outcome.model_dump(mode="json"), indent=2))
    if outcome.decision.decision in {RolloutDecision.PAUSE, RolloutDecision.TERMINATE}:
        raise SystemExit(2)
