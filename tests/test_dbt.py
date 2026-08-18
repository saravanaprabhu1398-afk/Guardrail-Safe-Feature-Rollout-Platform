from pathlib import Path

from guardrail.config import SnowflakeSettings
from guardrail.infrastructure.dbt import dbt_environment


def test_dbt_environment_uses_native_pat_and_transform_warehouse(tmp_path: Path) -> None:
    pat_file = tmp_path / "pat"
    pat_file.write_text("test-pat\n", encoding="utf-8")
    settings = SnowflakeSettings(
        snowflake_account="ORG-ACCOUNT",
        snowflake_user="ENGINEER",
        snowflake_pat_file=pat_file,
        _env_file=None,
    )

    environment = dbt_environment(settings)

    assert environment["DBT_SNOWFLAKE_ACCOUNT"] == "ORG-ACCOUNT"
    assert environment["DBT_SNOWFLAKE_USER"] == "ENGINEER"
    assert environment["DBT_SNOWFLAKE_PAT"] == "test-pat"
    assert environment["DBT_SNOWFLAKE_ROLE"] == "GUARDRAIL_ENGINEER"
    assert environment["DBT_SNOWFLAKE_WAREHOUSE"] == "GUARDRAIL_TRANSFORM_WH"
    assert environment["DBT_SNOWFLAKE_DATABASE"] == "GUARDRAIL"
