from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MetricType(StrEnum):
    PROPORTION = "proportion"
    CONTINUOUS = "continuous"


class MetricRole(StrEnum):
    PRIMARY = "primary"
    GUARDRAIL = "guardrail"


class MetricDirection(StrEnum):
    INCREASE = "increase"
    DECREASE = "decrease"


class DecisionRuleKind(StrEnum):
    MINIMUM_ABSOLUTE_IMPROVEMENT = "minimum_absolute_improvement"
    MAXIMUM_RELATIVE_INCREASE = "maximum_relative_increase"
    MAXIMUM_ABSOLUTE_INCREASE = "maximum_absolute_increase"
    MAXIMUM_VALUE = "maximum_value"


class MetricDefinitionStatus(StrEnum):
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"


class EventReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


class ValueReference(EventReference):
    field: str = Field(pattern=r"^attributes\.[a-z][a-z0-9_]*$")


class AttributionWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: int = Field(ge=1, le=168)
    unit: str = Field(pattern=r"^hours$")


class MetricDecisionRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: DecisionRuleKind
    value: float = Field(ge=0)


class MetricDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=120)
    version: int = Field(ge=1)
    description: str = Field(min_length=10, max_length=500)
    owner: str = Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=120)
    entity: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: MetricType
    role: MetricRole
    numerator: EventReference | None = None
    denominator: EventReference
    value: ValueReference | None = None
    attribution_window: AttributionWindow
    direction: MetricDirection
    minimum_detectable_effect: float = Field(gt=0)
    decision_rule: MetricDecisionRule

    @model_validator(mode="after")
    def validate_metric_shape(self) -> MetricDefinition:
        if self.type == MetricType.PROPORTION and (
            self.numerator is None or self.value is not None
        ):
            raise ValueError("proportion metrics require numerator and prohibit value")
        if self.type == MetricType.CONTINUOUS and (
            self.value is None or self.numerator is not None
        ):
            raise ValueError("continuous metrics require value and prohibit numerator")
        if self.role == MetricRole.PRIMARY and (
            self.decision_rule.kind != DecisionRuleKind.MINIMUM_ABSOLUTE_IMPROVEMENT
        ):
            raise ValueError("primary metrics require a minimum absolute improvement rule")
        if self.role == MetricRole.GUARDRAIL and (
            self.decision_rule.kind == DecisionRuleKind.MINIMUM_ABSOLUTE_IMPROVEMENT
        ):
            raise ValueError("guardrail metrics require a maximum tolerance rule")
        return self


class CompiledMetricArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: str = Field(min_length=1)
    metric_name: str
    metric_version: int = Field(ge=1)
    definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiled_sql: str = Field(min_length=1)
    compiled_sql_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: MetricDefinitionStatus
    definition: MetricDefinition
    compiled_at: datetime


class MetricArtifactRepository(Protocol):
    def save(self, artifact: CompiledMetricArtifact) -> CompiledMetricArtifact: ...

    def activate(self, metric_name: str, metric_version: int) -> CompiledMetricArtifact: ...

    def get(self, metric_name: str, metric_version: int) -> CompiledMetricArtifact: ...

    def list_active(self) -> tuple[CompiledMetricArtifact, ...]: ...


class MetricDefinitionError(ValueError):
    pass


class MetricReferenceError(MetricDefinitionError):
    pass


class MetricVersionConflictError(MetricDefinitionError):
    pass


class MetricArtifactNotFoundError(KeyError):
    pass


class MetricArtifactPersistenceError(RuntimeError):
    pass
