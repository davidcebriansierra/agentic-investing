#!/usr/bin/env bash
#
# Ejecuta la bateria de pruebas dentro de un contenedor Docker (sin instalar nada en
# el host). Pensado para WSL / Linux / macOS con Docker.
#
# Uso:
#   ./scripts/run_tests_docker.sh                 # ejecuta los tests
#   ./scripts/run_tests_docker.sh --coverage      # con cobertura
#   INDEX_URL=https://indice/simple ./scripts/run_tests_docker.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
echo "[docker-tests] Proyecto: $PROJECT_ROOT"

if ! docker version >/dev/null 2>&1; then
  echo "[docker-tests] Docker no esta disponible. Inicia Docker Desktop / el daemon." >&2
  exit 1
fi

PIP_INDEX=""
if [ -n "${INDEX_URL:-}" ]; then
  HOST="$(echo "$INDEX_URL" | sed -E 's#https?://([^/]+).*#\1#')"
  PIP_INDEX="--index-url $INDEX_URL --trusted-host $HOST"
  echo "[docker-tests] Usando indice pip: $INDEX_URL"
fi

PYTEST_CMD="python -m pytest -v"
for arg in "$@"; do
  if [ "$arg" = "--coverage" ]; then
    PYTEST_CMD="python -m pytest --cov=src --cov-report=term-missing"
  fi
done

INNER="set -e; pip install --no-cache-dir $PIP_INDEX -r requirements-dev.txt; PYTHONPATH=. $PYTEST_CMD"

echo "[docker-tests] Lanzando contenedor python:3.11-slim ..."
docker run --rm \
  -v "$PROJECT_ROOT:/app" \
  -w /app \
  python:3.11-slim \
  bash -lc "$INNER"

echo "[docker-tests] OK: todos los tests han pasado."
