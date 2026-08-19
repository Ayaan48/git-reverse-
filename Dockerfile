# Long-running deployment target (Render, Fly.io, Railway, Cloud Run, ECS...).
#
# Prefer this over a serverless function for real workloads: analyzing a large
# repository routinely takes longer than a serverless request budget allows,
# and this image ships a git binary, which enables the higher-fidelity
# repository backend.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/

ENV PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    HEALING_AGENT_WORKSPACE=/tmp/healing-agent-workspaces

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"

CMD ["uvicorn", "healing_agent.app:app", "--host", "0.0.0.0", "--port", "8000"]
