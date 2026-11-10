FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy package metadata and source before installing so the editable install discovers
# the package (and its static/config/eval data) at build time.
COPY pyproject.toml ./
COPY blackbox ./blackbox
RUN pip install --upgrade pip && pip install -e ".[rag,db]"

COPY seed ./seed
COPY scripts ./scripts
COPY .env.example ./.env.example

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "blackbox.main:app", "--host", "0.0.0.0", "--port", "8000"]
