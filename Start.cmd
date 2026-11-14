@echo off
setlocal
if not exist "%~dp0.runtime\python\pythonw.exe" (
  echo Please run Install.cmd first.
  pause
  exit /b 1
)
start "" /d "%~dp0" "%~dp0.runtime\python\pythonw.exe" "%~dp0tools\install\launcher.py" --open
