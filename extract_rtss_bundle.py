"""从用户提供的 RivaTuner Statistics Server.zip 提取裁剪版 RTSS 到 vendor/RTSS。

裁剪原则（grill 决策 A，NBJ_BUNDLE_RTSS=1 自用构建才内嵌）：
  * 排除：视频采集 (libmfxsw/Codec/SaveMedia/EncoderServer/DesktopOverlayHost)、
    SDK 开发头、Doc、Redist VC 重分发安装器、mainwnd.dibcache 皮肤缓存、Uninstall.exe
    —— 这些是录制/开发/安装器，与「帧率限制 + OSD」无关，约 50MB。
  * 保留：RTSS.exe + RTSSHooks* + 注入器 + RTUI/RTMUI/RTFC 前端 + Vulkan 层 +
    Profiles + RTSS.dat + Localization + Fonts + Skins + Plugins(OSD 数据源)。
    保证提取出的 RTSS 能真正启动并限制帧率。

安全：仅选择官方文件子集，不修改任何 RTSS 文件内容（不触 EULA 的 alter/modify）。
"""
import os
import zipfile

SRC_ZIP = r"D:\下载\localsend\RivaTuner Statistics Server.zip"
DST = r"C:\Users\13567\WorkBuddy\2026-07-18-10-22-49\ningbingjue\src\ningbingjue\vendor\RTSS"

EXCLUDE_DIRS = ("/SDK/", "/Doc/", "/Redist/", "/Codec/")
EXCLUDE_PREFIX = (
    "RivaTuner Statistics Server/libmfxsw",
    "RivaTuner Statistics Server/SaveMedia",
    "RivaTuner Statistics Server/EncoderServer",
    "RivaTuner Statistics Server/DesktopOverlayHost",
)
EXCLUDE_NAMES = ("mainwnd.dibcache", "Uninstall.exe")


def keep(name: str) -> bool:
    if any(name.startswith(p) for p in EXCLUDE_PREFIX):
        return False
    if any(d in name for d in EXCLUDE_DIRS):
        return False
    base = name.rsplit("/", 1)[-1]
    if base in EXCLUDE_NAMES:
        return False
    return True


def main() -> None:
    os.makedirs(DST, exist_ok=True)
    z = zipfile.ZipFile(SRC_ZIP)
    total = 0
    copied = 0
    skipped = 0
    for item in z.infolist():
        if item.filename.endswith("/"):
            continue
        total += item.file_size
        if not keep(item.filename):
            skipped += 1
            continue
        # 去掉顶层目录 "RivaTuner Statistics Server/"
        rel = item.filename.split("/", 1)[1] if "/" in item.filename else item.filename
        out = os.path.join(DST, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with z.open(item) as src, open(out, "wb") as fh:
            fh.write(src.read())
        copied += 1

    # 确保 Profiles/Global 存在（运行时 native/PS 都会写它的 Limit=；缺失会导致
    # 首次写入走「追加」分支，行为不确定）。
    prof = os.path.join(DST, "Profiles", "Global")
    if not os.path.exists(prof):
        os.makedirs(os.path.dirname(prof), exist_ok=True)
        header = (
            "[General]\r\n"
            "ProcessName=Global\r\n"
            "RunProcessInBackground=0\r\n"
            "\r\n"
            "[FrameLimit]\r\n"
            "Limit=0\r\n"
            "LimitDenominator=1\r\n"
            "LimitNumerator=0\r\n"
        )
        with open(prof, "w", encoding="mbcs") as fh:
            fh.write(header)
        print("已创建默认 Profiles/Global（Limit=0）")

    size = 0
    for root, _, files in os.walk(DST):
        for f in files:
            size += os.path.getsize(os.path.join(root, f))
    print(f"提取完成：{copied} 文件 → {DST}")
    print(f"  部署体积 ≈ {size/1048576:.1f} MB")
    print(f"  跳过（排除）文件数：{skipped}（zip 内总 {total/1048576:.1f} MB）")
    print(f"  RTSS.exe 存在：{os.path.exists(os.path.join(DST, 'RTSS.exe'))}")
    print(f"  RTSSHooks64.dll 存在：{os.path.exists(os.path.join(DST, 'RTSSHooks64.dll'))}")


if __name__ == "__main__":
    main()
