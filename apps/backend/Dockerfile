# ---------------------------------------------------------------------------
# OpsPilot Backend — FastAPI + LangGraph Agent
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy project manifests first for layer caching
COPY pyproject.toml ./
COPY src/ ./src/

# Sync dependencies (no dev extras in production image)
RUN uv pip install --system .

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production \
    OPSPILOT_SIM_HOST=simulator \
    OPSPILOT_SIM_PORT=9000

WORKDIR /app

# Install runtime deps via uv for consistency
COPY --from=builder /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn

COPY src/ ./src/
COPY pyproject.toml ./

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')" || exit 1

CMD ["uvicorn", "opspilot_backend.main:app", \
     "--host", "0.0.0.0", "--port", "8000"]
