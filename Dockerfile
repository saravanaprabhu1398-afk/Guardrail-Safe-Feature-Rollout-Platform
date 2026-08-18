# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim AS runtime

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN groupadd --gid 10001 guardrail \
    && useradd --uid 10001 --gid guardrail --no-create-home --shell /usr/sbin/nologin guardrail

WORKDIR /app
COPY --from=builder --chown=guardrail:guardrail /app/.venv /app/.venv
COPY --chown=guardrail:guardrail src ./src
COPY --chown=guardrail:guardrail migrations ./migrations
COPY --chown=guardrail:guardrail alembic.ini README.md ./
COPY --chown=guardrail:guardrail contracts ./contracts
COPY --chown=guardrail:guardrail metrics ./metrics
COPY --chown=guardrail:guardrail snowflake ./snowflake

USER 10001:10001
EXPOSE 8000 8501

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"

CMD ["guardrail-api"]
