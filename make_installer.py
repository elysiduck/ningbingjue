#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键把「凝冰结魄」打成 Windows 安装包 (setup.exe)。

流程：
  1. (可选) 先用 build_exe.py 产出冻结后的文件夹 dist/NingBingJue/；
  2. 自动确保本机有 Inno Setup 编译器（ISCC.exe）：
        - 先在常见路径 / PATH / tools/innosetup 里找；
        - 找不到就从 GitHub 下载 Inno Setup 6.7.3 并静默安装到 tools/innosetup；
  3. 生成 Inno Setup 脚本 (.iss)，把整个冻结文件夹打成一个安装包；
  4. 运行 ISCC 产出 dist/NingBingJue-Setup.exe；
  5. 顺手生成一个「新设备一键放行.bat」（解除文件锁定 + 加 Defender 排除）。

用法（在 ningbingjue/ 目录下）：
    .build_venv/Scripts/python.exe make_installer.py            # 完整一键
    .build_venv/Scripts/python.exe make_installer.py --no-build # 跳过 PyInstaller 重打
    .build_venv/Scripts/python.exe make_installer.py --bundle-dir 某路径/NingBingJue  # 指定已打好的文件夹

环境变量（均可选覆盖）：
    NBJ_DISTPATH     PyInstaller 输出根（默认 dist/），其下应有 NingBingJue/ 文件夹
    ISCC             直接指定 ISCC.exe 路径，跳过自动下载
    INNOSETUP_INSTALLER_URL   自定义 Inno Setup 安装包下载地址
"""
from __future__ import annotations

import os
import sys
import json
import shutil
import subprocess
import urllib.request
import zipfile
import tempfile

# ---------------------------------------------------------------------------
# 可调参数
# ---------------------------------------------------------------------------
APP_NAME = "NingBingJue 凝冰结魄"
APP_VERSION = "1.0.0"
APP_PUBLISHER = "凝冰结魄"
APP_URL = "https://github.com/"  # 占位，可改成你的仓库
# 固定 AppId -> 同机升级/卸载能正确识别
# 注意：Inno Setup 把 {xxx} 当常量，GUID 的花括号要转义成 {{ }}，
# 所以这里存「无括号」的纯 GUID，模板里写成 AppId={{@@APPID@@}}。
APP_ID = "9F3C7A2E-1B4D-4E8A-9C5B-7D2F0E6A1C33"

# Inno Setup 6.7.3 (已数字签名)，GitHub Release 直链
INNOSETUP_URL = os.environ.get(
    "INNOSETUP_INSTALLER_URL",
    "https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe",
)
INNOSETUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "innosetup")


# ---------------------------------------------------------------------------
# 配置打包过滤：决定哪些「用户配置」随安装包分发
# ---------------------------------------------------------------------------
# layout.json 中各配置项按类别分组。被排除的键会从「打包用的 layout.json」中
# 剔除，让安装后的程序对该项使用代码默认值（相当于恢复出厂）。
# 注意：此表必须覆盖 AppConfig.to_dict() 写出的【全部】顶层键，否则相应
# 本机配置会随安装包原样泄漏（例如之前漏掉的 SmtConfig / FanConfig 等）。
_CONFIG_KEY_GROUPS = {
    "tiles":        ["Tiles", "ExtraTileBlocks"],          # 磁贴卡片（主磁贴 + 额外区块）
    "card_colors":  ["CardColors"],                        # 卡片配色
    "layout":       ["Cards", "HiddenCards", "PageBreaks"],  # 卡片布局/分页
    "theme":        ["Theme"],                             # 主题配色
    "breath":       ["BreathPeriodS", "BreathColor", "BreathMaxBrightness"],  # 呼吸灯
    # EditorDimAlpha = 编辑页（布局编辑及所有子页面）蒙版深度，与托盘背景同组
    "tray":         ["TrayColor", "TrayOpacity", "TrayBorder", "TrayBlur",
                     "EditorDimAlpha"],                     # 托盘背景 + 编辑页蒙版
    "focus":        ["FocusColor", "FocusOpacity"],        # 焦点蒙层
    "power":        ["PowerConfig"],                       # 电源设置
    "smt":          ["SmtConfig"],                         # SMT 热核隔离
    "fan":          ["FanConfig"],                         # 风扇控制
    "freeze":       ["FreezeShowWindowsApps", "FreezeHidden",
                     "FreezePrioritizeForeground",
                     "FreezeMinimizeOnUnfreeze"],          # 进程冻结设置
    "window":       ["WindowAlign"],                       # 窗口对齐
    "procmon":      ["ProcMonConfigs"],                    # 进程监控配置
    "input":        ["Input", "XInputGateEnabled",
                     "XInputGateGames"],                   # 手柄/键盘绑定 + XInput 门控
    "button_style": ["ButtonStyle"],                       # 按钮样式
}
# 单独文件（不在 layout.json 内）的配置类别 -> 需排除的相对路径列表。
# 注意：冻结后的文件实际位于 _internal/ 下，故每个类别同时列出「顶层」与
# 「_internal/」两种相对路径，确保无论 PyInstaller 把数据放在哪一层都能正确剔除。
_CONFIG_FILE_GROUPS = {
    "rtss":      ["rtss_config.json", "_internal/rtss_config.json"],
    "undervolt": ["config/undervolt.json", "_internal/config/undervolt.json"],
    "buttons":   ["buttons", "_internal/buttons"],
}
ALL_CONFIG_CATEGORIES = list(_CONFIG_KEY_GROUPS) + list(_CONFIG_FILE_GROUPS)

# ---------------------------------------------------------------------------
# 打包默认行为：是否随安装包分发「本机」配置
# ---------------------------------------------------------------------------
# 新策略（2026-08-26 改进）：默认【不随安装包分发任何本机 凝冰结魄 配置】。
#   实现上由 build_exe 控制：默认不把 layout.json / config / buttons 烘焙进
#   冻结文件夹（见 build_exe.py 的 --with-config），因此冻结产物、便携版、
#   安装包三处默认都不含任何本机配置，安装后程序以代码出厂默认值运行
#   （锁帧关·30FPS、TDP 10W、GPU 700MHz、GPU 浮动关、降压关、风扇 Auto）。
#   只有显式要求打包配置时，build_exe 才会烘焙当前配置，再由下面的分类
#   过滤环节按需剔除：
#     * make_installer.py --with-config            → 打包全部当前配置；
#     * make_installer.py --config-choice 1,2,3   → 打包配置，仅保留指定类别；
#     * make_installer.py --exclude tiles         → 打包配置，剔除指定类别；
#     * 仅凭 --exclude / --config-choice / --exclude-all 也会隐式开启打包
#       （既然要做选择，必然先烘焙配置再剔除）。
#   安装器页面（现有配置）默认【不勾选】"覆盖为出厂默认"，即安装不覆盖任何
#   已有配置，除非用户在安装向导里主动勾选。
# 旧式 INCLUDE_CONFIG 白名单已废弃（保留仅为避免 ImportError），不再被读取。
INCLUDE_CONFIG: list = []  # 已废弃：默认不再随包分发任何本机配置。

# 交互菜单编号 -> 类别名
_CHOICE_TO_CATEGORY = {
    "1": "tiles", "2": "card_colors", "3": "layout", "4": "theme",
    "5": "breath", "6": "tray", "7": "focus", "8": "power",
    "9": "freeze", "10": "procmon", "11": "input", "12": "button_style",
    "13": "undervolt", "14": "buttons",
}


def _log(msg: str) -> None:
    print("[make-installer] " + msg)


def resolve_excludes(args) -> set:
    """汇总要排除（即【不随安装包分发】）的配置类别名。

    新策略（2026-08-26 改进）：
      * 默认（未传 --with-config，也未传任何 --exclude / --exclude-all /
        --config-choice）：打包阶段（build_exe）不烘焙任何本机配置，因此安装包
        里根本不含配置；此时无需排除，直接返回空集。安装后的程序以代码出厂
        默认值运行（锁帧关·30FPS、TDP 10W、GPU 700MHz、GPU 浮动关、降压关、
        风扇 Auto）。
      * 只要显式要求打包配置 —— ``--with-config``，或任何 ``--exclude`` /
        ``--exclude-all`` / ``--config-choice`` 选择（后者隐式开启打包）——
        打包阶段就会先烘焙当前配置，再按选择剔除：
          - 仅有 --with-config（无排除修饰）→ 全打包（返回空集，无排除）；
          - 带 --exclude / --exclude-all / --config-choice → 按选择剔除对应类别
            （见 ``_compute_excludes_from_cli``）。
    """
    has_cli = bool(
        (getattr(args, "exclude", None) or [])
        or getattr(args, "exclude_all", False)
        or (getattr(args, "config_choice", None) is not None)
    )
    # 是否真正打包配置：显式 --with-config，或做了类别选择（选择蕴含先打包）。
    with_config = bool(getattr(args, "with_config", False)) or has_cli
    if not with_config:
        _log("[默认] 不烘焙任何本机配置 → 安装包以出厂默认运行。")
        return set()
    if has_cli:
        return _compute_excludes_from_cli(args)
    _log("[--with-config] 打包当前本机配置（全部类别）。")
    return set()


def _compute_excludes_from_cli(args) -> set:
    """根据 --exclude / --exclude-all / --config-choice 计算要排除的类别集合。

    仅在 ``resolve_excludes`` 确认需要打包配置且用户显式做了类别选择时调用。
    """
    import re
    cats: set = set()
    for e in (getattr(args, "exclude", None) or []):
        eu = e.strip().lower()
        if eu == "all":
            cats.update(ALL_CONFIG_CATEGORIES)
        elif e in ALL_CONFIG_CATEGORIES:
            cats.add(e)
        else:
            _log(f"[warn] 未知配置类别 --exclude {e}，已忽略。"
                 f" 可选: {', '.join(ALL_CONFIG_CATEGORIES)}")
    if getattr(args, "exclude_all", False):
        cats.update(ALL_CONFIG_CATEGORIES)
    cc = getattr(args, "config_choice", None)
    if cc is not None:
        val = cc.strip()
        if val == "" or val.lower() == "default":
            cats.add("tiles")                      # 交互菜单回车 = 仅排除磁贴
        elif val.lower() in ("n", "none"):
            pass                                   # 全部打包
        elif val.lower() in ("a", "all"):
            cats.update(ALL_CONFIG_CATEGORIES)     # 全部排除
        else:
            for tok in re.split(r"[,\s]+", val):
                tok = tok.strip()
                if not tok:
                    continue
                if tok.lower() == "all":
                    cats.update(ALL_CONFIG_CATEGORIES)
                elif tok in ALL_CONFIG_CATEGORIES:
                    cats.add(tok)
                elif tok in _CHOICE_TO_CATEGORY:
                    cats.add(_CHOICE_TO_CATEGORY[tok])
                else:
                    _log(f"[warn] 无法识别的选项 {tok}，已忽略。")
    return cats



def prepare_config_filters(bundle_dir: str, excludes: set) -> tuple:
    """根据要排除的类别，返回 (exclude_rel_paths, inject_files)。

    exclude_rel_paths : 传给 Inno Setup ``Excludes`` 的相对路径列表。
    inject_files      : [(rel_dest_in_app, abs_src_path), ...] 需要临时改写后
                        单独注入的文件（例如剔除部分键后的 layout.json）。
    """
    root = os.path.dirname(os.path.abspath(__file__))
    exclude_paths: list = []
    inject_files: list = []

    # --- layout.json（单文件，含多类配置）---
    # 注意：PyInstaller 6 onedir 把 --add-data 的文件放到 _internal/ 下，
    # 冻结产物里真实路径是 <bundle>/_internal/layout.json（而非顶层）。
    # 这里同时探测两种位置，并用「实际所在位置的相对路径」做排除/注入，
    # 否则会出现：顶层没文件可排、_internal 里的真文件却原样被打进安装包。
    layout_cats = [c for c in excludes if c in _CONFIG_KEY_GROUPS]
    if layout_cats:
        layout_candidates = [
            os.path.join(bundle_dir, "_internal", "layout.json"),
            os.path.join(bundle_dir, "layout.json"),
        ]
        layout_path = next((p for p in layout_candidates if os.path.isfile(p)), None)
        layout_rel = (os.path.relpath(layout_path, bundle_dir).replace(os.sep, "/")
                      if layout_path else None)
        if layout_path is None:
            _log("[info] 冻结文件夹中无 layout.json，磁贴等配置本就不随包分发。")
        else:
            try:
                with open(layout_path, "r", encoding="utf-8-sig") as fh:
                    data = json.load(fh)
            except Exception as exc:  # noqa: 读取失败就不擅自改文件
                _log(f"[warn] 读取 layout.json 失败，跳过配置过滤: {exc}")
                data = None
            if isinstance(data, dict):
                drop = set()
                for c in layout_cats:
                    drop.update(_CONFIG_KEY_GROUPS[c])
                remaining = {k: v for k, v in data.items() if k not in drop}
                if remaining:
                    staging = os.path.join(root, "build", "config_staging")
                    os.makedirs(staging, exist_ok=True)
                    out = os.path.join(staging, "layout.json")
                    with open(out, "w", encoding="utf-8") as fh:
                        json.dump(remaining, fh, ensure_ascii=False, indent=2)
                    if layout_rel not in exclude_paths:
                        exclude_paths.append(layout_rel)
                    inject_files.append((layout_rel, out))
                    _log(f"已生成精简版 {layout_rel}（剔除: {', '.join(sorted(drop))}）")
                else:
                    if layout_rel not in exclude_paths:
                        exclude_paths.append(layout_rel)
                    _log(f"{layout_rel} 全部配置均被排除，安装后将使用出厂默认。")

    # --- 单独文件 / 目录 ---
    # 仅当该路径在冻结文件夹中真实存在时才加入排除（build_exe 默认不烘焙配置，
    # 且 rtss_config.json 等本就是运行时生成、从不随包分发；缺席时 Inno 的
    # Excludes 写成不存在的路径虽无害，但显式跳过更干净、也避免误匹配）。
    file_cats = [c for c in excludes if c in _CONFIG_FILE_GROUPS]
    for c in file_cats:
        for p in _CONFIG_FILE_GROUPS[c]:
            full = os.path.join(bundle_dir, p)
            if os.path.exists(full) and p not in exclude_paths:
                exclude_paths.append(p)

    # 去重并保持顺序
    seen: set = set()
    deduped: list = []
    for p in exclude_paths:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped, inject_files


def _git_log_entries(since_ref: str | None = None, max_n: int = 30) -> list[str]:
    """尝试用 git 生成更新条目；非 git 仓库 / git 不存在时返回空列表。"""
    try:
        import shutil
        if shutil.which("git") is None:
            return []
        root = os.path.dirname(os.path.abspath(__file__))
        if not os.path.isdir(os.path.join(root, ".git")):
            return []
        rng = f"{since_ref}..HEAD" if since_ref else "HEAD"
        out = subprocess.run(
            ["git", "log", f"--oneline", f"-n{max_n}", rng],
            cwd=root, capture_output=True, text=True, timeout=20,
        )
        if out.returncode != 0:
            return []
        entries = []
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            # 去掉 "hash " 前缀，仅保留提交信息。
            entry = line.split(" ", 1)[1] if " " in line else line
            entries.append(entry)
        return entries
    except Exception:
        return []


def _read_source_changelog(root: str) -> tuple[str, list[str]]:
    """读取源码根 ``changelog.json``（手工维护的更新说明真相源）。

    返回 ``(version, entries)``。该文件由开发期手工维护（Agent 每次完成任务
    追加条目到 ``entries``），make_installer 在打包时优先采用，避免非 git 仓库
    / 忘记带 ``--notes`` 时重建把更新内容清空。

    文件不存在或解析失败时返回 ``("", [])``，由调用方继续回退到 git log。
    """
    path = os.path.join(root, "changelog.json")
    if not os.path.isfile(path):
        return "", []
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        ver = str(data.get("version", "") or "")
        entries = [str(x) for x in (data.get("entries", []) or []) if str(x).strip()]
        return ver, entries
    except Exception:
        return "", []


def generate_changelog(bundle_dir: str, notes: list[str] | None = None) -> str:
    """在打包时生成 ``changelog.json`` 写入 ``bundle_dir/_internal/``。

    内容涵盖「上一次打包 -> 本次打包之间」的更新：
      - version      : 优先用源码根 changelog.json 的 version，否则 APP_VERSION
      - build_time   : 打包时刻（本地）
      - prev_version : 冻结文件夹里残留旧 changelog 的版本（覆盖安装探测）
      - entries      : 更新条目来源优先级
                       ``--notes`` > 源码根 changelog.json > git log > 空

    返回生成的文件路径。该文件会被主 [Files] 全量拷贝打进安装包。
    """
    import datetime
    internal = os.path.join(bundle_dir, "_internal")
    os.makedirs(internal, exist_ok=True)
    target = os.path.join(internal, "changelog.json")

    prev_version = ""
    # 覆盖安装场景：冻结文件夹里可能残留旧版的 changelog.json，取其版本作为
    # 「上一次打包版本」，从而算出本次差异区间。
    if os.path.isfile(target):
        try:
            with open(target, "r", encoding="utf-8-sig") as fh:
                old = json.load(fh)
            prev_version = str(old.get("version", "") or "")
        except Exception:
            prev_version = ""

    root = os.path.dirname(os.path.abspath(__file__))
    version = APP_VERSION
    entries: list[str] = []
    if notes:
        # 显式 --notes 优先级最高，覆盖其它来源。
        entries = [s.strip() for s in notes if s.strip()]
    else:
        # 1) 优先采用源码根手工维护的 changelog.json（Agent 维护的更新真相源），
        #    避免非 git 仓库 / 忘记带 --notes 时重建把更新内容清空。
        # 2) 否则退而求其次，用 git log 生成。
        src_ver, src_entries = _read_source_changelog(root)
        if src_entries:
            entries = src_entries
            if src_ver:
                version = src_ver
        else:
            entries = _git_log_entries(max_n=30)

    data = {
        "version": version,
        "build_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "prev_version": prev_version,
        "entries": entries,
    }
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    _log(f"已生成更新说明 changelog.json（v{version}"
         + (f"，上版 v{prev_version}" if prev_version else "")
         + f"，{len(entries)} 条更新）-> {target}")
    if not entries:
        _log("提示：未提供 --notes、源码根 changelog.json 无条目且非 git 仓库，"
             "更新内容为空。可在打包时加 --notes \"a;b;c\"，或先在源码根 "
             "changelog.json 维护 entries。")
    return target


def resolve_bundle_dir() -> str:
    """找到冻结后的文件夹（dist/NingBingJue 或 NBJ_DISTPATH 下的同名目录）。"""
    root = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    if os.environ.get("NBJ_DISTPATH"):
        candidates.append(os.path.join(os.environ["NBJ_DISTPATH"], "NingBingJue"))
    candidates.append(os.path.join(root, "dist", "NingBingJue"))
    candidates.append(os.path.join(root, "NingBingJue_portable", "NingBingJue"))
    for c in candidates:
        if os.path.isdir(c) and os.path.isfile(os.path.join(c, "NingBingJue.exe")):
            return c
    # 退化：返回默认路径，让后续报错更明确
    return os.path.join(root, "dist", "NingBingJue")


def find_iscc() -> str | None:
    env = os.environ.get("ISCC")
    if env and os.path.isfile(env):
        return env
    # 1) 我们已经静默装过的目录
    local = os.path.join(INNOSETUP_DIR, "ISCC.exe")
    if os.path.isfile(local):
        return local
    # 2) 系统常见安装位置
    prog = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    prog64 = os.environ.get("ProgramFiles", r"C:\Program Files")
    for base in (prog, prog64):
        for name in ("Inno Setup 6", "Inno Setup 7", "Inno Setup"):
            p = os.path.join(base, name, "ISCC.exe")
            if os.path.isfile(p):
                return p
    # 3) PATH
    found = shutil.which("ISCC.exe") or shutil.which("iscc")
    if found:
        return found
    return None


def download_file(url: str, dest: str) -> None:
    _log(f"下载中: {url}")
    # GitHub 偶尔需要 UA
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as fh:
        total = int(resp.headers.get("Content-Length", 0) or 0)
        got = 0
        chunk = 1 << 16
        while True:
            buf = resp.read(chunk)
            if not buf:
                break
            fh.write(buf)
            got += len(buf)
            if total:
                sys.stdout.write(f"\r     {got*100//total}% ({got>>20}MB/{total>>20}MB)")
                sys.stdout.flush()
    if total:
        sys.stdout.write("\n")
    _log(f"已下载 -> {dest}")


def ensure_iscc() -> str:
    existing = find_iscc()
    if existing:
        _log(f"找到 ISCC: {existing}")
        return existing

    _log("本机没有 Inno Setup，开始自动下载并静默安装…")
    os.makedirs(INNOSETUP_DIR, exist_ok=True)
    setup_exe = os.path.join(tempfile.gettempdir(), "innosetup_setup.exe")
    try:
        download_file(INNOSETUP_URL, setup_exe)
    except Exception as e:  # noqa
        _log("下载失败: " + str(e))
        _log("请手动安装 Inno Setup: https://jrsoftware.org/isdl.php")
        _log("装好后重跑本脚本，或设置环境变量 ISCC 指向 ISCC.exe。")
        raise SystemExit(2)

    _log(f"静默安装 Inno Setup -> {INNOSETUP_DIR}")
    # /VERYSILENT: 无界面；/SUPPRESSMSGBOXES: 不弹对话框；/DIR: 指定目录；/NORESTART
    rc = subprocess.run(
        [setup_exe, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
         f"/DIR={INNOSETUP_DIR}"],
        check=False,
    ).returncode
    if rc != 0:
        _log(f"Inno Setup 安装返回码 {rc}，可能失败。请手动安装后重试。")
        raise SystemExit(2)

    iscc = os.path.join(INNOSETUP_DIR, "ISCC.exe")
    if not os.path.isfile(iscc):
        _log(f"安装后未找到 {iscc}。请手动安装 Inno Setup 后重试。")
        raise SystemExit(2)
    _log(f"ISCC 就绪: {iscc}")
    # 尽力补齐中文语言包（GitHub 版有时不带）；失败不影响英文回退。
    try_fetch_chinese_isl()
    return iscc


def try_fetch_chinese_isl() -> None:
    """尽力从官方仓库拉 ChineseSimplified.isl 到 Languages 目录。
    失败（如网络受限）就静默跳过，generate_iss 会自动回退英文向导。"""
    dest = os.path.join(INNOSETUP_DIR, "Languages", "ChineseSimplified.isl")
    if os.path.isfile(dest):
        return
    # 候选源：优先官方 GitHub 源（raw 可能被墙，多试几个镜像/分支）
    candidates = [
        "https://raw.githubusercontent.com/jrsoftware/issrc/main/Files/Languages/ChineseSimplified.isl",
        "https://raw.githubusercontent.com/jrsoftware/issrc/master/Files/Languages/ChineseSimplified.isl",
        "https://cdn.jsdelivr.net/gh/jrsoftware/issrc@main/Files/Languages/ChineseSimplified.isl",
    ]
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=30).read()
            if len(data) < 1000:
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(data)
            _log("已补齐中文语言包 -> " + dest)
            return
        except Exception:  # noqa: 网络受限/超时都忽略
            continue
    _log("（未能下载中文语言包，安装向导将使用英文；不影响功能）")


def generate_iss(bundle_dir: str, output_dir: str,
                 exclude_paths: list | None = None,
                 inject_files: list | None = None) -> str:
    root = os.path.dirname(os.path.abspath(__file__))
    # ISCC 按 .iss 所在目录解析脚本里的相对路径，所以 bundle_dir/output_dir
    # 必须转成绝对路径，否则会被错拼到 build/ 下。
    bundle_dir = os.path.abspath(bundle_dir)
    output_dir = os.path.abspath(output_dir)
    build_dir = os.path.join(root, "build")
    os.makedirs(build_dir, exist_ok=True)
    iss_path = os.path.join(build_dir, "NingBingJue.iss")

    exclude_paths = exclude_paths or []
    inject_files = inject_files or []

    # 主 [Files] 入口的 Excludes（相对 bundle_dir 的路径，统一反斜杠）。
    # 关键：Inno Setup 把【行内分号】当作「参数分隔符」，因此多个排除项必须
    # 作为一个整体用一对双引号包住（分号作为 Excludes 列表内部的分隔符），
    # 而不能每个路径单独加引号（那样第二个路径会被误判成「新参数名」）。
    # 这些相对路径均不含空格，整段加一对引号即可。
    if exclude_paths:
        inner = ";".join(p.replace("/", "\\") for p in exclude_paths)
        excludes_block = ' ; Excludes: "%s"' % inner
    else:
        excludes_block = ""
    # 改写后需单独注入的文件（例如剔除部分键的 layout.json）
    extra_files_block = ""
    for rel_dest, abs_src in inject_files:
        d = os.path.dirname(rel_dest)
        dest_dir = "{app}" if not d else "{app}\\" + d.replace("/", "\\")
        extra_files_block += (
            f'Source: "{abs_src}"; DestDir: "{dest_dir}"; Flags: ignoreversion\n'
        )

    # 用原始字符串模板 + 占位符替换，避免 f-string 的反斜杠/大括号转义地狱。
    # 模板里 {app}/{autopf} 等是 Inno Setup 常量，保持原样；@@XXX@@ 才是占位符。
    template = r"""; 由 make_installer.py 自动生成，勿手改（改需求请改脚本）
#define MyAppName "@@APPNAME@@"
#define MyAppVersion "@@APPVER@@"
#define MyAppPublisher "@@APPPUB@@"
#define MyAppURL "@@APPURL@@"
#define MyAppExeName "NingBingJue.exe"

[Setup]
AppId={{@@APPID@@}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\NingBingJue
DefaultGroupName=NingBingJue
AllowNoIcons=yes
; 程序运行需要管理员（ryzenadj 写 Ring0 驱动），安装器也请求管理员
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64os
OutputDir=@@OUT@@
OutputBaseFilename=NingBingJue-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; 安装/卸载时若程序在跑，尝试关闭
CloseApplications=yes
SetupLogging=yes

@@LANG@@

[Files]
Source: "@@SRC@@\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs@@EXCLUDES@@

@@EXTRA_FILES@@
[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} Uninstall"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "额外快捷方式:"; Flags: unchecked

[Run]
; 安装后自动解除文件锁定（Mark of the Web），免去新设备手动右键解除
Filename: "powershell.exe"; Parameters: "@@RUNPARAMS@@"; Flags: runhidden

[UninstallDelete]
; 卸载时清理程序运行时产生的布局/配置/日志等
Type: filesandordirs; Name: "{app}"

@@CODE@@
"""
    # Inno Setup 用 "" 表示一个字面双引号；为避免 Python 裸三引号被 """ 提前闭合，
    # 这里用 Python 拼好整段 Parameters 值，再以占位符注入模板。
    run_params = (
        '-NoProfile -ExecutionPolicy Bypass -Command '
        '""Get-ChildItem -LiteralPath \'{app}\' -Recurse | Unblock-File""'
    )

    # 语言：优先中文；GitHub 版 Inno Setup 有时不带 ChineseSimplified.isl，
    # 这时回退到英文（Default.isl），向导变英文但功能完全正常。
    if os.path.isfile(os.path.join(INNOSETUP_DIR, "Languages", "ChineseSimplified.isl")):
        languages_block = (
            '[Languages]\n'
            'Name: "chinese"; MessagesFile: "compiler:Languages\\ChineseSimplified.isl"\n'
        )
    else:
        languages_block = (
            '[Languages]\n'
            'Name: "english"; MessagesFile: "compiler:Default.isl"\n'
        )

    # ----------------------------------------------------------------------
    # 覆盖安装时的「是否保留现有配置」逻辑（Inno Pascal Script）。
    #
    # 背景：本安装包默认【不打包任何本机配置】（见上面 Excludes 与 build_exe
    # 默认不烘焙配置）。但在「覆盖安装」场景下，目标目录里可能已经有上一版本
    # 运行时写出的配置文件（layout.json / rtss_config.json / config/undervolt.json
    # / buttons/）。而 Inno 检测到同 AppId 已安装时会先跑「上一版卸载程序」，其
    # [UninstallDelete] 会连同配置一起删掉 {app} 整个目录 —— 因此必须先把配置
    # 备份到 {tmp}，安装结束后再按需还原，否则「保留现有配置」无从谈起。
    #
    # 行为：
    #   - 启动时若目标目录已存在配置项 -> 备份到 {tmp}\NingBingJue_cfg_bak；
    #   - 向导中新开一页「现有配置」，给出一个【默认不勾选】的复选框：
    #        □ 覆盖为出厂默认（删除现有配置）
    #     该页仅当检测到已有配置时才出现；全新安装直接跳过。
    #   - 不勾选（默认）：安装阶段不动现有配置，结束后从备份还原（即便被上一版
    #     卸载程序删过也能恢复）—— 即「安装不覆盖任何配置」。
    #   - 勾选：安装阶段删除目标目录里的配置项 -> 程序回退到代码出厂默认；
    #   - 备份失败时（如权限不足）直接中止升级、绝不删除用户配置（加固）。
    #   - 最后清理 {tmp} 备份。
    # 注意：Pascal 字符串里的反斜杠必须原样保留（这里整体用原始字符串）。
    # ----------------------------------------------------------------------
    code_block = r"""
[Code]
var
  ConfigPage: TInputOptionWizardPage;
  ConfigBackupDir: string;
  DidBackup: Boolean;
  BackupFailed: Boolean;

function ConfigItemExists(const Base: string): Boolean;
begin
  Result :=
    FileExists(Base + '_internal\layout.json') or
    FileExists(Base + 'layout.json') or
    FileExists(Base + '_internal\rtss_config.json') or
    FileExists(Base + 'rtss_config.json') or
    FileExists(Base + '_internal\config\undervolt.json') or
    FileExists(Base + 'config\undervolt.json') or
    DirExists(Base + '_internal\buttons') or
    DirExists(Base + 'buttons') or
    DirExists(Base + 'config');
end;

// 取目标安装目录，但不依赖 WizardForm（早期事件里窗体尚未创建）。
// 与 DefaultDirName={autopf}\NingBingJue 保持一致。
function TargetDir(): string;
begin
  Result := AddBackslash(ExpandConstant('{autopf}\NingBingJue'));
end;

function ExistingConfigExists(): Boolean;
begin
  Result := ConfigItemExists(TargetDir());
end;

function CopyDir(const FromDir, ToDir: string): Boolean;
var
  FindRec: TFindRec;
  Src, Dst: string;
begin
  Result := True;
  if not DirExists(FromDir) then Exit;
  ForceDirectories(ToDir);
  if FindFirst(FromDir + '\*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Name = '.') or (FindRec.Name = '..') then Continue;
        Src := FromDir + '\' + FindRec.Name;
        Dst := ToDir + '\' + FindRec.Name;
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
        begin
          if not CopyDir(Src, Dst) then Result := False;
        end
        else
        begin
          if not FileCopy(Src, Dst, False) then Result := False;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

procedure BackupConfig();
var
  Src: string;
begin
  ConfigBackupDir := ExpandConstant('{tmp}\NingBingJue_cfg_bak');
  Src := TargetDir();
  BackupFailed := False;
  if DirExists(ConfigBackupDir) then
    DelTree(ConfigBackupDir, True, True, True);
  if ConfigItemExists(Src) then
  begin
    ForceDirectories(ConfigBackupDir);
    if FileExists(Src + '_internal\layout.json') then
      if not FileCopy(Src + '_internal\layout.json', ConfigBackupDir + '\layout_internal.json', False) then BackupFailed := True;
    if FileExists(Src + 'layout.json') then
      if not FileCopy(Src + 'layout.json', ConfigBackupDir + '\layout_top.json', False) then BackupFailed := True;
    if FileExists(Src + '_internal\rtss_config.json') then
      if not FileCopy(Src + '_internal\rtss_config.json', ConfigBackupDir + '\rtss_internal.json', False) then BackupFailed := True;
    if FileExists(Src + 'rtss_config.json') then
      if not FileCopy(Src + 'rtss_config.json', ConfigBackupDir + '\rtss_top.json', False) then BackupFailed := True;
    if FileExists(Src + '_internal\config\undervolt.json') then
      if not FileCopy(Src + '_internal\config\undervolt.json', ConfigBackupDir + '\undervolt_internal.json', False) then BackupFailed := True;
    if FileExists(Src + 'config\undervolt.json') then
      if not FileCopy(Src + 'config\undervolt.json', ConfigBackupDir + '\undervolt_top.json', False) then BackupFailed := True;
    { 整目录备份，覆盖 config/ 下除 undervolt.json 外的其它潜在文件 }
    if DirExists(Src + '_internal\config') then
      if not CopyDir(Src + '_internal\config', ConfigBackupDir + '\config_internal') then BackupFailed := True;
    if DirExists(Src + 'config') then
      if not CopyDir(Src + 'config', ConfigBackupDir + '\config_top') then BackupFailed := True;
    if DirExists(Src + '_internal\buttons') then
      if not CopyDir(Src + '_internal\buttons', ConfigBackupDir + '\buttons_internal') then BackupFailed := True;
    if DirExists(Src + 'buttons') then
      if not CopyDir(Src + 'buttons', ConfigBackupDir + '\buttons_top') then BackupFailed := True;
    DidBackup := not BackupFailed;
  end;
end;

function InitializeSetup(): Boolean;
begin
  DidBackup := False;
  BackupFailed := False;
  if ExistingConfigExists() then
  begin
    BackupConfig();
    // 加固：现有配置存在但备份失败（如权限不足），绝不继续升级，
    // 否则旧卸载程序会删除整个安装目录导致用户配置丢失。直接中止并提示。
    if BackupFailed then
    begin
      MsgBox('无法安全备份你现有的凝冰结魄配置（临时目录：' + ConfigBackupDir + '）。' + #13#10 +
             '为避免升级过程中丢失你的配置，安装已中止。' + #13#10 +
             '请关闭可能占用该安装目录的程序，或以管理员身份重新运行本安装包。',
             mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;
  Result := True;
end;

procedure InitializeWizard();
begin
  ConfigPage := CreateInputOptionPage(wpSelectTasks,
    '现有配置',
    '检测到已安装版本中存在配置文件',
    '默认保留你当前的配置，不做任何改动。如需放弃现有配置、恢复为出厂默认，请勾选下方选项。',
    True, False);
  ConfigPage.Add('覆盖为出厂默认（删除现有配置）');
  ConfigPage.Values[0] := False;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  { ConfigPage 可能在 InitializeWizard 之前被询问，做空判断避免触碰 WizardForm }
  if (ConfigPage <> nil) and (PageID = ConfigPage.ID) and (not ExistingConfigExists()) then
    Result := True;
end;

procedure DeleteConfig(const Base: string);
begin
  DelTree(Base + '_internal\layout.json', False, False, False);
  DelTree(Base + 'layout.json', False, False, False);
  DelTree(Base + '_internal\rtss_config.json', False, False, False);
  DelTree(Base + 'rtss_config.json', False, False, False);
  DelTree(Base + '_internal\config\undervolt.json', False, False, False);
  DelTree(Base + 'config\undervolt.json', False, False, False);
  DelTree(Base + '_internal\buttons', True, True, True);
  DelTree(Base + 'buttons', True, True, True);
end;

procedure RestoreConfig(const Base: string);
begin
  if FileExists(ConfigBackupDir + '\layout_internal.json') then
    FileCopy(ConfigBackupDir + '\layout_internal.json', Base + '_internal\layout.json', True);
  if FileExists(ConfigBackupDir + '\layout_top.json') then
    FileCopy(ConfigBackupDir + '\layout_top.json', Base + 'layout.json', True);
  if FileExists(ConfigBackupDir + '\rtss_internal.json') then
    FileCopy(ConfigBackupDir + '\rtss_internal.json', Base + '_internal\rtss_config.json', True);
  if FileExists(ConfigBackupDir + '\rtss_top.json') then
    FileCopy(ConfigBackupDir + '\rtss_top.json', Base + 'rtss_config.json', True);
  if FileExists(ConfigBackupDir + '\undervolt_internal.json') then
    FileCopy(ConfigBackupDir + '\undervolt_internal.json', Base + '_internal\config\undervolt.json', True);
  if FileExists(ConfigBackupDir + '\undervolt_top.json') then
    FileCopy(ConfigBackupDir + '\undervolt_top.json', Base + 'config\undervolt.json', True);
  if DirExists(ConfigBackupDir + '\config_internal') then
    CopyDir(ConfigBackupDir + '\config_internal', Base + '_internal\config');
  if DirExists(ConfigBackupDir + '\config_top') then
    CopyDir(ConfigBackupDir + '\config_top', Base + 'config');
  if DirExists(ConfigBackupDir + '\buttons_internal') then
    CopyDir(ConfigBackupDir + '\buttons_internal', Base + '_internal\buttons');
  if DirExists(ConfigBackupDir + '\buttons_top') then
    CopyDir(ConfigBackupDir + '\buttons_top', Base + 'buttons');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Base: string;
begin
  Base := TargetDir();
  if CurStep = ssInstall then
  begin
    { 仅当勾选「覆盖为出厂默认」时才删除现有配置；默认不勾选即保留 }
    if (ConfigPage <> nil) and ConfigPage.Values[0] then
      DeleteConfig(Base);
  end
  else if CurStep = ssPostInstall then
  begin
    { 未勾选（保留现有配置）或 ConfigPage 异常时都还原备份，避免误删用户配置 }
    if DidBackup and ((ConfigPage = nil) or (not ConfigPage.Values[0])) then
      RestoreConfig(Base);
    if DirExists(ConfigBackupDir) then
      DelTree(ConfigBackupDir, True, True, True);
  end;
end;
"""

    iss = (template
           .replace("@@APPNAME@@", APP_NAME)
           .replace("@@APPVER@@", APP_VERSION)
           .replace("@@APPPUB@@", APP_PUBLISHER)
           .replace("@@APPURL@@", APP_URL)
           .replace("@@APPID@@", APP_ID)
           .replace("@@OUT@@", output_dir)
           .replace("@@SRC@@", bundle_dir)
           .replace("@@RUNPARAMS@@", run_params)
           .replace("@@LANG@@", languages_block)
           .replace("@@EXCLUDES@@", excludes_block)
           .replace("@@EXTRA_FILES@@", extra_files_block)
           .replace("@@CODE@@", code_block))
    # 写 UTF-8 with BOM（Inno Setup 6 认 BOM 即按 UTF-8 解析，中文 AppName 才正常）
    with open(iss_path, "w", encoding="utf-8-sig") as fh:
        fh.write(iss)
    _log(f".iss 已生成: {iss_path}")
    return iss_path


def run_iscc(iscc: str, iss_path: str) -> int:
    _log("运行 ISCC 编译安装包（这一步可能要几十秒）…")
    proc = subprocess.run([iscc, iss_path], check=False)
    return proc.returncode


def generate_unblock_bat(output_dir: str) -> str:
    """生成「新设备一键放行.bat」：放在安装目录里以管理员运行，
    解除文件锁定并把该目录加入 Defender 排除项。"""
    path = os.path.join(output_dir, "新设备一键放行.bat")
    # 内容保持 ASCII，避免 .bat 在非 UTF-8 代码页下中文乱码；
    # 用法说明放在本脚本之外的消息里（中文）。
    content = (
        "@echo off\n"
        ":: NingBingJue trust tool - run as administrator inside the install folder\n"
        "setlocal\n"
        "set \"TARGET=%~dp0\"\n"
        "echo Unblocking files under %TARGET% ...\n"
        "powershell -NoProfile -ExecutionPolicy Bypass -Command \"Get-ChildItem -LiteralPath '%TARGET%' -Recurse | Unblock-File\"\n"
        "echo Adding %TARGET% to Windows Defender exclusions ...\n"
        "powershell -NoProfile -ExecutionPolicy Bypass -Command \"Add-MpPreference -ExclusionPath '%TARGET%'\"\n"
        "echo Done. You can now run NingBingJue.exe.\n"
        "pause\n"
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    _log(f"放行脚本已生成: {path}")
    return path


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Build NingBingJue installer (setup.exe)")
    parser.add_argument("--no-build", action="store_true",
                        help="跳过 PyInstaller 重打，直接用已有冻结文件夹")
    parser.add_argument("--bundle-dir", default=None,
                        help="指定已打好的冻结文件夹（含 NingBingJue.exe）")
    parser.add_argument("--with-config", action="store_true",
                        help="把当前本机配置(layout.json/config/buttons)一并打进安装包。"
                             "默认不打，安装后以出厂默认运行。与 --exclude/--config-choice "
                             "搭配可做类别选择；单独传 --exclude/--config-choice 也会隐式开启。")
    parser.add_argument("--exclude", action="append", metavar="CAT",
                        help="排除某类配置不打包(可重复)。可选: "
                             + ",".join(ALL_CONFIG_CATEGORIES) + " 或 all")
    parser.add_argument("--exclude-all", action="store_true",
                        help="排除全部用户配置(layout.json/undervolt/buttons 等)，"
                             "安装后恢复出厂默认")
    parser.add_argument("--config-choice", dest="config_choice", default=None,
                        help="交互菜单选择串：空=仅排除磁贴, N=全打包, "
                             "A=全排除, 或编号(如 1 或 1,2,3)")
    parser.add_argument("--notes", default=None,
                        help="更新内容条目，分号分隔；如 \"修复风扇;新增任务列表\""
                             "。未提供时尝试用 git log 生成。")
    args = parser.parse_args()

    root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(root)

    # 是否打包配置：显式 --with-config，或做了类别选择（选择蕴含先打包）。
    has_exclude_cli = bool(
        (args.exclude or [])
        or args.exclude_all
        or (args.config_choice is not None)
    )
    with_config = bool(args.with_config) or has_exclude_cli

    # 1) 构建冻结文件夹
    if not args.no_build:
        _log("步骤 1/5：PyInstaller 构建冻结文件夹…")
        import build_exe
        try:
            build_exe.main(["--with-config"] if with_config else [])
        except SystemExit as e:
            if e.code not in (0, None):
                _log(f"build_exe 返回 {e.code}，中止。")
                return 2
    else:
        _log("步骤 1/5：跳过 PyInstaller（--no-build）。")

    # 2) 定位冻结文件夹
    bundle_dir = args.bundle_dir or resolve_bundle_dir()
    if not (os.path.isdir(bundle_dir) and os.path.isfile(os.path.join(bundle_dir, "NingBingJue.exe"))):
        _log(f"找不到冻结文件夹或其中的 NingBingJue.exe：{bundle_dir}")
        _log("请先跑 build_exe.py，或用 --bundle-dir 指定。")
        return 2
    _log(f"冻结文件夹: {bundle_dir}")

    # 2b) 生成更新说明 changelog.json（让「更新内容」卡片有数据可显示）。
    notes = [s for s in (args.notes or "").split(";")] if args.notes else None
    generate_changelog(bundle_dir, notes=notes)

    # 3) 确保编译器
    _log("步骤 2/5：确保 Inno Setup 编译器…")
    iscc = ensure_iscc()

    # 4) 生成 .iss 并编译
    output_dir = os.path.join(root, "dist")
    os.makedirs(output_dir, exist_ok=True)

    # 4a) 配置过滤：决定哪些用户配置随包分发
    excludes = resolve_excludes(args)
    if with_config and excludes:
        _log("步骤 3b/5：配置过滤 — 将【不打包】: " + ", ".join(sorted(excludes)))
    elif with_config:
        _log("步骤 3b/5：配置过滤 — 打包当前全部配置（无排除）。")
    else:
        _log("步骤 3b/5：配置过滤 — 默认不打包任何本机配置（出厂默认）。")
    exclude_paths, inject_files = prepare_config_filters(bundle_dir, excludes)

    _log("步骤 3/5：生成 .iss 脚本…")
    iss_path = generate_iss(bundle_dir, output_dir, exclude_paths, inject_files)
    _log("步骤 4/5：编译安装包…")
    rc = run_iscc(iscc, iss_path)
    if rc != 0:
        _log(f"ISCC 返回 {rc}，安装包编译失败。")
        return 2

    # 5) 放行脚本
    _log("步骤 5/5：生成新设备放行脚本…")
    generate_unblock_bat(output_dir)

    setup = os.path.join(output_dir, "NingBingJue-Setup.exe")
    if os.path.isfile(setup):
        size = os.path.getsize(setup) >> 20
        _log("=" * 60)
        _log(f"完成！安装包: {setup}  ({size} MB)")
        if with_config and excludes:
            _log("本次安装包【已打包当前配置】，但剔除: " + ", ".join(sorted(excludes)))
            _log("（安装后的程序对这些剔除项使用出厂默认；其余配置沿用你当前的设置。）")
        elif with_config:
            _log("本次安装包包含你当前的全部配置。")
        else:
            _log("本次安装包【不含任何本机配置】，安装后以出厂默认运行。")
            _log("（安装向导默认保留目标机已有配置；如需恢复出厂默认需主动勾选。）")
        _log("把它拷到新设备双击安装即可（首次会被 SmartScreen 拦，点仍要运行）。")
        _log("若 Defender 仍误报，把同目录的「新设备一键放行.bat」放进安装目录以管理员运行。")
        _log("=" * 60)
        return 0
    _log("未找到产物 setup.exe，疑似编译失败。")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
