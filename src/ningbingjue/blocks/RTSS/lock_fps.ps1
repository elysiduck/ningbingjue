# lock_fps.ps1 — RTSS 锁帧小工具
# 放在 rtss-cli.exe 旁边，双击「锁帧.bat」即可打开。
# 功能：读取当前锁帧状态、输入目标帧数并应用、一键取消锁定。

try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing

    # rtss-cli.exe 与本脚本同目录
    $cli = Join-Path $PSScriptRoot "rtss-cli.exe"

    if (-not (Test-Path $cli)) {
        [System.Windows.Forms.MessageBox]::Show(
            "未找到 rtss-cli.exe。`n请把它放在本脚本同一目录下。",
            "错误", [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error)
        exit 1
    }

    # 调用 rtss-cli 并取回标准输出（去掉首尾空白）
    function Get-RtssOut {
        param([string[]]$Arguments)
        $raw = ($(& $cli @Arguments 2>$null) | Out-String)
        return $raw.Trim()
    }

    # ── 窗体 ──────────────────────────────────────────
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "RTSS 锁帧工具"
    $form.Size = New-Object System.Drawing.Size(370, 250)
    $form.StartPosition = "CenterScreen"
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false

    # 当前状态标签
    $labelCur = New-Object System.Windows.Forms.Label
    $labelCur.Location = New-Object System.Drawing.Point(15, 15)
    $labelCur.Size = New-Object System.Drawing.Size(330, 20)
    $labelCur.Text = "正在读取当前状态…"
    $form.Controls.Add($labelCur)

    # 提示
    $labelHint = New-Object System.Windows.Forms.Label
    $labelHint.Location = New-Object System.Drawing.Point(15, 52)
    $labelHint.Size = New-Object System.Drawing.Size(330, 20)
    $labelHint.Text = "输入要锁定的帧数（0 = 不锁定）："
    $form.Controls.Add($labelHint)

    # 输入框
    $txtFps = New-Object System.Windows.Forms.TextBox
    $txtFps.Location = New-Object System.Drawing.Point(15, 77)
    $txtFps.Size = New-Object System.Drawing.Size(150, 23)
    $txtFps.Text = ""
    $form.Controls.Add($txtFps)

    # 应用锁定
    $btnApply = New-Object System.Windows.Forms.Button
    $btnApply.Location = New-Object System.Drawing.Point(180, 75)
    $btnApply.Size = New-Object System.Drawing.Size(80, 26)
    $btnApply.Text = "应用锁定"
    $form.Controls.Add($btnApply)

    # 取消锁定
    $btnUnlock = New-Object System.Windows.Forms.Button
    $btnUnlock.Location = New-Object System.Drawing.Point(270, 75)
    $btnUnlock.Size = New-Object System.Drawing.Size(80, 26)
    $btnUnlock.Text = "取消锁定"
    $form.Controls.Add($btnUnlock)

    # 状态栏
    $labelStatus = New-Object System.Windows.Forms.Label
    $labelStatus.Location = New-Object System.Drawing.Point(15, 125)
    $labelStatus.Size = New-Object System.Drawing.Size(335, 70)
    $labelStatus.Text = ""
    $form.Controls.Add($labelStatus)

    function Refresh-Current {
        try {
            $lim = Get-RtssOut limiter:get
            $limVal = Get-RtssOut limit:get
            $labelCur.Text = "当前：限制器 $(if($lim -eq '1'){'启用'}else{'关闭'})，帧率上限 $limVal FPS"
        } catch {
            $labelCur.Text = "无法读取 RTSS 状态（RTSS 可能未运行）"
        }
    }

    function Set-Status($msg, $ok = $true) {
        $labelStatus.Text = $msg
        $labelStatus.ForeColor = if ($ok) {
            [System.Drawing.Color]::FromArgb(0, 150, 0)
        } else {
            [System.Drawing.Color]::FromArgb(200, 0, 0)
        }
    }

    $btnApply.Add_Click({
        $raw = $txtFps.Text.Trim()
        if (-not ($raw -match '^\d+$')) {
            Set-Status "请输入一个非负整数帧数。" $false
            return
        }
        $fps = [int]$raw
        try {
            Get-RtssOut limiter:set 1 | Out-Null
            Get-RtssOut limit:set $fps | Out-Null
            Refresh-Current
            if ($fps -eq 0) {
                Set-Status "已设为不锁定（帧率上限 0）。"
            } else {
                Set-Status "已锁定到 $fps FPS，限制器已启用。"
            }
        } catch {
            Set-Status "执行失败：$($_.Exception.Message)" $false
        }
    })

    $btnUnlock.Add_Click({
        try {
            Get-RtssOut limit:set 0 | Out-Null
            Get-RtssOut limiter:set 0 | Out-Null
            Refresh-Current
            Set-Status "已取消锁定（限制器关闭，帧率上限 0）。"
        } catch {
            Set-Status "执行失败：$($_.Exception.Message)" $false
        }
    })

    $form.AcceptButton = $btnApply   # 在输入框按回车即应用锁定

    $form.Add_Shown({
        Refresh-Current
        $txtFps.Focus()
    })

    [System.Windows.Forms.Application]::EnableVisualStyles() | Out-Null
    $form.ShowDialog() | Out-Null

} catch {
    # 任何启动期异常都不静默退出：弹窗 + 写日志
    $err = $_.Exception.ToString()
    try {
        [System.Windows.Forms.MessageBox]::Show(
            $err, "锁帧工具启动失败",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error)
    } catch {}
    try {
        $err | Out-File "$env:TEMP\lock_fps_error.log" -Append
    } catch {}
    throw
}
