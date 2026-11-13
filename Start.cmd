@echo off
setlocal
if not exist "%~dp0.runtime\python\pythonw.exe" (
  echo Please run Install.cmd first.
  pause
  exit /b 1
)
start "" "%~dp0.runtime\python\pythonw.exe" "%~dp0tools\install\launcher.py" --open
