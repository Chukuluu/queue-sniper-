@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo The virtual environment is missing. Run install.bat first.
  pause
  exit /b 1
)

echo Installing/updating build dependencies...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --upgrade pyinstaller pywebview
if errorlevel 1 (
  echo Could not install the build dependencies.
  pause
  exit /b 1
)

echo.
echo Building QueueSniperDashboard.exe (this takes a few minutes)...
".venv\Scripts\python.exe" -m PyInstaller ^
  --noconfirm --clean --onefile --windowed ^
  --name QueueSniperDashboard ^
  --distpath "." ^
  --workpath "build\dashboard" ^
  --specpath "build" ^
  --add-data "%CD%\dashboard;dashboard" ^
  --collect-all webview ^
  --hidden-import clr ^
  --hidden-import pythonnet ^
  --hidden-import webview.platforms.edgechromium ^
  dashboard_app.py
if errorlevel 1 (
  echo.
  echo Build failed. Scroll up for the error.
  pause
  exit /b 1
)

echo.
echo Built: %~dp0QueueSniperDashboard.exe
echo.
echo Keep config.json, .env and alarm.wav next to the exe.
echo Double-click QueueSniperDashboard.exe to open the dashboard and run the sniper.
pause
