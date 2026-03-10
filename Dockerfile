# ── Stage 1: Build ────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies
RUN pip install --no-cache-dir hatchling

# Copy project metadata first (cache-friendly layer ordering)
COPY pyproject.toml ./
COPY tripwire/ ./tripwire/

# Build a wheel and install dependencies into a virtual env
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir .


# ── Stage 2: Runtime ─────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Security: non-root user
RUN groupadd --gid 1000 tripwire && \
    useradd --uid 1000 --gid tripwire --create-home tripwire

# Copy virtual env from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
WORKDIR /app
COPY tripwire/ ./tripwire/

# Own files as non-root user
RUN chown -R tripwire:tripwire /app
USER tripwire

EXPOSE 3402

# Health check against the /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:3402/health')" || exit 1

CMD ["uvicorn", "tripwire.main:app", "--host", "0.0.0.0", "--port", "3402"]
