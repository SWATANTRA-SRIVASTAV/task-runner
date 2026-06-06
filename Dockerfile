# ── Stage 1: builder ──────────────────────────────────────────────────────────
# Install dependencies into a venv so we can copy just the venv into the
# runtime stage — no build tools (gcc, pip, wheel caches) in the final image.
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build deps (needed for some C-extension wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime ──────────────────────────────────────────────────────────
# Start from a clean base. Copy only the venv and application code.
# Result: ~20MB image vs ~780MB if we built in a single stage.
FROM python:3.12-slim AS runtime

# Create a non-root user. Running as root inside a container doesn't
# give full host access (namespaces protect that), but it does mean a
# container escape escalates directly to root on the host. Avoidable.
RUN groupadd --gid 1001 appgroup \
    && useradd --uid 1001 --gid appgroup --no-create-home appuser

WORKDIR /app

# Copy the pre-built venv from builder
COPY --from=builder /opt/venv /opt/venv

# Copy application source
COPY app/ ./app/

# Use the venv Python
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOG_LEVEL=INFO

# Drop to non-root
USER appuser

EXPOSE 8000

# Liveness probe endpoint — Docker will mark the container unhealthy
# after 3 consecutive failures, triggering a restart.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
