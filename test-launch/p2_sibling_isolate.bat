@echo off
setlocal EnableExtensions

set "SCRIPT=%~dp0py-script\p2_sibling_isolate.py"
set "PY=%~dp0..\runtime\python\python.exe"
if not exist "%PY%" (
    echo [警告] 未找到内置 python：%PY%
    echo        回退到系统 python ...
    set "PY=python"
)
if not exist "%SCRIPT%" (
    echo [错误] P2 脚本缺失：%SCRIPT%
    echo        请保持 ningbingjue 文件夹结构完整。
    pause
    exit /b 1
)

:menu
cls
echo ============================================================
echo  P2 兄弟隔离启动器（单核喂频）
echo ============================================================
echo.
echo  本启动器封装 p2_sibling_isolate.py，请选择运行模式：
echo.
echo   1) 机制自检      在当前进程验证亲和读写（零风险，真机先跑这个）
echo.
echo   2) 只读演练      检测热核并报告将排除哪个兄弟，不修改系统
echo.
echo   3) 只读循环      持续报告，不修改系统（游戏请保持前台）
echo.
echo   4) 真施加+循环   排除热核的 SMT 兄弟（前台游戏），SKIP/Ctrl+C 自动还原
echo                    运行时请保持游戏在前台
echo.
echo   5) 真施加+指定PID 同 4，但指定进程 PID（不依赖前台焦点）
echo.
echo   q) 退出
echo.
set "CHOICE="
set /p "CHOICE=请输入选项 [1-5/q]: "

if "%CHOICE%"=="1" goto m_selftest
if "%CHOICE%"=="2" goto m_dry_once
if "%CHOICE%"=="3" goto m_dry_loop
if "%CHOICE%"=="4" goto m_apply_loop
if "%CHOICE%"=="5" goto m_apply_pid
if /i "%CHOICE%"=="q" goto m_quit
echo [!] 无效选项，请重试。
echo.
pause
goto menu

:m_selftest
echo.
echo [模式] 机制自检（零风险）
"%PY%" "%SCRIPT%" --selftest
goto after

:m_dry_once
echo.
echo [模式] 只读演练（不修改系统）
"%PY%" "%SCRIPT%" --out "%~dp0p2_result.txt"
goto after

:m_dry_loop
echo.
echo [模式] 只读循环（不修改系统，Ctrl+C 停止）
"%PY%" "%SCRIPT%" --loop --out "%~dp0p2_result.txt"
goto after

:m_apply_loop
echo.
echo [模式] 真施加+循环（会改系统，保持游戏前台，Ctrl+C 停止并还原）
"%PY%" "%SCRIPT%" --apply --loop --out "%~dp0p2_result.txt"
goto after

:m_apply_pid
echo.
set "PID="
set /p "PID=请输入游戏进程 PID: "
if "%PID%"=="" (
    echo [!] 未输入 PID，返回菜单。
    pause
    goto menu
)
echo [模式] 真施加+指定PID（会改系统，Ctrl+C 停止并还原）
"%PY%" "%SCRIPT%" --apply --loop --pid %PID% --out "%~dp0p2_result.txt"
goto after

:m_quit
echo.
echo 已退出。
exit /b 0

:after
echo.
echo [完成] 退出码 = %errorlevel%
echo.
pause
goto menu
