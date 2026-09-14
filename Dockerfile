FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SCENICPILOT_TOOL_MODE=offline \
    SCENICPILOT_LLM_PROVIDER=offline \
    SCENICPILOT_CHECKPOINT_DB_PATH=/app/data/runtime/scenicpilot.sqlite3

WORKDIR /app

COPY pyproject.toml README.md ./
COPY scenic_pilot ./scenic_pilot
COPY web ./web
COPY data/demo ./data/demo

RUN python -m pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 scenicpilot \
    && mkdir -p /app/data/runtime \
    && chown -R scenicpilot:scenicpilot /app/data/runtime

USER scenicpilot
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=2)"]

CMD ["uvicorn", "scenic_pilot.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
