HWiNFO 便携二进制投放目录（可选内嵌）
========================================

本目录用于「NBJ_BUNDLE_HWINFO=1」自用时构建：把官方便携包里的文件放进来，
build_exe.py 会把整个目录打进 pyinstaller 构建，运行时释放到
  %LOCALAPPDATA%\NingBingJue\HWiNFO\

需要放哪些文件
--------------
把 HWiNFO64 便携 zip 解压后的内容整目录放进来即可，至少包含：
  HWiNFO64.exe        官方原版（建议 8.50+，本仓库测试基准 8.48-5990）
  HWiNFO64.INI        （可选）nbj 启动时会用自己的审计版 INI 覆盖关键键，见下

默认【不】内嵌
--------------
出厂构建（NBJ_BUNDLE_HWINFO 未设）不会读取本目录。此时 nbj 不携带 HWiNFO
二进制，只在监控卡片上检测系统是否已安装、是否运行、共享内存是否新鲜；
缺失时引导用户去 https://www.hwinfo.com/download/ 下载便携版。

一步到位把 HWiNFO 打进安装包
----------------------------
推荐用根目录脚本（双击即可，无需手填环境变量）：

    build_installer_with_hwinfo.bat "D:\下载\localsend\HWiNFO64-2.zip"

它会自动：① 从 zip 解压 HWiNFO64.exe + 写审计版 INI 到此目录；② 设
NBJ_BUNDLE_HWINFO=1 跑 build_exe；③ 跑 make_installer 出 setup.exe。
也可以手工分步：

    set NBJ_BUNDLE_HWINFO=1
    python build_exe.py
    python make_installer.py --bundle-hwinfo --hwinfo-src "D:\下载\localsend\HWiNFO64-2.zip"

安装 nbj 后，监控卡片首次加载会调用 ensure_hwinfo() 把内置 HWiNFO 释放到
  %LOCALAPPDATA%\NingBingJue\HWiNFO\
卸载 nbj 时安装器也会一并清理该目录（顺带装则顺带卸）。


为什么默认不内嵌（2026-08-31 grill 决策）
-----------------------------------------
1. 再分发条款未确认：hwinfo.com/files/license.pdf 被 Cloudflare 挡住读不到原文，
   需使用者自行核对是否允许随第三方软件再分发。
2. 体积 10.9 MB，会让 portable 包近乎翻倍。
3. 免费版共享内存有 12 小时自动断供限制，内嵌也换不来「常驻可靠」，
   收益不足以覆盖前两项成本。

nbj 释放的审计版 INI 会保留 SensorsSM=1 / SensorsOnly=1 / MinimalizeSensors=1 /
AutoUpdate=0，并删除 EC=0 等关闭项（让电池 EC/ACPI 与 SVI 电压读数能出来），
且绝不在 INI 已存在时覆盖用户改动。
