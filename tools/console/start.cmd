@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if exist "..\..\.venv\Scripts\python.exe" (
  "..\..\.venv\Scripts\python.exe" -B app.py
) else (
  "C:\veighna_studio\python.exe" -B app.py
)
if errorlevel 1 pause
