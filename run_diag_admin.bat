@echo off
net session >nul 2>&1
if %errorLevel% == 0 (
  cd /d "%~dp0"
  runtime\python\python.exe gpu_cpu_diag.py
  echo.
  echo [done] press any key to close
  pause >nul
) else (
  powershell -Command "Start-Process '%~f0' -Verb RunAs"
)
