@echo off
setlocal
cd /d "%~dp0"
title Model Console
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting things up. This takes about a minute and needs internet.
  %PY% -m venv .venv || goto :fail
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :fail
)
set "PYTHONPATH=%~dp0src"
".venv\Scripts\python.exe" -m aimodels dashboard
goto :eof
:fail
echo.
echo Setup failed. Please install Python 3.10 or newer from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" in the installer), then double-click this file again.
pause
