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

默认【内嵌】（自用 2026-08-31 起）
-------------------------------
自用场景默认随包分发：打包/installer 默认读取本目录（只要放了 HWiNFO64.exe），
安装后 nbj 首次运行把内置 HWiNFO 释放到 %LOCALAPPDATA%\NingBingJue\HWiNFO\。
若本目录没放 HWiNFO64.exe，构建会优雅跳过（打印警告），运行时卡片引导官方下载。
仅 NBJ_BUNDLE_HWINFO=0 才出不含 HWiNFO 的纯净包。

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


为什么仍保留 NBJ_BUNDLE_HWINFO=0 关闭开关（2026-08-31）
-----------------------------------------
1. 体积 10.9 MB，会让 portable 包近乎翻倍——介意体积者可关。
2. 免费版共享内存有 12 小时自动断供限制，需 nbj 监控卡片预警+兜底配合；
   若想完全不依赖内置 HWiNFO、只用系统已装版本，用 =0 关。
（再分发许可：用户已确认自用场景不受限制，故默认内嵌。若要分发给他人，
请先自行核对 hwinfo.com/files/license.pdf 是否允许随第三方软件再分发。）

nbj 释放的审计版 INI 会保留 SensorsSM=1 / SensorsOnly=1 / MinimalizeSensors=1 /
AutoUpdate=0，并删除 EC=0 等关闭项（让电池 EC/ACPI 与 SVI 电压读数能出来），
且绝不在 INI 已存在时覆盖用户改动。
