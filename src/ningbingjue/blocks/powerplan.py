"""电源计划区块（移植 YeManCC 的电源计划管理能力）。

YeManCC 内置 3 套预设电源计划（YMSilent / YMPerformance / YMMedium），以 ``.pow``
文件随包分发。凝冰结魄把它们放在 ``blocks/powerplans/`` 下，首次运行时用**固定
GUID** 导入到系统（预设计划不可移除），用户也可在卡片编辑项里导入/移除自己的
计划。卡片显示当前电源计划并高亮正在运行的那一项，点一下即可切换。

权限模型（与 Windows powercfg 一致）：
  - ``powercfg /setactive``（切换）普通用户即可。
  - ``powercfg /import`` / ``/delete``（导入/删除）**需要管理员**。
  - 因此：预设计划的「导入」与用户计划的「增删」都受管理员权限门控；非管理员时
    这些操作静默失败并提示用户以管理员身份运行。

跨卡片联动：配置预设（PresetBlock）在应用预设时会调用本区块的 ``OnApplyPreset``
钩子把「当前电源计划」切到预设记录的那一项；同时本区块在切换计划后通过
``app.sync_all()`` 让所有卡片（含电源计划卡片自身）立即刷新。

注意：本区块只管「电源计划」这一维，不影响 TDP/GPU/风扇/锁帧——后者由各自的区块
与「配置预设」统一编排。
"""

from __future__ import annotations
from ..ui.panels import _show_block_toast

import os
import re
import json
import shutil
import subprocess
import threading
import time
import uuid
import logging
from typing import Any

from ..core.config import AppConfig
from .base import BlockBlueprint


def _short_pow_err(out: str, n: int = 160) -> str:
    """把 powercfg 的 stderr/stdout 截断成可在 toast 显示的一行。"""
    out = (out or "").strip()
    if not out:
        return "（powercfg 无报错输出）"
    out = " ".join(out.split())  # 压掉换行，避免 toast 撑爆
    return out if len(out) <= n else out[:n] + "…"


def _log_powerplan(msg: str) -> None:
    """电源计划相关诊断统一走 'ningbingjue' logger（与 app.py 一致）。"""
    try:
        logging.getLogger("ningbingjue").warning(msg)
    except Exception:
        pass

def _norm_guid(guid: str) -> str:
    """规范化 GUID：去花括号 + 转小写。

    powercfg 的 ``/list``、``/getactivescheme`` 输出**不带**花括号；而
    ``/import`` 在**带**花括号时会返回「参数无效」并拒绝导入（实测：
    ``/import file {guid}`` → 参数无效，``/import file guid`` → 成功）。
    因此所有进入 powercfg 的 GUID 都必须是不带花括号的规范形态，系统回读的
    GUID 天然也是这个形态——统一规范化后，``_plan_exists`` 的比对才不会因
    「带/不带花括号」永远匹配不上而反复重导入。"""
    return (guid or "").strip().strip("{}").strip().lower()


# 预设电源计划：固定 GUID（避免每次导入生成随机 GUID 导致重复/漂移），随包 .pow。
# 重要（2026-08-28 修正）：本表 guid 必须与对应 .pow 文件**实际嵌入的 scheme GUID**
# 完全一致——否则卡片按错 GUID 去 /query、/setactive，永远查不到 → 显示「未知」、
# 「未应用」。早期版本这里写的是占位符 a1b2c3d4-0001/0002/0003，而 .pow 里嵌入的
# 是另一套真实 GUID，二者对不上就是根因。现改为从各 .pow 字节级读出并核对的真实值：
#   YMSilent      → 81833830-a9bc-11eb-82a8-a5ba27f939e5  (YMSilent.pow 嵌入)
#   YMPerformance → b5b141ec-902b-4343-92ba-7d92f8cf8fb5  (YMPerformance.pow 嵌入，
#                   由原与 Silent 撞车的 81833830-… 改为此值，避免两方案抢同一 GUID)
#   YMMedium      → 6231efb0-7e75-11eb-8425-e5380525389d  (YMmedium.pow 嵌入)
# 注：YMPerformance 另已删除「第3类核心（核型C）」调度实例（36687f9e-…-6865、
# bc5038f7-…-35ee）。HX370 只有 P 核+E 核 2 类拓扑，无第3类核心；原 .pow 含这些
# 实例会被 powercfg 在导入回扫时 prune 成幽灵方案（/import 报成功、/query 即不存在）。
# 注意：GUID 以**不带花括号**的规范形态存储（见 _norm_guid）。
# 卡片自上而下顺序即此列表顺序（预设在前、用户计划在后）：
#   1) YMSilent      - 仅小核关睿频
#   2) YMPerformance - Performance-全小核-开睿频
#   3) YMMedium      - 仅大核开关睿频
# 注意：guid 与随包 .pow 文件一一对应（改名不改 guid/文件），display name 含中文
# 语义说明，卡片直接展示该 name（与系统内部 .pow 注册名无关）。
BUILTIN_PLANS: list[dict[str, str]] = [
    {"name": "YMSilent - 仅小核关睿频", "guid": "81833830-a9bc-11eb-82a8-a5ba27f939e5",
     "file": "YMSilent.pow"},
    {"name": "Performance-全小核-开睿频", "guid": "b5b141ec-902b-4343-92ba-7d92f8cf8fb5",
     "file": "YMPerformance.pow"},
    {"name": "YMMedium - 仅大核开关睿频", "guid": "6231efb0-7e75-11eb-8425-e5380525389d",
     "file": "YMmedium.pow"},
]

# 预设计划「schema 版本」：代码侧每改一次随包 .pow 的关键结构（如摘核3、解 GUID
# 撞车），就 +1。真机上若已落地的版本低于此值，_ensure_builtin_plans 会一次性
# delete+import 全部预设计划，把修复后的 .pow 真正刷进既有系统——否则「已存在就跳过」
# 会让旧机器上的撞车/prune 残骸永不被刷新（详见 2026-08-28 复盘）。
BUILTIN_SCHEMA_VERSION = 1


def _builtin_schema_path() -> str:
    """强刷版本号持久化文件（与随包 .pow 同目录，避免牵动 layout.json 的保存流）。"""
    return os.path.join(os.path.dirname(__file__), "powerplans", "builtin_schema.json")


def _read_builtin_schema_version() -> int:
    try:
        with open(_builtin_schema_path(), "r", encoding="utf-8") as fh:
            return int((json.load(fh) or {}).get("version", 0))
    except Exception:
        return 0


def _write_builtin_schema_version(v: int) -> None:
    try:
        with open(_builtin_schema_path(), "w", encoding="utf-8") as fh:
            json.dump({"version": v}, fh)
    except Exception:
        pass


# 卡片标题语义色（与 LS/更新内容 同属「说明/工具」暖色系）。
POWERPLAN_COLOR = "#f5d74b"

# 处理器电源管理子组 + 两个「最大频率」设置的 powercfg GUID（标准设置，跨版本稳定）。
# 在 AMD Ryzen 上（目标硬件 HX370 / 8945HS 掌机），这两个都是真实存在、且都必须同值
# 写入的电源设置——只调其中一个锁不住小核/效率核上限：
# _PROC_MAXFREQ     = 75b0ae3f-…-c9611c25e100 = 「处理器最大频率」(Processor maximum
#                    frequency, MHz)。
# _PROC_MAXFREQ_EFF1 = 75b0ae3f-…-c9611c25e101 = 「第 1 类处理器电源效率处理器的最大
#                    频率」(Processor maximum frequency for Processor Power Efficiency
#                    Class 1, MHz)。它和上面的 e100 仅最后一位十六进制不同——两者都是
#                    有效 GUID，缺一不可（用户已在 AMD 掌机用 Power Settings Explorer
#                    实测确认：e100=处理器最大频率，e101=第1类最大频率）。
# apply_freq_settings 的 mhz 维度会把同一个 mhz 值同时写入这两个 GUID（见下方循环）。
_PROC_SUBGROUP = "54533251-82be-4824-96c1-47b60b740d00"
_PROC_MAXFREQ = "75b0ae3f-bce0-45a7-8c89-c9611c25e100"
_PROC_MAXFREQ_EFF1 = "75b0ae3f-bce0-45a7-8c89-c9611c25e101"
# 最大处理器状态（PROCTHROTTLEMAX）：百分比 0-100，100=不限。与 e100 / e101 互补的
# 兜底旋钮——若某平台未暴露 MHz 频率设置，靠它仍能按比例限频。
_PROC_MAXSTATE = "bc5038f7-23e0-4960-96da-33abaf5935ec"
# 处理器性能提升模式（Processor performance boost mode）：与上面同属「处理器电源
# 管理」子组。取值语义见下方 BOOST_MODE_LABELS（0=已禁用、2=高效、3=高效激进…）。
# 卡片「睿频」按钮检测/循环切换的就是它。
_PROC_BOOSTMODE = "be337238-0d82-4146-a960-4f3749d470c7"

# 提升模式取值语义表（微软 PERFBOOSTMODE；中文名取 Windows 控制面板常见译名）。
# 旧注释里的「高效率」= 文档中的 Efficient Aggressive = 控制面板的「高效激进」，
# 即值 3 那一档，与值 2「高效」(Efficient Enabled) 不是同一档。
# 5 / 6 是 Windows 11 新增的「在保证频率下」(at guaranteed) 变体，掌机上基本见不到。
BOOST_MODE_LABELS = {
    0: "已禁用",
    1: "已启用",
    2: "高效",
    3: "高效激进",
    4: "激进",
    5: "高效激进(保证)",
    6: "高效(保证)",
}
# 卡片「睿频」按钮的循环档位：已禁用 → 高效 → 高效激进 → 激进 → 回到已禁用。
# 1 / 5 / 6 与循环内档位语义重叠（都属「已启用」家族），不进循环；读到的值不在
# 循环内时下一跳统一落到 0，保证任何起始状态最多点 4 次都能回到「已禁用」。
BOOST_MODE_CYCLE = (0, 2, 3, 4)


def boost_mode_label(val: object) -> str:
    """把提升模式原始值翻译成中文短名；未知值/读不到回退为「值N」/「未知」。"""
    try:
        v = int(val)  # type: ignore[arg-type]
    except Exception:
        return "未知"
    return BOOST_MODE_LABELS.get(v, f"值{v}")


def next_boost_mode(cur: object) -> int:
    """返回循环序列的下一档；当前值不在序列内（含 None / 1 / 5 / 6）时回到首档 0。"""
    try:
        c = int(cur)  # type: ignore[arg-type]
    except Exception:
        return BOOST_MODE_CYCLE[0]
    if c in BOOST_MODE_CYCLE:
        i = BOOST_MODE_CYCLE.index(c)
        return BOOST_MODE_CYCLE[(i + 1) % len(BOOST_MODE_CYCLE)]
    return BOOST_MODE_CYCLE[0]

# 用户态电源方案注册表根。powercfg /import 本质就是往这里写；
# 每个方案节点存 FriendlyName / Description（决定控制面板里显示的名字），
# 以及各 subgroup\setting 下的 ACSettingIndex / DCSettingIndex 当前值。
_POW_USER_KEY = r"HKLM\SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"
# 全局电源设置定义位置（与具体方案无关）：Windows 把每个电源设置的「定义」挂在
# HKLM\...\Control\Power\PowerSettings\{子组}\{设置}。判断「平台是否提供某设置」
# 应看这里，而不是某个方案下物化的覆盖键（后者可能不存在，见 _global_setting_defined）。
_POWER_DEF_KEY = r"HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerSettings"


def _reg_read_dword(scheme_guid: str, setting: str) -> int | None:
    """读某电源方案某设置的 AC 当前值（MHz / 百分比 / 索引）。

    绕开 powercfg 的「隐藏设置过滤」：``75b0ae3f…e100``（处理器最大频率）在本机
    注册的 ``Attributes = 0x1``（HIDE），``powercfg /query`` 不会回带它的值，但
    注册表里该键**真实存在且生效**（实测活动方案 AC=DC=0x157c = 5500 MHz）。
    PowerSettings Explorer 能显示「处理器最大频率」正是因为它直接读注册表、忽略
    HIDE 位。这里同样直接读注册表，拿到的才是真实值。

    非管理员也可读（User\\PowerSchemes 当前用户有权限）。读不到（键/值不存在）
    返回 None——调用方据此判断该设置是否在该方案上真实存在。"""
    if not scheme_guid or not setting:
        return None
    key = (f"{_POW_USER_KEY}\\{_norm_guid(scheme_guid)}"
           f"\\{_PROC_SUBGROUP}\\{_norm_guid(setting)}")
    try:
        p = subprocess.run(
            ["reg", "query", key, "/v", "ACSettingIndex"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None
    if p.returncode != 0:
        return None
    out = _decode_console(p.stdout)
    for line in out.splitlines():
        if line.strip().upper().startswith("ACSETTINGINDEX"):
            try:
                return int(line.split()[-1], 0)
            except Exception:
                return None
    return None


def _global_setting_defined(setting: str) -> bool:
    """平台是否提供某个电源设置（看**全局定义**，而非某方案的覆盖键）。

    Windows 把每个电源设置的**定义**挂在
    ``HKLM\\SYSTEM\\CurrentControlSet\\Control\\Power\\PowerSettings\\{子组}\\{设置}``，
    与具体方案无关。只要 CPU/系统支持该处理器电源管理设置（含「处理器最大频率」
    75b0ae3f…e100），这个定义键必然存在——这是判断「平台是否支持」的正确依据。

    对照 :func:`_reg_read_dword`：后者读的是「某方案下物化的 ACSettingIndex 覆盖键」，
    那个键**只在方案被显式设过时才存在**；用它判定支持，会把「该方案没写过 e100」
    误判成「平台不支持」（这正是卡片「本平台不支持」时好时坏、又出现的根因）。
    本函数只看全局定义，从而彻底消除这个误报。"""
    if not setting:
        return False
    key = (f"{_POWER_DEF_KEY}\\{_PROC_SUBGROUP}"
           f"\\{_norm_guid(setting)}")
    try:
        p = subprocess.run(
            ["reg", "query", key],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return False
    return p.returncode == 0


def _is_admin() -> bool:
    """当前进程是否管理员（Windows）。非 Windows / 异常时返回 False。"""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _decode_console(data: bytes) -> str:
    """把 powercfg 等控制台程序的原始输出按系统 ANSI 代码页解码。

    powercfg 在中文 Windows 下按 GBK/CP936（系统 ANSI 代码页）输出，若用 utf-8
    硬解，中文方案名（平衡/高性能…）会变成乱码甚至被吞掉，导致刷新后
    active_name 为空、UI 显示「（未知）」。``mbcs`` 即「系统 ANSI 代码页」，跨
    locale 自适应（zh-CN=cp936、en-US=cp1252）；非 Windows 不可用则回退 utf-8。"""
    for enc in ("mbcs", "gbk", "utf-8"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="ignore")


def _run_powercfg(args: list[str]) -> tuple[int, str]:
    """跑一条 powercfg 命令，返回 (返回码, 合并输出)。非 Windows 直接失败。"""
    if os.name != "nt":
        return (1, "非 Windows 平台，powercfg 不可用")
    # 冻结 exe 是无控制台父进程；powercfg 是控制台子系统程序。仅重定向
    # stdout/stderr（capture_output）不足以抑制窗口——stdin 仍继承父进程的
    # 无效控制台句柄，Windows 会为它分配一个可见黑窗。启动期多次调用（/list、
    # /getactivescheme、首次运行的 3× /import）即表现为「很多一闪而过的黑窗」。
    # 显式 CREATE_NO_WINDOW 可彻底禁掉该窗口，且不改变命令本身的输入输出。
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["powercfg", *args],
            capture_output=True,   # 返回 bytes：powercfg 按系统 ANSI 代码页(中文=GBK)输出，
                                   # 不能用 text/utf-8 硬解，否则中文方案名变乱码 → 显示「未知」
            shell=False, timeout=20, creationflags=flags,
        )
        # 合并 stdout/stderr，按系统代码页解码并归一化 CRLF，便于后面正则逐行解析。
        raw = _decode_console(proc.stdout or b"") + _decode_console(proc.stderr or b"")
        return (proc.returncode, raw.replace("\r\n", "\n").replace("\r", "\n"))
    except Exception as exc:  # noqa: BLE001 - 任何异常都降级为「不可用」
        return (1, f"powercfg 执行失败：{exc}")


# powercfg /list 与 /getactivescheme 输出形如：
#   电源方案 GUID: 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c  (高性能) [*]
# 注意：GUID 不带花括号；名称可能在全角/半角括号内，也可能**缺失**（导入的
# YM*.pow 方案在系统里就是匿名行：`电源方案 GUID: a1b2c3d4-...  `）——因此名称
# 段必须设为可选，否则这些匿名方案永远匹配不到 → _plan_exists 恒 False → 每次
# 启动/切换都重导入，且活动方案名显示为「（未知）」。行尾偶带 '*' 标记活动方案。
# 行尾**不再**锚定 ``$``：幽灵方案下 /getactivescheme 的 stderr 会被 _run_powercfg
# 合并进同一行，形如
#   电源方案 GUID: b5b141ec-... 指定的电源方案、子组或设置不存在。
# 旧的 ``[ \t]*\*?[ \t]*$`` 要求行尾只剩空白和 '*'，遇到这种行会整行匹配失败，
# 导致连 GUID 都解析不出来 —— 而它恰恰是当前活动方案，卡片因此显示「（未知）」、
# read_max_state 等依赖 active_guid 的读取全部失效。改为只取 GUID + 可选括号名，
# 行内余下的报错文本直接忽略。
_SCHEME_RE = re.compile(
    r"GUID:[ \t]*[{]?([0-9a-fA-F-]{36})[}]?"
    r"(?:[ \t]+[（(][ \t]*(.*?)[ \t]*[）)])?",
    re.MULTILINE,
)


def _parse_list(out: str) -> list[dict[str, str]]:
    """从 ``powercfg /list`` 输出解析 [(guid, name), ...]。"""
    plans: list[dict[str, str]] = []
    for m in _SCHEME_RE.finditer(out):
        guid = m.group(1).strip().lower()
        name = (m.group(2) or "").strip()
        if guid:  # 名称允许为空（某些系统方案），但 GUID 必须有
            plans.append({"guid": guid, "name": name})
    return plans


class PowerPlanBlock:
    """电源计划管理区块（真实区块：持有计划列表 + powercfg 编排）。"""

    def __init__(self, config: "AppConfig", app: Any = None,
                 plans_dir: str | None = None,
                 probe_in_background: bool = False) -> None:
        self.config: "AppConfig" = config
        self.app = app  # Application 引用（用于 sync_all / save_all）
        # 预设 .pow 所在目录：默认与本模块同级的 powerplans/。
        if plans_dir is None:
            plans_dir = os.path.join(os.path.dirname(__file__), "powerplans")
        self.plans_dir = plans_dir
        # 用户自行导入的 .pow 托管目录：导入时复制副本到这里，支持重导入/导出，
        # 与预设计划（blocks/powerplans/*.pow 随包）对称。
        self.user_plans_dir = os.path.join(self.plans_dir, "user")
        try:
            os.makedirs(self.user_plans_dir, exist_ok=True)
        except Exception:
            pass
        # 面板 toast 所需的 ui 字典（BlockPanel._install_toast 会注入 toast_label/
        # toast_panel；show_toast 再读回来）。先给个空壳，避免属性缺失。
        self.ui: dict[str, Any] = {}
        self.admin = _is_admin()
        # 实际已导入到系统的计划（含内置 + 用户），刷新时填充。
        self.imported: list[dict[str, str]] = []
        # 当前活动方案 GUID / 名称（来自 powercfg /getactivescheme）。
        self.active_guid: str = ""
        self.active_name: str = ""
        # 用户主动选择的计划（app 自有意图，必准）；与系统 active_guid 可能发散。
        self.selected_guid: str = ""
        self.selected_name: str = ""
        # 系统实际与用户选择是否不一致（由 refresh 计算，供 UI 偏离告警）。
        self.diverged: bool = False
        # 首次 ensure 时未导入成功的预设计划（非管理员等），供 UI 提示。
        self.builtin_missing: list[str] = []
        # 状态锁：后台启动探测线程与 UI 线程（切换/导入/refresh）都会写
        # imported / active_* / selected_* 等状态；用 RLock——ensure 内部会调
        # refresh，同线程重入必须放行。
        self._state_lock = threading.RLock()
        self.probe_ready = True

        if probe_in_background:
            # 启动耗时热点修复（2026-08-30，grill-me 打点取证）：__init__ 串行跑
            # 5+ 条 powercfg 子进程（3×/query 存在性 + /list + /getactivescheme，
            # 每条数百 ms），实测独占 3.82s = boot 最大热点。GUI 模式改为后台线程
            # 探测：卡片先以空态上屏，主窗 1Hz sync_all 一秒内自然回填，无需跨线
            # 程 UI 调用；用户切换/导入操作经 _state_lock 与探测线程串行化。
            # headless / 单测保持默认 False（同步语义），既有测试不受影响。
            self.probe_ready = False
            threading.Thread(
                target=self._boot_probe,
                name="powerplan-boot-probe", daemon=True).start()
        else:
            self._boot_probe_sync()

    # -- 启动期探测（同步 / 后台两态共用） ----------------------------
    def _boot_probe_sync(self) -> None:
        """同步探测：ensure 预设 → refresh 系统 → 载入并校验 selected_guid。"""
        self._ensure_builtin_plans()
        self.refresh()
        self._load_selected_guid()
        self._validate_selected_guid()
        self.probe_ready = True

    def _boot_probe(self) -> None:
        """后台线程版启动探测：逐步独立持锁，不长时间霸占锁拖慢 UI 操作。"""
        try:
            with self._state_lock:
                self._ensure_builtin_plans()
            with self._state_lock:
                self.refresh()
            with self._state_lock:
                self._load_selected_guid()
                self._validate_selected_guid()
        except Exception as exc:  # noqa: BLE001 - 探测失败不致命，UI refresh 兜底
            _log_powerplan(
                f"[error] 启动后台探测失败（卡片空态待下次刷新）：{exc!r}")
        finally:
            self.probe_ready = True

    def _load_selected_guid(self) -> None:
        """载入上次用户主动选择的计划（跨重启持久化，用于 Q5 偏离显示）。"""
        try:
            sp = getattr(self.config.powerplan_config, "selected_guid", "")
            sn = getattr(self.config.powerplan_config, "selected_name", "")
            self.selected_guid = str(sp).strip().lower()
            self.selected_name = str(sn).strip()
        except Exception:
            pass

    # -- 预设计划导入 ------------------------------------------------
    def _pow_path(self, filename: str) -> str:
        return os.path.join(self.plans_dir, filename)

    def _user_pow_path(self, guid: str) -> str:
        """用户计划托管副本路径（约定以 GUID 命名，便于按 guid 重建）。"""
        return os.path.join(self.user_plans_dir, f"{_norm_guid(guid)}.pow")

    def _plan_exists(self, guid: str) -> bool:
        # 必须用 /query 判定，不能用 /list：
        # 自定义计划（含我们导入的 YM* 固定 GUID 方案）在【非管理员】上下文下
        # 不会被 /list 枚举（实测 /list 计数恒为 5，全是系统自带方案），但 /query
        # 仍能命中落地后的计划。若用 /list 判定，会出现「计划已导入却查不到」→
        # 卡片显示「未应用」、切换时反复触发假「导入失败」。/query 在本机与活动
        # 方案探测共用同一套「落地即可见」语义，是更权威的存在性判定。
        rc, _ = _run_powercfg(["/query", _norm_guid(guid)])
        return rc == 0

    def _listed_but_unqueryable(self, guid: str) -> bool:
        """方案是否处于「幽灵态」：``/list`` 能列出，但 ``/query`` 查不到。

        正常方案两者一致。幽灵态的典型成因是方案在注册表里残留了 key 与子组，
        却丢失了 ``FriendlyName`` 等值，导致电源策略引擎不承认它是有效方案：

        * ``powercfg /list`` 会把它列出来（甚至带着 ``*`` 表示当前活动方案），
          但名称为空；
        * ``powercfg /query <guid>`` 直接返回 rc=1「指定的电源方案、子组或设置
          不存在」；
        * 控制面板的电源计划列表里该项显示为空白，看起来像「啥都没显示」。

        对程序而言最致命的是：``_plan_exists`` 基于 ``/query``，在幽灵态下恒为
        False，于是每次切换都会走「补导入」，而 ``/import`` 又因为 GUID 已被占用
        而失败（或再写一个幽灵），用户永远切不过去。

        判定：``/query`` 失败 **且** ``/list`` 输出里出现了该 GUID。
        """
        g = _norm_guid(guid)
        if not g:
            return False
        rc_q, _ = _run_powercfg(["/query", g])
        if rc_q == 0:
            return False  # 能查到 = 健康，绝不是幽灵
        rc_l, out_l = _run_powercfg(["/list"])
        if rc_l != 0 or not out_l:
            return False  # /list 本身失败，不做任何猜测
        return g in out_l.lower()

    def _ensure_builtin_plans(self) -> None:
        """确保 3 个预设 .pow 以固定 GUID 干净落地到系统。

        幂等（默认已存在则跳过）；非管理员时 import 失败仅记录到 ``builtin_missing``。
        强刷：若系统已落地的预设计划 schema 版本低于 ``BUILTIN_SCHEMA_VERSION``
        （旧机器上残留的撞车/被 prune 残骸），则对每个预设计划先 ``/delete`` 再
        ``/import``，从修复后的 .pow 重建，并恢复活动方案、重导保存的频率/状态
        （避免用户微调被清零）。完成后把版本写入 sidecar 持久化。"""
        self.builtin_missing = []
        force = _read_builtin_schema_version() < BUILTIN_SCHEMA_VERSION

        # 幽灵方案自检：某些机器上预设计划会退化成「/list 能列出、/query 查不到」
        # 的幽灵态（注册表里 key 与子组都在，却缺少 FriendlyName，电源策略引擎
        # 不承认它是有效方案）。本机实测：YMPerformance(b5b141ec) 正是此态 ——
        # /list 里带着 *（说明它就是当前活动方案）却没有名称，/query 直接报
        # 「指定的电源方案、子组或设置不存在」，控制面板里对应项显示空白。
        # 这种状态下 _plan_exists()（基于 /query）恒为 False，若没有本检测，
        # 后续 /import 会因为 GUID 已存在而失败或写成又一个幽灵，用户永远切不过去。
        ghosts: list[str] = []
        if not force:
            for plan in BUILTIN_PLANS:
                g = _norm_guid(plan["guid"])
                if self._plan_exists(g):
                    continue
                if self._listed_but_unqueryable(g):
                    ghosts.append(plan["name"])
            if ghosts:
                force = True
                _log_powerplan(
                    f"[warn] 检测到预设计划处于幽灵态，将强制重建：{ghosts}")

        if force:
            # 强刷前先刷新一次，拿到真实活动方案用于事后恢复，避免误切系统默认。
            self.refresh()
            prev_active = self.active_guid

        for plan in BUILTIN_PLANS:
            guid = _norm_guid(plan["guid"])
            if self._plan_exists(guid) and not force:
                continue
            if not self.admin:
                self.builtin_missing.append(plan["name"])
                continue
            path = self._pow_path(plan["file"])
            if not os.path.exists(path):
                self.builtin_missing.append(plan["name"])
                _log_powerplan(
                    f"[warn] 预设计划文件缺失，跳过导入：name={plan['name']} "
                    f"guid={guid} path={path}")
                continue
            if force:
                # 清掉可能残留的旧方案（撞车 GUID / 被 prune 的幽灵），保证从干净
                # .pow 重建。若被删项恰是当前活动方案，Windows 会先回落到别的方案，
                # 后面统一用 prev_active 恢复。
                _run_powercfg(["/delete", guid])
            rc, out = _run_powercfg(["/import", path, guid])
            if rc != 0:
                # 幽灵态：注册表里残留同名 GUID（空名/半导入）导致 /import 报
                # 「已存在」却不被 /list 枚举。先 /delete 清掉残留再重试一次。
                if ("已存在" in out) or ("already exists" in out.lower()):
                    _log_powerplan(
                        f"[warn] 预设计划幽灵残留，先删除再导入：name={plan['name']} "
                        f"guid={guid}")
                    _run_powercfg(["/delete", guid])
                    rc, out = _run_powercfg(["/import", path, guid])
                if rc != 0:
                    self.builtin_missing.append(plan["name"])
                    _log_powerplan(
                    f"[error] 预设计划导入失败：name={plan['name']} guid={guid} "
                    f"rc={rc} pow={path} powercfg_out={out!r}")
                    continue
            # 导入「成功」（rc=0）不等于计划真的落地：本机电源策略引擎可能在回扫时
            # 因 .pow 含本机不支持的设置项（如混合架构 CPU 的第3类核心调度实例）而
            # 直接把整个计划 prune 掉——典型症状就是 /import 报成功、下一秒 /query
            # 就「不存在」。立即复核，避免卡片出现「导入了却怎么都查不到 / 显示未知」。
            rcq, qout = _run_powercfg(["/query", guid])
            if rcq != 0:
                self.builtin_missing.append(plan["name"])
                _log_powerplan(
                    f"[error] 预设计划导入后未能落地（被系统 prune）：name={plan['name']} "
                    f"guid={guid} pow={path}。常见根因：随包 .pow 含本机 CPU/Windows "
                    f"不支持的设置项（如第3类核心调度）。建议改用 powercfg /duplicatescheme "
                    f"从「高性能」基准重建该计划，仅写入本机支持的处理器频率/状态项。")

        if force and self.admin:
            # 恢复强刷前的活动方案（预设计划或用户计划，二者都还在系统里）。
            self.refresh()
            if prev_active and self._plan_exists(prev_active):
                _run_powercfg(["/setactive", prev_active])
                self.refresh()
            # 重导用户保存的频率/状态到当前活动计划（仅当活动计划是预设计划，
            # 避免把微调写进系统自带方案）。max_freq_mhz 默认 0（未设）、
            # max_state_pct 默认 100（不限）。
            mhz = getattr(self.config.powerplan_config, "max_freq_mhz", 0) or 0
            pct = getattr(self.config.powerplan_config, "max_state_pct", 100)
            if (mhz > 0) or (pct != 100):
                if any(p["guid"].lower() == self.active_guid for p in BUILTIN_PLANS):
                    g = self.active_guid
                    if mhz > 0:
                        for sid in (_PROC_MAXFREQ, _PROC_MAXFREQ_EFF1):
                            self._write_setting(g, sid, int(mhz))
                    if pct != 100:
                        self._write_setting(g, _PROC_MAXSTATE, max(0, min(100, int(pct))))
                    _run_powercfg(["/setactive", g])
            _write_builtin_schema_version(BUILTIN_SCHEMA_VERSION)

    def _validate_selected_guid(self) -> None:
        """清理/迁移持久化的 selected_guid。

        旧代码 BUILTIN_PLANS 用占位符 a1b2c3d4-0001/0002/0003，早期 set_active 因
        占位 GUID 永远查不到而从未成功激活，但万一某版把占位符或旧撞车 GUID 写进了
        SelectedGuid，这里：① 若仍有效（系统里有 / 是预设计划 / 是用户计划）则不动；
        ② 否则尝试按 selected_name 映射回预设计划真实 GUID；③ 仍对不上则清空，
        避免卡片永久误报「你的选择 vs 系统实际」偏离。"""
        sel = (getattr(self.config.powerplan_config, "selected_guid", "") or "").strip().lower()
        if not sel:
            return
        # 仍有效 → 不动。
        if (self._plan_exists(sel)
                or any(p["guid"].lower() == sel for p in BUILTIN_PLANS)
                or any(u["guid"] == sel for u in self._user_plans())):
            return
        # 失效：尝试按名称映射到预设计划真实 GUID。
        name = (getattr(self.config.powerplan_config, "selected_name", "") or "").strip()
        for p in BUILTIN_PLANS:
            if p["name"] == name:
                self.config.powerplan_config.selected_guid = p["guid"].lower()
                self.config.powerplan_config.selected_name = p["name"]
                self.selected_guid = p["guid"].lower()
                self.selected_name = p["name"]
                self._persist_powerplan_config()
                return
        # 名称也对不上 → 彻底清空，避免误报偏离。
        self.config.powerplan_config.selected_guid = ""
        self.config.powerplan_config.selected_name = ""
        self.selected_guid = ""
        self.selected_name = ""
        self._persist_powerplan_config()

    def _persist_powerplan_config(self) -> None:
        """尽力把 powerplan_config 落盘（依赖 app.save_all；不可用时静默跳过）。"""
        if self.app is not None and hasattr(self.app, "save_all"):
            try:
                self.app.save_all()
            except Exception:
                pass

    # -- 列表 / 活动方案 --------------------------------------------
    def _user_plans(self) -> list[dict[str, str]]:
        """用户导入的计划（来自 config，可移除）。"""
        out: list[dict[str, str]] = []
        for p in getattr(self.config.powerplan_config, "user_plans", []) or []:
            guid = _norm_guid(p.get("guid", ""))
            name = str(p.get("name", "")).strip()
            if guid and name:
                out.append({"guid": guid, "name": name, "user": True,
                            "pow": str(p.get("pow", ""))})
        return out

    def plans(self) -> list[dict[str, str]]:
        """卡片展示用的完整计划列表：预设（不可移除）在前，用户计划在后。

        即便预设计划尚未成功导入（非管理员），仍列出其名称——这样用户能看到
        「有哪些预设」并点击尝试切换（切换失败会提示需管理员导入）。"""
        builtins = [
            {"guid": p["guid"].lower(), "name": p["name"], "user": False}
            for p in BUILTIN_PLANS
        ]
        return builtins + self._user_plans()

    def refresh(self) -> None:
        """重新探测系统中已导入的计划与当前活动方案。

        持 _state_lock：与后台启动探测线程（probe_in_background）串行化对
        imported / active_* 的写入；RLock 允许 ensure 强刷路径内同线程重入。"""
        with self._state_lock:
            rc, out = _run_powercfg(["/list"])
            self.imported = _parse_list(out) if rc == 0 else []
            # /getactivescheme 即便 rc!=0，也常在报错的同时把 GUID 打到 stdout
            #（活动方案名被清空/半损坏时尤其常见）。因此不论 rc 如何都先尝试解析
            # GUID，避免卡片永久显示无意义的「（未知）」。
            rc2, aout = _run_powercfg(["/getactivescheme"])
            self.active_guid = ""
            self.active_name = ""
            m = _SCHEME_RE.search(aout)
            if m:
                self.active_guid = m.group(1).strip().lower()
                self.active_name = (m.group(2) or "").strip()
            if not self.active_guid:
                _log_powerplan(
                    f"[warn] 无法从 /getactivescheme 解析活动方案：rc2={rc2} "
                    f"out={aout!r}")
            elif not self.active_name:
                # 无名方案（幽灵态）：/getactivescheme 只回 GUID、不回名称——实测本机
                # YMPerformance 幽灵态正是如此，卡片会显示成「（未知）」，用户完全
                # 不知道自己正跑在哪个计划上。这里用内置命名表按 GUID 补回显示名。
                self.active_name = self._name_for(self.active_guid)
            # 系统实际与用户主动选择是否发散（Q5 偏离检测）。
            self.diverged = bool(self.selected_guid) and self.selected_guid != self.active_guid
            # 若活动方案不在我们的命名表里（例如系统「平衡」），仍保留其真实名称。

    # -- 切换 / 导入 / 删除 -----------------------------------------
    def _reimport_if_needed(self, guid: str) -> bool:
        """计划尚未导入系统时，从托管 .pow 副本补导入。

        处理预设（blocks/powerplans/*.pow 随包）与用户计划（托管副本
        blocks/powerplans/user/<guid>.pow）。需管理员。已存在 / 导入成功返回
        True；缺权限或副本丢失返回 False（并弹 toast）。"""
        if self._plan_exists(guid):
            return True
        if not self.admin:
            self.show_toast(
                "✗ 需以管理员身份运行才能导入/切换该电源计划", success=False)
            return False
        # 预设计划：从随包 .pow 找。
        builtin = next((p for p in BUILTIN_PLANS
                        if p["guid"].lower() == guid), None)
        if builtin:
            path = self._pow_path(builtin["file"])
            if os.path.exists(path):
                rc, out = _run_powercfg(["/import", path, _norm_guid(guid)])
                if rc == 0:
                    # 补 FriendlyName：powercfg /import 对已存在 GUID 走 merge 时可能丢
                    # 名字（实测本机 YMPerformance 导入后注册表缺 FriendlyName，控制面板
                    # 显示成裸 GUID）。此处主动 /changename 写入 .pow 里的计划名，确保
                    # 系统方案显示真实名字。失败不阻断切换（仅记日志）。
                    try:
                        rc_n, out_n = _run_powercfg(
                            ["/changename", _norm_guid(guid), builtin["name"]])
                        if rc_n != 0:
                            _log_powerplan(
                                f"[warn] 补 FriendlyName 失败（不阻断切换）："
                                f"guid={guid} name={builtin['name']!r} out={out_n!r}")
                    except Exception as _e:
                        _log_powerplan(f"[warn] 补 FriendlyName 异常（不阻断切换）：{_e!r}")
                    # 导入「成功」不代表立刻可见：/import 返回后，系统电源策略引擎
                        # 可能还没让 /query 看到新方案（导入-查询竞态）。旧代码只查一次，
                        # 一失败就把 /import 的「已成功导入电源方案」当成错误文本弹出
                        # （用户看到自相矛盾的「导入失败：已成功导入」），并直接放弃后续
                        # /setactive。现在：① 重试 3 次（间隔 150ms）吸收竞态；
                        # ② 仍失败也不再打断切换，改为乐观继续（见下方 return True），
                        #   由 set_active 的写后读回校验给出真实结果。
                        qerr = ""
                        for _attempt in range(3):
                            rcq, qout = _run_powercfg(["/query", _norm_guid(guid)])
                            if rcq == 0:
                                return True
                            qerr = qout
                            if _attempt < 2:
                                time.sleep(0.15)
                        # 复核始终失败：绝大多数情况是导入-查询竞态（/import 刚返回，
                        # 电源策略引擎还没让 /query 看到新方案）。此时**不要**放弃切换
                        # ——旧代码一失败就 return False，导致后面的 /setactive 根本没
                        # 执行，用户看到「已成功导入电源方案」却没有任何计划被应用。
                        # 改为乐观继续：把结果交给 set_active 的写后读回校验兜底
                        #（/setactive 后 refresh 比对 active_guid）；若系统真的没切过去，
                        # 那里会给出明确的失败提示，绝不静默成功。
                        _log_powerplan(
                            f"[warn] 切换时预设计划导入后 /query 复核未通过，仍尝试应用："
                            f"guid={guid} pow={path} query_out={qerr!r}")
                        return True
                    # 快速连点切换时，/list 可能瞬间没探到该 GUID，导致这里误判「未导入」
                    # 而计划其实已在系统（固定 GUID 早就导入过）→ /import 报「指定GUID已有
                    # 方案」。此时计划实际存在，重探一次确认即可，直接当作已导入继续
                    # setactive，不再弹「导入失败」假报错（切换本就不该依赖重新 import）。
                    if self._plan_exists(guid):
                        return True
                    # 走到这里说明 /import 本身失败（rc != 0）：错误文本用 /import 的输出。
                    self.show_toast(
                        f"✗ 预设计划导入失败：{_short_pow_err(out)}", success=False)
                    _log_powerplan(
                        f"[error] 切换时预设计划导入失败：guid={guid} rc={rc} "
                        f"pow={path} powercfg_out={out!r}")
                    return False
            self.show_toast("✗ 预设计划文件缺失，无法导入", success=False)
            _log_powerplan(
                f"[warn] 切换时预设计划文件缺失：guid={guid} path={path}")
            return False
        # 用户计划：从托管副本找（优先 config 记录的路径，回退按 guid 约定）。
        user = next((p for p in self._user_plans()
                     if p["guid"] == guid), None)
        cand = (user.get("pow", "") if user else "") or self._user_pow_path(guid)
        if cand and os.path.exists(cand):
            rc, out = _run_powercfg(["/import", cand, _norm_guid(guid)])
            if rc == 0:
                # 与预设计划一致：/import 成功后复核 /query，重试 3 次（间隔 150ms）
                # 吸收导入-查询竞态；仍失败也乐观继续（见下方 return True），由
                # set_active 的写后读回校验兜底，不再「导入成功却不应用」。
                qerr = ""
                for _attempt in range(3):
                    rcq, qout = _run_powercfg(["/query", _norm_guid(guid)])
                    if rcq == 0:
                        return True
                    qerr = qout
                    if _attempt < 2:
                        time.sleep(0.15)
                # 同预设计划：复核失败多为导入-查询竞态，乐观继续交给 set_active
                # 的写后读回校验兜底，不再「导入成功却不应用」。
                _log_powerplan(
                    f"[warn] 切换时用户计划导入后 /query 复核未通过，仍尝试应用："
                    f"guid={guid} pow={cand} query_out={qerr!r}")
                return True
            # 同预设计划：快速连点竞态下 /import 可能因 GUID 已存在而失败，重探确认
            # 计划其实在系统里，则当作已导入继续 setactive，不弹假报错。
            if self._plan_exists(guid):
                return True
            self.show_toast(
                f"✗ 用户计划导入失败：{_short_pow_err(out)}", success=False)
            _log_powerplan(
                f"[error] 切换时用户计划导入失败：guid={guid} rc={rc} "
                f"pow={cand} powercfg_out={out!r}")
            return False
        self.show_toast("✗ 该电源计划未导入系统且无副本可恢复", success=False)
        return False

    def set_active(self, guid: str) -> bool:
        """切换当前电源计划。普通用户即可（计划须已存在于系统）。

        返回是否成功；失败时弹 toast 说明原因（未导入 / 无权限 / 写后校验不符）。
        写后读回校验（Q2-b）：/setactive 后 refresh 比对 active_guid，若系统实际
        未切换到目标，判失败并提示，绝不静默成功。"""
        guid = _norm_guid(guid.strip())
        if not guid:
            return False
        # 记录用户主动选择（app 自有意图）——即便随后 apply 失败也保留，
        # 这样「你的选择 vs 系统实际」的偏离能被正确显示（Q5）。
        name = self._name_for(guid) or guid
        self.selected_guid = guid
        self.selected_name = name
        try:
            self.config.powerplan_config.selected_guid = guid
            self.config.powerplan_config.selected_name = name
        except Exception:
            pass
        if not self._reimport_if_needed(guid):
            return False
        rc, _ = _run_powercfg(["/setactive", _norm_guid(guid)])
        if rc != 0:
            self.show_toast("✗ 切换电源计划失败", success=False)
            return False
        self.refresh()
        # Q2(b) 写后读回校验：系统实际活动方案必须与目标一致，否则判失败。
        if self.active_guid and self.active_guid != guid:
            self.show_toast(
                f"⚠ 已发送切换指令，但系统实际仍为：{self.active_name}",
                success=False)
            self._sync_ui()
            return False
        self._sync_ui()
        self.show_toast(f"✓ 已切换电源计划：{name}", success=True)
        return True

    def refresh_and_sync(self) -> None:
        """探测系统当前计划 + 让卡片立即重绘。供页面激活 / 唤醒前台 / AC-DC
        切换等事件触发，确保「系统实际」始终最新。"""
        self.refresh()
        self._sync_ui()

    def import_plan(self, pow_path: str, name: str | None = None) -> str | None:
        """导入用户自己的 .pow 文件，返回一个新 GUID；失败返回 None。

        需管理员（powercfg /import）。GUID 用 uuid4 随机生成，避免与既有方案冲突。"""
        if not self.admin:
            self.show_toast("✗ 导入电源计划需管理员权限", success=False)
            return None
        if not os.path.exists(pow_path):
            self.show_toast("✗ 文件不存在", success=False)
            return None
        guid = str(uuid.uuid4())
        rc, out = _run_powercfg(["/import", pow_path, guid])
        if rc != 0:
            self.show_toast(
                f"✗ 导入失败：{_short_pow_err(out)}", success=False)
            _log_powerplan(
                f"[error] 用户 .pow 导入失败：rc={rc} pow={pow_path} "
                f"powercfg_out={out!r}")
            return None
        # 复制 .pow 到托管目录作为副本（与预设对称，可重导入/导出）。复制失败不致命。
        managed_path = self._user_pow_path(guid)
        try:
            os.makedirs(self.user_plans_dir, exist_ok=True)
            shutil.copy2(pow_path, managed_path)
        except Exception:
            managed_path = ""
        # 从系统回读真实名称（用户可能没给名字，或给的不准）。
        real_name = name or self._name_for(guid) or os.path.basename(pow_path)
        self.config.powerplan_config.user_plans.append(
            {"guid": guid, "name": real_name, "pow": managed_path})
        if self.app is not None and hasattr(self.app, "save_all"):
            try:
                self.app.save_all()
            except Exception:
                pass
        self.refresh()
        self._sync_ui()
        self.show_toast(f"✓ 已导入电源计划：{real_name}", success=True)
        return guid

    def remove_plan(self, guid: str) -> bool:
        """移除用户计划（预设计划不可移除）。需管理员。"""
        guid = _norm_guid(guid.strip())
        # 预设计划一律拒绝移除。
        if any(p["guid"].lower() == guid for p in BUILTIN_PLANS):
            self.show_toast("✗ 预设电源计划不可移除", success=False)
            return False
        if not self.admin:
            self.show_toast("✗ 移除电源计划需管理员权限", success=False)
            return False
        rc, _ = _run_powercfg(["/delete", _norm_guid(guid)])
        if rc != 0:
            self.show_toast("✗ 移除失败", success=False)
            return False
        self.config.powerplan_config.user_plans = [
            p for p in self.config.powerplan_config.user_plans
            if str(p.get("guid", "")).strip().lower() != guid
        ]
        # 一并删除托管副本（若存在）。
        try:
            mp = self._user_pow_path(guid)
            if os.path.exists(mp):
                os.remove(mp)
        except Exception:
            pass
        if self.app is not None and hasattr(self.app, "save_all"):
            try:
                self.app.save_all()
            except Exception:
                pass
        self.refresh()
        self._sync_ui()
        self.show_toast("✓ 已移除电源计划", success=True)
        return True

    def export_plan(self, guid: str, dest_dir: str) -> str | None:
        """导出用户计划的托管副本到 dest_dir，返回目标路径；失败返回 None。

        用于备份/迁移：把 blocks/powerplans/user/<guid>.pow 复制出去。"""
        user = next((p for p in self._user_plans()
                     if p["guid"] == guid), None)
        src = (user.get("pow", "") if user else "") or self._user_pow_path(guid)
        if not src or not os.path.exists(src):
            self.show_toast("✗ 该计划无可导出的副本", success=False)
            return None
        try:
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(src))
            shutil.copy2(src, dest)
            self.show_toast(f"✓ 已导出：{dest}", success=True)
            return dest
        except Exception as exc:
            self.show_toast(f"✗ 导出失败：{exc}", success=False)
            return None

    def _name_for(self, guid: str) -> str:
        guid = guid.strip().lower()
        for p in self.plans():
            if p["guid"] == guid:
                return p["name"]
        for p in self.imported:
            if p["guid"] == guid:
                return p["name"]
        return ""

    def _sync_ui(self) -> None:
        """切换/导入/删除后让所有卡片刷新（含本卡片）。"""
        if self.app is not None and hasattr(self.app, "sync_all"):
            try:
                self.app.sync_all()
            except Exception:
                pass

    # -- 最大频率（滑块）-----------------------------------------------
    def freq_setting_available(self, guid: str | None = None) -> bool:
        """平台是否暴露了「处理器最大频率」(75b0ae3f…e100) 这个电源设置。

        ⚠️ 关键修正（2026-08-30）：旧实现用「当前活动方案下 e100 的
        ``ACSettingIndex`` 是否存在」来判定「是否支持」。但那个键**只在方案被显式
        设过 e100 时才物化**——新建/复制/导入的方案（尤其是 YM 那几个 .pow 预设）
        往往没有它。结果就是：只要切到一个没写过 e100 的活动方案，卡片就误报
        「本平台不支持」，而平台其实完全支持。这正是该提示「时好时坏、又出现」的
        根因。

        现改为看**全局设置定义**是否存在（``HKLM\\...\\Control\\Power\\PowerSettings
        \\{子组}\\75b0ae3f…e100``，见 :func:`_global_setting_defined`）：定义存在即
        视为平台支持。具体方案下读不到 e100 值（键未物化）时由 :meth:`read_max_freq`
        回退为 0（不限），**绝不**再误报「本平台不支持」。在任意带处理器电源管理的
        Windows（含 HX370/8945HS 掌机）上，该定义都存在，故滑块始终可用。"""
        return _global_setting_defined(_PROC_MAXFREQ)

    def read_max_freq(self, guid: str | None = None) -> int:
        """读当前(或指定)计划的「处理器最大频率」(75b0ae3f) 当前值(MHz)。

        直接读注册表 ACSettingIndex（见 :func:`_reg_read_dword`），不受 powercfg
        对隐藏设置的过滤影响——隐藏设置拿到的仍是真实生效值（如 5500）。返回 0
        仅表示读不到（键/值不存在）；正常情况返回真实 MHz。写入走
        :meth:`apply_freq_settings`，不依赖此读值。"""
        g = _norm_guid(guid) if guid else self.active_guid
        if not g:
            return 0
        v = _reg_read_dword(g, _PROC_MAXFREQ)
        return v if v is not None else 0

    def _write_setting(self, guid: str, sid: str, val: int) -> bool:
        """对单一电源设置写 AC+DC 双电源索引；两个都成功才算成功。"""
        rc_ac, o_ac = _run_powercfg(
            ["/setacvalueindex", guid, _PROC_SUBGROUP, sid, str(val)])
        rc_dc, o_dc = _run_powercfg(
            ["/setdcvalueindex", guid, _PROC_SUBGROUP, sid, str(val)])
        if rc_ac == 0 and rc_dc == 0:
            return True
        _log_powerplan(
            f"[warn] 频率设置跳过：sid={sid} rc_ac={rc_ac} rc_dc={rc_dc} "
            f"out_ac={o_ac!r}")
        return False

    def read_max_state(self, guid: str | None = None) -> int:
        """读当前(或指定)计划的「最大处理器状态」(PROCTHROTTLEMAX, bc5038f7) 百分比。

        仅作滑块初值参考；返回 0 表示未限制/最高或读不到。写入走 apply_max_state /
        apply_freq_settings，不依赖此读值。"""
        g = _norm_guid(guid) if guid else self.active_guid
        if not g:
            return 0
        rc, out = _run_powercfg(["/query", g, _PROC_SUBGROUP, _PROC_MAXSTATE])
        if rc != 0:
            return 0
        m = re.search(r"电源设置索引[:：]\s*0x([0-9a-fA-F]+)", out)
        if not m:
            return 0
        try:
            return int(m.group(1), 16)
        except Exception:
            return 0

    def read_boost_mode(self, guid: str | None = None) -> int | None:
        """读当前(或指定)计划的「处理器性能提升模式」(be337238-0d82-4146-a960-4f3749d470c7) 当前值。

        取值语义见模块级 BOOST_MODE_LABELS：0=已禁用、2=高效、3=高效激进、4=激进。
        返回 None 表示读不到（键/值不存在）。直接读注册表 ACSettingIndex，
        与最大频率同机制，非管理员也可读。"""
        g = _norm_guid(guid) if guid else self.active_guid
        if not g:
            return None
        return _reg_read_dword(g, _PROC_BOOSTMODE)

    def boost_mode_label(self, val: object) -> str:
        """实例代理 ``boost_mode_label``：UI 层经 block 实例取档位中文名，
        避免 ``ui.panels`` 反向 import 本模块造成循环导入。"""
        return boost_mode_label(val)

    def next_boost_mode(self, cur: object) -> int:
        """实例代理 ``next_boost_mode``：返回循环序列的下一档，理由同上。"""
        return next_boost_mode(cur)

    def apply_boost_mode(self, val: int) -> bool:
        """把「处理器性能提升模式」(be337238-0d82-4146-a960-4f3749d470c7) 写到【当前活动】计划（AC+DC），
        再 setactive 生效。需管理员。成功返回 True。

        只做单设置写入，不动最大频率/最大处理器状态；值被平台拒绝时软失败。
        下一跳由调用方用 ``next_boost_mode()`` 决定，这里不关心循环逻辑。"""
        if not self.admin:
            self.show_toast("✗ 调整睿频需以管理员身份运行", success=False)
            return False
        g = self.active_guid
        if not g:
            self.show_toast("✗ 未检测到当前活动电源计划", success=False)
            return False
        val = int(val)
        if self._write_setting(g, _PROC_BOOSTMODE, val):
            _run_powercfg(["/setactive", g])
            self.show_toast(f"✓ 睿频已设为{boost_mode_label(val)}",
                            success=True)
            return True
        self.show_toast("✗ 写入睿频设置失败（计划/平台不支持）",
                        success=False)
        return False

    def apply_freq_settings(self, mhz: int | None = None,
                            state_pct: int | None = None) -> bool:
        """把「处理器最大频率」(e100) + 「第 1 类处理器电源效率处理器的最大频率」(e101)
        与「最大处理器状态」(PROCTHROTTLEMAX, bc5038f7) 一并写到【当前活动】计划（AC+DC），
        再 setactive 生效。

        - 只改当前活动计划，绝不 /import / /delete，不破坏预设计划文件——符合
          「只改动当前正在应用的电源计划，不要破坏预设电源」的约束。
        - 需管理员（写入系统电源策略）。非管理员直接提示并返回 False。
        - 两个「最大频率」电源设置（e100 处理器最大频率、e101 第1类效率核最大频率）
          在 AMD Ryzen 上均存在，必须同值一起写入才能锁住小核/效率核上限；若某平台
          未暴露其中某个 GUID，写失败的会被软跳过（不致命），另一个仍生效。
        - bc5038f7（最大处理器状态，百分比）是与 e100 / e101 互补的兜底旋钮，纳入
          滑块控制，确保掌机上调频绝对生效。
        返回是否至少成功写入一个设置。"""
        if not self.admin:
            self.show_toast("✗ 调整频率需以管理员身份运行", success=False)
            return False
        g = self.active_guid
        if not g:
            self.show_toast("✗ 未检测到当前活动电源计划", success=False)
            return False
        if mhz is None and state_pct is None:
            return False
        applied = 0
        skipped: list[str] = []
        # 处理器最大频率维度（MHz）：e100(处理器最大频率) + e101(第1类效率核最大频率)
        # 两个 GUID 同值一起写入（AMD 上两者都存在）。
        if mhz is not None:
            mhz = max(0, int(mhz))
            for sid in (_PROC_MAXFREQ, _PROC_MAXFREQ_EFF1):
                if self._write_setting(g, sid, mhz):
                    applied += 1
                else:
                    skipped.append(sid)
        # 最大处理器状态维度（百分比，AMD 可靠）：bc5038f7。
        if state_pct is not None:
            pct = max(0, min(100, int(state_pct)))
            if self._write_setting(g, _PROC_MAXSTATE, pct):
                applied += 1
            else:
                skipped.append(_PROC_MAXSTATE)
        if applied == 0:
            self.show_toast(
                "✗ 未能写入任何频率设置（当前计划/平台可能不支持）",
                success=False)
            return False
        # 写入后必须 setactive 才会让新值生效。
        _run_powercfg(["/setactive", g])
        # 持久化滑块目标值，供重启恢复 + 下次 Y 直接复用。
        try:
            if mhz is not None:
                self.config.powerplan_config.max_freq_mhz = mhz
            if state_pct is not None:
                self.config.powerplan_config.max_state_pct = pct
            if self.app is not None and hasattr(self.app, "save_all"):
                self.app.save_all()
        except Exception:
            pass
        self.refresh()
        self._sync_ui()
        # 「最大频率」维度同时写到了哪两个电源设置，必须在提示里说清楚：
        # e100(处理器最大频率) 与 e101(第1类处理器电源效率处理器的最大频率) 同值写入。
        # 在 AMD 掌机上两者都存在→小核/效率核上限被同值锁住；若某平台只暴露 e100，
        # 则 e101 被软跳过，这里把被跳过的设置名明确列出来，避免误判。
        _SID_NAME = {
            _PROC_MAXFREQ: "处理器最大频率(75b0ae3f…e100)",
            _PROC_MAXFREQ_EFF1: "第1类效率核最大频率(75b0ae3f…e101)",
            _PROC_MAXSTATE: "最大处理器状态",
        }
        note = ""
        if skipped:
            skipped_names = "、".join(_SID_NAME.get(s, s) for s in skipped)
            note = f"（{skipped_names} 当前平台未暴露该电源设置，已跳过）"
        parts = []
        if mhz is not None:
            # mhz 维度始终同值写入 e100 与 e101 两个 GUID（AMD 上两者都存在）。
            both = _PROC_MAXFREQ not in skipped and _PROC_MAXFREQ_EFF1 not in skipped
            freq_desc = ("处理器最大频率(75b0ae3f…e100) + 第1类效率核最大频率(75b0ae3f…e101)"
                         if both else "处理器最大频率(75b0ae3f…e100)")
            parts.append(f"{freq_desc} = {mhz} MHz")
        if state_pct is not None:
            parts.append(f"最大处理器状态 = {pct}%")
        self.show_toast(
            f"✓ 已应用：{' / '.join(parts)}{note}", success=True)
        return True

    def apply_max_freq(self, mhz: int) -> bool:
        """兼容旧调用：仅写「处理器最大频率」(MHz) 维度。"""
        return self.apply_freq_settings(mhz=mhz, state_pct=None)

    def apply_max_state(self, pct: int) -> bool:
        """仅写「最大处理器状态」(PROCTHROTTLEMAX, 百分比) 维度。"""
        return self.apply_freq_settings(mhz=None, state_pct=pct)

    # -- 预设快照 / 回填 --------------------------------------------
    def on_export_preset(self) -> dict[str, Any]:
        """导出当前电源计划：活动方案名称 + GUID。

        命名用活动方案的真实名称（无论是预设还是系统「平衡」）。若探测失败则回退
        到首个预设计划名，保证预设名称段不为空。"""
        name = self.active_name or (BUILTIN_PLANS[0]["name"] if BUILTIN_PLANS else "")
        guid = self.active_guid or (BUILTIN_PLANS[0]["guid"] if BUILTIN_PLANS else "")
        return {"name": name, "guid": guid}

    def on_apply_preset(self, data: dict[str, Any]) -> None:
        """从预设回填电源计划：切到预设记录的那一项。"""
        if not isinstance(data, dict):
            return
        guid = str(data.get("guid", "")).strip()
        if not guid:
            return
        self.set_active(guid)

    # -- 配置持久化 --------------------------------------------------
    def save_config(self) -> None:
        """用户计划已就地写入 config.powerplan_config.user_plans，落盘由统一的
        app.save_all() 负责（AppConfig.to_dict 已序列化 PowerPlanConfig）。

        重要：本方法**不再**自行调用 app.save_all()——那样会与 app.save_all 遍历各
        区块 OnSaveConfig 钩子形成「save_all → 钩子 → save_all」重入死循环（曾导致
        boot() 卡死在 "Starting NingBingJue..."）。作为 OnSaveConfig 钩子，这里只需
        保证内存态已就绪（user_plans 的改动已由调用方就地写入），故为空操作；需要
        立即落盘时由调用方（import_plan / remove_plan）直接调 self.app.save_all()。"""
        # 内存态已在调用方就地修改；app.save_all 统一持久化。故意不回调，避免重入。
        return

    def load_config(self) -> None:
        """重新探测系统计划（用户计划列表来自 config，无需重载）。"""
        self.admin = _is_admin()
        self.refresh()

    # -- toast（PyQt6 版，面板会注入 toast_label / toast_panel） ----
    def show_toast(self, text: str, success: bool = True) -> None:
        _show_block_toast(self.ui, text, success)

    # -- 注册 --------------------------------------------------------
    def register(self, registry: Any) -> None:
        registry.register(BlockBlueprint(
            block_type="powerplan",
            name="电源计划",
            color=POWERPLAN_COLOR,
            card=None,
            hooks={
                "GetCardHeight": lambda: 340,
                "OnRefresh": self.refresh,
                "OnSaveConfig": self.save_config,
                "OnLoadConfig": self.load_config,
                "OnExportPreset": self.on_export_preset,
                "OnApplyPreset": self.on_apply_preset,
            },
        ).to_block())
