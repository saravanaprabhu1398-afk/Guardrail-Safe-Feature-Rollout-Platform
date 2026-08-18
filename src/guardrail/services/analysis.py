from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil, sqrt

from scipy.stats import norm, t, ttest_ind_from_stats

from guardrail.domain.analysis import (
    AnalysisEvidenceRepository,
    AnalysisSnapshotRepository,
    ExperimentAnalysisEvidence,
    ExperimentAnalysisReport,
    ExperimentAnalysisSnapshotPayload,
    ExperimentAnalysisStatus,
    MetricAnalysisResult,
    MetricAnalysisStatus,
    MetricDirection,
    MetricType,
    StatisticalTest,
)
from guardrail.domain.integrity import ExperimentHealthReport, ExperimentIntegrityEvidence
from guardrail.services.integrity import INTEGRITY_POLICY_VERSION, IntegrityService

ANALYSIS_POLICY_VERSION = "phase4-analysis-v1"


@dataclass(frozen=True)
class AnalysisPolicy:
    minimum_proportion_successes: int = 5
    minimum_proportion_failures: int = 5
    minimum_continuous_observations: int = 2
    confidence_level: float = 0.95
    target_power: float = 0.80
    conversion_minimum_detectable_effect: float = 0.05
    delivery_minimum_detectable_effect: float = 5.0

    def __post_init__(self) -> None:
        if (
            min(
                self.minimum_proportion_successes,
                self.minimum_proportion_failures,
                self.minimum_continuous_observations,
            )
            < 1
        ):
            raise ValueError("Analysis minimum sample requirements must be positive.")
        if not 0.5 < self.confidence_level < 1:
            raise ValueError("Analysis confidence level must be between 0.5 and one.")
        if not 0.5 < self.target_power < 1:
            raise ValueError("Analysis target power must be between 0.5 and one.")
        if not 0 < self.conversion_minimum_detectable_effect < 1:
            raise ValueError("Conversion minimum detectable effect must be between zero and one.")
        if self.delivery_minimum_detectable_effect <= 0:
            raise ValueError("Delivery minimum detectable effect must be positive.")


@dataclass(frozen=True)
class _SampleSizePlan:
    minimum_detectable_effect: float
    target_power: float
    estimated_power: float
    required_control_sample_size: int
    required_treatment_sample_size: int
    sample_size_satisfied: bool


class StatisticalAnalyzer:
    """Analyze treatment-minus-control effects from warehouse summary statistics."""

    def __init__(self, policy: AnalysisPolicy | None = None) -> None:
        self._policy = policy or AnalysisPolicy()

    @property
    def policy(self) -> AnalysisPolicy:
        return self._policy

    def analyze(
        self,
        evidence: ExperimentAnalysisEvidence,
        *,
        expected_treatment_percentage: int = 50,
    ) -> tuple[MetricAnalysisResult, ...]:
        if not 0 < expected_treatment_percentage < 100:
            raise ValueError("Expected treatment percentage must be between 1 and 99.")
        return (
            self._analyze_conversion(evidence, expected_treatment_percentage),
            self._analyze_delivery_minutes(evidence, expected_treatment_percentage),
        )

    @staticmethod
    def _relative_lift(control: float, absolute_lift: float) -> float | None:
        return absolute_lift / control if control != 0 else None

    @property
    def _confidence_critical_value(self) -> float:
        return float(norm.ppf(1 - (1 - self._policy.confidence_level) / 2))

    @staticmethod
    def _wilson_interval(
        successes: int, observations: int, critical_value: float
    ) -> tuple[float, float]:
        rate = successes / observations
        squared_critical_value = critical_value**2
        denominator = 1 + squared_critical_value / observations
        center = (rate + squared_critical_value / (2 * observations)) / denominator
        margin = (
            critical_value
            * sqrt(
                rate * (1 - rate) / observations + squared_critical_value / (4 * observations**2)
            )
            / denominator
        )
        return center - margin, center + margin

    def _proportion_difference_interval(
        self,
        control_successes: int,
        control_observations: int,
        treatment_successes: int,
        treatment_observations: int,
    ) -> tuple[float, float]:
        critical_value = self._confidence_critical_value
        control_lower, control_upper = self._wilson_interval(
            control_successes, control_observations, critical_value
        )
        treatment_lower, treatment_upper = self._wilson_interval(
            treatment_successes, treatment_observations, critical_value
        )
        return treatment_lower - control_upper, treatment_upper - control_lower

    def _sample_size_plan(
        self,
        *,
        control_variance: float,
        treatment_variance: float,
        minimum_detectable_effect: float,
        control_sample_size: int,
        treatment_sample_size: int,
        expected_treatment_percentage: int,
        minimum_control_sample_size: int,
        minimum_treatment_sample_size: int,
    ) -> _SampleSizePlan:
        allocation_ratio = expected_treatment_percentage / (100 - expected_treatment_percentage)
        alpha_critical_value = self._confidence_critical_value
        power_critical_value = float(norm.ppf(self._policy.target_power))
        variance_at_allocation = control_variance + treatment_variance / allocation_ratio
        required_control = max(
            minimum_control_sample_size,
            ceil(
                (alpha_critical_value + power_critical_value) ** 2
                * variance_at_allocation
                / minimum_detectable_effect**2
            ),
        )
        required_treatment = max(
            minimum_treatment_sample_size,
            ceil(required_control * allocation_ratio),
        )

        estimated_standard_error = sqrt(
            control_variance / control_sample_size + treatment_variance / treatment_sample_size
        )
        noncentrality = minimum_detectable_effect / estimated_standard_error
        estimated_power = float(
            norm.sf(alpha_critical_value - noncentrality)
            + norm.cdf(-alpha_critical_value - noncentrality)
        )
        sample_size_satisfied = (
            control_sample_size >= required_control
            and treatment_sample_size >= required_treatment
            and estimated_power >= self._policy.target_power
        )
        return _SampleSizePlan(
            minimum_detectable_effect=minimum_detectable_effect,
            target_power=self._policy.target_power,
            estimated_power=estimated_power,
            required_control_sample_size=required_control,
            required_treatment_sample_size=required_treatment,
            sample_size_satisfied=sample_size_satisfied,
        )

    def _proportion_sample_size_plan(
        self,
        *,
        control_rate: float,
        control_sample_size: int,
        treatment_sample_size: int,
        expected_treatment_percentage: int,
    ) -> _SampleSizePlan | None:
        treatment_rate = control_rate + self._policy.conversion_minimum_detectable_effect
        if not 0 < control_rate < 1 or treatment_rate >= 1:
            return None

        minimum_control = max(
            ceil(self._policy.minimum_proportion_successes / control_rate),
            ceil(self._policy.minimum_proportion_failures / (1 - control_rate)),
        )
        minimum_treatment = max(
            ceil(self._policy.minimum_proportion_successes / treatment_rate),
            ceil(self._policy.minimum_proportion_failures / (1 - treatment_rate)),
        )
        return self._sample_size_plan(
            control_variance=control_rate * (1 - control_rate),
            treatment_variance=treatment_rate * (1 - treatment_rate),
            minimum_detectable_effect=self._policy.conversion_minimum_detectable_effect,
            control_sample_size=control_sample_size,
            treatment_sample_size=treatment_sample_size,
            expected_treatment_percentage=expected_treatment_percentage,
            minimum_control_sample_size=minimum_control,
            minimum_treatment_sample_size=minimum_treatment,
        )

    def _analyze_conversion(
        self,
        evidence: ExperimentAnalysisEvidence,
        expected_treatment_percentage: int,
    ) -> MetricAnalysisResult:
        control_n = evidence.control_exposed_users
        treatment_n = evidence.treatment_exposed_users
        control_successes = evidence.control_converted_users
        treatment_successes = evidence.treatment_converted_users
        control_estimate = control_successes / control_n if control_n else None
        treatment_estimate = treatment_successes / treatment_n if treatment_n else None
        control_failures = control_n - control_successes
        treatment_failures = treatment_n - treatment_successes
        confidence_interval = (
            self._proportion_difference_interval(
                control_successes,
                control_n,
                treatment_successes,
                treatment_n,
            )
            if control_n and treatment_n
            else None
        )
        sample_plan = (
            self._proportion_sample_size_plan(
                control_rate=control_estimate,
                control_sample_size=control_n,
                treatment_sample_size=treatment_n,
                expected_treatment_percentage=expected_treatment_percentage,
            )
            if control_estimate is not None and treatment_estimate is not None
            else None
        )
        adequate_cells = (
            min(control_successes, treatment_successes) >= self._policy.minimum_proportion_successes
            and min(control_failures, treatment_failures)
            >= self._policy.minimum_proportion_failures
        )

        if control_estimate is None or treatment_estimate is None or not adequate_cells:
            return MetricAnalysisResult(
                metric_name="order_conversion_rate",
                metric_type=MetricType.PROPORTION,
                desired_direction=MetricDirection.INCREASE,
                status=MetricAnalysisStatus.INSUFFICIENT_DATA,
                statistical_test=StatisticalTest.TWO_PROPORTION_Z,
                control_sample_size=control_n,
                treatment_sample_size=treatment_n,
                control_estimate=control_estimate,
                treatment_estimate=treatment_estimate,
                absolute_lift=(
                    treatment_estimate - control_estimate
                    if control_estimate is not None and treatment_estimate is not None
                    else None
                ),
                relative_lift=(
                    self._relative_lift(
                        control_estimate,
                        treatment_estimate - control_estimate,
                    )
                    if control_estimate is not None and treatment_estimate is not None
                    else None
                ),
                standard_error=None,
                test_statistic=None,
                degrees_of_freedom=None,
                p_value=None,
                confidence_level=(
                    self._policy.confidence_level if confidence_interval is not None else None
                ),
                confidence_interval_lower=(
                    confidence_interval[0] if confidence_interval is not None else None
                ),
                confidence_interval_upper=(
                    confidence_interval[1] if confidence_interval is not None else None
                ),
                minimum_detectable_effect=(
                    sample_plan.minimum_detectable_effect if sample_plan is not None else None
                ),
                target_power=sample_plan.target_power if sample_plan is not None else None,
                estimated_power=sample_plan.estimated_power if sample_plan is not None else None,
                required_control_sample_size=(
                    sample_plan.required_control_sample_size if sample_plan is not None else None
                ),
                required_treatment_sample_size=(
                    sample_plan.required_treatment_sample_size if sample_plan is not None else None
                ),
                sample_size_satisfied=(
                    sample_plan.sample_size_satisfied if sample_plan is not None else None
                ),
                message=(
                    "Each variant needs at least five converted and five non-converted users "
                    "before the z-test can be evaluated."
                ),
            )

        pooled_rate = (control_successes + treatment_successes) / (control_n + treatment_n)
        standard_error = sqrt(pooled_rate * (1 - pooled_rate) * (1 / control_n + 1 / treatment_n))
        absolute_lift = treatment_estimate - control_estimate
        if standard_error == 0:
            return MetricAnalysisResult(
                metric_name="order_conversion_rate",
                metric_type=MetricType.PROPORTION,
                desired_direction=MetricDirection.INCREASE,
                status=MetricAnalysisStatus.INSUFFICIENT_DATA,
                statistical_test=StatisticalTest.TWO_PROPORTION_Z,
                control_sample_size=control_n,
                treatment_sample_size=treatment_n,
                control_estimate=control_estimate,
                treatment_estimate=treatment_estimate,
                absolute_lift=absolute_lift,
                relative_lift=self._relative_lift(control_estimate, absolute_lift),
                standard_error=0.0,
                test_statistic=None,
                degrees_of_freedom=None,
                p_value=None,
                message="Conversion variance is zero, so a z-test cannot be evaluated.",
            )

        z_statistic = absolute_lift / standard_error
        p_value = 2 * float(norm.sf(abs(z_statistic)))
        assert confidence_interval is not None
        if sample_plan is None:
            return MetricAnalysisResult(
                metric_name="order_conversion_rate",
                metric_type=MetricType.PROPORTION,
                desired_direction=MetricDirection.INCREASE,
                status=MetricAnalysisStatus.INSUFFICIENT_DATA,
                statistical_test=StatisticalTest.TWO_PROPORTION_Z,
                control_sample_size=control_n,
                treatment_sample_size=treatment_n,
                control_estimate=control_estimate,
                treatment_estimate=treatment_estimate,
                absolute_lift=absolute_lift,
                relative_lift=self._relative_lift(control_estimate, absolute_lift),
                standard_error=standard_error,
                test_statistic=z_statistic,
                degrees_of_freedom=None,
                p_value=p_value,
                confidence_level=self._policy.confidence_level,
                confidence_interval_lower=confidence_interval[0],
                confidence_interval_upper=confidence_interval[1],
                message=(
                    "The configured conversion effect cannot be planned from this "
                    "boundary control rate."
                ),
            )
        metric_status = (
            MetricAnalysisStatus.COMPLETE
            if sample_plan.sample_size_satisfied
            else MetricAnalysisStatus.UNDERPOWERED
        )
        return MetricAnalysisResult(
            metric_name="order_conversion_rate",
            metric_type=MetricType.PROPORTION,
            desired_direction=MetricDirection.INCREASE,
            status=metric_status,
            statistical_test=StatisticalTest.TWO_PROPORTION_Z,
            control_sample_size=control_n,
            treatment_sample_size=treatment_n,
            control_estimate=control_estimate,
            treatment_estimate=treatment_estimate,
            absolute_lift=absolute_lift,
            relative_lift=self._relative_lift(control_estimate, absolute_lift),
            standard_error=standard_error,
            test_statistic=z_statistic,
            degrees_of_freedom=None,
            p_value=p_value,
            confidence_level=self._policy.confidence_level,
            confidence_interval_lower=confidence_interval[0],
            confidence_interval_upper=confidence_interval[1],
            minimum_detectable_effect=sample_plan.minimum_detectable_effect,
            target_power=sample_plan.target_power,
            estimated_power=sample_plan.estimated_power,
            required_control_sample_size=sample_plan.required_control_sample_size,
            required_treatment_sample_size=sample_plan.required_treatment_sample_size,
            sample_size_satisfied=sample_plan.sample_size_satisfied,
            message=(
                "Two-sided pooled two-proportion z-test completed."
                if sample_plan.sample_size_satisfied
                else "Conversion statistics are available, but the configured power "
                "requirement is not satisfied."
            ),
        )

    def _analyze_delivery_minutes(
        self,
        evidence: ExperimentAnalysisEvidence,
        expected_treatment_percentage: int,
    ) -> MetricAnalysisResult:
        control_n = evidence.control_delivery_observations
        treatment_n = evidence.treatment_delivery_observations
        control_mean = evidence.control_delivery_mean
        treatment_mean = evidence.treatment_delivery_mean
        control_variance = evidence.control_delivery_variance
        treatment_variance = evidence.treatment_delivery_variance
        sufficient = (
            control_n >= self._policy.minimum_continuous_observations
            and treatment_n >= self._policy.minimum_continuous_observations
            and control_mean is not None
            and treatment_mean is not None
            and control_variance is not None
            and treatment_variance is not None
        )
        absolute_lift = (
            treatment_mean - control_mean
            if control_mean is not None and treatment_mean is not None
            else None
        )
        if not sufficient:
            return MetricAnalysisResult(
                metric_name="average_delivery_minutes",
                metric_type=MetricType.CONTINUOUS,
                desired_direction=MetricDirection.DECREASE,
                status=MetricAnalysisStatus.INSUFFICIENT_DATA,
                statistical_test=StatisticalTest.WELCH_T,
                control_sample_size=control_n,
                treatment_sample_size=treatment_n,
                control_estimate=control_mean,
                treatment_estimate=treatment_mean,
                absolute_lift=absolute_lift,
                relative_lift=(
                    self._relative_lift(control_mean, absolute_lift)
                    if control_mean is not None and absolute_lift is not None
                    else None
                ),
                standard_error=None,
                test_statistic=None,
                degrees_of_freedom=None,
                p_value=None,
                message="Each variant needs at least two delivery observations with variance.",
            )

        assert control_mean is not None
        assert treatment_mean is not None
        assert control_variance is not None
        assert treatment_variance is not None
        control_component = control_variance / control_n
        treatment_component = treatment_variance / treatment_n
        standard_error = sqrt(control_component + treatment_component)
        absolute_lift = treatment_mean - control_mean
        if standard_error == 0:
            return MetricAnalysisResult(
                metric_name="average_delivery_minutes",
                metric_type=MetricType.CONTINUOUS,
                desired_direction=MetricDirection.DECREASE,
                status=MetricAnalysisStatus.INSUFFICIENT_DATA,
                statistical_test=StatisticalTest.WELCH_T,
                control_sample_size=control_n,
                treatment_sample_size=treatment_n,
                control_estimate=control_mean,
                treatment_estimate=treatment_mean,
                absolute_lift=absolute_lift,
                relative_lift=self._relative_lift(control_mean, absolute_lift),
                standard_error=0.0,
                test_statistic=None,
                degrees_of_freedom=None,
                p_value=None,
                message="Delivery-time variance is zero, so Welch's test cannot be evaluated.",
            )

        degrees_of_freedom = (control_component + treatment_component) ** 2 / (
            control_component**2 / (control_n - 1) + treatment_component**2 / (treatment_n - 1)
        )
        test_result = ttest_ind_from_stats(
            mean1=treatment_mean,
            std1=sqrt(treatment_variance),
            nobs1=treatment_n,
            mean2=control_mean,
            std2=sqrt(control_variance),
            nobs2=control_n,
            equal_var=False,
            alternative="two-sided",
        )
        confidence_margin = (
            float(t.ppf(1 - (1 - self._policy.confidence_level) / 2, degrees_of_freedom))
            * standard_error
        )
        sample_plan = self._sample_size_plan(
            control_variance=control_variance,
            treatment_variance=treatment_variance,
            minimum_detectable_effect=self._policy.delivery_minimum_detectable_effect,
            control_sample_size=control_n,
            treatment_sample_size=treatment_n,
            expected_treatment_percentage=expected_treatment_percentage,
            minimum_control_sample_size=self._policy.minimum_continuous_observations,
            minimum_treatment_sample_size=self._policy.minimum_continuous_observations,
        )
        metric_status = (
            MetricAnalysisStatus.COMPLETE
            if sample_plan.sample_size_satisfied
            else MetricAnalysisStatus.UNDERPOWERED
        )
        return MetricAnalysisResult(
            metric_name="average_delivery_minutes",
            metric_type=MetricType.CONTINUOUS,
            desired_direction=MetricDirection.DECREASE,
            status=metric_status,
            statistical_test=StatisticalTest.WELCH_T,
            control_sample_size=control_n,
            treatment_sample_size=treatment_n,
            control_estimate=control_mean,
            treatment_estimate=treatment_mean,
            absolute_lift=absolute_lift,
            relative_lift=self._relative_lift(control_mean, absolute_lift),
            standard_error=standard_error,
            test_statistic=float(test_result.statistic),
            degrees_of_freedom=degrees_of_freedom,
            p_value=float(test_result.pvalue),
            confidence_level=self._policy.confidence_level,
            confidence_interval_lower=absolute_lift - confidence_margin,
            confidence_interval_upper=absolute_lift + confidence_margin,
            minimum_detectable_effect=sample_plan.minimum_detectable_effect,
            target_power=sample_plan.target_power,
            estimated_power=sample_plan.estimated_power,
            required_control_sample_size=sample_plan.required_control_sample_size,
            required_treatment_sample_size=sample_plan.required_treatment_sample_size,
            sample_size_satisfied=sample_plan.sample_size_satisfied,
            message=(
                "Two-sided Welch unequal-variance t-test completed."
                if sample_plan.sample_size_satisfied
                else "Delivery-time statistics are available, but the configured power "
                "requirement is not satisfied."
            ),
        )


class ExperimentAnalysisService:
    def __init__(
        self,
        integrity_service: IntegrityService,
        repository: AnalysisEvidenceRepository,
        analyzer: StatisticalAnalyzer | None = None,
        snapshot_repository: AnalysisSnapshotRepository | None = None,
    ) -> None:
        self._integrity_service = integrity_service
        self._repository = repository
        self._analyzer = analyzer or StatisticalAnalyzer()
        self._snapshot_repository = snapshot_repository

    def get_results(
        self,
        experiment_id: str,
        allocation_version: int,
        expected_treatment_percentage: int,
    ) -> ExperimentAnalysisReport:
        integrity_evidence, health = self._integrity_service.get_health_with_evidence(
            experiment_id,
            allocation_version,
            expected_treatment_percentage,
        )
        blocking_checks = tuple(
            check.check_name for check in health.checks if check.blocks_analysis
        )
        if blocking_checks:
            report = ExperimentAnalysisReport(
                experiment_id=experiment_id,
                allocation_version=allocation_version,
                status=ExperimentAnalysisStatus.BLOCKED,
                analysis_blocked=True,
                blocking_checks=blocking_checks,
                metrics=(),
            )
            return self._persist_snapshot(
                report,
                integrity_evidence=integrity_evidence,
                health_report=health,
                analysis_evidence=None,
            )

        evidence = self._repository.get_evidence(experiment_id, allocation_version)
        metrics = self._analyzer.analyze(
            evidence,
            expected_treatment_percentage=expected_treatment_percentage,
        )
        complete_count = sum(metric.status == MetricAnalysisStatus.COMPLETE for metric in metrics)
        if complete_count == len(metrics):
            status = ExperimentAnalysisStatus.COMPLETE
        elif complete_count:
            status = ExperimentAnalysisStatus.PARTIAL
        elif all(metric.status == MetricAnalysisStatus.UNDERPOWERED for metric in metrics):
            status = ExperimentAnalysisStatus.UNDERPOWERED
        elif any(metric.status == MetricAnalysisStatus.UNDERPOWERED for metric in metrics):
            status = ExperimentAnalysisStatus.PARTIAL
        else:
            status = ExperimentAnalysisStatus.INSUFFICIENT_DATA
        report = ExperimentAnalysisReport(
            experiment_id=experiment_id,
            allocation_version=allocation_version,
            status=status,
            analysis_blocked=status != ExperimentAnalysisStatus.COMPLETE,
            blocking_checks=(),
            metrics=metrics,
        )
        return self._persist_snapshot(
            report,
            integrity_evidence=integrity_evidence,
            health_report=health,
            analysis_evidence=evidence,
        )

    def _persist_snapshot(
        self,
        report: ExperimentAnalysisReport,
        *,
        integrity_evidence: ExperimentIntegrityEvidence,
        health_report: ExperimentHealthReport,
        analysis_evidence: ExperimentAnalysisEvidence | None,
    ) -> ExperimentAnalysisReport:
        if self._snapshot_repository is None:
            return report

        record = self._snapshot_repository.persist(
            ExperimentAnalysisSnapshotPayload(
                integrity_policy_version=INTEGRITY_POLICY_VERSION,
                integrity_policy=asdict(self._integrity_service.policy),
                integrity_evidence=integrity_evidence,
                health_report=health_report,
                analysis_policy_version=ANALYSIS_POLICY_VERSION,
                analysis_policy=asdict(self._analyzer.policy),
                analysis_evidence=analysis_evidence,
                analysis_report=report,
            )
        )
        return report.model_copy(
            update={
                "health_snapshot_id": record.health_snapshot_id,
                "analysis_snapshot_id": record.analysis_snapshot_id,
                "snapshot_created_at": record.created_at,
            }
        )
