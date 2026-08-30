"""LS block tests (headless, MockLosslessScaling)."""

from __future__ import annotations

from conftest import FakeLabel

from ningbingjue.blocks.ls import LSBlock
from ningbingjue.platform.lossless_scaling import MockLosslessScaling, parse_ls_status


def _ls(presets=None):
    ls = MockLosslessScaling()
    if presets:
        ls.presets = presets
    return ls


def test_refresh_loads_presets_and_status():
    ls = _ls({"高速": {}, "省电": {}})
    ls._status = {"ScalingType": "整数", "FrameGeneration": "关闭", "PerfMode": "2.0"}
    ls.set_running(True)
    ls.set_current_profile("高速")
    block = LSBlock(ls, ui={"status_label": FakeLabel()})
    assert block.presets == ["省电", "高速"]
    # LSBlock 不再跟踪/显示运行态：朴素启动，点击即打开 exe，状态行不含「运行中」。
    assert "运行中" not in block.ui.get("status_label").text
    assert block._profile == "高速"
    assert "缩放: 整数" in block.ui.get("status_label").text


def test_parse_status_off_and_values():
    off = parse_ls_status("<ScalingType>off</ScalingType><FrameGeneration>false</FrameGeneration><LSFG2Mode>none</LSFG2Mode>")
    assert off["ScalingType"] == "关闭"
    assert off["FrameGeneration"] == "关闭"
    on = parse_ls_status("<ScalingType>整数</ScalingType><FrameGeneration>开启</FrameGeneration><LSFG3Mode1>2.0</LSFG3Mode1>")
    assert on["ScalingType"] == "整数"
    assert on["FrameGeneration"] == "开启"
    assert on["PerfMode"] == "2.0"


def test_navigation_activates_item_mode():
    """LSBlock 进入 item 模式后焦点落在预设列表（导航已统一到 InteractionController）。"""
    ls = _ls({"高速": {}, "省电": {}, "极致": {}})
    block = LSBlock(ls)
    block.on_activate_item()
    assert block.in_item_mode is True
    assert block.focus_type == "list"
    assert block.focus_idx.value == 0


def test_item_confirm_loads_preset():
    ls = _ls({"高速": {"x": 1}})
    block = LSBlock(ls)
    block.on_activate_item()
    block.focus_idx.value = 0
    block._load(0)  # 当前 item 确认即加载对应预设
    assert block._profile == "高速"
    assert ls.current == {"x": 1}


def test_parse_wmic_execpath():
    """WMI/WMIC ExecutablePath CSV 解析（含 BOM / 空行 / 多行）。"""
    from ningbingjue.platform.process_manager import Win32ProcessManager
    raw = b"\xff\xfe" + "Node,ExecutablePath\r\r\nHOST,C:\\Games\\LS\\losslessscaling.exe\r\r\n".encode("utf-16-le")
    assert Win32ProcessManager._parse_wmic_execpath(raw) == r"C:\Games\LS\losslessscaling.exe"
    # 纯 UTF-8（部分 PowerShell CIM 输出）
    raw2 = b"Node,ExecutablePath\nPC,C:\\LS\\losslessscaling.exe\n"
    assert Win32ProcessManager._parse_wmic_execpath(raw2) == r"C:\LS\losslessscaling.exe"
    # 仅表头 → 空
    assert Win32ProcessManager._parse_wmic_execpath(b"Node,ExecutablePath\n") == ""


def test_find_exe_path_prefers_wmi(monkeypatch):
    """find_exe_path_by_name 优先采用 WMI 路径，不回退到 enumerate。"""
    from ningbingjue.platform.process_manager import Win32ProcessManager
    pm = Win32ProcessManager()
    called = {"wmi": 0, "enum": 0}

    def fake_wmi(self, name):  # noqa: ANN001
        called["wmi"] += 1
        return r"C:\LS\losslessscaling.exe"

    monkeypatch.setattr(Win32ProcessManager, "_find_exe_path_via_wmi", fake_wmi)
    monkeypatch.setattr(Win32ProcessManager, "enumerate",
                        lambda self: (_ for _ in ()).throw(AssertionError("enumerate 不应被调用")))
    assert pm.find_exe_path_by_name("losslessscaling.exe") == r"C:\LS\losslessscaling.exe"
    assert called["wmi"] == 1


def test_launch_always_opens_even_if_is_running_false_positive(tmp_path):
    """点击「运行LS」不再被 is_running 短路：即便误报运行中，也真正拉起 exe。"""
    from ningbingjue.platform.lossless_scaling import MockLosslessScaling
    exe = tmp_path / "losslessscaling.exe"
    exe.write_text("MZ")  # 伪 exe，仅验证路径有效
    ls = MockLosslessScaling()
    ls.set_exe_path(str(exe))
    ls.set_running(True)  # 模拟「is_running 误报 True」的旧 bug 场景
    ok, key = ls.launch()
    assert key == "launched"   # 关键：不再返回 focused 而不启动
    assert ok is True


def test_launch_nopath_without_exe(tmp_path):
    """无任何有效路径时返回 nopath，由调用方提示去设置。"""
    from ningbingjue.platform.lossless_scaling import MockLosslessScaling
    ls = MockLosslessScaling()
    ls.set_exe_path(str(tmp_path / "nope.exe"))  # 文件不存在
    ok, key = ls.launch()
    assert key == "nopath"
    assert ok is False


def test_detect_exe_path_offline_search(tmp_path, monkeypatch):
    """LS 未运行时，detect_exe_path 应离线搜索常见安装位置找到 exe。"""
    import os
    from ningbingjue.platform.lossless_scaling import LosslessScaling
    ls_dir = tmp_path / "steamapps" / "common" / "Lossless Scaling"
    ls_dir.mkdir(parents=True)
    exe = ls_dir / "losslessscaling.exe"
    exe.write_text("MZ")
    ls = LosslessScaling("settings.xml", str(tmp_path / "presets"))
    # 无运行进程可查
    monkeypatch.setattr(ls, "_get_pm", lambda: None)
    # 把 Steam 根目录指向临时目录，触发离线搜索命中
    monkeypatch.setattr(ls, "_steam_roots", lambda: [str(tmp_path)])
    found = ls.detect_exe_path()
    assert os.path.normcase(found) == os.path.normcase(str(exe))


def test_detect_exe_path_offline_empty_when_absent(tmp_path, monkeypatch):
    """LS 既没运行也无处安装时，离线搜索返回空串（不抛异常）。"""
    from ningbingjue.platform.lossless_scaling import LosslessScaling
    ls = LosslessScaling("settings.xml", str(tmp_path / "presets"))
    monkeypatch.setattr(ls, "_get_pm", lambda: None)
    monkeypatch.setattr(ls, "_steam_roots", lambda: [])
    assert ls.detect_exe_path() == ""


def test_exe_path_persists_in_appdata_not_presets_dir(tmp_path, monkeypatch):
    """修复：LS 路径必须存到源码树之外（AppData），拉取源码不会被冲掉。

    - set_exe_path 写入的位置是 AppData/ningbingjue/ls_config.json，而非
      presets 目录（presets 目录在同步/拉取范围内会被 robocopy /MIR 抹掉）。
    - 即便 presets 目录里的旧 ls_config.json 不存在，exe_path 仍应从 AppData 还原。
    - 旧位置残留有效路径时，应一次性迁移到新位置。
    """
    import os
    from ningbingjue.platform.lossless_scaling import LosslessScaling

    appdata = tmp_path / "appdata"
    monkeypatch.setattr(LosslessScaling, "_user_data_dir",
                        staticmethod(lambda: str(appdata)))

    presets = tmp_path / "presets"
    ls = LosslessScaling("settings.xml", str(presets))

    # 1) 写入：落点必须是 AppData，而非 presets 目录
    ls.set_exe_path(r"C:\LS\losslessscaling.exe")
    new_file = appdata / "ls_config.json"
    assert new_file.exists(), "路径应写入 AppData/ningbingjue/ls_config.json"
    assert not (presets / "ls_config.json").exists(), "不应再写进源码树内的 presets 目录"
    # 即使 presets 目录（旧位置）被「拉取源码」抹掉，也能从 AppData 还原
    assert ls.exe_path == r"C:\LS\losslessscaling.exe"

    # 2) 迁移：旧位置残留有效路径 -> 自动迁到新位置
    presets.mkdir(parents=True, exist_ok=True)
    (presets / "ls_config.json").write_text(
        '{"exe_path": "D:\\\\old\\\\losslessscaling.exe"}', encoding="utf-8")
    new_file.unlink()  # 清空新位置，模拟「全新拉取后只有旧位置有值」
    assert ls.exe_path == r"D:\old\losslessscaling.exe"
    assert (appdata / "ls_config.json").exists()


def test_launch_transient_detect_does_not_persist(tmp_path, monkeypatch):
    """已存路径失效时，launch() 本次临时探测并启动，且绝不写回 AppData。

    关键回归：修复「点击启动LS 没用」+「掌机路径被覆盖」——
    无效的已存路径不应再触发静默打开设置，也不应把临时探测到的路径持久化。"""
    from ningbingjue.platform.lossless_scaling import LosslessScaling
    called: dict[str, list[str]] = {"startfile": []}
    monkeypatch.setattr(
        "ningbingjue.platform.lossless_scaling.os.startfile",
        lambda p: called["startfile"].append(p))

    appdata = tmp_path / "appdata"
    monkeypatch.setattr(LosslessScaling, "_user_data_dir",
                        staticmethod(lambda: str(appdata)))
    ls = LosslessScaling("settings.xml", str(tmp_path / "presets"))
    # 已存路径为空（模拟从未设置 / 被清空），临时探测给一个假路径
    monkeypatch.setattr(ls, "detect_exe_path",
                        lambda: str(tmp_path / "losslessscaling.exe"))

    ok, key = ls.launch()
    assert key == "launched"
    assert called["startfile"] == [str(tmp_path / "losslessscaling.exe")]
    # 临时探测绝不写回：AppData 下不应生成 ls_config.json（不覆盖用户路径）
    assert not (appdata / "ls_config.json").exists()


class _FakeLsNoWrite:
    """最小桩：LSBlock 启动钩子测试用，记录 set/launch 调用。"""
    available = False  # 跳过 refresh 的预设扫描

    def __init__(self, exe_path: str = "") -> None:
        self._exe = exe_path
        self.set_calls = 0
        self.detect_calls = 0
        self.launch_calls = 0

    @property
    def exe_path(self) -> str:
        return self._exe

    def detect_exe_path(self) -> str:
        self.detect_calls += 1
        return r"C:\detected\losslessscaling.exe"

    def set_exe_path(self, p: str) -> None:
        self.set_calls += 1
        self._exe = p

    def launch(self):
        self.launch_calls += 1
        return True, "launched"


def test_startup_detect_keeps_user_path():
    """用户已手动设过路径（无论当前是否有效），startup_detect 绝不覆盖。"""
    fake = _FakeLsNoWrite(exe_path=r"D:\LS\losslessscaling.exe")
    LSBlock(fake).startup_detect()
    assert fake.set_calls == 0, "已设路径不应被自动探测覆盖"
    assert fake.detect_calls == 0


def test_startup_detect_bootstraps_when_empty():
    """路径从未设置（空）时，startup_detect 才扫描一次写回做引导。"""
    fake = _FakeLsNoWrite(exe_path="")
    LSBlock(fake).startup_detect()
    assert fake.set_calls == 1, "空路径应引导写回一次"
    assert fake._exe == r"C:\detected\losslessscaling.exe"


def test_load_preset_restarts_ls_when_running(tmp_path):
    """加载预设时若 LS 正在运行：先 close 再 launch（使新 settings.xml 生效）。"""
    from ningbingjue.platform.lossless_scaling import MockLosslessScaling
    exe = tmp_path / "losslessscaling.exe"
    exe.write_text("MZ")
    ls = MockLosslessScaling()
    ls.presets = {"高速": {"x": 1}, "省电": {}}
    ls.set_exe_path(str(exe))
    ls.set_running(True)  # LS 当前运行中
    block = LSBlock(ls, ui={"status_label": FakeLabel()})
    idx = block.presets.index("高速")  # list_presets 按字母序，高速非 index 0
    block.focus_idx.value = idx
    block._load(idx)  # 加载「高速」
    # 关键断言：运行中 → 关闭一次 + 重新拉起一次
    assert ls.close_calls == 1, "运行中应关闭 LS 一次"
    assert ls.launch_calls == 1, "关闭后应重新拉起 LS"
    assert block._profile == "高速"
    assert ls.current == {"x": 1}


def test_load_preset_no_restart_when_ls_not_running(tmp_path):
    """LS 未运行时加载预设：只写配置、不关闭也不拉起（留在原地等下次启动套用）。"""
    from ningbingjue.platform.lossless_scaling import MockLosslessScaling
    exe = tmp_path / "losslessscaling.exe"
    exe.write_text("MZ")
    ls = MockLosslessScaling()
    ls.presets = {"高速": {"x": 1}, "省电": {}}
    ls.set_exe_path(str(exe))
    ls.set_running(False)  # LS 未运行
    block = LSBlock(ls, ui={"status_label": FakeLabel()})
    idx = block.presets.index("高速")
    block.focus_idx.value = idx
    block._load(idx)
    assert ls.close_calls == 0, "未运行不应关闭 LS"
    assert ls.launch_calls == 0, "未运行不应拉起 LS（保持朴素，下次启动才套用）"
    assert block._profile == "高速"


def test_load_preset_running_but_path_lost_reports_fail(tmp_path):
    """运行中但 LS 路径已失效：关闭成功、重启因 nopath 失败，提示但不卡死。"""
    from ningbingjue.platform.lossless_scaling import MockLosslessScaling
    ls = MockLosslessScaling()
    ls.presets = {"高速": {"x": 1}}
    ls.set_exe_path(str(tmp_path / "gone.exe"))  # 文件不存在 → launch 返回 nopath
    ls.set_running(True)
    block = LSBlock(ls, ui={"status_label": FakeLabel()})
    block.focus_idx.value = 0
    block._load(0)
    assert ls.close_calls == 1, "运行中仍应先关闭"
    assert ls.launch_calls == 1, "仍尝试重启（即便会 nopath）"
    assert block._profile == "高速"  # 预设本身已加载



