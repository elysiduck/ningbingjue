
$ErrorActionPreference = 'SilentlyContinue'
try {
  while ($true) {
    try {
      # 用 SilentlyContinue 代替 -ErrorAction Stop：掌机（尤其 HX370）上
      # \GPU Engine(*) 计数器偶发“一个性能计数器示例中的数据无效”（Status≠0
      # 的坏实例），用 Stop 会让整次 Get-Counter 抛异常、永远读不到任何值，
      # 导致 GPU 浮动/自动锁频因拿不到利用率而彻底失效。改为取回 CounterSamples
      # 后逐实例过滤 Status=0 的有效样本再求和，坏实例被跳过、有效实例仍能用。
      $c = Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction SilentlyContinue
      $sum = 0.0
      $valid = 0
      if ($c -and $c.CounterSamples) {
        foreach ($s in $c.CounterSamples) {
          if ($s.Status -eq 0 -and $null -ne $s.CookedValue) {
            $sum += [double]$s.CookedValue
            $valid++
          }
        }
      }
      if ($valid -gt 0) {
        # 多引擎实例（3D/Copy/Video…）求和可能 >100%，按上限钳制，
        # 避免浮动/自动锁频的 88/92% 阈值被恒触发。
        if ($sum -gt 100.0) { $sum = 100.0 }
        [Console]::Out.WriteLine($sum)
        [Console]::Out.Flush()   # stdout is a pipe (not TTY) -> .NET does not flush per line
      } else {
        [Console]::Error.WriteLine('LOOP_ERR: 所有 GPU Engine 实例状态无效（计数器可能已损坏，请以管理员运行 lodctr /r 重建）')
      }
    } catch {
      [Console]::Error.WriteLine(('LOOP_ERR: ' + $_.Exception.Message))
    }
    Start-Sleep -Milliseconds 300
  }
} catch {
  [Console]::Error.WriteLine(('FATAL: ' + $_.Exception.Message))
}
