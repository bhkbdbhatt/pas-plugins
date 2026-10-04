# syntax=docker/dockerfile:1
#
# One image definition, seven plugin entry points. Building a separate image per
# plugin would mean seven near-identical Dockerfiles drifting apart; instead the
# command selects the app module and everything else - dependencies, user, health
# check - stays in one place.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

# curl is used by the container healthcheck below; it is installed once here
# rather than assumed present in the base image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies are installed before the source is copied so that editing code does
# not invalidate the dependency layer on every build.
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir \
      "fastapi>=0.115" \
      "uvicorn[standard]>=0.32" \
      "pydantic>=2.9" \
      "pydantic-settings>=2.6" \
      "httpx>=0.27" \
      "numpy>=2.1" \
      "openpyxl>=3.1" \
      "reportlab>=4.2" \
      "pyyaml>=6.0" \
      "structlog>=24.4" \
      "python-multipart>=0.0.17"

COPY src ./src
COPY scripts ./scripts

# Run as an unprivileged user. The service writes nothing to its own filesystem, so
# the root filesystem can be mounted read-only in the pod spec.
RUN useradd --create-home --uid 10001 pas \
 && chown -R pas:pas /app
USER pas

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PLUGIN_PORT:-8000}/health" || exit 1

# PLUGIN_PORT is set per deployment; the entrypoint derives the module name from it
# so one image serves all seven plugins without a rebuild.
ENV PLUGIN_MODULE=pas_plugins.plugin1_gateway.main \
    PLUGIN_PORT=8001

CMD ["sh", "-c", "exec uvicorn ${PLUGIN_MODULE}:app --host 0.0.0.0 --port ${PLUGIN_PORT}"]