@echo off
cd /d "%~dp0\..\.."
if exist "C:\veighna_studio\python.exe" (
  "C:\veighna_studio\python.exe" "%~dp0launch.py" stop
) else (
  python "%~dp0launch.py" stop
)
