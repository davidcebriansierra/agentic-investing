<#
.SYNOPSIS
    Ejecuta la bateria de pruebas dentro de un contenedor Docker (sin instalar Python
    ni dependencias en el host). Pensado para Windows 11 con Docker Desktop.

.DESCRIPTION
    Monta el repositorio en un contenedor python:3.11-slim, instala las dependencias de
    test desde requirements-dev.txt y lanza pytest. No deja rastro en el host.

.PARAMETER IndexUrl
    Indice pip alternativo (si el contenedor no llega a PyPI publico).

.PARAMETER Coverage
    Ejecuta con informe de cobertura.

.EXAMPLE
    ./scripts/run_tests_docker.ps1

.EXAMPLE
    ./scripts/run_tests_docker.ps1 -Coverage
#>
[CmdletBinding()]
param(
    [string]$IndexUrl = $env:PIP_INDEX_URL,
    [switch]$Coverage
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Write-Host "[docker-tests] Proyecto: $ProjectRoot" -ForegroundColor Cyan

# Verifica que Docker esta disponible.
try {
    docker version --format '{{.Server.Version}}' | Out-Null
} catch {
    Write-Host "[docker-tests] Docker no esta disponible. Inicia Docker Desktop." -ForegroundColor Red
    exit 1
}

# Construye el comando pip con indice opcional.
$pipIndex = ""
if ($IndexUrl) {
    $hostName = ([System.Uri]$IndexUrl).Host
    $pipIndex = "--index-url $IndexUrl --trusted-host $hostName"
    Write-Host "[docker-tests] Usando indice pip: $IndexUrl" -ForegroundColor Yellow
}

$pytestCmd = "python -m pytest -v"
if ($Coverage) { $pytestCmd = "python -m pytest --cov=src --cov-report=term-missing" }

$inner = "set -e; " +
         "pip install --no-cache-dir $pipIndex -r requirements-dev.txt; " +
         "PYTHONPATH=. $pytestCmd"

Write-Host "[docker-tests] Lanzando contenedor python:3.11-slim ..." -ForegroundColor Cyan
docker run --rm `
    -v "${ProjectRoot}:/app" `
    -w /app `
    python:3.11-slim `
    bash -lc "$inner"

$exitCode = $LASTEXITCODE
if ($exitCode -eq 0) {
    Write-Host "[docker-tests] OK: todos los tests han pasado." -ForegroundColor Green
} else {
    Write-Host "[docker-tests] FALLO: pytest devolvio $exitCode." -ForegroundColor Red
}
exit $exitCode
