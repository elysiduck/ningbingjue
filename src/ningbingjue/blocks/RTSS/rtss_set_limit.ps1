# rtss_set_limit.ps1 — 通过 RTSS 全局 profile 文件 + RTSSHooks64.dll 设置帧率上限。
#
# 这是凝冰结魄「锁帧控制」卡片的新后端，取代失效的 rtss-cli.exe。
# 工作原理（与原 RTSS-FPS.ps1 一致，但更健壮）：
#   1. 确保 RTSS.exe 在运行；
#   2. 把全局 profile 文件（Profiles\Global）里的 Limit=<n> 改掉；
#   3. 调 RTSSHooks64.dll 的 LoadProfile/SaveProfile/UpdateProfiles 让 RTSS 热重载。
#
# 参数：
#   -Limit    <int>   目标帧率上限；0 = 解除锁定（不限制）。必填。
#   -RTSSPath <string> 覆盖 RTSS 安装目录（一般自动探测，无需手填）。
#   -Elevated         内部使用：标记已提权，防止自提权死循环。
#
# 权限：写 Program Files 下的 profile 需要管理员。若当前非管理员且写入被拒，
#       本脚本会自提权一次（弹一次 UAC）后重跑；已提权仍失败则报错退出。
#       自动锁帧（每几秒改写一次）由 Python 端做节流，避免反复弹 UAC。

param(
    [Parameter(Mandatory = $true)][int]$Limit,
    [string]$RTSSPath = "",
    [switch]$Elevated
)

$ErrorActionPreference = "Stop"

# -- 定位 RTSS 安装目录 ------------------------------------------------
function Get-RtssDir {
    param([string]$Override)
    if ($Override -and (Test-Path $Override)) { return $Override }

    # 1) 注册表探测（RTSS 会把安装路径写进 HKLM\SOFTWARE\RTSS* 的某个值）
    $roots = @("HKLM:\SOFTWARE\RTSS", "HKLM:\SOFTWARE\WOW6432Node\RTSS")
    foreach ($r in $roots) {
        try {
            $props = Get-ItemProperty -Path $r -ErrorAction SilentlyContinue
            if ($props) {
                foreach ($p in $props.PSObject.Properties) {
                    if ($p.Value -is [string] -and
                        ($p.Value -like "*RivaTuner*" -or $p.Value -like "*RTSS*") -and
                        (Test-Path $p.Value)) {
                        return $p.Value
                    }
                }
            }
        } catch { }
    }

    # 2) 默认安装位置
    $defaults = @(
        "C:\Program Files (x86)\RivaTuner Statistics Server",
        "C:\Program Files\RivaTuner Statistics Server"
    )
    foreach ($d in $defaults) {
        if (Test-Path $d) { return $d }
    }
    return $null
}

$rtssDir = Get-RtssDir -Override $RTSSPath
if (-not $rtssDir) {
    Write-Warning "未找到 RTSS 安装目录，请确认已安装 RivaTuner Statistics Server。"
    exit 2
}

$rtssExe      = Join-Path $rtssDir "RTSS.exe"
$globalProfile = Join-Path $rtssDir "Profiles\Global"
$dll          = Join-Path $rtssDir "RTSSHooks64.dll"

if (-not (Test-Path $dll)) {
    Write-Warning "未找到 RTSSHooks64.dll：$dll"
    exit 3
}

# -- 确保 RTSS 在运行（不在则静默拉起）-------------------------------
$proc = Get-Process -Name "RTSS" -ErrorAction SilentlyContinue
if (-not $proc) {
    if (Test-Path $rtssExe) {
        Start-Process -FilePath $rtssExe -WindowStyle Hidden
        Start-Sleep -Milliseconds 800
    }
}

# -- 写 Limit 到全局 profile（就地替换，保留原编码 ANSI）---------------
try {
    if (-not (Test-Path $globalProfile)) {
        Write-Warning "未找到全局 profile 文件：$globalProfile"
        exit 4
    }
    $enc     = [System.Text.Encoding]::Default
    $content = [System.IO.File]::ReadAllText($globalProfile, $enc)
    # 搜索以 Limit= 开头的行并替换（兼容不同 RTSS 版本/行号）
    $newContent = [regex]::Replace($content, '(?m)^\s*Limit\s*=.*$', "Limit=$Limit")
    if ($newContent -eq $content) {
        # 极少数情况：profile 里没有 Limit= 行，追加一条
        $newContent = $content.TrimEnd() + "`r`nLimit=$Limit`r`n"
    }
    [System.IO.File]::WriteAllText($globalProfile, $newContent, $enc)
}
catch [System.UnauthorizedAccessException] {
    # 需要管理员：自提权一次（手动锁弹一次 UAC；自动锁由 Python 端节流）
    if (-not $Elevated) {
        $args = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Limit $Limit -Elevated"
        if ($RTSSPath) { $args += " -RTSSPath `"$RTSSPath`"" }
        try {
            $p = Start-Process -FilePath "powershell.exe" -Verb RunAs `
                -WindowStyle Hidden -ArgumentList $args -PassThru -Wait
            if ($p) { exit $p.ExitCode } else { exit 1 }
        } catch {
            Write-Warning "用户取消了提权或提权失败，无法写入 RTSS profile。"
            exit 1
        }
    } else {
        Write-Warning "即使提权后仍无权写入全局 profile：$globalProfile"
        exit 5
    }
}
catch {
    Write-Warning "写入全局 profile 失败：$($_.Exception.Message)"
    exit 5
}

# -- 通过 RTSSHooks64.dll 让 RTSS 重新加载 profile ----------------------
try {
    # 用占位符 + 替换，避免把含反斜杠的路径直接塞进 C# 字符串字面量。
    # 注意：C# 源码用普通字符串 "__DLL__"（不加 @ 逐字前缀），路径反斜杠转义成
    # "\\" 以适配普通 C# 字符串字面量。here-string 的结束符必须独占一行。
    $dllEscaped = $dll -replace '\\', '\\'
    $cs = @"
using System;
using System.Runtime.InteropServices;
public class RTSS {
    [DllImport("__DLL__", CharSet = CharSet.Ansi)]
    public static extern void LoadProfile(string profile);
    [DllImport("__DLL__", CharSet = CharSet.Ansi)]
    public static extern void SaveProfile(string profile);
    [DllImport("__DLL__", CharSet = CharSet.Ansi)]
    public static extern void UpdateProfiles();
}
"@
    $cs = $cs -replace '__DLL__', $dllEscaped

    Add-Type -TypeDefinition $cs -ErrorAction Stop
    [RTSS]::LoadProfile($globalProfile)
    [RTSS]::SaveProfile($globalProfile)
    [RTSS]::UpdateProfiles()
}
catch {
    Write-Warning "调用 RTSSHooks64.dll 失败：$($_.Exception.Message)"
    exit 6
}

Write-Output "OK Limit=$Limit"
exit 0
