@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0runtime-deps;%PYTHONPATH%"
python -B -m collector %*
exit /b %ERRORLEVEL%
