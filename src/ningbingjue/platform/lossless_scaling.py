"""Lossless Scaling preset adapter — read/write ``settings.xml`` presets.

Mirrs the LS block: the active profile is ``settings.xml``; a named preset is a
copy of that file under a presets directory.  ``MockLosslessScaling`` keeps an
in-memory dict so the logic is fully testable.  Status parsing of
``ScalingType`` / ``FrameGeneration`` / ``LSFG2Mode`` / ``LSFG3Mode1`` is a
pure function so it works identically on host and in tests.
"""

from __future__ import annotations


import json
import os
import re
import shutil
from typing import Any

try:  # Windows-only；非 Windows 下置空，离线搜索直接返回空。
    import winreg
except Exception:  # noqa: BLE001
    winreg = None  # type: ignore[assignment]

from .base import ExternalAdapter
from .process_manager import ProcessManager, Win32ProcessManager


def parse_ls_status(text: str) -> dict[str, str]:
    """Extract the three LS status fields from ``settings.xml`` text.

    Each field is shown as the literal value when present, else ``"读取失败"``.
    A value of off/false/none/0 is rendered as the off indicator.
    """
    def grab(tag: str) -> str:
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.IGNORECASE)
        if not m:
            return ""
        return m.group(1).strip()

    def norm(v: str) -> str:
        return "关闭" if v.lower() in ("off", "false", "none", "0", "") else v

    return {
        "ScalingType": norm(grab("ScalingType")),
        "FrameGeneration": norm(grab("FrameGeneration")),
        "PerfMode": norm(grab("LSFG2Mode") or grab("LSFG3Mode1")),
    }


class LosslessScaling(ExternalAdapter):
    def __init__(self, settings_xml: str, presets_dir: str, current_profile_file: str = "",
                 process_manager: ProcessManager | None = None) -> None:
        self.settings_xml = settings_xml
        self.presets_dir = presets_dir
        self.current_profile_file = current_profile_file
        self._pm = process_manager

    @property
    def available(self) -> bool:
        return os.path.exists(self.settings_xml) or os.path.isdir(self.presets_dir)

    # -- LS 主程序路径（机器级用户配置，必须存到源码树之外，否则一拉源码就被冲掉）--
    @staticmethod
    def _user_data_dir() -> str:
        """持久化用户数据目录：%LOCALAPPDATA%/ningbingjue。

        关键修复：LS 主程序路径是「本机专属」配置，绝不能存在源码/工作树里——
        拉取源码（sync.bat 的 ``robocopy /MIR``，或 ``git pull/clone/clean``）会
        刷新整棵工作树，把工作树里的 ``ls_config.json`` 抹掉或覆盖成另一台机器的
        路径，于是出现「手动设置的路径一拉源码就消失」。AppData 不在同步范围内，
        每次拉取都原样保留。该目录与本项目的 vdesktop.log 同源（build_virtual_desktop
        已用），是本项目持久化用户数据的既定位置。"""
        d = os.path.join(os.path.expanduser("~"), "AppData", "Local", "ningbingjue")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d

    @property
    def _exe_config_path(self) -> str:
        return os.path.join(self._user_data_dir(), "ls_config.json")

    @property
    def _legacy_exe_config_path(self) -> str:
        """旧位置（presets 目录内）。仅用于一次性迁移，新版本不再写入。"""
        return os.path.join(self.presets_dir, "ls_config.json")

    @staticmethod
    def _read_exe_from(p: str) -> str:
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                return (data.get("exe_path") or "").strip()
        except Exception:
            pass
        return ""

    @property
    def exe_path(self) -> str:
        """已配置的 Lossless Scaling 主程序路径；未设置/读取失败返回 ""。

        启动时若新位置（AppData）为空，但旧位置（presets 目录）仍残留有效路径，
        则一次性迁移过去，避免老用户升级后路径「凭空消失」。"""
        try:
            val = self._read_exe_from(self._exe_config_path)
            if val:
                return val
            legacy = self._read_exe_from(self._legacy_exe_config_path)
            if legacy:
                self.set_exe_path(legacy)  # 迁移：旧位置 -> 新位置（源码树之外）
                return legacy
        except Exception:
            pass
        return ""

    def set_exe_path(self, path: str) -> None:
        """持久化 LS 主程序路径到 AppData/ningbingjue/ls_config.json（源码树之外）。"""
        try:
            p = self._exe_config_path
            os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"exe_path": path or ""}, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _get_pm(self) -> ProcessManager | None:
        pm = self._pm
        if pm is None:
            try:
                pm = Win32ProcessManager()
                self._pm = pm
            except Exception:
                return None
        return pm

    def detect_exe_path(self) -> str:
        """定位 Lossless Scaling 主程序路径。

        优先级：① 扫描当前运行的 ``losslessscaling.exe`` 进程（最准，能拿到真实
        镜像路径）；② 离线兜底——LS 没运行时按常见安装位置（Steam 库 / Program
        Files / LocalAppData）搜索 ``losslessscaling.exe``。都找不到返回 ""。"""
        pm = self._get_pm()
        if pm is not None and pm.available:
            try:
                hit = pm.find_exe_path_by_name("losslessscaling.exe")
                if hit:
                    return hit
            except Exception:
                pass
        # 离线兜底：LS 未运行时也尽量定位 exe，避免「自动设置」永远提示找不到。
        return self._search_known_locations()

    def _steam_roots(self) -> list[str]:
        """返回所有 Steam 库根目录（主库 + libraryfolders.vdf 里的额外库）。"""
        roots: list[str] = []
        if winreg is not None:
            for hive, sub in (
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            ):
                try:
                    with winreg.OpenKey(hive, sub) as k:  # type: ignore[arg-type]
                        val = winreg.QueryValueEx(k, "SteamPath")[0]  # type: ignore[arg-type]
                        if val:
                            roots.append(str(val))
                except Exception:
                    pass
        # libraryfolders.vdf：额外库路径（格式 "path" "X:\..."）。
        for r in list(roots):
            vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
            if not os.path.exists(vdf):
                continue
            try:
                with open(vdf, "r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        m = re.search(r'"\s*path"\s+"([^"]+)"', line)
                        if m:
                            roots.append(m.group(1))
            except Exception:
                pass
        # 去重保序
        seen: set[str] = set()
        uniq: list[str] = []
        for r in roots:
            rp = (r or "").strip()
            if rp and rp not in seen:
                seen.add(rp)
                uniq.append(rp)
        return uniq

    def _search_known_locations(self) -> str:
        """LS 未运行时，按常见安装位置离线搜索 ``losslessscaling.exe``。

        覆盖 Steam 库（主库 + 额外库）、Program Files(x86)/Program Files、
        LocalAppData 下的 ``Lossless Scaling`` 目录。命中最先找到的返回全路径。"""
        candidates: list[str] = []
        for steam_root in self._steam_roots():
            candidates.append(os.path.join(
                steam_root, "steamapps", "common",
                "Lossless Scaling", "losslessscaling.exe"))
        for base in (os.environ.get("ProgramFiles"),
                     os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.append(os.path.join(
                    base, "Lossless Scaling", "losslessscaling.exe"))
        for c in candidates:
            if c and os.path.exists(c):
                return c
        return ""

    def _running_pid(self) -> int | None:
        pm = self._get_pm()
        if pm is None or not pm.available:
            return None
        try:
            for p in pm.enumerate():
                if "losslessscaling" in (p.name or "").lower():
                    return p.pid
        except Exception:
            pass
        return None

    def focus_running(self) -> bool:
        """把已在运行的 LS 窗口前置（单实例，避免重复拉起）。best-effort。"""
        pid = self._running_pid()
        if pid is None:
            return False
        pm = self._get_pm()
        if pm is not None and hasattr(pm, "focus_window"):
            try:
                return bool(pm.focus_window(pid))
            except Exception:
                return False
        return False

    def launch(self) -> tuple[bool, str]:
        """拉起 Lossless Scaling —— 朴素启动，与磁贴卡片「双击打开应用」完全一致。

        直接用 ``os.startfile(path)``（Windows ShellExecute 的 open 动词，即双击
        exe），**不做任何运行检测 / 前置窗口 / 状态判断**。Lossless Scaling 是单实例
        程序，已运行时 ShellExecute 会把它自身窗口前置，关闭时则真正拉起——所以
        「永远直接打开」既能在关闭时启动、又能在已运行时无害前置。

        路径解析（点击启动**永远尝试**，绝不静默打开设置面板）：
          1. 已存路径有效 → 直接用它；
          2. 已存路径空 / 失效 → 本次点击**临时探测**（扫描进程 + 常见安装目录），
             探测到的路径**只用于本次启动、绝不写回**，避免覆盖用户在掌机上手动设的
             路径；
          3. 都找不到 → 返回 ``nopath``，由调用方提示去设置。

        返回 ``(ok, key)``，``key`` ∈ {"launched","nopath","fail"}：
          launched — 已拉起（或交由单实例前置已有窗口）
          nopath   — 没有任何可用路径（调用方应提示去设置）
          fail     — 启动过程抛异常
        """
        path = self.exe_path
        if not path or not os.path.exists(path):
            # 路径空/失效：本次点击临时探测，不写回（杜绝覆盖用户已设路径）。
            path = self.detect_exe_path()
        if not path:
            return False, "nopath"
        try:
            os.startfile(path)  # noqa: S603 - 已校验过的 LS 主程序（双击即打开）
            return True, "launched"
        except Exception:
            return False, "fail"

    def save_preset(self, name: str) -> bool:
        if not os.path.exists(self.settings_xml) or not name:
            return False
        try:
            os.makedirs(self.presets_dir, exist_ok=True)
            shutil.copy(self.settings_xml, os.path.join(self.presets_dir, f"{name}.xml"))
            return True
        except Exception:
            return False

    def load_preset(self, name: str) -> bool:
        src = os.path.join(self.presets_dir, f"{name}.xml")
        if not os.path.exists(src):
            return False
        try:
            shutil.copy(src, self.settings_xml)
            self.set_current_profile(name)
            return True
        except Exception:
            return False

    def delete_preset(self, name: str) -> bool:
        src = os.path.join(self.presets_dir, f"{name}.xml")
        if not os.path.exists(src):
            return False
        try:
            os.remove(src)
            return True
        except Exception:
            return False

    def list_presets(self) -> list[str]:
        if not os.path.isdir(self.presets_dir):
            return []
        return sorted(os.path.splitext(f)[0] for f in os.listdir(self.presets_dir) if f.endswith(".xml"))

    def current_profile(self) -> str:
        if not self.current_profile_file or not os.path.exists(self.current_profile_file):
            return "未追踪"
        try:
            with open(self.current_profile_file, "r", encoding="utf-8") as fh:
                return fh.read().strip() or "未追踪"
        except Exception:
            return "读取失败"

    def set_current_profile(self, name: str) -> None:
        if not self.current_profile_file:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.current_profile_file)), exist_ok=True)
            with open(self.current_profile_file, "w", encoding="utf-8") as fh:
                fh.write(name)
        except Exception:
            pass

    def read_status(self) -> dict[str, str]:
        if not os.path.exists(self.settings_xml):
            return {"ScalingType": "读取失败", "FrameGeneration": "读取失败", "PerfMode": "读取失败"}
        try:
            with open(self.settings_xml, "r", encoding="utf-8", errors="ignore") as fh:
                return parse_ls_status(fh.read())
        except Exception:
            return {"ScalingType": "读取失败", "FrameGeneration": "读取失败", "PerfMode": "读取失败"}

    def is_running(self) -> bool:
        """Detect whether the Lossless Scaling process is running (Windows)."""
        pm = self._pm
        if pm is None:
            try:
                pm = Win32ProcessManager()
                self._pm = pm
            except Exception:
                return False
        if not pm.available:
            return False
        try:
            return any("losslessscaling" in (p.name or "").lower() for p in pm.enumerate())
        except Exception:
            return False

    def close(self) -> bool:
        """强制关闭正在运行的 Lossless Scaling（taskkill /F）。

        用于「加载预设后使配置生效」：LS 只在启动时读一次 ``settings.xml``，
        改了预设必须关掉进程再拉起才会真正套用。best-effort，绝不抛异常。
        返回 True 表示已向运行中的 LS 进程发出关闭信号（找不到进程则返回
        False，且不报错）。"""
        pid = self._running_pid()
        if pid is None:
            return False
        pm = self._get_pm()
        if pm is None or not pm.available:
            return False
        try:
            return bool(pm.stop(pid))
        except Exception:
            return False

    def set_running(self, running: bool) -> None:
        # The real adapter only reports LS process state; it does not start/stop
        # Lossless Scaling itself (that's the user's action). No-op for parity.
        pass


class MockLosslessScaling(LosslessScaling):
    def __init__(self, settings_xml: str = "", presets_dir: str = "", current_profile_file: str = "") -> None:
        super().__init__(settings_xml or "settings.xml", presets_dir or ".", current_profile_file)
        self.presets: dict[str, Any] = {}
        self.current: Any = None
        self._status: dict[str, str] = {
            "ScalingType": "关闭", "FrameGeneration": "关闭", "PerfMode": "关闭",
        }
        self._running: bool = False
        self._exe_path: str = ""
        self._detected_exe: str = ""  # 测试可注入的「自动设置」结果
        self._focused: bool = False
        self.close_calls: int = 0
        self.launch_calls: int = 0

    @property
    def exe_path(self) -> str:
        return self._exe_path

    def set_exe_path(self, path: str) -> None:
        self._exe_path = path or ""

    @property
    def available(self) -> bool:
        return True

    def save_preset(self, name: str) -> bool:
        if not name:
            return False
        self.presets[name] = dict(self.current or {})
        return True

    def load_preset(self, name: str) -> bool:
        if name not in self.presets:
            return False
        self.current = dict(self.presets[name])
        self.current_profile_name = name
        return True

    def delete_preset(self, name: str) -> bool:
        return self.presets.pop(name, None) is not None

    def list_presets(self) -> list[str]:
        return sorted(self.presets.keys())

    def current_profile(self) -> str:
        return getattr(self, "current_profile_name", "未追踪")

    def set_current_profile(self, name: str) -> None:
        self.current_profile_name = name

    def read_status(self) -> dict[str, str]:
        return dict(self._status)

    def is_running(self) -> bool:
        return self._running

    def set_running(self, running: bool) -> None:
        self._running = running

    def detect_exe_path(self) -> str:
        return self._detected_exe

    def focus_running(self) -> bool:
        self._focused = True
        return True

    def close(self) -> bool:
        if not self._running:
            return False
        self._running = False
        self.close_calls += 1
        return True

    def launch(self) -> tuple[bool, str]:
        self.launch_calls += 1
        if not self._exe_path or not os.path.exists(self._exe_path):
            return False, "nopath"
        self._running = True
        return True, "launched"