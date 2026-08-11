# Imagen de la aplicacion del Sistema Agentico de Inversion.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Dependencias del sistema minimas.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Instalar dependencias primero (mejor cache de capas).
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config

# Instala el paquete con los extras necesarios para ejecutar la app.
# NOTA: en red corporativa, pasar el indice interno con --build-arg PIP_INDEX_URL=...
ARG PIP_INDEX_URL=
RUN if [ -n "$PIP_INDEX_URL" ]; then \
        pip install --index-url "$PIP_INDEX_URL" -e ".[infra]"; \
    else \
        pip install -e ".[infra]"; \
    fi

# Usuario no root.
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/metrics || exit 1

CMD ["python", "-m", "src.app.main"]
