# 凝冰结魄 - 虚拟桌面移动诊断 / 一键修复
# 用途：确认「按 PID 把窗口移到后台虚拟桌面」在你机器上能否工作。
# 这是交互式脚本，可安装模块、回答 NuGet 提示——正是 App 子进程里做不到的。

$ErrorActionPreference = 'Stop'
function Line($t,$c='Gray'){ Write-Host $t -ForegroundColor $c }

Line "==== 凝冰结魄 虚拟桌面诊断 ====" 'Cyan'
Line ("PowerShell 版本: " + $PSVersionTable.PSVersion) 'DarkGray'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Line ("是否管理员运行: " + $admin) 'DarkGray'
Line ""

# 0) 环境块瘦身（关键）：导入模块会用 Add-Type 启动 csc.exe，
#    子进程环境块上限 65535 字节。若本机有超大环境变量（如 ACC_PRODUCT_CONFIG_V3），
#    会导致 csc.exe 无法启动、Import-Module 失败。这里先移除超大变量。
Line "[0] 检查环境块大小..." 'Yellow'
$envBytes = 0
foreach ($e in [Environment]::GetEnvironmentVariables().GetEnumerator()) {
    $envBytes += ($e.Key.Length + $e.Value.ToString().Length + 2)
}
Line ("    当前环境块: $envBytes 字节 (上限 65535)" ) $(if ($envBytes -gt 65535) { 'Red' } else { 'DarkGray' })
if ($envBytes -gt 60000) {
    Get-ChildItem Env: | Where-Object { ($_.Name.Length + $_.Value.Length + 2) -gt 16384 } | ForEach-Object {
        Line ("    移除超大环境变量: " + $_.Name + " (" + $_.Value.Length + " 字节)") 'Yellow'
        Remove-Item ("Env:\" + $_.Name) -ErrorAction SilentlyContinue
    }
    $envBytes = 0
    foreach ($e in [Environment]::GetEnvironmentVariables().GetEnumerator()) {
        $envBytes += ($e.Key.Length + $e.Value.ToString().Length + 2)
    }
    Line ("    瘦身后环境块: $envBytes 字节" ) 'Green'
}
Line ""

# 1) 模块
Line "[1] 检查 VirtualDesktop 模块..." 'Yellow'
$mod = Get-Module -ListAvailable VirtualDesktop
# 以管理员运行时装到 AllUsers，这样无论 App 以普通用户还是管理员启动都能找到模块。
# 这正是「App 里窗口不动、但手动能移」的头号原因：模块只装在普通用户 scope。
$scope = if ($admin) { 'AllUsers' } else { 'CurrentUser' }
if (-not $mod) {
    Line "    未安装，正在从 PowerShell Gallery 安装（首次可能提示安装 NuGet，输入 Y 回车）..." 'Yellow'
    Line ("    以 $scope 权限安装（与 App 运行权限一致）。") 'DarkGray'
    try {
        Install-Module VirtualDesktop -Scope $scope -Force -SkipPublisherCheck
        Line "    安装完成。" 'Green'
    } catch {
        Line "    安装失败: $($_.Exception.Message)" 'Red'
        Line "    可手动运行: Install-Module VirtualDesktop -Scope $scope -Force -SkipPublisherCheck" 'Red'
        return
    }
} else {
    Line ("    已安装，版本: " + ($mod.Version -join ',')) 'Green'
    if ($admin) {
        # 已装但当前是管理员：确认 AllUsers 也有，避免 App 以管理员运行时找不到。
        $allUsers = $mod | Where-Object { $_.ModuleBase -like '*\Program Files\*' -or $_.ModuleBase -like '*\WindowsPowerShell\Modules' -and $false }
        if (-not ($mod.Path -like '*\Program Files\*')) {
            Line "    检测到以管理员运行，补装一份 AllUsers 副本以免 App(管理员) 找不到模块..." 'Yellow'
            try { Install-Module VirtualDesktop -Scope AllUsers -Force -SkipPublisherCheck } catch {}
        }
    }
}
Import-Module VirtualDesktop -Force -DisableNameChecking
if (-not (Get-Command Move-Window -ErrorAction SilentlyContinue)) {
    Line "    模块导入后仍找不到 Move-Window，模块可能损坏。" 'Red'; return
}
Line "    模块可用。" 'Green'
Line ""

# 2) 桌面数量
$count = Get-DesktopCount
$current = Get-CurrentDesktop
Line "[2] 当前虚拟桌面数量: $count" 'Yellow'
if ($count -lt 2) { Line "    只有 1 个桌面，诊断时会自动新建一个用于测试。" 'DarkGray' }
Line ""

# 3) 选目标 PID
Line "[3] 选择要移动的进程" 'Yellow'
$inPid = Read-Host "    输入进程 PID（直接回车则测试记事本 notepad）"
if ([string]::IsNullOrWhiteSpace($inPid)) {
    $np = Get-Process notepad -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $np) {
        Line "    没有记事本在运行，正在打开一个用于测试..." 'DarkGray'
        Start-Process notepad; Start-Sleep -Milliseconds 800
        $np = Get-Process notepad -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    $thePid = $np.Id
} else { $thePid = [int]$inPid }
Line "    目标 PID = $thePid" 'Green'

# 4) PID -> HWND
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Collections.Generic;
public class WinEnumPid {
    public delegate bool EnumDelegate(IntPtr hWnd, IntPtr lParam);
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumDelegate lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr hWnd, out int lpdwProcessId);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    public static List<IntPtr> FindByPid(int pid) {
        var result = new List<IntPtr>();
        EnumWindows((hWnd, _) => {
            int p = 0; GetWindowThreadProcessId(hWnd, out p);
            if (p == pid && IsWindowVisible(hWnd)) result.Add(hWnd);
            return true;
        }, IntPtr.Zero);
        return result;
    }
}
'@
function Get-PidTree([int]$root){
    $all=Get-CimInstance Win32_Process -EA SilentlyContinue
    $byParent=@{}
    foreach($pr in $all){ $pp=0; try{$pp=[int]$pr.ParentProcessId}catch{}; if(-not $byParent.ContainsKey($pp)){$byParent[$pp]=New-Object System.Collections.Generic.List[int]}; $byParent[$pp].Add([int]$pr.ProcessId) }
    $seen=New-Object System.Collections.Generic.HashSet[int]; $q=New-Object System.Collections.Generic.Queue[int]; $q.Enqueue($root); [void]$seen.Add($root)
    while($q.Count -gt 0){ $cur=$q.Dequeue(); if($byParent.ContainsKey($cur)){ foreach($c in $byParent[$cur]){ if($seen.Add($c)){ $q.Enqueue($c) } } } }
    return $seen
}
$hwnd=[IntPtr]::Zero
foreach($id in (Get-PidTree $thePid)){ $p=Get-Process -Id $id -EA SilentlyContinue; if($p -and $p.MainWindowHandle -ne 0){$hwnd=$p.MainWindowHandle; break} }
if($hwnd -eq [IntPtr]::Zero){ foreach($id in (Get-PidTree $thePid)){ $hs=[WinEnumPid]::FindByPid($id); if($hs.Count -gt 0){$hwnd=$hs[0]; break} } }
Line "[4] 解析窗口句柄: $hwnd" 'Yellow'
if($hwnd -eq [IntPtr]::Zero){ Line "    找不到该 PID 的可见窗口（可能是无界面进程）。" 'Red'; return }

# 5) 移到后台桌面
Line "[5] 移动到后台虚拟桌面..." 'Yellow'
if($count -lt 2){ $null=New-Desktop; Switch-Desktop -Desktop $current; $count=Get-DesktopCount }
$target=$null
for($i=$count-1;$i -ge 0;$i--){ $d=Get-Desktop -Index $i; if($d.DesktopId -ne $current.DesktopId){$target=$d; break} }
if(-not $target){ $null=New-Desktop; Switch-Desktop -Desktop $current; $target=Get-Desktop -Index ((Get-DesktopCount)-1) }
try {
    Move-Window -Hwnd $hwnd -Desktop $target
    Line "    成功！窗口已移到后台桌面。用 Win+Tab 应能看到它在另一个桌面。" 'Green'
    Line "    3 秒后自动移回当前桌面..." 'DarkGray'
    Start-Sleep -Seconds 3
    Move-Window -Hwnd $hwnd -Desktop (Get-CurrentDesktop)
    Line "    已移回。诊断通过 —— 你的机器支持该功能。" 'Green'
    Line ""
    Line ">> 结论：机制可用。如果 App 里仍不生效，多半是 App 以管理员运行、" 'Cyan'
    Line "   而模块只装在了普通用户 scope。请在【与 App 相同的权限】下再跑一次本脚本安装模块。" 'Cyan'
} catch {
    Line "    Move-Window 失败: $($_.Exception.Message)" 'Red'
    Line "    多为模块版本与当前 Windows 版本不匹配，尝试: Update-Module VirtualDesktop" 'Red'
}
