from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class GuardrailApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GuardrailApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        opener: Callable[..., Any] = urlopen,
        timeout_seconds: float = 30.0,
        access_token: str | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._opener = opener
        self._timeout_seconds = timeout_seconds
        self._access_token = access_token

    def create_experiment(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/experiments", payload)

    def launch_experiment(self, experiment_id: str) -> dict[str, Any]:
        return self._request("POST", f"/experiments/{quote(experiment_id, safe='')}/launch")

    def get_experiment(self, experiment_id: str) -> dict[str, Any]:
        return self._request("GET", f"/experiments/{quote(experiment_id, safe='')}")

    def get_health(self, experiment_id: str) -> dict[str, Any]:
        return self._request("GET", f"/experiments/{quote(experiment_id, safe='')}/health")

    def get_results(self, experiment_id: str) -> dict[str, Any]:
        return self._request("GET", f"/experiments/{quote(experiment_id, safe='')}/results")

    def get_decisions(self, experiment_id: str) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            f"/experiments/{quote(experiment_id, safe='')}/decisions",
        )
        if not isinstance(response, list):
            raise GuardrailApiError("The decision-history response is not a list.")
        return response

    def apply_transition(
        self,
        experiment_id: str,
        *,
        requested_by: str,
        approved_by: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/experiments/{quote(experiment_id, safe='')}/transitions",
            {"requested_by": requested_by, "approved_by": approved_by},
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        request = Request(
            f"{self._base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                return json.loads(response.read().decode())
        except HTTPError as exc:
            detail = self._error_detail(exc)
            raise GuardrailApiError(detail, status_code=exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GuardrailApiError("The Guardrail API is unavailable.") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GuardrailApiError("The Guardrail API returned invalid JSON.") from exc

    @staticmethod
    def _error_detail(error: HTTPError) -> str:
        try:
            payload = json.loads(error.read().decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return f"Guardrail API request failed with status {error.code}."
        detail = payload.get("detail") if isinstance(payload, dict) else None
        return (
            detail
            if isinstance(detail, str)
            else f"Guardrail API request failed with status {error.code}."
        )
