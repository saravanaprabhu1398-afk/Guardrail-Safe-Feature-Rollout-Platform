from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class ApplicationRole(StrEnum):
    VIEWER = "guardrail_viewer"
    EXPERIMENT_OPERATOR = "guardrail_experiment_operator"
    ROLLOUT_APPROVER = "guardrail_rollout_approver"
    ASSIGNMENT_CLIENT = "guardrail_assignment_client"
    TELEMETRY_PRODUCER = "guardrail_telemetry_producer"


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject: str = Field(min_length=1, max_length=255)
    email: str | None = None
    roles: frozenset[ApplicationRole]


class TokenAuthenticator(Protocol):
    def authenticate(self, token: str) -> Principal: ...


class AuthenticationError(ValueError):
    pass
