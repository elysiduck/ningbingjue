@echo off
REM build.bat - Compile fake XInput DLLs (MSVC Build Tools 18 required)
REM
REM Output: xinput1_3.dll, xinput1_4.dll, XInput9_1_0.dll (64-bit)

setlocal

set "VCVARS=C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsall.bat"

if not exist "%VCVARS%" (
    echo ERROR: vcvarsall.bat not found
    echo   %VCVARS%
    echo Please install MSVC Build Tools.
    exit /b 1
)

call "%VCVARS%" x64
if errorlevel 1 (
    echo ERROR: vcvarsall.bat failed
    exit /b 1
)

cd /d "%~dp0"

echo.
echo === Building xinput1_3.dll ===
cl /nologo /LD /O2 /DWANT_XINPUT13 /Fe:xinput1_3.dll xinput_gate.c /link /DEF:xinput1_3.def
if errorlevel 1 goto :failed

echo.
echo === Building xinput1_4.dll ===
cl /nologo /LD /O2 /DWANT_XINPUT14 /Fe:xinput1_4.dll xinput_gate.c /link /DEF:xinput1_4.def
if errorlevel 1 goto :failed

echo.
echo === Building XInput9_1_0.dll ===
cl /nologo /LD /O2 /DWANT_XINPUT910 /Fe:XInput9_1_0.dll xinput_gate.c /link /DEF:XInput9_1_0.def
if errorlevel 1 goto :failed

echo.
echo === Build succeeded ===
echo Verifying exports...
dumpbin /exports xinput1_3.dll | findstr "XInput"
dumpbin /exports xinput1_4.dll | findstr "XInput"
dumpbin /exports XInput9_1_0.dll | findstr "XInput"

del /q *.obj *.exp *.lib 2>nul

exit /b 0

:failed
echo.
echo === Build FAILED ===
exit /b 1
