FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Install dependencies first so they're cached separately from source changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY database.py dashboard.py dashboard.html errors.py main.py schemas.py storage.py worker.py ./

ENV PATH="/app/.venv/bin:$PATH"

# RELAY_DATABASE_URL is expected to be provided at run time (compose.yaml
# points it at the postgres service); it falls back to a local SQLite file
# only when the app is run standalone without that override.
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
