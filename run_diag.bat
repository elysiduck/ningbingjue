@echo off
cd /d "%~dp0"
runtime\python\python.exe gpu_cpu_diag.py
echo.
echo [done] press any key to close
pause >nul
