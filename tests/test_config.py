from pathlib import Path

import pytest

from guardrail.config import SnowflakeSettings
from guardrail.infrastructure.snowflake import connection_parameters


def settings(pat_file: Path) -> SnowflakeSettings:
    return SnowflakeSettings(
        snowflake_account="ORG-ACCOUNT",
        snowflake_user="USER",
        snowflake_pat_file=pat_file,
        _env_file=None,
    )


def test_pat_is_read_from_file_and_trimmed(tmp_path: Path) -> None:
    pat_file = tmp_path / "pat"
    pat_file.write_text("secret-token\n", encoding="utf-8")

    assert settings(pat_file).read_pat() == "secret-token"


def test_missing_pat_file_fails_without_exposing_a_secret(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(FileNotFoundError, match="PAT file does not exist"):
        settings(missing).read_pat()


def test_connection_context_can_be_deferred_until_bootstrap(tmp_path: Path) -> None:
    pat_file = tmp_path / "pat"
    pat_file.write_text("secret-token", encoding="utf-8")

    parameters = connection_parameters(settings(pat_file), include_data_context=False)

    assert parameters["role"] == "GUARDRAIL_ENGINEER"
    assert "warehouse" not in parameters
    assert "database" not in parameters
    assert "schema" not in parameters
