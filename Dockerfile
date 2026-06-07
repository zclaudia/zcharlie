FROM python:3.13-slim AS builder

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ src/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# ---- Runtime ----
FROM python:3.13-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 sqlite3 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY src/ src/
COPY config/ config/
COPY alembic/ alembic/
COPY alembic.ini ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

CMD ["python", "-c", "print('investment-agent container ready')"]
