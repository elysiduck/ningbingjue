@echo off
chcp 65001 >nul
REM ============================================================
REM  凝冰结魄 开发同步 · 笔记本 -> 掌机（阶段1 兜底，一条命令）
REM
REM  用法：以管理员身份运行（写 Program Files 需权限；掌机需开默认管理共享
REM        D$，或把掌机目录映射为网络盘并改下方 DRIVE / NBJ_SHARE）。
REM
REM  做了什么：
REM   1) 推送 src -> 掌机 D:\Program Files\NingBingJue\_ext\src
REM      （差异同步 /MIR，排除 __pycache__；掌机 exe 启动时优先加载它）
REM   2) 写入掌机 sync_config.json（notebook 共享 = \\%COMPUTERNAME%\NBJ）
REM   3) 回传掌机 logs -> 笔记本 logs（仅增不删 /E /XO）
REM ============================================================
setlocal
set HANDHELD=192.168.1.126
set DRIVE=D
set NBJ_SHARE=\\%HANDHELD%\%DRIVE%$\Program Files\NingBingJue
set SRC=%~dp0src
set LOGS=%~dp0logs

if not exist "%NBJ_SHARE%" (
  echo [错误] 无法访问掌机目录：%NBJ_SHARE%
  echo   请确认掌机开机、IP 正确、且 %DRIVE%$ 管理共享可用（或改脚本路径）。
  pause
  exit /b 1
)

echo [1/3] 推送源码 src -> 掌机 _ext\src（差异同步，排除 __pycache__）
robocopy "%SRC%" "%NBJ_SHARE%\_ext\src" /MIR /XD __pycache__ /R:1 /W:1
if errorlevel 8 ( echo [警告] 源码推送返回非零，请检查网络 ; pause )

echo [2/3] 写入掌机 sync_config.json（笔记本共享 = \\%COMPUTERNAME%\NBJ）
(
echo {
echo   "laptop_share": "\\%COMPUTERNAME%\NBJ",
echo   "local_ext_src": "%DRIVE%:\Program Files\NingBingJue\_ext\src",
echo   "auto_upload_logs": false
echo }
) > "%NBJ_SHARE%\sync_config.json"

echo [3/3] 回传掌机日志 -> 笔记本 logs（仅增不删）
if not exist "%LOGS%" mkdir "%LOGS%"
robocopy "%NBJ_SHARE%\logs" "%LOGS%" /E /XO /R:1 /W:1
if errorlevel 8 ( echo [警告] 日志回传返回非零 ; pause )

echo.
echo 完成。掌机重开凝冰结魄即加载新源码（需外置源码覆盖已构建进 exe）。
pause
