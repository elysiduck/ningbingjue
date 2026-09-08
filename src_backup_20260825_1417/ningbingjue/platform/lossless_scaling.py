"""Lossless Scaling preset adapter — read/write ``settings.xml`` presets.

Mirrs the LS block: the active profile is ``settings.xml``; a named preset is a
copy of that file under a presets directory.  ``MockLosslessScaling`` keeps an
in-memory dict so the logic is fully testable.  Status parsing of
``ScalingType`` / ``FrameGeneration`` / ``LSFG2Mode`` / ``LSFG3Mode1`` is a
pure function so it works identically on host and in tests.
"""

from __future__ import annotations

import os
import re
import shutil
from typing import Any

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
