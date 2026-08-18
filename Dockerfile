FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY contracts ./contracts
COPY metrics ./metrics
COPY snowflake ./snowflake

RUN uv sync --frozen --no-dev

EXPOSE 8000 8501

CMD ["uv", "run", "--no-dev", "uvicorn", "guardrail.main:app", "--host", "0.0.0.0", "--port", "8000"]
