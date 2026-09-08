@echo off
REM ============================================================
REM  一步到位：把 HWiNFO 官方便携包内嵌进 凝冰结魄，并打出安装包
REM  （安装 nbj 时会顺带把 HWiNFO 部署好，监控卡片开箱即用）
REM
REM  用法：
REM    build_installer_with_hwinfo.bat
REM        —— 默认从 D:\下载\localsend\HWiNFO64-2.zip 取 HWiNFO
REM    build_installer_with_hwinfo.bat "路径\HWiNFO64-xxx.zip"
REM        —— 指定官方 HWiNFO64 便携 zip（或直接的 HWiNFO64.exe）
REM
REM  注意：HWiNFO 再分发条款需你自行核对
REM        https://www.hwinfo.com/files/license.pdf
REM ============================================================
chcp 65001 >nul
setlocal EnableExtensions

cd /d "%~dp0" || (echo [ERROR] 无法切换到脚本目录 & pause & exit /b 1)

set "SRC=%~1"
if "%SRC%"=="" set "SRC=D:\下载\localsend\HWiNFO64-2.zip"

if not exist "%SRC%" (
    echo [ERROR] 找不到 HWiNFO 源文件: %SRC%
    echo.
    echo   请把官方 HWiNFO64 便携 zip（或 HWiNFO64.exe）作为第一个参数传入，例如：
    echo     build_installer_with_hwinfo.bat "D:\下载\localsend\HWiNFO64-2.zip"
    echo.
    echo   或先把官方便携包里的 HWiNFO64.exe 放進：
    echo     src\ningbingjue\vendor\HWiNFO\
    echo   然后直接双击本脚本（无需参数）。
    echo.
    pause
    exit /b 1
)

echo [INFO] HWiNFO 源: %SRC%
echo [INFO] 开始：提取 HWiNFO + 构建冻结文件夹 + 生成安装包 ...
echo.

REM 委托 make_installer.bat 处理 venv/依赖/构建/安装包；--bundle-hwinfo 让菜单跳过。
call make_installer.bat --bundle-hwinfo --hwinfo-src "%SRC%"
set "RC=%ERRORLEVEL%"

echo.
if %RC%==0 (
    echo [OK] 完成。安装包: dist\NingBingJue-Setup.exe（已内嵌 HWiNFO）
) else (
    echo [FAIL] rc=%RC%。查看 make_installer.log 排错。
)
echo.
pause
