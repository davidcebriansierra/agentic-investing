<#
.SYNOPSIS
    Ejecuta la bateria de pruebas del nucleo deterministico del sistema agentico.

.DESCRIPTION
    Prepara el entorno (opcionalmente un venv), instala las dependencias minimas
    necesarias para los tests y lanza pytest.

    La red corporativa bloquea PyPI publico (SSL handshake). Por eso la instalacion
    de dependencias es OPCIONAL y permite indicar un indice interno mediante el
    parametro -IndexUrl o la variable de entorno PIP_INDEX_URL.

.PARAMETER Install
    Si se indica, instala/actualiza las dependencias antes de ejecutar los tests.

.PARAMETER UseVenv
    Si se indica, crea/usa un entorno virtual en .venv dentro del proyecto.

.PARAMETER IndexUrl
    URL del indice pip a utilizar (p. ej. el repositorio interno de Artifactory).
    Si no se indica, se usa PIP_INDEX_URL del entorno o el indice por defecto de pip.

.PARAMETER Coverage
    Si se indica, ejecuta los tests con informe de cobertura (requiere pytest-cov).

.EXAMPLE
    ./scripts/run_tests.ps1
    Ejecuta los tests asumiendo que las dependencias ya estan instaladas.

.EXAMPLE
    ./scripts/run_tests.ps1 -Install -UseVenv -IndexUrl https://artifactory.corp/api/pypi/pypi/simple
    Crea un venv, instala dependencias desde el indice interno y ejecuta los tests.
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$UseVenv,
    [string]$IndexUrl = $env:PIP_INDEX_URL,
    [switch]$Coverage
)

$ErrorActionPreference = "Stop"

# Raiz del proyecto = carpeta padre de este script.
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
Write-Host "[run_tests] Proyecto: $ProjectRoot" -ForegroundColor Cyan

# Selecciona el interprete de Python (venv o global).
$Python = "python"
if ($UseVenv) {
    $VenvPath = Join-Path $ProjectRoot ".venv"
    if (-not (Test-Path $VenvPath)) {
        Write-Host "[run_tests] Creando entorno virtual en .venv ..." -ForegroundColor Yellow
        python -m venv $VenvPath
    }
    $Python = Join-Path $VenvPath "Scripts/python.exe"
}

Write-Host "[run_tests] Interprete: $Python" -ForegroundColor Cyan
& $Python --version

# Dependencias minimas para ejecutar los tests del nucleo deterministico.
$Deps = @(
    "pydantic>=2.6",
    "PyYAML>=6.0",
    "pytest>=8.0",
    "pytest-asyncio>=0.23"
)
if ($Coverage) { $Deps += "pytest-cov>=5.0" }

if ($Install) {
    $pipArgs = @("-m", "pip", "install", "--upgrade")
    if ($IndexUrl) {
        Write-Host "[run_tests] Usando indice pip: $IndexUrl" -ForegroundColor Yellow
        $pipArgs += @("--index-url", $IndexUrl)
        # Permite host de confianza si el indice es http o tiene certificado interno.
        try {
            $hostName = ([System.Uri]$IndexUrl).Host
            if ($hostName) { $pipArgs += @("--trusted-host", $hostName) }
        } catch { }
    }
    $pipArgs += $Deps
    Write-Host "[run_tests] Instalando dependencias ..." -ForegroundColor Yellow
    & $Python @pipArgs
}

# Ejecuta pytest. PYTHONPATH se fija para resolver el paquete `src` y `tests`.
$env:PYTHONPATH = $ProjectRoot

$pytestArgs = @("-m", "pytest", "-v")
if ($Coverage) { $pytestArgs += @("--cov=src", "--cov-report=term-missing") }

Write-Host "[run_tests] Lanzando pytest ..." -ForegroundColor Cyan
& $Python @pytestArgs
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "[run_tests] OK: todos los tests han pasado." -ForegroundColor Green
} else {
    Write-Host "[run_tests] FALLO: pytest devolvio codigo $exitCode." -ForegroundColor Red
}
exit $exitCode
