from __future__ import annotations

from io import StringIO
from pathlib import Path

from guardrail.config import SnowflakeSettings
from guardrail.infrastructure.snowflake import connect

PROJECT_ROOT = Path(__file__).resolve().parents[3]
INGESTION_DDL = PROJECT_ROOT / "snowflake" / "ddl" / "02_ingestion.sql"


def apply_sql_file(path: Path, settings: SnowflakeSettings) -> tuple[int, tuple | None]:
    sql = path.read_text(encoding="utf-8")
    statement_count = 0
    final_row: tuple | None = None

    with connect(settings) as connection:
        for cursor in connection.execute_stream(StringIO(sql), remove_comments=True):
            statement_count += 1
            if cursor.description is not None:
                final_row = cursor.fetchone()

    return statement_count, final_row


def run_ingestion_setup() -> None:
    statement_count, final_row = apply_sql_file(INGESTION_DDL, SnowflakeSettings())  # type: ignore[call-arg]
    print(f"Applied {statement_count} Phase 2 ingestion statements.")
    if final_row is not None:
        print(
            "Verified: "
            f"role={final_row[0]} warehouse={final_row[1]} "
            f"active_contracts={final_row[2]}"
        )
