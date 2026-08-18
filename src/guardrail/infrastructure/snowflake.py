from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import snowflake.connector
from snowflake.connector import SnowflakeConnection

from guardrail.config import SnowflakeSettings


def connection_parameters(
    settings: SnowflakeSettings, *, include_data_context: bool = True
) -> dict[str, Any]:
    """Build connector parameters without exposing credentials to logs."""

    parameters: dict[str, Any] = {
        "account": settings.snowflake_account,
        "user": settings.snowflake_user,
        "password": settings.read_pat(),
        "role": settings.snowflake_role,
        "application": "guardrail-platform",
        "login_timeout": 15,
        "network_timeout": 30,
    }
    if include_data_context:
        parameters.update(
            {
                "warehouse": settings.snowflake_warehouse,
                "database": settings.snowflake_database,
                "schema": settings.snowflake_schema,
            }
        )
    return parameters


@contextmanager
def connect(
    settings: SnowflakeSettings, *, include_data_context: bool = True
) -> Iterator[SnowflakeConnection]:
    connection = snowflake.connector.connect(
        **connection_parameters(settings, include_data_context=include_data_context)
    )
    try:
        yield connection
    finally:
        connection.close()


def run_check() -> None:
    """Verify identity and role without requiring project objects to exist."""

    settings = SnowflakeSettings()  # type: ignore[call-arg]
    with connect(settings, include_data_context=False) as connection:
        row = (
            connection.cursor()
            .execute("SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_REGION()")
            .fetchone()
        )

    if row is None:
        raise RuntimeError("Snowflake connection check returned no result.")
    print(f"Connected: user={row[0]} role={row[1]} region={row[2]}")
