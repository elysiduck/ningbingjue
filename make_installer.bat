@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
REM ============================================================
REM  NingBingJue 一键打包安装器
REM  双击运行。需要已存在 .build_venv。
REM  可选参数透传给 make_installer.py，例如：
REM    make_installer.bat --no-build
REM  全部输出同时写入 make_installer.log。
REM  默认行为：只打包程序（py代码），不打包任何用户配置；
REM  装到已有凝冰结魄时不会覆盖你的现有配置文件。
REM  如需把本机配置也打进安装包，运行时会询问要排除哪些。
REM ============================================================

cd /d "%~dp0" || (echo [ERROR] 无法切换到脚本目录 & pause & exit /b 1)

set "LOG=%~dp0make_installer.log"
echo [%date% %time%] === START === > "%LOG%"
echo CWD: %CD% >> "%LOG%"

if not exist ".build_venv\Scripts\python.exe" (
    echo [ERROR] .build_venv\Scripts\python.exe 未找到 >> "%LOG%"
    echo.
    echo [ERROR] 构建 venv 缺失。请先创建：
    echo   runtime\python\python.exe -m venv --system-site-packages .build_venv
    echo   runtime\python\python.exe -m pip install pyinstaller -r requirements.txt
    echo   （venv 用 --system-site-packages 继承 runtime\python 自带包，含 pygame/PyQt6 等）
    echo   详见 make_installer.log。
    echo.
    pause
    exit /b 1
)

echo Using python: %CD%\.build_venv\Scripts\python.exe >> "%LOG%"

REM --- 幂等同步构建依赖（声明驱动；已满足则秒过，失败不阻断）---
echo Syncing build deps from requirements.txt... >> "%LOG%"
call .build_venv\Scripts\pip install --quiet pyinstaller -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 (
    echo [warn] 依赖同步失败（可能离线）。runtime\python 自带包仍可支撑构建。 >> "%LOG%"
)

REM --- 配置打包选项（交互菜单）---
REM  若已显式传入 --no-prompt / --exclude / --exclude-all / --include-all，
REM  则跳过交互菜单，直接按命令行参数处理。
set "SKIP_MENU=0"
if not "%~1"=="" (
    for %%a in (%*) do (
        if "%%a"=="--no-prompt" set "SKIP_MENU=1"
        if "%%a"=="--exclude" set "SKIP_MENU=1"
        if "%%a"=="--exclude-all" set "SKIP_MENU=1"
        if "%%a"=="--include-all" set "SKIP_MENU=1"
        if "%%a"=="--bundle-hwinfo" set "SKIP_MENU=1"
    )
)

if %SKIP_MENU%==0 (
    call :CONFIG_MENU
    set "MODE=PACKAGE_CONFIG"
    if "!CHOICE!"=="" set "MODE=PYONLY"
    if /i "!CHOICE!"=="0" set "MODE=PYONLY"
    if /i "!CHOICE!"=="a" set "MODE=PYONLY"
    if "!MODE!"=="PYONLY" (
        echo [INFO] 默认只打包程序（py代码），不打包任何用户配置 >> "%LOG%"
        call .build_venv\Scripts\python.exe make_installer.py %* >> "%LOG%" 2>&1
    ) else (
        call .build_venv\Scripts\python.exe make_installer.py %* --config-choice "!CHOICE!" >> "%LOG%" 2>&1
    )
) else (
    call .build_venv\Scripts\python.exe make_installer.py %* >> "%LOG%" 2>&1
)
set "RC=%ERRORLEVEL%"
echo. >> "%LOG%"
echo [%date% %time%] === DONE rc=%RC% === >> "%LOG%"

echo.
if %RC%==0 (
    echo [OK] 安装包已生成: dist\NingBingJue-Setup.exe
) else (
    echo [FAIL] rc=%RC%。打开 make_installer.log 查看错误。
)
echo Log: %LOG%
echo.
pause
goto :eof


:CONFIG_MENU
echo.
echo ============================================
echo   配置打包选项  (NingBingJue 安装包)
echo ============================================
echo   【默认】只打包程序（py代码），不打包任何用户配置。
echo   装到已有凝冰结魄时，你的现有配置不会被覆盖。
echo.
echo   如需把【本机当前配置】也打进安装包（例如换新机一次性
echo   迁移），请选择要【排除(不打进安装包)】的配置类别，
echo   其余类别将保留并随包分发；安装后对这些项沿用出厂默认。
echo.
echo   [1]  磁贴卡片配置  (主磁贴 + 额外区块)
echo   [2]  卡片配色 CardColors
echo   [3]  卡片布局/分页 (Cards / 页面)
echo   [4]  主题配色 Theme
echo   [5]  呼吸灯 Breath
echo   [6]  托盘背景 Tray
echo   [7]  焦点蒙层 Focus
echo   [8]  电源设置 Power
echo   [9]  进程冻结设置 Freeze
echo   [10] 进程监控配置 ProcMon
echo   [11] 手柄/键盘绑定 Input
echo   [12] 按钮样式 ButtonStyle
echo   [13] 降压配置 config/undervolt.json
echo   [14] 按钮数据 buttons/
echo.
echo   请输入要【排除】的编号 (逗号或空格分隔)：
echo     直接回车 / 0 = 只打包程序（py代码），不打包任何配置  ← 推荐默认
echo     输入 N   = 打包全部当前配置 (不排任何)
echo     输入 A   = 只打包程序 (排除全部配置，等价于回车/0)
echo.
set "CHOICE="
set /p CHOICE=请输入选择: 
goto :eof
