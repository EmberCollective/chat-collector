@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo 此工具需要管理员权限：请右键本文件，选择“以管理员身份运行”。
if exist "%~dp0..\.runtime\python\python.exe" (
  "%~dp0..\.runtime\python\python.exe" -B "%~dp0qq_recover.py"
) else (
  "C:\veighna_studio\python.exe" -B "%~dp0qq_recover.py"
)
echo.
pause
