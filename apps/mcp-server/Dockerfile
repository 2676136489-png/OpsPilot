# ---------------------------------------------------------------------------
# OpsPilot MCP Server — Tool Server for Agent
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml ./
COPY src/ ./src/

RUN uv pip install --system .

# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages/ /usr/local/lib/python3.12/site-packages/

COPY src/ ./src/

EXPOSE 8001

# MCP server runs stdio by default; expose HTTP transport for Docker
CMD ["python", "-m", "opspilot_mcp_server.server"]
