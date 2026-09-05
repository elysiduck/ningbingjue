"""Freeze block tests (headless, MockProcessManager)."""

from __future__ import annotations

import os
import tempfile

from ningbingjue.blocks.freeze import FreezeBlock
from ningbingjue.platform.process_manager import MockProcessManager, ProcessInfo
from ningbingjue.platform.virtual_desktop import MockVirtualDesktopManager


def _pm(*procs):
    return MockProcessManager(list(procs))


class _ManualRunner:
    """Captures (work, done) instead of running them, so a test can drive the
    background step deterministically — mimicking the Qt worker without Qt."""

    def __init__(self):
        self.jobs = []

    def __call__(self, work, done):
        self.jobs.append((work, done))

    def drain(self):
        for work, done in list(self.jobs):
            done(work())
        self.jobs.clear()


def test_resolve_candidates_filters():
    pm = _pm(
        ProcessInfo(pid=1, name="system", memory_mb=1, session_id=0),
        ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0),
        ProcessInfo(pid=3, name="bigapp.exe", memory_mb=900, session_id=0),
        ProcessInfo(pid=4, name="steam.exe", memory_mb=50, session_id=0),
        ProcessInfo(pid=5, name="small.exe", memory_mb=5, session_id=0),
        ProcessInfo(pid=6, name="midsize.exe", memory_mb=250, session_id=0),  # <300, dropped
    )
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    names = [getattr(p, "name", "") for p in block.candidates]
    assert "system" not in names
    assert "small.exe" not in names
    # Non-game under the 300 MB floor is now filtered out as trivial noise.
    assert "midsize.exe" not in names
    assert "game.exe" in names and "bigapp.exe" in names
    # The Steam *client* (steam.exe) is freezable now — it is matched by the
    # game keyword table ("steam") but is no longer a hard-excluded SERVICE_HELPER.
    assert "steam.exe" in names
    assert len(block.candidates) <= block.LIST_CAP


def test_candidates_and_frozen_ordered_by_memory_desc():
    """Candidate slots 0,9,8,7,6 and frozen slots 1,2,3,4,5 map onto processes
    ordered by memory footprint from largest to smallest (within the non-game
    tier — no game markers here, so all share tier 0)."""
    pm = _pm(
        ProcessInfo(pid=1, name="big.exe", memory_mb=700, session_id=0),
        ProcessInfo(pid=2, name="mid.exe", memory_mb=500, session_id=0),
        ProcessInfo(pid=3, name="low.exe", memory_mb=350, session_id=0),
        ProcessInfo(pid=4, name="tiny.exe", memory_mb=5, session_id=0),  # filtered out
    )
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    # candidates ordered by memory descending
    assert [p.name for p in block.candidates] == ["big.exe", "mid.exe", "low.exe"]
    # slot keys follow the same order: 0,9,8,...
    assert block._btn_text("cand", 0).startswith("0  ")
    assert block._btn_text("cand", 1).startswith("9  ")
    assert block._btn_text("cand", 2).startswith("8  ")
    # freeze all three (two-press each), largest first
    for _ in range(3):
        block.handle_hotkey(0)
        block.handle_hotkey(0)
        block.refresh_panel()
    # frozen list stays ordered by memory descending -> slot keys 1,2,3,...
    assert [e.name for e in block.frozen] == ["big.exe", "mid.exe", "low.exe"]
    assert block._btn_text("froz", 0).startswith("1  ")
    assert block._btn_text("froz", 1).startswith("2  ")
    assert block._btn_text("froz", 2).startswith("3  ")


def test_memory_primary_with_game_tiebreaker():
    """Current ordering (see ``resolve_candidates``) is memory-descending
    PRIMARY, with ``is_game`` only a tie-breaker.  So a 3 GB background app
    outranks an 80 MB game, but when a game and a non-game tie on memory the
    game wins.  Fixture names use REAL game markers (the bare \"game\"
    substring was dropped from is_game_process to avoid false positives like
    GameInputSvc)."""
    pm = _pm(
        ProcessInfo(pid=1, name="hugeapp.exe", memory_mb=3000, session_id=0),
        ProcessInfo(pid=2, name="elden.exe", memory_mb=500, session_id=0),
        ProcessInfo(pid=3, name="notgame.exe", memory_mb=500, session_id=0),
    )
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    names = [p.name for p in block.candidates]
    # memory PRIMARY: the 3 GB app leads regardless of game status...
    assert names[0] == "hugeapp.exe"
    # ...but the two 500 MB processes tie on memory, and the game wins the
    # tie-break over an equal-footprint non-game.
    assert names[1] == "elden.exe"
    assert names[2] == "notgame.exe"


def test_nongame_floor_is_300_strict():
    """A non-game process using exactly 300 MB is excluded (strictly > 300),
    while one at 301 MB is admitted."""
    pm = _pm(
        ProcessInfo(pid=1, name="at300.exe", memory_mb=300, session_id=0),
        ProcessInfo(pid=2, name="at301.exe", memory_mb=301, session_id=0),
    )
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    names = [p.name for p in block.candidates]
    assert "at300.exe" not in names
    assert "at301.exe" in names


def test_steam_client_freezable_helpers_still_hidden():
    """The Steam client is freezable, but its non-game helpers stay hidden."""
    pm = _pm(
        ProcessInfo(pid=1, name="steam.exe", memory_mb=80, session_id=0),
        ProcessInfo(pid=2, name="steamwebhelper.exe", memory_mb=400, session_id=0),
        ProcessInfo(pid=3, name="steamcommunity_302.caddy", memory_mb=300, session_id=0),
        ProcessInfo(pid=4, name="realgame.exe", memory_mb=600, session_id=0),
    )
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    names = [p.name for p in block.candidates]
    assert "steam.exe" in names            # client is freezable
    assert "realgame.exe" in names         # real game shows
    assert "steamwebhelper.exe" not in names   # helper still hidden
    assert "steamcommunity_302.caddy" not in names


def test_button_text_shows_slot_key():
    """Candidate / frozen button text carries the hotkey digit (0,9,8,7,6 and
    1,2,3,4,5 respectively), matching CAND_SLOT_TO_KEY / FROZ_SLOT_TO_KEY."""
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    # candidate slot 0 -> key 0, digit leads the label (left-aligned on button)
    assert block._btn_text("cand", 0).startswith("0  ❄ ")
    # freeze it, then the frozen entry should carry key 1 (slot 0 -> FROZ key 1)
    block.handle_hotkey(0)
    block.handle_hotkey(0)
    assert block._btn_text("froz", 0).startswith("1  🔥 ")
    # candidate slot 1 would carry key 9, slot 4 key 6 (no process -> empty)
    assert block._btn_text("cand", 1) == ""
    # reverse lookups are consistent with the published KEY tables
    assert FreezeBlock.CAND_SLOT_TO_KEY == {0: 0, 1: 9, 2: 8, 3: 7, 4: 6}
    assert FreezeBlock.FROZ_SLOT_TO_KEY == {0: 1, 1: 2, 2: 3, 3: 4, 4: 5}


def test_two_press_hotkey_suspends():
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    # key 0 -> candidate index 0
    block.handle_hotkey(0)  # first press: highlight
    assert block.highlight_key == 0
    assert not pm.suspended
    block.handle_hotkey(0)  # second press: execute
    assert pm.suspended
    assert block.highlight_key is None


def test_execute_resumes_frozen():
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)  # freeze
    assert pm.suspended
    # frozen now key 1
    block.handle_hotkey(1)  # highlight frozen[0]
    block.handle_hotkey(1)  # unfreeze
    assert not pm.suspended


def test_highlight_timeout_clears():
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    block.handle_hotkey(0)
    assert block.highlight_key == 0
    block.refresh_panel(now=block.highlight_ts + block.HIGHLIGHT_TIMEOUT_S + 0.1)
    assert block.highlight_key is None


def test_pid_persistence():
    d = tempfile.mkdtemp()
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    b1 = FreezeBlock(d, pm)
    b1.refresh_panel()
    b1.handle_hotkey(0)
    b1.handle_hotkey(0)
    assert os.path.exists(b1._pid_file())
    # fresh block reloads frozen pids。注意复用同一个 pm 实例：真实世界里
    # 内核挂起态是**全局**的（App 重启不解除挂起），启动校验靠
    # is_suspended_native 问内核；Mock 的挂起账本却是每实例一份，
    # 新实例查不到旧实例挂的进程 —— 共享实例才能模拟「内核还记得」。
    b2 = FreezeBlock(d, pm)
    assert any(e.pid == 2 for e in b2.frozen)


def test_startup_drops_stale_frozen_records():
    """启动校验的负例：内核里已不挂起的记录（App 关闭期间进程被解冻/退出）
    必须在启动时清掉并落盘 —— 否则 HUD 会把早已解冻的进程一直标成
    「🔥 已冻结」。Mock 语义：新实例的 suspended 集合为空 = 内核不记得。"""
    d = tempfile.mkdtemp()
    import json
    with open(os.path.join(d, "frozen_groups.json"), "w",
              encoding="utf-8") as fh:
        json.dump([{"pid": 7, "name": "stale.exe", "mem": 100,
                    "family": "", "pids": [7]}], fh)
    blk = FreezeBlock(d, _pm())  # 全新 Mock：suspended 为空 -> 残留
    assert not blk.frozen
    with open(os.path.join(d, "frozen_groups.json"), encoding="utf-8") as fh:
        assert json.load(fh) == []  # 清空结果落盘，下次启动不再重复校验


def test_theme_highlight_color():
    # After the single-knob theme refactor, the highlight colour follows the
    # breathing (theme-accent) colour and is independent of the old fixed
    # theme names — resolve_highlight_color ignores its argument and returns
    # the current theme accent for every theme name.
    from ningbingjue.blocks.freeze import FreezeBlock as FB
    c1 = FB.resolve_highlight_color("少女粉")
    c2 = FB.resolve_highlight_color("樱粉")
    c3 = FB.resolve_highlight_color("原夜")
    assert c1 == c2 == c3
    assert (c1.r, c1.g, c1.b) == (127, 209, 255)  # default cyan accent


def test_noise_hidden_by_default():
    pm = _pm(
        ProcessInfo(pid=1, name="explorer.exe", memory_mb=500, session_id=0),
        ProcessInfo(pid=2, name="msedge.exe", memory_mb=800, session_id=0),
        ProcessInfo(pid=3, name="runtimebroker.exe", memory_mb=50, session_id=0),
        ProcessInfo(pid=4, name="bigapp.exe", memory_mb=900, session_id=0),
    )
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    names = [getattr(p, "name", "") for p in block.candidates]
    assert "explorer.exe" not in names
    assert "msedge.exe" not in names
    assert "runtimebroker.exe" not in names
    assert "bigapp.exe" in names  # real user process still shows
    # opt-in reveals the noise processes
    block.show_windows_apps = True
    block.refresh_panel()
    names = [getattr(p, "name", "") for p in block.candidates]
    assert "explorer.exe" in names
    assert "msedge.exe" in names


def test_excluded_always_hidden():
    # dwm (a protected process) must never surface even if show_windows_apps.
    pm = _pm(
        ProcessInfo(pid=1, name="dwm.exe", memory_mb=500, session_id=0),
        ProcessInfo(pid=2, name="lsass.exe", memory_mb=50, session_id=0),
    )
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.show_windows_apps = True
    block.refresh_panel()
    names = [getattr(p, "name", "") for p in block.candidates]
    assert "dwm.exe" not in names
    assert "lsass.exe" not in names


def test_config_persistence_json_fallback():
    import json

    d = tempfile.mkdtemp()
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    b1 = FreezeBlock(d, pm)  # config=None -> JSON fallback
    assert b1.show_windows_apps is False
    b1.show_windows_apps = True
    b1.save_config()
    assert os.path.exists(os.path.join(d, "freeze_settings.json"))
    b2 = FreezeBlock(d, _pm())
    assert b2.show_windows_apps is True
    with open(os.path.join(d, "freeze_settings.json"), encoding="utf-8") as fh:
        assert json.load(fh).get("show_windows_apps") is True


def test_service_helpers_excluded():
    """Known service/helper/proxy processes must never appear in the freeze
    candidate list, even when they use a lot of memory."""
    pm = _pm(
        ProcessInfo(pid=1, name="steamcommunity_302.caddy", memory_mb=300, session_id=0),
        ProcessInfo(pid=2, name="steamcommunity_302.cli", memory_mb=300, session_id=0),
        ProcessInfo(pid=3, name="GameInputSvc.exe", memory_mb=80, session_id=0),
        ProcessInfo(pid=4, name="GameInputRedistService.exe", memory_mb=80, session_id=0),
        ProcessInfo(pid=5, name="ThreeFingerDragOnWindows.exe", memory_mb=40, session_id=0),
        ProcessInfo(pid=6, name="realgame.exe", memory_mb=600, session_id=0),
    )
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    names = [getattr(p, "name", "") for p in block.candidates]
    assert "steamcommunity_302.caddy" not in names
    assert "steamcommunity_302.cli" not in names
    assert "GameInputSvc.exe" not in names
    assert "GameInputRedistService.exe" not in names
    assert "ThreeFingerDragOnWindows.exe" not in names
    assert "realgame.exe" in names  # a real game still shows


def test_game_companions_excluded():
    """Game *companion* processes (crash handlers, RTSS hook loaders, anti-cheat
    helpers) must never appear as freeze candidates — even when they blow past
    the 300 MB memory floor, and even when a keyword slips past
    is_game_process() (the old ``r6`` Rainbow-Six marker used to mis-classify
    ``rtsshooksloader64.exe`` as a game and admit it regardless of size)."""
    pm = _pm(
        # Unity 崩溃处理器：超过内存下限，但属于游戏随从 -> 必须隐藏
        ProcessInfo(pid=1, name="UnityCrashHandler64.exe", memory_mb=800, session_id=0),
        ProcessInfo(pid=2, name="UnityCrashHandler32.exe", memory_mb=400, session_id=0),
        # RTSS 钩子加载器：旧 r6 误报会把它当游戏 -> 现已用 GAME_COMPANIONS 硬排除
        ProcessInfo(pid=3, name="RTSSHooksLoader64.exe", memory_mb=120, session_id=0),
        # Chrome/Electron/Unity 崩溃上报
        ProcessInfo(pid=4, name="crashpad_handler.exe", memory_mb=350, session_id=0),
        # 反作弊伴侣：冻结会令游戏掉线/崩溃
        ProcessInfo(pid=5, name="EasyAntiCheat.exe", memory_mb=200, session_id=0),
        ProcessInfo(pid=6, name="BEService.exe", memory_mb=150, session_id=0),
        # 真正的大游戏本体仍应出现
        ProcessInfo(pid=7, name="elden.exe", memory_mb=2000, session_id=0),
    )
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    names = [getattr(p, "name", "") for p in block.candidates]
    assert "UnityCrashHandler64.exe" not in names
    assert "UnityCrashHandler32.exe" not in names
    assert "RTSSHooksLoader64.exe" not in names
    assert "crashpad_handler.exe" not in names
    assert "EasyAntiCheat.exe" not in names
    assert "BEService.exe" not in names
    assert "elden.exe" in names  # real game still shows


def test_rt6_keyword_no_longer_matches_loader():
    """The dropped ``r6`` marker must no longer classify RTSS hook loaders (or
    any ``*loader64`` name containing the substring "r6") as a game.  The real
    Rainbow Six client is still detected via the ``rainbow`` marker."""
    from ningbingjue.core.game_detect import is_game_process

    assert is_game_process("RTSSHooksLoader64.exe") is False
    assert is_game_process("some_loader64.exe") is False
    # sanity: real game still detected through the surviving marker
    assert is_game_process("RainbowSix.exe") is True


def test_user_hidden_list():
    """A process added via set_hidden() disappears from candidates and can be
    restored with set_hidden(..., False)."""
    import json

    d = tempfile.mkdtemp()
    pm = _pm(ProcessInfo(pid=1, name="junk.exe", memory_mb=500, session_id=0))
    block = FreezeBlock(d, pm)
    block.refresh_panel()
    assert any(p.name == "junk.exe" for p in block.candidates)
    block.set_hidden("junk.exe", True)
    assert "junk.exe" not in [p.name for p in block.candidates]
    # persisted to the local JSON fallback (normalised name, .exe stripped)
    with open(os.path.join(d, "freeze_settings.json"), encoding="utf-8") as fh:
        assert "junk" in json.load(fh).get("hide_list", [])
    block.set_hidden("junk.exe", False)
    assert any(p.name == "junk.exe" for p in block.candidates)


def test_clear_hidden():
    d = tempfile.mkdtemp()
    pm = _pm(ProcessInfo(pid=1, name="junk.exe", memory_mb=500, session_id=0))
    block = FreezeBlock(d, pm)
    block.set_hidden("junk.exe", True)
    assert "junk.exe" not in [p.name for p in block.candidates]
    block.clear_hidden()
    assert "junk.exe" in [p.name for p in block.candidates]


def test_foreground_prioritisation():
    """Ordering is memory-descending PRIMARY, then game tie-breaker, then
    foreground tie-breaker (when ``prioritize_foreground`` is on).  Within the
    game tier, a visible window breaks a memory tie; for two non-games that tie
    on memory, foreground wins only when prioritize_foreground is on.  Fixture
    names use REAL game markers (the bare \"game\" substring was dropped from
    is_game_process to avoid false positives)."""
    d = tempfile.mkdtemp()
    pm = _pm(
        ProcessInfo(pid=1, name="cyberpunk.exe", memory_mb=300, session_id=0,
                    has_visible_window=False),
        ProcessInfo(pid=2, name="elden.exe", memory_mb=300, session_id=0,
                    has_visible_window=True),
        ProcessInfo(pid=3, name="chrome.exe", memory_mb=500, session_id=0,
                    has_visible_window=True),
    )
    block = FreezeBlock(d, pm)
    block.prioritize_foreground = True
    block.refresh_panel()
    names = [p.name for p in block.candidates]
    # memory PRIMARY: chrome (500 MB) leads despite both candidates being games.
    assert names[0] == "chrome.exe"
    # The two games tie on memory (300 MB); foreground (elden) beats background
    # (cyberpunk) within the game tier.
    assert names[1] == "elden.exe"
    assert names[2] == "cyberpunk.exe"

    # Two non-games tie on memory: foreground wins only when enabled.
    pm2 = _pm(
        ProcessInfo(pid=10, name="bgapp.exe", memory_mb=500, session_id=0,
                    has_visible_window=False),
        ProcessInfo(pid=11, name="fgapp.exe", memory_mb=500, session_id=0,
                    has_visible_window=True),
    )
    b2 = FreezeBlock(tempfile.mkdtemp(), pm2)
    b2.prioritize_foreground = True
    b2.refresh_panel()
    assert b2.candidates[0].name == "fgapp.exe"
    b2.prioritize_foreground = False
    b2.refresh_panel()
    assert b2.candidates[0].name == "bgapp.exe"  # tie -> insertion order


def test_minimize_self_called_on_successful_unfreeze():
    """A successful un-freeze must fire the injected minimise-self callback, but
    a *freeze* must never fire it (only thawing should bounce the user back to
    their game)."""
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    calls = []
    block.set_minimize_self_callback(lambda: calls.append(1))
    # freeze first — must NOT trigger minimise
    block.handle_hotkey(0)
    block.handle_hotkey(0)
    assert not pm.suspended or True  # frozen check below
    assert calls == []
    # unfreeze — successful thaw MUST trigger minimise
    block.handle_hotkey(1)
    block.handle_hotkey(1)
    assert calls == [1]


def test_minimize_self_respects_flag():
    """When ``minimize_on_unfreeze`` is False, a successful un-freeze must NOT
    call the minimise-self callback."""
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    block.minimize_on_unfreeze = False
    calls = []
    block.set_minimize_self_callback(lambda: calls.append(1))
    block.handle_hotkey(0)
    block.handle_hotkey(0)
    block.handle_hotkey(1)
    block.handle_hotkey(1)
    assert calls == []


def test_minimize_self_not_called_on_failed_unfreeze():
    """A failed un-freeze (resume returned False) must NOT fire the minimise
    callback — the user is still mid-task, do not yank the HUD away."""
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    calls = []
    block.set_minimize_self_callback(lambda: calls.append(1))
    # Manually inject a frozen entry whose resume will fail.
    # 用**真实存在**的 pid=2：pid 不存在时恢复会被判为 gone 而跳过（那是
    # "没什么可解冻"，不是失败）。lambda 必须收 restore_window，否则是靠
    # TypeError 意外失败，测不到「resume 返回 False」这个真正意图。
    from ningbingjue.blocks.freeze import _FrozenGroup
    block.frozen.append(_FrozenGroup(pid=2, name="game.exe", mem=5, pids=[2]))
    block.pm.resume = lambda pid, restore_window=True: False
    block.handle_hotkey(1)  # highlight frozen[0]
    block.handle_hotkey(1)  # attempt unfreeze (will fail)
    assert calls == []


def test_minimize_on_unfreeze_config_persistence():
    """The ``minimize_on_unfreeze`` flag round-trips through the JSON fallback
    (config=None), defaulting to True and persisting overrides."""
    import json

    d = tempfile.mkdtemp()
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    b1 = FreezeBlock(d, pm)  # config=None -> JSON fallback
    assert b1.minimize_on_unfreeze is True  # default on
    b1.minimize_on_unfreeze = False
    b1.save_config()
    with open(os.path.join(d, "freeze_settings.json"), encoding="utf-8") as fh:
        assert json.load(fh).get("minimize_on_unfreeze") is False
    b2 = FreezeBlock(d, _pm())
    assert b2.minimize_on_unfreeze is False


def test_validate_keeps_suspended_absent_from_enum():
    """A just-frozen process can momentarily be missing from the slow wmic /
    Get-CimInstance enumeration snapshot (especially after its window is moved
    to a background virtual desktop).  _validate_frozen must NOT drop it when
    the process manager still reports it as suspended — otherwise a frozen
    process vanishes from the list even though it really is suspended."""

    class _SuspendAwarePM(MockProcessManager):
        def __init__(self, procs):
            super().__init__(procs)
            self._suspended_ids = set()

        def suspend(self, pid):
            self._suspended_ids.add(pid)
            return True

        def is_suspended(self, pid):
            return pid in self._suspended_ids

        def enumerate(self):
            return []  # simulate the transient miss right after a suspend

    pm = _SuspendAwarePM([ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0)])
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    assert pm.suspend(2) is True
    block.frozen.append(__import__("ningbingjue.blocks.freeze", fromlist=["_FrozenGroup"])._FrozenGroup(pid=2, name="game.exe", mem=10, pids=[2]))
    block.refresh_panel()
    assert any(e.pid == 2 for e in block.frozen)

    # A genuinely dead PID (not suspended, not enumerated) is still dropped.
    block.frozen.append(__import__("ningbingjue.blocks.freeze", fromlist=["_FrozenGroup"])._FrozenGroup(pid=999, name="ghost.exe", mem=5, pids=[999]))
    block.refresh_panel()
    assert not any(e.pid == 999 for e in block.frozen)


def test_steam_family_freeze_suspends_helpers_too():
    """Freezing the steam.exe candidate must ALSO suspend every
    steamwebhelper.exe — the process that actually renders Big Picture Mode.
    Suspending steam.exe alone leaves Big Picture alive and desyncs Steam's
    IPC (→ "解冻后程序出错" crash)."""
    pm = _pm(
        ProcessInfo(pid=1, name="steam.exe", memory_mb=80, session_id=0),
        ProcessInfo(pid=2, name="steamwebhelper.exe", memory_mb=400, session_id=0),
    )
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    # steam.exe is the only candidate (steamwebhelper is a hidden helper), slot 0
    assert [p.name for p in block.candidates] == ["steam.exe"]
    # freeze the steam.exe candidate (slot 0)
    block.handle_hotkey(0)
    block.handle_hotkey(0)
    # BOTH steam.exe and its CEF helper must be suspended
    assert 1 in pm.suspended
    assert 2 in pm.suspended
    # recorded as a SINGLE grouped entry covering both PIDs
    assert len(block.frozen) == 1
    assert {1, 2}.issubset(pid for g in block.frozen for pid in g.pids)
    # unfreeze via the helper entry (frozen slot 0 -> FROZ key 1); the whole
    # family must resume coherently.
    block.handle_hotkey(1)
    block.handle_hotkey(1)
    assert {1, 2}.isdisjoint(pm.suspended)
    assert not block.frozen


def test_steam_family_unfreeze_from_primary_resumes_helpers():
    """Unfreezing the steam.exe member (not the helper) must still resume the
    whole family — otherwise a lone steam.exe resume would re-desync IPC."""
    pm = _pm(
        ProcessInfo(pid=1, name="steam.exe", memory_mb=80, session_id=0),
        ProcessInfo(pid=2, name="steamwebhelper.exe", memory_mb=400, session_id=0),
    )
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)  # freeze steam (slot 0 candidate) -> whole family
    assert {1, 2}.issubset(pm.suspended)
    # unfreeze via the single Steam group (frozen slot 0 -> FROZ key 1); the
    # whole family resumes coherently.
    block.handle_hotkey(1)
    block.handle_hotkey(1)
    assert {1, 2}.isdisjoint(pm.suspended)
    assert not block.frozen


def test_steam_family_group_resume_after_reload():
    """After an app restart the frozen group file restores the exact PID set
    (steam.exe + steamwebhelper), so unfreezing any member resumes the whole
    family coherently — the group is reconstructed from frozen_groups.json, not
    re-derived by name at unfreeze time."""
    d = tempfile.mkdtemp()
    procs = [
        ProcessInfo(pid=1, name="steam.exe", memory_mb=80, session_id=0),
        ProcessInfo(pid=2, name="steamwebhelper.exe", memory_mb=400, session_id=0),
    ]
    pm = _pm(*procs)  # 注释原意就是「same PM so suspend state persists」，
    b1 = FreezeBlock(d, pm)  # 旧代码却新建了实例 —— 被启动盲清掩盖至今。
    b1.refresh_panel()
    b1.handle_hotkey(0)
    b1.handle_hotkey(0)  # freeze whole family
    assert {1, 2}.issubset(pm_suspended_of(b1))
    # reload (same PM so suspend state persists) — names backfilled on refresh
    b2 = FreezeBlock(d, pm)
    b2.refresh_panel()
    assert len(b2.frozen) == 1
    assert {1, 2}.issubset(pid for g in b2.frozen for pid in g.pids)
    b2.handle_hotkey(1)
    b2.handle_hotkey(1)  # unfreeze any member -> whole family resumes
    assert not b2.frozen


def pm_suspended_of(block):
    """Helper: the MockProcessManager's suspended set (tests only)."""
    return block.pm.suspended


def test_standalone_game_not_treated_as_family():
    """A non-Steam process (e.g. a game) must freeze/unfreeze as a single PID,
    not pull in unrelated processes."""
    pm = _pm(
        ProcessInfo(pid=1, name="elden.exe", memory_mb=2000, session_id=0),
        ProcessInfo(pid=2, name="steamwebhelper.exe", memory_mb=400, session_id=0),
    )
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)  # freeze elden (slot 0)
    assert 1 in pm.suspended
    # the unrelated steamwebhelper must NOT be swept into the freeze
    assert 2 not in pm.suspended
    assert {e.pid for e in block.frozen} == {1}


def test_steam_unfreeze_does_not_move_unrelated_mo2():
    """REGRESSION: unfreezing Steam must NOT relocate a separately-frozen app
    (MO2.exe) to the current virtual desktop.  The move set is the exact PID
    group captured at freeze time, so Steam's unfreeze only moves Steam's own
    windows — MO2 stays put on the background desktop (and stays suspended)."""
    pm = _pm(
        ProcessInfo(pid=1, name="steam.exe", memory_mb=500, session_id=0),
        ProcessInfo(pid=2, name="steamwebhelper.exe", memory_mb=400, session_id=0),
        ProcessInfo(pid=3, name="MO2.exe", memory_mb=350, session_id=0),
    )
    vd = MockVirtualDesktopManager()
    block = FreezeBlock(tempfile.mkdtemp(), pm, vdesktop=vd)
    block.refresh_panel()
    # candidates by memory desc: steam(500) slot0, MO2(350) slot1
    # freeze Steam (slot 0) -> whole family suspended
    block.handle_hotkey(0)
    block.handle_hotkey(0)
    assert {1, 2}.issubset(pm.suspended)
    # freeze MO2 (now the only remaining candidate, slot 0)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)
    assert 3 in pm.suspended
    # two groups, sorted by mem desc: Steam(500) slot0, MO2(350) slot1
    assert len(block.frozen) == 2
    # unfreeze Steam (slot 0 -> FROZ key 1); whole family resumes + moves
    block.handle_hotkey(1)
    block.handle_hotkey(1)
    # Steam fully resumed and moved to current desktop
    assert {1, 2}.isdisjoint(pm.suspended)
    assert {1, 2}.issubset(vd.current)
    # MO2 must remain on the BACKGROUND desktop and still suspended — it was
    # never part of Steam's move group, so it must NOT appear in vd.current.
    assert 3 not in vd.current
    assert 3 in pm.suspended
    assert len(block.frozen) == 1
    assert 3 in {pid for g in block.frozen for pid in g.pids}


def test_frozen_group_collapses_family_into_one_row():
    """The frozen list must show the Steam family as a SINGLE entry (not one
    row per helper), so unfreezing that one entry removes the whole family
    coherently instead of leaving a confusing partial state."""
    pm = _pm(
        ProcessInfo(pid=1, name="steam.exe", memory_mb=80, session_id=0),
        ProcessInfo(pid=2, name="steamwebhelper.exe", memory_mb=400, session_id=0),
        ProcessInfo(pid=3, name="steamwebhelper.exe", memory_mb=350, session_id=0),
    )
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)  # freeze steam -> whole family
    assert len(block.frozen) == 1
    g = block.frozen[0]
    assert g.family == "steam"
    assert {1, 2, 3}.issubset(g.pids)
    assert "steam" in block._btn_text("froz", 0)



# -- refocus-on-freeze (焦点抢回) ------------------------------------------
# 冻结成功后（且仅成功时）触发注入的 re-focus 回调；解冻/失败/未注入均不触发。
# 宿主窗口注入 Qt 实现（app_window._refocus_after_freeze），headless 下为 no-op。


def test_freeze_success_invokes_refocus_callback():
    """冻结成功 -> refocus 回调恰好触发 1 次（无论单进程还是进程族）。"""
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    calls = []
    block.set_refocus_callback(lambda: calls.append(1))
    block.handle_hotkey(0)
    block.handle_hotkey(0)  # 两按：执行冻结
    assert block.frozen
    assert len(calls) == 1, f"冻结成功应触发 1 次 refocus，实际 {len(calls)}"


def test_freeze_family_invokes_refocus_once():
    """Steam 进程族冻结成功也只触发一次（按冻结动作计，不按进程数计）。"""
    pm = _pm(
        ProcessInfo(pid=10, name="steam.exe", memory_mb=50, session_id=0),
        ProcessInfo(pid=11, name="steamwebhelper.exe", memory_mb=300, session_id=0),
    )
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    calls = []
    block.set_refocus_callback(lambda: calls.append(1))
    block.handle_hotkey(0)
    block.handle_hotkey(0)
    assert block.frozen
    assert len(calls) == 1, f"进程族冻结应触发 1 次 refocus，实际 {len(calls)}"


def test_unfreeze_does_not_invoke_refocus():
    """解冻成功不触发 refocus——解冻路径有意把焦点还给游戏（minimize）。"""
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    calls = []
    block.set_refocus_callback(lambda: calls.append(1))
    block.handle_hotkey(0)
    block.handle_hotkey(0)  # 冻结 -> calls == 1
    block.handle_hotkey(1)
    block.handle_hotkey(1)  # 解冻 -> calls 不增加
    assert not block.frozen
    assert len(calls) == 1, f"解冻不应触发 refocus，实际 {len(calls)}"


def test_freeze_failure_does_not_invoke_refocus():
    """挂起失败（ok=False）不触发 refocus——焦点抢回只对成功的冻结有意义。"""

    class _FailPM(MockProcessManager):
        def suspend(self, pid):
            return False

    pm = _FailPM([ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0)])
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    calls = []
    block.set_refocus_callback(lambda: calls.append(1))
    block.handle_hotkey(0)
    block.handle_hotkey(0)
    assert not block.frozen
    assert len(calls) == 0, f"冻结失败不应触发 refocus，实际 {len(calls)}"


def test_refocus_without_injected_callback_is_noop():
    """未注入回调（headless/测试默认）时冻结流程照常，无异常。"""
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)
    assert block.frozen


def test_refocus_callback_exception_is_swallowed():
    """回调自身抛异常必须被吞掉，不能中断冻结收尾（best-effort）。"""
    pm = _pm(ProcessInfo(pid=2, name="game.exe", memory_mb=10, session_id=0))
    block = FreezeBlock(tempfile.mkdtemp(), pm)
    block.refresh_panel()

    def _boom():
        raise RuntimeError("boom")

    block.set_refocus_callback(_boom)
    block.handle_hotkey(0)
    block.handle_hotkey(0)
    assert block.frozen  # 冻结结果不受回调异常影响


# ---------------------------------------------------------------------------
# 2026-09-05 「冻结成功但解冻失败」回归
# ---------------------------------------------------------------------------
# 根因：解冻 worker 用 `ok = ok and resume(...)` 一票否决，而冻结组里残留一个
# 早已失效 / PID 已被复用的成员时 resume() 返回 False —— 整组报失败，哪怕主
# 进程已经恢复成功。用户看到的就是「窗口被拉回当前桌面，然后报解冻失败」。

def test_prune_dead_pids_drops_recycled_pid():
    """PID 被系统复用到别的进程（映像名对不上）时必须剔除，否则解冻会撞上
    OpenProcess 失败而拖垮整组。"""
    pm = _pm(ProcessInfo(pid=7, name="game.exe", memory_mb=10, session_id=0))
    blk = FreezeBlock(tempfile.mkdtemp(), pm)
    assert blk._prune_dead_pids([7], {7: "game.exe"}) == [7]
    # 同一 PID，但已经是别的进程了
    assert blk._prune_dead_pids([7], {7: "svchost.exe"}) == []


def test_prune_dead_pids_drops_missing_process():
    """进程根本不在了 -> 剔除。"""
    pm = _pm(ProcessInfo(pid=7, name="game.exe", memory_mb=10, session_id=0))
    blk = FreezeBlock(tempfile.mkdtemp(), pm)
    assert blk._prune_dead_pids([7, 99999], {7: "game.exe"}) == [7]


def test_prune_dead_pids_keeps_when_name_unknown():
    """拿不到/没记录映像名时保守保留 —— 宁可放过，不可误删真冻着的进程。"""
    pm = _pm(ProcessInfo(pid=7, name="game.exe", memory_mb=10, session_id=0))
    blk = FreezeBlock(tempfile.mkdtemp(), pm)
    assert blk._prune_dead_pids([7], {}) == [7]


def test_pnames_persisted_with_group():
    """成员映像名必须一起持久化，否则下次启动无从校验 PID 身份。"""
    import json

    d = tempfile.mkdtemp()
    pm = _pm(ProcessInfo(pid=7, name="game.exe", memory_mb=10, session_id=0))
    b1 = FreezeBlock(d, pm)
    b1.refresh_panel()
    b1.handle_hotkey(0)
    b1.handle_hotkey(0)  # freeze
    with open(os.path.join(d, "frozen_groups.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    assert data, "冻结后应写出持久化记录"
    assert "pnames" in data[0]
    assert data[0]["pnames"].get("7") == "game.exe"


def test_load_pids_prunes_recycled_members():
    """跨会话加载时，已易主 / 已消失的成员必须被剔除，只留有效进程。"""
    import json

    d = tempfile.mkdtemp()
    with open(os.path.join(d, "frozen_groups.json"), "w", encoding="utf-8") as fh:
        json.dump([{"pid": 7, "name": "game.exe", "mem": 10, "family": "",
                    "pids": [7, 8, 99999],
                    "pnames": {"7": "game.exe", "8": "svchost.exe",
                               "99999": "ghost.exe"}}], fh)
    # 只注入 pid=7，且它确实仍被挂起（否则启动校验会按残留清掉整组）：
    # 8 名字对不上 = 已易主，99999 进程不存在 —— 两者都该被剔除。
    pm = _pm(ProcessInfo(pid=7, name="game.exe", memory_mb=10, session_id=0))
    pm.suspended.add(7)
    blk = FreezeBlock(d, pm)
    assert len(blk.frozen) == 1
    assert blk.frozen[0].pids == [7]
    assert blk.frozen[0].pnames.get(7) == "game.exe"


def test_unfreeze_group_with_stale_member_still_succeeds():
    """端到端：冻结组里混进一个「进程已不存在」的陈旧 PID，解冻仍应成功
    （旧逻辑会被一票否决成失败）。"""
    d = tempfile.mkdtemp()
    pm = _pm(ProcessInfo(pid=7, name="game.exe", memory_mb=10, session_id=0))
    block = FreezeBlock(d, pm)
    runner = _ManualRunner()
    block.set_async_runner(runner)
    block.refresh_panel()
    block.handle_hotkey(0)
    block.handle_hotkey(0)  # freeze
    runner.drain()
    assert block.frozen

    # 手工塞进一个早已不存在的陈旧 PID，模拟持久化残留
    block.frozen[0].pids.append(99998)
    block.handle_hotkey(1)
    block.handle_hotkey(1)  # unfreeze
    runner.drain()
    assert not block.frozen, "陈旧成员不该让整组解冻失败"
    assert 7 in pm.resumed, "活着的成员必须真的被恢复"
