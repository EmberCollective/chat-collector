@echo off
cd /d "%~dp0\..\.."
if exist "%~dp0..\..\.runtime\python\python.exe" (
  "%~dp0..\..\.runtime\python\python.exe" "%~dp0launch.py"
) else if exist "C:\veighna_studio\python.exe" (
  "C:\veighna_studio\python.exe" "%~dp0launch.py"
) else (
  python "%~dp0launch.py"
)
