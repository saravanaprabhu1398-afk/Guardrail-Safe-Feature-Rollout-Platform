# Local and Snowflake setup

## 1. Install and verify

Prerequisites are Git, `uv`, Docker Desktop for the container workflow, and optional
Snowflake access for persisted telemetry and decisions.

```bash
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv run guardrail-phase6-demo --scenario all
```

The offline path needs no cloud credentials. The three demo scenarios must report
`passed: true`.

To exercise production configuration, TLS, OIDC/RBAC, durable PostgreSQL state,
secured Redpanda, backup/restore, and dependency recovery in an isolated Docker
project, use the [local production acceptance runbook](local-production-acceptance.md).

## 2. Configure Snowflake without exposing the PAT

Copy `.env.example` to `.env` and fill only the non-secret account and user settings.
Create `.secrets/snowflake_pat`, write the role-restricted PAT into that file, and set
its mode to `600`. Never export, print, or commit the token.

Run the account bootstrap once in Snowsight as `ACCOUNTADMIN`:

```text
snowflake/bootstrap/01_platform.sql
```

All routine commands then use `GUARDRAIL_ENGINEER`:

```bash
uv run guardrail-snowflake-check
uv run guardrail-snowflake-setup-ingestion
uv run guardrail-snowflake-setup-analysis
uv run guardrail-snowflake-setup-rollout
uv run guardrail-metric-compile metrics/*.yml --persist --activate
```

Every setup command is idempotent. The metric command should return four `ACTIVE`
version-1 artifacts.

## 3. Start the API and UI natively

Use two terminals from the repository root:

```bash
uv run uvicorn guardrail.main:app --reload
```

```bash
uv run guardrail-ui
```

Open the API documentation at <http://localhost:8000/docs> and Guardrail Control at
<http://localhost:8501>. The UI calls the API through `GUARDRAIL_API_URL`, which
defaults to `http://localhost:8000`.

## 4. Start the container stack

```bash
docker compose up --build
```

This starts Redpanda, Redpanda Console, the API, and Guardrail Control. Include the
Snowflake sink worker when persisted transport is required:

```bash
docker compose --profile snowflake up --build
```

Expected health endpoints:

```text
http://localhost:8000/health
http://localhost:8501/_stcore/health
http://localhost:8080
```

## 5. Load analytical data

Create and launch an experiment from the UI or API, then run a simulation with
`persist_events: true`. After the sink commits and Snowflake Dynamic Tables refresh:

```bash
uv run guardrail-dbt build
uv run guardrail-dbt source freshness
uv run guardrail-phase3-smoke
```

The Phase 3 smoke result must show non-empty facts and zero invariant violations before
integrity or rollout decisions are interpreted.

## Troubleshooting

- `could not read Username for 'https://github.com'` means this process has no GitHub
  credential helper. Push from an authenticated terminal or configure a credential
  helper; do not place a token in the remote URL.
- A Phase 4 or Phase 5 CLI exit status of `2` is an expected safety result for
  `BLOCKED`, `PAUSE`, or `TERMINATE`, not an infrastructure crash.
- A Snowflake `503` from the API means evidence or persistence was unavailable. Check
  the PAT file, network policy, role, warehouse, and setup commands in that order.
- An empty result after simulation usually means the sink or Dynamic Table refresh has
  not completed. Check reconciliation before rerunning analysis.
