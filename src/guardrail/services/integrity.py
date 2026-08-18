from __future__ import annotations

from dataclasses import dataclass
from math import erfc, sqrt

from guardrail.domain.integrity import (
    ExperimentHealthReport,
    ExperimentIntegrityEvidence,
    IntegrityAction,
    IntegrityCheckName,
    IntegrityCheckResult,
    IntegrityCheckStatus,
    IntegrityEvidenceRepository,
    IntegritySeverity,
)

INTEGRITY_POLICY_VERSION = "phase4-integrity-v1"


@dataclass(frozen=True)
class IntegrityPolicy:
    """Blocking thresholds for Phase 4 experiment-integrity evaluation."""

    sample_ratio_mismatch_alpha: float = 0.001
    minimum_expected_variant_users: float = 5.0
    maximum_variant_flicker_rate: float = 0.0
    maximum_invalid_exposure_rate: float = 0.0
    minimum_variant_assignments_for_event_loss: int = 20
    maximum_variant_event_loss_rate: float = 0.05
    maximum_event_loss_rate_skew: float = 0.03
    maximum_duplicate_delivery_rate: float = 0.05
    maximum_variant_late_event_rate: float = 0.05
    maximum_freshness_skew_seconds: float = 120.0

    def __post_init__(self) -> None:
        probability_fields = (
            self.sample_ratio_mismatch_alpha,
            self.maximum_variant_flicker_rate,
            self.maximum_invalid_exposure_rate,
            self.maximum_variant_event_loss_rate,
            self.maximum_event_loss_rate_skew,
            self.maximum_duplicate_delivery_rate,
            self.maximum_variant_late_event_rate,
        )
        if any(value < 0 or value > 1 for value in probability_fields):
            raise ValueError("Integrity policy rates must be between zero and one.")
        if self.sample_ratio_mismatch_alpha == 0:
            raise ValueError("Sample-ratio mismatch alpha must be greater than zero.")
        if self.minimum_expected_variant_users <= 0:
            raise ValueError("Minimum expected variant users must be greater than zero.")
        if self.minimum_variant_assignments_for_event_loss <= 0:
            raise ValueError("Minimum variant assignments for event loss must be positive.")
        if self.maximum_freshness_skew_seconds < 0:
            raise ValueError("Maximum freshness skew cannot be negative.")


class IntegrityEvaluator:
    """Turn Phase 3 evidence into blocking, reproducible integrity results."""

    def __init__(self, policy: IntegrityPolicy | None = None) -> None:
        self._policy = policy or IntegrityPolicy()

    @property
    def policy(self) -> IntegrityPolicy:
        return self._policy

    def evaluate(
        self,
        evidence: ExperimentIntegrityEvidence,
        *,
        expected_treatment_percentage: int,
    ) -> ExperimentHealthReport:
        if not 0 < expected_treatment_percentage < 100:
            raise ValueError("Expected treatment percentage must be between 1 and 99.")

        checks = (
            self._sample_ratio_check(evidence, expected_treatment_percentage),
            self._variant_flicker_check(evidence),
            self._assignment_exposure_check(evidence),
            self._event_loss_check(evidence),
            self._duplicate_delivery_check(evidence),
            self._late_data_check(evidence),
            self._freshness_skew_check(evidence),
        )
        return ExperimentHealthReport(
            experiment_id=evidence.experiment_id,
            allocation_version=evidence.allocation_version,
            expected_treatment_percentage=expected_treatment_percentage,
            analysis_blocked=any(check.blocks_analysis for check in checks),
            checks=checks,
        )

    def _sample_ratio_check(
        self,
        evidence: ExperimentIntegrityEvidence,
        expected_treatment_percentage: int,
    ) -> IntegrityCheckResult:
        treatment_share = expected_treatment_percentage / 100
        control_share = 1 - treatment_share
        total = evidence.exposed_users
        expected_control = total * control_share
        expected_treatment = total * treatment_share
        observed_share = evidence.treatment_exposed_users / total if total else None
        base_evidence: dict[str, int | float] = {
            "control_exposed_users": evidence.control_exposed_users,
            "treatment_exposed_users": evidence.treatment_exposed_users,
            "expected_control_users": expected_control,
            "expected_treatment_users": expected_treatment,
        }

        if min(expected_control, expected_treatment) < self._policy.minimum_expected_variant_users:
            return IntegrityCheckResult(
                check_name=IntegrityCheckName.SAMPLE_RATIO_MISMATCH,
                status=IntegrityCheckStatus.INSUFFICIENT_DATA,
                severity=IntegritySeverity.WARNING,
                blocks_analysis=True,
                observed_value=observed_share,
                expected_range=(
                    "Each expected allocation cell must contain at least "
                    f"{self._policy.minimum_expected_variant_users:g} users"
                ),
                threshold=self._policy.minimum_expected_variant_users,
                evidence=base_evidence,
                message="There is not enough exposure data to evaluate sample-ratio mismatch.",
                recommended_action=IntegrityAction.CONTINUE,
            )

        chi_square = (evidence.control_exposed_users - expected_control) ** 2 / expected_control + (
            evidence.treatment_exposed_users - expected_treatment
        ) ** 2 / expected_treatment
        # With two variants Pearson's statistic has one degree of freedom. Its
        # survival function is erfc(sqrt(chi_square / 2)), avoiding a heavy runtime
        # dependency while retaining the reviewed closed-form calculation.
        p_value = erfc(sqrt(chi_square / 2))
        failed = p_value < self._policy.sample_ratio_mismatch_alpha
        base_evidence["chi_square"] = chi_square
        return IntegrityCheckResult(
            check_name=IntegrityCheckName.SAMPLE_RATIO_MISMATCH,
            status=IntegrityCheckStatus.FAIL if failed else IntegrityCheckStatus.PASS,
            severity=IntegritySeverity.CRITICAL if failed else IntegritySeverity.INFO,
            blocks_analysis=failed,
            observed_value=observed_share,
            expected_range=f"Treatment share consistent with {treatment_share:.4f}",
            threshold=self._policy.sample_ratio_mismatch_alpha,
            p_value=p_value,
            evidence=base_evidence,
            message=(
                "Observed exposure allocation is inconsistent with the configured split."
                if failed
                else "Observed exposure allocation is consistent with the configured split."
            ),
            recommended_action=IntegrityAction.PAUSE if failed else IntegrityAction.NONE,
        )

    def _variant_flicker_check(self, evidence: ExperimentIntegrityEvidence) -> IntegrityCheckResult:
        if evidence.exposed_users == 0:
            return IntegrityCheckResult(
                check_name=IntegrityCheckName.VARIANT_FLICKER,
                status=IntegrityCheckStatus.INSUFFICIENT_DATA,
                severity=IntegritySeverity.WARNING,
                blocks_analysis=True,
                observed_value=None,
                expected_range="At least one exposed user",
                threshold=self._policy.maximum_variant_flicker_rate,
                evidence={
                    "exposed_users": 0,
                    "variant_flicker_users": evidence.variant_flicker_users,
                },
                message="There are no exposed users to evaluate for variant flicker.",
                recommended_action=IntegrityAction.CONTINUE,
            )

        flicker_rate = evidence.variant_flicker_users / evidence.exposed_users
        failed = flicker_rate > self._policy.maximum_variant_flicker_rate
        return IntegrityCheckResult(
            check_name=IntegrityCheckName.VARIANT_FLICKER,
            status=IntegrityCheckStatus.FAIL if failed else IntegrityCheckStatus.PASS,
            severity=IntegritySeverity.CRITICAL if failed else IntegritySeverity.INFO,
            blocks_analysis=failed,
            observed_value=flicker_rate,
            expected_range=(
                f"Variant flicker rate <= {self._policy.maximum_variant_flicker_rate:.4f}"
            ),
            threshold=self._policy.maximum_variant_flicker_rate,
            evidence={
                "exposed_users": evidence.exposed_users,
                "variant_flicker_users": evidence.variant_flicker_users,
            },
            message=(
                "Users were exposed to multiple variants in one allocation version."
                if failed
                else "No exposed user appeared in multiple variants."
            ),
            recommended_action=IntegrityAction.PAUSE if failed else IntegrityAction.NONE,
        )

    def _assignment_exposure_check(
        self, evidence: ExperimentIntegrityEvidence
    ) -> IntegrityCheckResult:
        if evidence.assigned_users == 0 and evidence.total_exposures == 0:
            return IntegrityCheckResult(
                check_name=IntegrityCheckName.ASSIGNMENT_EXPOSURE_CONSISTENCY,
                status=IntegrityCheckStatus.INSUFFICIENT_DATA,
                severity=IntegritySeverity.WARNING,
                blocks_analysis=True,
                observed_value=None,
                expected_range="At least one assignment or exposure",
                evidence={
                    "assigned_users": 0,
                    "total_exposures": 0,
                },
                message="There is no assignment or exposure data to evaluate.",
                recommended_action=IntegrityAction.CONTINUE,
            )

        invalid_exposure_count = evidence.orphan_exposures + evidence.variant_mismatch_exposures
        invalid_exposure_rate = (
            invalid_exposure_count / evidence.total_exposures if evidence.total_exposures else 0.0
        )
        assignment_flicker_rate = (
            evidence.assignment_variant_flicker_users / evidence.assigned_users
            if evidence.assigned_users
            else 0.0
        )
        failed = (
            invalid_exposure_rate > self._policy.maximum_invalid_exposure_rate
            or evidence.assignment_variant_flicker_users > 0
        )
        observed_value = max(invalid_exposure_rate, assignment_flicker_rate)
        return IntegrityCheckResult(
            check_name=IntegrityCheckName.ASSIGNMENT_EXPOSURE_CONSISTENCY,
            status=IntegrityCheckStatus.FAIL if failed else IntegrityCheckStatus.PASS,
            severity=IntegritySeverity.CRITICAL if failed else IntegritySeverity.INFO,
            blocks_analysis=failed,
            observed_value=observed_value,
            expected_range=(
                "invalid exposure rate <= "
                f"{self._policy.maximum_invalid_exposure_rate:.4f}; "
                "no assignment variant flicker"
            ),
            evidence={
                "assigned_users": evidence.assigned_users,
                "total_exposures": evidence.total_exposures,
                "orphan_exposures": evidence.orphan_exposures,
                "variant_mismatch_exposures": evidence.variant_mismatch_exposures,
                "invalid_exposure_rate": invalid_exposure_rate,
                "assignment_variant_flicker_users": (evidence.assignment_variant_flicker_users),
            },
            message=(
                "Assignment and exposure evidence is inconsistent."
                if failed
                else "Assignment and exposure evidence is consistent."
            ),
            recommended_action=IntegrityAction.PAUSE if failed else IntegrityAction.NONE,
        )

    def _event_loss_check(self, evidence: ExperimentIntegrityEvidence) -> IntegrityCheckResult:
        if (
            evidence.control_assignments < self._policy.minimum_variant_assignments_for_event_loss
            or evidence.treatment_assignments
            < self._policy.minimum_variant_assignments_for_event_loss
        ):
            return IntegrityCheckResult(
                check_name=IntegrityCheckName.EVENT_LOSS,
                status=IntegrityCheckStatus.INSUFFICIENT_DATA,
                severity=IntegritySeverity.WARNING,
                blocks_analysis=True,
                observed_value=None,
                expected_range=(
                    "At least "
                    f"{self._policy.minimum_variant_assignments_for_event_loss} assignments "
                    "in each variant"
                ),
                threshold=float(self._policy.minimum_variant_assignments_for_event_loss),
                evidence={
                    "control_assignments": evidence.control_assignments,
                    "treatment_assignments": evidence.treatment_assignments,
                    "assignments_without_exposure": evidence.assignments_without_exposure,
                },
                message="Both variants need enough assignments before event loss can be evaluated.",
                recommended_action=IntegrityAction.CONTINUE,
            )

        control_loss_rate = (
            evidence.control_assignments - evidence.control_exposed_users
        ) / evidence.control_assignments
        treatment_loss_rate = (
            evidence.treatment_assignments - evidence.treatment_exposed_users
        ) / evidence.treatment_assignments
        rate_skew = abs(control_loss_rate - treatment_loss_rate)
        failed = (
            max(control_loss_rate, treatment_loss_rate)
            > self._policy.maximum_variant_event_loss_rate
            or rate_skew > self._policy.maximum_event_loss_rate_skew
        )
        return IntegrityCheckResult(
            check_name=IntegrityCheckName.EVENT_LOSS,
            status=IntegrityCheckStatus.FAIL if failed else IntegrityCheckStatus.PASS,
            severity=IntegritySeverity.CRITICAL if failed else IntegritySeverity.INFO,
            blocks_analysis=failed,
            observed_value=max(control_loss_rate, treatment_loss_rate),
            expected_range=(
                "variant event-loss rate <= "
                f"{self._policy.maximum_variant_event_loss_rate:.4f}; "
                "between-variant skew <= "
                f"{self._policy.maximum_event_loss_rate_skew:.4f}"
            ),
            evidence={
                "control_assignments": evidence.control_assignments,
                "control_exposed_users": evidence.control_exposed_users,
                "control_event_loss_rate": control_loss_rate,
                "treatment_assignments": evidence.treatment_assignments,
                "treatment_exposed_users": evidence.treatment_exposed_users,
                "treatment_event_loss_rate": treatment_loss_rate,
                "event_loss_rate_skew": rate_skew,
                "assignments_without_exposure": evidence.assignments_without_exposure,
            },
            message=(
                "Assignment-to-exposure loss is excessive or differs by variant."
                if failed
                else "Assignment-to-exposure loss is within policy."
            ),
            recommended_action=IntegrityAction.PAUSE if failed else IntegrityAction.NONE,
        )

    def _duplicate_delivery_check(
        self, evidence: ExperimentIntegrityEvidence
    ) -> IntegrityCheckResult:
        if evidence.raw_event_deliveries == 0:
            return IntegrityCheckResult(
                check_name=IntegrityCheckName.DUPLICATE_DELIVERY,
                status=IntegrityCheckStatus.INSUFFICIENT_DATA,
                severity=IntegritySeverity.WARNING,
                blocks_analysis=True,
                observed_value=None,
                expected_range="At least one raw event delivery",
                threshold=self._policy.maximum_duplicate_delivery_rate,
                evidence={"raw_event_deliveries": 0, "duplicate_event_deliveries": 0},
                message="There are no raw event deliveries to evaluate for duplicates.",
                recommended_action=IntegrityAction.CONTINUE,
            )

        duplicate_rate = evidence.duplicate_event_deliveries / evidence.raw_event_deliveries
        failed = duplicate_rate > self._policy.maximum_duplicate_delivery_rate
        return IntegrityCheckResult(
            check_name=IntegrityCheckName.DUPLICATE_DELIVERY,
            status=IntegrityCheckStatus.FAIL if failed else IntegrityCheckStatus.PASS,
            severity=IntegritySeverity.WARNING if failed else IntegritySeverity.INFO,
            # Stable event identity keeps analytical facts effectively-once even when
            # transport retries exceed the operational duplicate-delivery threshold.
            blocks_analysis=False,
            observed_value=duplicate_rate,
            expected_range=(
                f"raw duplicate-delivery rate <= {self._policy.maximum_duplicate_delivery_rate:.4f}"
            ),
            threshold=self._policy.maximum_duplicate_delivery_rate,
            evidence={
                "raw_event_deliveries": evidence.raw_event_deliveries,
                "duplicate_event_deliveries": evidence.duplicate_event_deliveries,
            },
            message=(
                "Raw duplicate delivery exceeds the operational threshold; clean facts remain "
                "deduplicated by event_id."
                if failed
                else "Raw duplicate delivery is within the operational threshold."
            ),
            recommended_action=IntegrityAction.CONTINUE if failed else IntegrityAction.NONE,
        )

    def _late_data_check(self, evidence: ExperimentIntegrityEvidence) -> IntegrityCheckResult:
        if evidence.control_canonical_events == 0 or evidence.treatment_canonical_events == 0:
            return IntegrityCheckResult(
                check_name=IntegrityCheckName.LATE_DATA,
                status=IntegrityCheckStatus.INSUFFICIENT_DATA,
                severity=IntegritySeverity.WARNING,
                blocks_analysis=True,
                observed_value=None,
                expected_range="At least one canonical event in each variant",
                threshold=self._policy.maximum_variant_late_event_rate,
                evidence={
                    "control_canonical_events": evidence.control_canonical_events,
                    "treatment_canonical_events": evidence.treatment_canonical_events,
                },
                message="Both variants need canonical events before lateness can be evaluated.",
                recommended_action=IntegrityAction.CONTINUE,
            )

        control_late_rate = evidence.control_late_events / evidence.control_canonical_events
        treatment_late_rate = evidence.treatment_late_events / evidence.treatment_canonical_events
        maximum_rate = max(control_late_rate, treatment_late_rate)
        failed = maximum_rate > self._policy.maximum_variant_late_event_rate
        return IntegrityCheckResult(
            check_name=IntegrityCheckName.LATE_DATA,
            status=IntegrityCheckStatus.FAIL if failed else IntegrityCheckStatus.PASS,
            severity=IntegritySeverity.CRITICAL if failed else IntegritySeverity.INFO,
            blocks_analysis=failed,
            observed_value=maximum_rate,
            expected_range=(
                "late-event rate in each variant <= "
                f"{self._policy.maximum_variant_late_event_rate:.4f}"
            ),
            threshold=self._policy.maximum_variant_late_event_rate,
            evidence={
                "control_canonical_events": evidence.control_canonical_events,
                "control_late_events": evidence.control_late_events,
                "control_late_event_rate": control_late_rate,
                "treatment_canonical_events": evidence.treatment_canonical_events,
                "treatment_late_events": evidence.treatment_late_events,
                "treatment_late_event_rate": treatment_late_rate,
            },
            message=(
                "Late event arrival exceeds the configured attribution-safety threshold."
                if failed
                else "Late event arrival is within policy."
            ),
            recommended_action=IntegrityAction.CONTINUE if failed else IntegrityAction.NONE,
        )

    def _freshness_skew_check(self, evidence: ExperimentIntegrityEvidence) -> IntegrityCheckResult:
        control_p95 = evidence.control_p95_receive_lag_seconds
        treatment_p95 = evidence.treatment_p95_receive_lag_seconds
        if control_p95 is None or treatment_p95 is None:
            return IntegrityCheckResult(
                check_name=IntegrityCheckName.VARIANT_FRESHNESS_SKEW,
                status=IntegrityCheckStatus.INSUFFICIENT_DATA,
                severity=IntegritySeverity.WARNING,
                blocks_analysis=True,
                observed_value=None,
                expected_range="Receive-lag evidence for both variants",
                threshold=self._policy.maximum_freshness_skew_seconds,
                evidence={
                    "control_canonical_events": evidence.control_canonical_events,
                    "treatment_canonical_events": evidence.treatment_canonical_events,
                },
                message="Both variants need receive-lag evidence before skew can be evaluated.",
                recommended_action=IntegrityAction.CONTINUE,
            )

        skew_seconds = abs(control_p95 - treatment_p95)
        failed = skew_seconds > self._policy.maximum_freshness_skew_seconds
        return IntegrityCheckResult(
            check_name=IntegrityCheckName.VARIANT_FRESHNESS_SKEW,
            status=IntegrityCheckStatus.FAIL if failed else IntegrityCheckStatus.PASS,
            severity=IntegritySeverity.CRITICAL if failed else IntegritySeverity.INFO,
            blocks_analysis=failed,
            observed_value=skew_seconds,
            expected_range=(
                "absolute p95 receive-lag skew <= "
                f"{self._policy.maximum_freshness_skew_seconds:.1f} seconds"
            ),
            threshold=self._policy.maximum_freshness_skew_seconds,
            evidence={
                "control_p95_receive_lag_seconds": control_p95,
                "treatment_p95_receive_lag_seconds": treatment_p95,
                "p95_receive_lag_skew_seconds": skew_seconds,
            },
            message=(
                "Receive latency differs materially between experiment variants."
                if failed
                else "Receive latency is consistent between experiment variants."
            ),
            recommended_action=IntegrityAction.CONTINUE if failed else IntegrityAction.NONE,
        )


class IntegrityService:
    def __init__(
        self,
        repository: IntegrityEvidenceRepository,
        evaluator: IntegrityEvaluator | None = None,
    ) -> None:
        self._repository = repository
        self._evaluator = evaluator or IntegrityEvaluator()

    def get_health(
        self,
        experiment_id: str,
        allocation_version: int,
        expected_treatment_percentage: int,
    ) -> ExperimentHealthReport:
        _, report = self.get_health_with_evidence(
            experiment_id,
            allocation_version,
            expected_treatment_percentage,
        )
        return report

    @property
    def policy(self) -> IntegrityPolicy:
        return self._evaluator.policy

    def get_health_with_evidence(
        self,
        experiment_id: str,
        allocation_version: int,
        expected_treatment_percentage: int,
    ) -> tuple[ExperimentIntegrityEvidence, ExperimentHealthReport]:
        evidence = self._repository.get_evidence(experiment_id, allocation_version)
        report = self._evaluator.evaluate(
            evidence,
            expected_treatment_percentage=expected_treatment_percentage,
        )
        return evidence, report
