from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from guardrail.config import SnowflakeSettings

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DBT_PROJECT_DIR = PROJECT_ROOT / "dbt_guardrail"


def dbt_environment(settings: SnowflakeSettings) -> dict[str, str]:
    """Create dbt's environment without exposing the PAT in shell history."""

    environment = os.environ.copy()
    environment.update(
        {
            "DBT_SNOWFLAKE_ACCOUNT": settings.snowflake_account,
            "DBT_SNOWFLAKE_USER": settings.snowflake_user,
            "DBT_SNOWFLAKE_PAT": settings.read_pat(),
            "DBT_SNOWFLAKE_ROLE": settings.snowflake_role,
            "DBT_SNOWFLAKE_WAREHOUSE": settings.snowflake_transform_warehouse,
            "DBT_SNOWFLAKE_DATABASE": settings.snowflake_database,
        }
    )
    return environment


def run() -> None:
    """Run dbt with repository defaults and a PAT read directly from its file."""

    dbt_executable = shutil.which("dbt")
    if dbt_executable is None:
        raise RuntimeError("dbt is not installed. Run `uv sync --dev` first.")

    os.chdir(PROJECT_ROOT)
    settings = SnowflakeSettings()  # type: ignore[call-arg]
    arguments = sys.argv[1:] or ["--help"]
    command = [
        dbt_executable,
        *arguments,
        "--project-dir",
        str(DBT_PROJECT_DIR),
        "--profiles-dir",
        str(DBT_PROJECT_DIR),
    ]
    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=dbt_environment(settings),
        check=False,
    )
    raise SystemExit(completed.returncode)
