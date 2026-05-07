# syntax=docker/dockerfile:1.6
#
# ProPaths backend image. Multi-stage: a Python builder stage resolves
# deps into a wheelhouse, then a slim runtime copies only what's needed.
# Frontend assets are already in static/ — no bundler needed for the
# existing HTML/JS. The React island (see react-app/) is built in its
# own stage below.

# ─────────────────────────────────────────────────────────────────────
# Stage 1: Python deps
# ─────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS python-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# System deps needed to compile wheels (psycopg2, etc).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      libpq-dev \
 && rm -rf /var/lib/apt/lists/*

# Copy only requirement manifest first — Docker layer cache hits when
# code changes but deps don't.
COPY requirements.txt .
RUN pip wheel --wheel-dir=/wheels -r requirements.txt

# ─────────────────────────────────────────────────────────────────────
# Stage 2: React build (if react-app/ exists)
# ─────────────────────────────────────────────────────────────────────
FROM node:20-alpine AS react-builder

WORKDIR /react

# Copy package manifests first for layer cache.
COPY react-app/package.json react-app/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

# Copy the rest and build.
COPY react-app/ ./
RUN npm run build

# ─────────────────────────────────────────────────────────────────────
# Stage 3: Runtime
# ─────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PROPATHS_HOME=/app \
    FLASK_ENV=production

# Runtime libs for psycopg2 (only the runtime shared object, not -dev).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpq5 \
      curl \
 && rm -rf /var/lib/apt/lists/*

# Drop root privileges for the Flask process.
RUN useradd --create-home --shell /bin/bash propaths
WORKDIR /app

# Install Python deps from the builder's wheelhouse.
COPY --from=python-builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
 && rm -rf /wheels

# Copy the source tree.
COPY --chown=propaths:propaths . .

# Copy built React assets into static/ so Flask serves them at
# /static/react/. This is the opt-in island mount; the rest of static/
# stays as vanilla HTML + D3.
COPY --from=react-builder --chown=propaths:propaths /react/dist /app/static/react

# Flask listens on 5000; gunicorn wraps it in production.
EXPOSE 5000

USER propaths

# Default: gunicorn with sane worker / thread settings for SSE.
# Threaded workers (gthread) because SSE streams are long-lived and
# block synchronous workers. Override via CMD in compose / k8s.
CMD ["gunicorn", \
     "--bind=0.0.0.0:5000", \
     "--workers=2", \
     "--threads=8", \
     "--worker-class=gthread", \
     "--timeout=120", \
     "--access-logfile=-", \
     "--error-logfile=-", \
     "app:app"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:5000/ || exit 1
