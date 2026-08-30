@echo off
setlocal EnableExtensions
REM ============================================================
REM  凝冰结魄 · 布局编辑按钮字号补丁启动器
REM  双击本文件即可应用「保存/取消放宽 + 字号放大 + 加粗」修改
REM  应用后重新双击 launch.bat 查看效果
REM ============================================================
set "HERE=%~dp0"
pushd "%HERE%" >nul 2>&1

set "PY=%HERE%runtime\python\python.exe"
if not exist "%PY%" set "PY=python"

set "PATCH=%HERE%patch_layout_buttons.py"
if not exist "%PATCH%" (
    echo [错误] 未找到 patch_layout_buttons.py（应与本文件同目录）
    goto end
)

echo 正在应用布局按钮字号补丁...
echo ============================================
"%PY%" "%PATCH%"
echo ============================================
echo 若上面显示「全部规则落地」即成功；若显示 FAIL 请把本窗口内容发回给 AI。
:end
echo 按任意键关闭...
pause
popd
exit /b
