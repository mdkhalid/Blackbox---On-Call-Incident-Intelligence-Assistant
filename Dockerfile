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

# Install python deps
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install -e ".[dev,rag,db]" 2>/dev/null || pip install -e ".[rag,db]"

# Copy app
COPY blackbox ./blackbox
COPY .env.example ./.env.example

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "blackbox.main:app", "--host", "0.0.0.0", "--port", "8000"]
