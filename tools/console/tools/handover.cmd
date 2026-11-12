@echo off
setlocal
cd /d "%~dp0.."
set "PYTHONUTF8=1"
"C:\veighna_studio\python.exe" -B tools\handover.py
pause
