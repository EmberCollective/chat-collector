@echo off
setlocal
chcp 65001 >nul
if not exist "%~dp0.runtime\python\python.exe" (
  call "%~dp0Install.cmd"
  exit /b %ERRORLEVEL%
)
"%~dp0.runtime\python\python.exe" "%~dp0tools\install\setup.py"
if errorlevel 1 pause
