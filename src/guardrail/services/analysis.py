from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from scipy.stats import norm, ttest_ind_from_stats

from guardrail.domain.analysis import (
    AnalysisEvidenceRepository,
    ExperimentAnalysisEvidence,
    ExperimentAnalysisReport,
    ExperimentAnalysisStatus,
    MetricAnalysisResult,
    MetricAnalysisStatus,
    MetricDirection,
    MetricType,
    StatisticalTest,
)
from guardrail.services.integrity import IntegrityService


@dataclass(frozen=True)
class AnalysisPolicy:
    minimum_proportion_successes: int = 5
    minimum_proportion_failures: int = 5
    minimum_continuous_observations: int = 2

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


class StatisticalAnalyzer:
    """Analyze treatment-minus-control effects from warehouse summary statistics."""

    def __init__(self, policy: AnalysisPolicy | None = None) -> None:
        self._policy = policy or AnalysisPolicy()

    def analyze(self, evidence: ExperimentAnalysisEvidence) -> tuple[MetricAnalysisResult, ...]:
        return (
            self._analyze_conversion(evidence),
            self._analyze_delivery_minutes(evidence),
        )

    @staticmethod
    def _relative_lift(control: float, absolute_lift: float) -> float | None:
        return absolute_lift / control if control != 0 else None

    def _analyze_conversion(self, evidence: ExperimentAnalysisEvidence) -> MetricAnalysisResult:
        control_n = evidence.control_exposed_users
        treatment_n = evidence.treatment_exposed_users
        control_successes = evidence.control_converted_users
        treatment_successes = evidence.treatment_converted_users
        control_estimate = control_successes / control_n if control_n else None
        treatment_estimate = treatment_successes / treatment_n if treatment_n else None
        control_failures = control_n - control_successes
        treatment_failures = treatment_n - treatment_successes
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
                relative_lift=None,
                standard_error=None,
                test_statistic=None,
                degrees_of_freedom=None,
                p_value=None,
                message=(
                    "Each variant needs at least five converted and five non-converted users."
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
        return MetricAnalysisResult(
            metric_name="order_conversion_rate",
            metric_type=MetricType.PROPORTION,
            desired_direction=MetricDirection.INCREASE,
            status=MetricAnalysisStatus.COMPLETE,
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
            message="Two-sided pooled two-proportion z-test completed.",
        )

    def _analyze_delivery_minutes(
        self, evidence: ExperimentAnalysisEvidence
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
        return MetricAnalysisResult(
            metric_name="average_delivery_minutes",
            metric_type=MetricType.CONTINUOUS,
            desired_direction=MetricDirection.DECREASE,
            status=MetricAnalysisStatus.COMPLETE,
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
            message="Two-sided Welch unequal-variance t-test completed.",
        )


class ExperimentAnalysisService:
    def __init__(
        self,
        integrity_service: IntegrityService,
        repository: AnalysisEvidenceRepository,
        analyzer: StatisticalAnalyzer | None = None,
    ) -> None:
        self._integrity_service = integrity_service
        self._repository = repository
        self._analyzer = analyzer or StatisticalAnalyzer()

    def get_results(
        self,
        experiment_id: str,
        allocation_version: int,
        expected_treatment_percentage: int,
    ) -> ExperimentAnalysisReport:
        health = self._integrity_service.get_health(
            experiment_id,
            allocation_version,
            expected_treatment_percentage,
        )
        blocking_checks = tuple(
            check.check_name for check in health.checks if check.blocks_analysis
        )
        if blocking_checks:
            return ExperimentAnalysisReport(
                experiment_id=experiment_id,
                allocation_version=allocation_version,
                status=ExperimentAnalysisStatus.BLOCKED,
                analysis_blocked=True,
                blocking_checks=blocking_checks,
                metrics=(),
            )

        evidence = self._repository.get_evidence(experiment_id, allocation_version)
        metrics = self._analyzer.analyze(evidence)
        complete_count = sum(metric.status == MetricAnalysisStatus.COMPLETE for metric in metrics)
        if complete_count == len(metrics):
            status = ExperimentAnalysisStatus.COMPLETE
        elif complete_count:
            status = ExperimentAnalysisStatus.PARTIAL
        else:
            status = ExperimentAnalysisStatus.INSUFFICIENT_DATA
        return ExperimentAnalysisReport(
            experiment_id=experiment_id,
            allocation_version=allocation_version,
            status=status,
            analysis_blocked=status != ExperimentAnalysisStatus.COMPLETE,
            blocking_checks=(),
            metrics=metrics,
        )
