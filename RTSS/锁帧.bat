@echo off
pushd "%~dp0"

if not exist "%CD%\lock_fps.ps1" (
    echo Cannot find lock_fps.ps1 (should be in same folder as this bat).
    popd
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -STA -File "%CD%\lock_fps.ps1"

if errorlevel 1 (
    echo.
    echo [LockFPS] Error - see %TEMP%\lock_fps_error.log
    popd
    pause
    exit /b 1
)

popd
