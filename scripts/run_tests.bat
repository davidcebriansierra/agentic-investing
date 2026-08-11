@echo off
REM Lanza la bateria de pruebas usando el script PowerShell run_tests.ps1.
REM Pasa cualquier argumento adicional directamente al script (p. ej. -Install -UseVenv).
setlocal
set SCRIPT_DIR=%~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%run_tests.ps1" %*
exit /b %ERRORLEVEL%
