#!/usr/bin/env bash
#
# Ejecuta la bateria de pruebas en una maquina con acceso normal a PyPI
# (Linux / macOS / WSL). Crea un venv, instala las dependencias de test y lanza pytest.
#
# Uso:
#   ./scripts/run_tests.sh                 # crea .venv, instala y ejecuta
#   ./scripts/run_tests.sh --no-venv       # usa el python actual sin crear venv
#   ./scripts/run_tests.sh --no-install    # no instala dependencias
#   ./scripts/run_tests.sh --coverage      # ejecuta con informe de cobertura
#   INDEX_URL=https://indice/simple ./scripts/run_tests.sh   # indice pip alternativo
#
set -euo pipefail

# Raiz del proyecto = carpeta padre de este script.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

USE_VENV=1
INSTALL=1
COVERAGE=0
for arg in "$@"; do
  case "$arg" in
    --no-venv)    USE_VENV=0 ;;
    --no-install) INSTALL=0 ;;
    --coverage)   COVERAGE=1 ;;
    *) echo "Argumento desconocido: $arg"; exit 2 ;;
  esac
done

echo "[run_tests] Proyecto: $PROJECT_ROOT"

PYTHON="python3"
if [ "$USE_VENV" -eq 1 ]; then
  if [ ! -d ".venv" ]; then
    echo "[run_tests] Creando entorno virtual en .venv ..."
    python3 -m venv .venv
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  PYTHON="python"
fi

"$PYTHON" --version

PIP_ARGS=()
if [ -n "${INDEX_URL:-}" ]; then
  echo "[run_tests] Usando indice pip: $INDEX_URL"
  PIP_ARGS+=(--index-url "$INDEX_URL")
fi

if [ "$INSTALL" -eq 1 ]; then
  echo "[run_tests] Instalando dependencias ..."
  "$PYTHON" -m pip install --upgrade pip "${PIP_ARGS[@]}"
  "$PYTHON" -m pip install "${PIP_ARGS[@]}" -r requirements-dev.txt
fi

export PYTHONPATH="$PROJECT_ROOT"

PYTEST_ARGS=(-v)
if [ "$COVERAGE" -eq 1 ]; then
  PYTEST_ARGS+=(--cov=src --cov-report=term-missing)
fi

echo "[run_tests] Lanzando pytest ..."
"$PYTHON" -m pytest "${PYTEST_ARGS[@]}"
echo "[run_tests] OK: todos los tests han pasado."
