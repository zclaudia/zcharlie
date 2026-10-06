FROM python:3.13-slim AS builder

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ src/
# Editable install on purpose: the runtime image ships the same /app/src tree
# (web templates/static are resolved relative to the source checkout and are
# not declared as package data), so the code lives in the
# image and compose no longer bind-mounts src/.
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# ---- Runtime ----
FROM python:3.13-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 sqlite3 curl && \
    rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user; only /app/data is writable by it.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --system --gid "${APP_GID}" app && \
    useradd --system --uid "${APP_UID}" --gid app --home-dir /app \
        --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY src/ src/
COPY config/ config/
COPY alembic/ alembic/
COPY alembic.ini ./
RUN mkdir -p /app/data && chown -R app:app /app/data

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

USER app

EXPOSE 8000

# Default to API; docker-compose overrides for scheduler service
CMD ["ia", "api", "serve", "--host", "0.0.0.0", "--port", "8000"]
