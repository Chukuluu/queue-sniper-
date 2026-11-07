@echo off
setlocal
cd /d "%~dp0"

set "PYEXE="
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE (
  where py >nul 2>nul
  if not errorlevel 1 set "PYEXE=py -3"
)
if not defined PYEXE (
  where python >nul 2>nul
  if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
  echo Python was not found.
  echo Install Python 3.12 from https://www.python.org/downloads/ and tick "Add to PATH".
  pause
  exit /b 1
)

echo Using: %PYEXE%
%PYEXE% -m venv .venv
if errorlevel 1 (
  echo Failed to create the virtual environment.
  pause
  exit /b 1
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency install failed. Scroll up for the error.
  pause
  exit /b 1
)

python -c "import discord, sys; print('OK - discord.py-self', discord.__version__, 'on Python', sys.version.split()[0])"
echo.
echo Install complete. Next:
echo   1. run.bat --setup
echo   2. run.bat
pause
