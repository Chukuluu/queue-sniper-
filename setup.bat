@echo off
cd /d "%~dp0"

if not exist "QueueSniper.exe" (
  echo QueueSniper.exe was not found next to this file.
  echo Run build.bat first, or use run.bat --setup from source.
  pause
  exit /b 1
)

QueueSniper.exe --setup
