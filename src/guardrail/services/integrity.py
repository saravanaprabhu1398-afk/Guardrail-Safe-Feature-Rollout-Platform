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


@dataclass(frozen=True)
class IntegrityPolicy:
    """Blocking thresholds for the first Phase 4 integrity slice."""

    sample_ratio_mismatch_alpha: float = 0.001
    minimum_expected_variant_users: float = 5.0
    maximum_variant_flicker_rate: float = 0.0
    maximum_assignment_exposure_gap_rate: float = 0.05
    maximum_invalid_exposure_rate: float = 0.0

    def __post_init__(self) -> None:
        probability_fields = (
            self.sample_ratio_mismatch_alpha,
            self.maximum_variant_flicker_rate,
            self.maximum_assignment_exposure_gap_rate,
            self.maximum_invalid_exposure_rate,
        )
        if any(value < 0 or value > 1 for value in probability_fields):
            raise ValueError("Integrity policy rates must be between zero and one.")
        if self.sample_ratio_mismatch_alpha == 0:
            raise ValueError("Sample-ratio mismatch alpha must be greater than zero.")
        if self.minimum_expected_variant_users <= 0:
            raise ValueError("Minimum expected variant users must be greater than zero.")


class IntegrityEvaluator:
    """Turn Phase 3 evidence into blocking, reproducible integrity results."""

    def __init__(self, policy: IntegrityPolicy | None = None) -> None:
        self._policy = policy or IntegrityPolicy()

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

        gap_rate = (
            evidence.assignments_without_exposure / evidence.assigned_users
            if evidence.assigned_users
            else 0.0
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
            gap_rate > self._policy.maximum_assignment_exposure_gap_rate
            or invalid_exposure_rate > self._policy.maximum_invalid_exposure_rate
            or evidence.assignment_variant_flicker_users > 0
        )
        observed_value = max(gap_rate, invalid_exposure_rate, assignment_flicker_rate)
        return IntegrityCheckResult(
            check_name=IntegrityCheckName.ASSIGNMENT_EXPOSURE_CONSISTENCY,
            status=IntegrityCheckStatus.FAIL if failed else IntegrityCheckStatus.PASS,
            severity=IntegritySeverity.CRITICAL if failed else IntegritySeverity.INFO,
            blocks_analysis=failed,
            observed_value=observed_value,
            expected_range=(
                "assignment gap <= "
                f"{self._policy.maximum_assignment_exposure_gap_rate:.4f}; "
                "invalid exposure rate <= "
                f"{self._policy.maximum_invalid_exposure_rate:.4f}; "
                "no assignment variant flicker"
            ),
            evidence={
                "assigned_users": evidence.assigned_users,
                "total_exposures": evidence.total_exposures,
                "assignments_without_exposure": evidence.assignments_without_exposure,
                "assignment_exposure_gap_rate": gap_rate,
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
        evidence = self._repository.get_evidence(experiment_id, allocation_version)
        return self._evaluator.evaluate(
            evidence,
            expected_treatment_percentage=expected_treatment_percentage,
        )
