# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS base

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade --yes \
    && rm -rf /var/lib/apt/lists/*

FROM base AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.15@sha256:e590846f4776907b254ac0f44b5b380347af5d90d668138ca7938d1b0c2f98d3 /uv /bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM base AS runtime

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
