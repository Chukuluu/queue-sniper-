@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo The virtual environment is missing. Run install.bat first.
  pause
  exit /b 1
)

echo Installing/updating PyInstaller...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --upgrade pyinstaller
if errorlevel 1 (
  echo Could not install PyInstaller.
  pause
  exit /b 1
)

echo.
echo Building QueueSniper.exe (this takes a minute)...
".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm --clean --onefile --console ^
  --name QueueSniper ^
  --distpath "." ^
  --workpath "build" ^
  --specpath "build" ^
  sniper.py
if errorlevel 1 (
  echo.
  echo Build failed. Scroll up for the error.
  pause
  exit /b 1
)

echo.
echo Built: %~dp0QueueSniper.exe
echo.
echo Put config.json and .env next to the exe (--setup does this for you).
echo Double-click QueueSniper.exe to start, or run:
echo   QueueSniper.exe --setup
echo   QueueSniper.exe --list
echo   QueueSniper.exe --dry-run
echo   QueueSniper.exe --test-alarm
echo   QueueSniper.exe --self-test
pause
