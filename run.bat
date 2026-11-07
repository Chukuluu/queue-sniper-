@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo The virtual environment is missing. Run install.bat first.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" sniper.py %*
echo.
pause
