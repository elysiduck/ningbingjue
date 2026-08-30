"""freeze block — suspend/resume processes (kernel-level freeze).

Faithful logic port of ``blocks/freeze.ps1``.  Core behavior:
* candidate list = top-5 processes by memory usage (descending; slot 0 is
  the single largest).  Games and non-games are both eligible; when two
  processes tie on memory a game wins.  Non-games must exceed 300 MB RAM
  (games any footprint), excluding
  self / already-frozen / a small ignore set / known service helpers /
  game companions (crash handlers, RTSS hook loaders, anti-cheat helpers) /
  user-hidden processes.  (The old ``"steam" in name`` wildcard was removed:
  it pulled in Steam helpers/proxies that are not games.)
* two-press activation: first press highlights (3 s timeout); a second
  press of the *same* key executes freeze/unfreeze.
* frozen PIDs persist to a text file (one PID per line).
* highlight color follows the theme (Update-FreezeHighlightColor).
"""

from __future__ import annotations

import fnmatch
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from ..core.color.palette import COLOR_BLUE, COLOR_PEACH
from ..core.color.rgb import Color
from ..core.color.semantic import get_theme_accent
from ..core.game_detect import is_game_process
from ..core.navigation import (
    FocusHolder,
    clear_block_focus,
    set_control_focus_visual,
)

from ..platform.process_manager import ProcessManager
from ..platform.virtual_desktop import VirtualDesktopManager
from ..platform.win_hwnd import related_pids
from .base import BlockBlueprint


@dataclass
class _FrozenGroup:
    pid: int = 0
    name: str = ""
    mem: int = 0
    # Family key (e.g. "steam") when this group was frozen as part of a process
    # family; "" for standalone processes.  Used so that unfreezing ANY member
    # of a family resumes the WHOLE family coherently (never a lone member).
    family: str = ""
    # ALL PIDs suspended together for this single freeze action (the Steam
    # client + every steamwebhelper, or a game's launch group).  This is the
    # authoritative set used for window relocation and resume — it is captured
    # ONCE at freeze time, so a later unfreeze moves/resumes EXACTLY these
    # processes and can never drag in an unrelated app (e.g. a separately-frozen
    # MO2) that merely shares a process-tree ancestor.
    pids: list[int] = field(default_factory=list)


class _Btn:
    def __init__(self) -> None:
        self.text = ""
        self._colors: tuple | None = None
        self.focused = False
        self.fore_color: Color | None = None
        self.border_color: Color | None = None
        self.border_thickness: int | None = None

    def set_colors(self, *args: Any) -> None:
        self._colors = tuple(args)

    def set_focus_visual(self, focused: bool) -> None:
        self.focused = focused

    def invalidate(self) -> None:
        pass


class FreezeBlock:
    MEM_THRESH_MB = 300.0
    LIST_CAP = 5
    HIGHLIGHT_TIMEOUT_S = 3.0
    SESSION_ID = 0
    # Hard-excluded: freezing any of these can crash Windows / the desktop (or
    # cut off the session).  NEVER shown, and NOT controlled by the
    # "show Windows apps" toggle below.
    EXCLUDED = (
        "system", "idle", "csrss", "wininit", "services", "lsass", "smss",
        "dwm", "lsaiso", "winlogon", "svchost",
    )
    # Windows "noise" processes: safe to freeze but they clutter the candidate
    # list.  Hidden by default; revealed only when ``show_windows_apps`` is True.
    NOISE = (
        "explorer", "msedge", "searchhost", "shellexperiencehost",
        "startmenuexperiencehost", "runtimebroker", "fontdrvhost", "audiodg",
        "searchindexer", "ctfmon", "dllhost", "phoneexperiencehost",
        "wmiprvse", "sihost", "taskhostw",
    )
    # Known service / helper / proxy processes that are NOT games and whose
    # presence in the freeze list is pure noise.  These are hard-excluded just
    # like EXCLUDED, but for a different reason: freezing them is pointless or
    # has side effects (e.g. GameInputSvc breaking controller input).  Matched
    # with fnmatch so entries may use ``*`` wildcards.  Kept deliberately small
    # — users extend it via the edit-panel exclusion list, not here.
    SERVICE_HELPERS = (
        # Steam's non-game helper processes (NOT the games, and NOT the
        # steam.exe client — the client may be frozen to reclaim RAM, so it is
        # deliberately NOT listed here).
        "steamwebhelper",
        "steamcommunity_302.caddy", "steamcommunity_302.cli",
        # Xbox / Windows input services
        "gameinputsvc", "gameinputredistservice",
        # Trackpad gesture helper (e.g. ThreeFingerDragOnWindows)
        "threefingerdragonwindows",
        # 32-bit print driver host (Windows spooler WOW64 helper). Not a game;
        # its name merely contains the substring "wow", which earlier tripped
        # is_game_process() and surfaced it as a ~24MB "game" candidate. Kept
        # here as a belt-and-suspenders guard even after the "wow" marker was
        # narrowed to "wow.exe".
        "splwow64",
    )
    # Game *companion* processes: spawned BY a game but are not the game itself
    # (crash handlers, RTSS hook loaders, anti-cheat helpers).  Freezing them is
    # pointless (they hold little of value) or actively harmful (freezing an
    # anti-cheat / overlay hook makes the game drop connection or crash).  They
    # must never appear as freeze candidates even when they balloon past the
    # 300 MB floor or a keyword slips past is_game_process().  Matched with
    # fnmatch so 32/64-bit and varied suffixes are all covered.  This is the
    # freeze-list-specific home for "not a game, never freeze it" — kept
    # deliberately small; users extend it via the edit-panel exclusion list.
    GAME_COMPANIONS = (
        # Unity 崩溃处理器（游戏崩溃时才弹出的随从，不是游戏本体）
        "unitycrashhandler*",
        "unitycrashreport*",
        # RivaTuner 注入钩子加载器（RTSS 给目标进程注入帧率显示的瞬时助手）
        "rtsshooksloader*",
        # Chrome / Electron / Unity 等广泛使用的崩溃上报进程
        "crashpad_handler*", "crashpad-handler*", "crashhandler*",
        # 反作弊伴侣进程：冻结它们会直接令游戏掉线 / 崩溃，绝非冻结目标
        "easyanticheat*", "battleye*", "beservice*", "eoswebhelper*",
    )
    # Process *families*: freezing one member must suspend the whole
    # multi-process app coherently.  Steam is the canonical case — its visible
    # UI (incl. Big Picture Mode / 大屏模式) is NOT rendered by ``steam.exe`` but
    # by ``steamwebhelper.exe`` (CEF).  Suspending ``steam.exe`` alone therefore
    # (a) leaves the Big Picture UI fully alive ("冻结了却还在") and (b) desyncs
    # Steam's internal IPC between the client and its helpers, which on resume
    # surfaces as a fatal "程序出错，需要重启" error.  Freezing the whole family
    # together fixes both: the UI (steamwebhelper) is actually suspended and all
    # Steam processes wake coherently on resume.
    PROCESS_FAMILIES = {
        "steam": ("steam.exe", "steamwebhelper.exe"),
    }
    # reverse map: member (normalised) name -> family key
    _FAMILY_OF: dict[str, str] = {}
    for _fk, _members in PROCESS_FAMILIES.items():
        for _fm in _members:
            _fn = _fm.lower()
            if _fn.endswith(".exe"):
                _fn = _fn[:-4]
            _FAMILY_OF[_fn] = _fk
    CAND_KEYS = {0: 0, 9: 1, 8: 2, 7: 3, 6: 4}
    FROZ_KEYS = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4}
    # slot index -> digit shown on the button / used as the hotkey
    CAND_SLOT_TO_KEY = {v: k for k, v in CAND_KEYS.items()}
    FROZ_SLOT_TO_KEY = {v: k for k, v in FROZ_KEYS.items()}

    def __init__(self, config_dir: str, process_manager: ProcessManager,
                 ui: dict[str, Any] | None = None,
                 vdesktop: VirtualDesktopManager | None = None,
                 config: Any | None = None) -> None:
        self.config_dir = config_dir
        self.pm = process_manager
        self.ui: dict[str, Any] = dict(ui or {})
        self.vd = vdesktop
        self.config = config
        self.show_windows_apps = False
        # User-maintained exclusion list (normalised process names, fnmatch
        # patterns allowed).  Empty by default — populated from the edit panel.
        self.hide_list: list[str] = []
        # When True, processes with a visible top-level window rank above
        # background-only processes of equal game/memory tier.  Game processes
        # still always win regardless of this flag.
        self.prioritize_foreground = True
        self.theme_color = get_theme_accent()
        self.lt_blue = COLOR_BLUE
        self.candidates: list[Any] = []
        self.frozen: list[_FrozenGroup] = []
        self.cand_btns: list[_Btn] = [_Btn() for _ in range(self.LIST_CAP)]
        self.froz_btns: list[_Btn] = [_Btn() for _ in range(self.LIST_CAP)]
        self.nav_idx = FocusHolder(-1)
        self.nav_list = "cand"
        self.needs_refresh = False
        self.highlight_key: int | None = None
        self.highlight_color: Color = Color.from_rgb(175, 90, 70)
        self.highlight_ts: float = 0.0
        # PIDs whose freeze/unfreeze is in flight on the background worker — a
        # guard so a second press while the (possibly slow) window move runs
        # cannot double-suspend or race the list update.
        self._pending: set[int] = set()
        # Optional ``runner(work, done)`` that runs ``work()`` on a background
        # thread and calls ``done(result)`` back on the UI thread.  The GUI
        # injects a Qt-backed runner (see app_window); when unset the action
        # runs synchronously so headless logic stays deterministic / testable.
        self._async_runner: Any | None = None
        # Optional callback invoked whenever the frozen list changes
        # (freeze/unfreeze completed) so the GUI can repaint immediately
        # instead of waiting for the next periodic refresh tick.
        self._on_change: Any | None = None
        # When True (default), a successful un-freeze also asks the host app
        # to minimise itself — the user just thawed a game and wants to get
        # straight back to it.  The actual minimise is a GUI concern, so it is
        # injected via ``set_minimize_self_callback``; headless / test usage
        # leaves the callback unset and nothing happens.
        self.minimize_on_unfreeze: bool = True
        self._minimize_self: Any | None = None
        # Optional host-app callback that re-focuses the Ningbingjue window
        # after a FREEZE completes.  Freezing minimises the target's windows
        # (ProcessManager.suspend -> ShowWindow(SW_MINIMIZE)); minimising a
        # foreground window makes Windows hand focus to some OTHER window, and
        # the HUD being top-most (WindowStaysOnTopHint) does NOT mean it holds
        # focus — so the overlay goes dim.  The GUI injects a Qt-based
        # re-activation here; headless / test usage leaves it unset (no-op).
        self._refocus_cb: Any | None = None
        self._load_pids()
        # 跨重启：内核挂起态随进程/系统一起丢失，磁盘上的 frozen_groups.json 只是
        # 历史残留，绝不可能是「当前仍被挂起」的真实状态。启动时强制清空冻结列表并
        # 落盘，避免 HUD 向用户显示误导性的「🔥 已冻结」标记（用户选择方案 A）。
        self.frozen = []
        self._save_pids()
        self.load_config()

    # -- theme highlight color --------------------------------------
    @staticmethod
    def resolve_highlight_color(theme_name: str) -> Color:
        # Highlight now follows the breathing (theme-accent) colour, not a
        # fixed theme name.  The ``theme_name`` arg is kept for API compat.
        return get_theme_accent()

    def update_highlight_color(self) -> None:
        self.highlight_color = get_theme_accent()

    # -- pid persistence ----------------------------------------------
    def _pid_file(self) -> str:
        return os.path.join(self.config_dir, "frozen_pids.txt")

    def _group_file(self) -> str:
        return os.path.join(self.config_dir, "frozen_groups.json")

    def _save_pids(self) -> None:
        try:
            os.makedirs(self.config_dir, exist_ok=True)
            # Flat PID file stays for backward-compatible readers.
            with open(self._pid_file(), "w", encoding="ascii") as fh:
                for g in self.frozen:
                    for p in g.pids:
                        fh.write(f"{p}\n")
            # Group structure: lets a reload reconstruct the EXACT move/resume
            # set without re-deriving it (which could mis-cluster a sibling app
            # sharing a process-tree ancestor, re-introducing the MO2 bug).
            with open(self._group_file(), "w", encoding="utf-8") as fh:
                json.dump(
                    [{"pid": g.pid, "name": g.name, "mem": g.mem,
                      "family": g.family, "pids": list(g.pids)}
                     for g in self.frozen],
                    fh,
                )
        except Exception:
            pass

    def _cluster_legacy(self, pids: list[int]) -> list:
        """Group raw PIDs loaded from the flat file (old installs / pre-update
        state).  Family members (steam + steamwebhelper) are clustered together;
        everything else is a standalone group.  Uses live process names so the
        family tag is correct; never re-derives a broad launch group."""
        names: dict[int, str] = {}
        if self.pm is not None and getattr(self.pm, "available", False):
            try:
                for p in self.pm.enumerate():
                    names[getattr(p, "pid", 0)] = self._norm_name(
                        getattr(p, "name", ""))
            except Exception:
                pass
        clusters: dict[str, list[int]] = {}
        singles: list[int] = []
        for pid in pids:
            fk = self._family_key(names.get(pid, "")) if pid in names else None
            if fk:
                clusters.setdefault(fk, []).append(pid)
            else:
                singles.append(pid)
        out: list = []
        for fk, members in clusters.items():
            out.append(_FrozenGroup(pid=members[0], name="", family=fk,
                                    pids=list(members)))
        for pid in singles:
            out.append(_FrozenGroup(pid=pid, name="", pids=[pid]))
        return out

    def _load_pids(self) -> None:
        gpath = self._group_file()
        if os.path.exists(gpath):
            try:
                with open(gpath, encoding="utf-8") as fh:
                    data = json.load(fh)
                self.frozen = []
                for d in data:
                    pids = [int(x) for x in d.get("pids", [])]
                    if not pids:
                        continue
                    self.frozen.append(_FrozenGroup(
                        pid=int(d.get("pid", pids[0])),
                        name=d.get("name", ""),
                        mem=int(d.get("mem", 0) or 0),
                        family=d.get("family", ""),
                        pids=pids,
                    ))
                return
            except Exception:
                pass
        # Legacy fallback: flat PID file -> standalone groups (re-cluster families).
        path = self._pid_file()
        if not os.path.exists(path):
            return
        try:
            raw: list[int] = []
            with open(path, "r", encoding="ascii") as fh:
                for line in fh:
                    line = line.strip()
                    if line.isdigit():
                        raw.append(int(line))
            if not raw:
                return
            self.frozen = self._cluster_legacy(raw)
        except Exception:
            pass

    # -- candidate selection -----------------------------------------
    @staticmethod
    def _norm_name(name: str) -> str:
        """Lowercase and drop a trailing ``.exe`` so that real Windows process
        names (``dwm.exe``) match the canonical bare names in EXCLUDED/NOISE."""
        n = (name or "").lower()
        if n.endswith(".exe"):
            n = n[:-4]
        return n

    def resolve_candidates(self, processes: list[Any]) -> list[Any]:
        frozen_pids = {e.pid for e in self.frozen}
        ranked: list[tuple[tuple, Any]] = []
        for p in processes:
            pid = getattr(p, "pid", 0)
            name = getattr(p, "name", "") or ""
            if pid == 0:
                continue
            if pid in frozen_pids:
                continue
            nname = self._norm_name(name)
            if nname in self.EXCLUDED:
                continue
            # Windows "noise" processes only surface when the user opts in via
            # the layout-editor toggle.  This never affects EXCLUDED above.
            if not self.show_windows_apps and nname in self.NOISE:
                continue
            # User-maintained exclusion list (edit panel) — fnmatch patterns.
            if self._is_hidden(nname):
                continue
            # Known service/helper/proxy processes that are not games.
            if self._is_service_helper(nname):
                continue
            # Game companion processes (crash handlers / RTSS hook loaders /
            # anti-cheat helpers) — never freeze targets, even if they exceed
            # the memory floor or a keyword slips past is_game_process().
            if self._is_game_companion(nname):
                continue
            mem = getattr(p, "memory_mb", 0.0)
            is_game = is_game_process(name)
            # Two-tier filter: a game process is *always* a candidate regardless
            # of how little RAM it uses (small indie titles / emulators matter
            # just as much as AAA); non-game processes must exceed MEM_THRESH_MB
            # or they are treated as trivial background noise and dropped.
            if not (is_game or mem > self.MEM_THRESH_MB):
                continue
            # Ordering: PRIMARY key = memory (descending), so the HUD's slot
            # keys 0,9,8,7,6 map onto the largest RAM hogs first and slot 0 is
            # the single biggest process among the top-5.  "is game" is kept
            # ONLY as a *tie-breaker* when two processes use exactly the same
            # memory, so a game still wins over an equal-footprint background
            # app.  A visible top-level window is a tertiary tie-breaker (and
            # only when the "优先前台窗口" toggle is on).  Net effect: the list
            # is sorted by memory from largest to smallest, with games gently
            # preferred on ties — instead of the old "games pinned to the top
            # regardless of size".
            fg = 1 if (self.prioritize_foreground
                       and bool(getattr(p, "has_visible_window", False))) else 0
            tier = 1 if is_game else 0
            ranked.append((mem, tier, fg, p))
        ranked.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
        return [p for _, _, _, p in ranked[: self.LIST_CAP]]

    # -- exclusion helpers --------------------------------------------
    def _is_hidden(self, nname: str) -> bool:
        return any(fnmatch.fnmatch(nname, e) for e in self.hide_list)

    def _is_service_helper(self, nname: str) -> bool:
        return any(fnmatch.fnmatch(nname, e) for e in self.SERVICE_HELPERS)

    def _is_game_companion(self, nname: str) -> bool:
        return any(fnmatch.fnmatch(nname, e) for e in self.GAME_COMPANIONS)

    # -- process-family (multi-process app) helpers ----------------------
    def _family_key(self, name: str) -> str | None:
        """Return the family key a (normalised) process name belongs to, or
        ``None`` if it is a standalone process (not part of a family)."""
        return self._FAMILY_OF.get(self._norm_name(name))

    def _gather_family_targets(self, primary_pid: int, primary_name: str,
                               primary_mem: int) -> dict[int, tuple[str, int]]:
        """Build ``{pid: (name, mem)}`` for the primary process plus every
        currently-alive family member (e.g. all ``steamwebhelper.exe`` for the
        Steam family).  Standalone processes return just the primary.

        This is the heart of the Steam fix: it guarantees that freezing the
        steam.exe candidate also suspends the CEF helpers that actually render
        Big Picture Mode, so the UI really freezes and Steam's IPC never
        desyncs (the old "解冻后程序出错" crash)."""
        targets: dict[int, tuple[str, int]] = {}
        key = self._family_key(primary_name)
        if key:
            member_names = {self._norm_name(m) for m in self.PROCESS_FAMILIES[key]}
            for p in (self.pm.enumerate() if self.pm.available else []):
                pn = self._norm_name(getattr(p, "name", ""))
                if pn in member_names:
                    targets[p.pid] = (getattr(p, "name", ""),
                                      int(getattr(p, "memory_mb", 0) or 0))
        if primary_pid and primary_pid not in targets:
            targets[primary_pid] = (primary_name, primary_mem)
        return targets

    def set_hidden(self, name: str, hidden: bool) -> None:
        """Add or remove a (normalised) process name from the user exclusion
        list and persist it.  A no-op if the state already matches."""
        nname = self._norm_name(name)
        if hidden:
            if not any(fnmatch.fnmatch(nname, e) for e in self.hide_list):
                self.hide_list.append(nname)
        else:
            self.hide_list = [e for e in self.hide_list
                              if not fnmatch.fnmatch(nname, e)]
        self.save_config()
        try:
            self.refresh_panel()
        except Exception:
            pass

    def clear_hidden(self) -> None:
        """Remove every user exclusion (restores the built-in defaults only)."""
        self.hide_list = []
        self.save_config()
        try:
            self.refresh_panel()
        except Exception:
            pass

    # -- config persistence (OnSaveConfig / OnLoadConfig) --------------
    def _settings_file(self) -> str:
        return os.path.join(self.config_dir, "freeze_settings.json")

    def save_config(self) -> None:
        """OnSaveConfig hook.  Persist the Windows-apps toggle, user exclusion
        list and foreground-prioritisation flag to the shared ``AppConfig``
        (layout.json) when available, else to a local JSON file so headless /
        test usage still round-trips."""
        if self.config is not None:
            try:
                self.config.freeze_show_windows_apps = self.show_windows_apps
                self.config.freeze_hidden = list(self.hide_list)
                self.config.freeze_prioritize_foreground = self.prioritize_foreground
                self.config.freeze_minimize_on_unfreeze = self.minimize_on_unfreeze
            except Exception:
                pass
        else:
            try:
                os.makedirs(self.config_dir, exist_ok=True)
                with open(self._settings_file(), "w", encoding="utf-8") as fh:
                    json.dump({
                        "show_windows_apps": self.show_windows_apps,
                        "hide_list": list(self.hide_list),
                        "prioritize_foreground": self.prioritize_foreground,
                        "minimize_on_unfreeze": self.minimize_on_unfreeze,
                    }, fh)
            except Exception:
                pass

    def load_config(self) -> None:
        """OnLoadConfig hook.  Mirror of :meth:`save_config`."""
        if self.config is not None:
            try:
                self.show_windows_apps = bool(
                    getattr(self.config, "freeze_show_windows_apps", False))
                self.hide_list = list(getattr(self.config, "freeze_hidden", []) or [])
                self.prioritize_foreground = bool(
                    getattr(self.config, "freeze_prioritize_foreground", True))
                self.minimize_on_unfreeze = bool(
                    getattr(self.config, "freeze_minimize_on_unfreeze", True))
            except Exception:
                self.show_windows_apps = False
                self.hide_list = []
                self.prioritize_foreground = True
                self.minimize_on_unfreeze = True
        else:
            try:
                with open(self._settings_file(), "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self.show_windows_apps = bool(data.get("show_windows_apps", False))
                self.hide_list = list(data.get("hide_list", []) or [])
                self.prioritize_foreground = bool(
                    data.get("prioritize_foreground", True))
                self.minimize_on_unfreeze = bool(
                    data.get("minimize_on_unfreeze", True))
            except Exception:
                self.show_windows_apps = False
                self.hide_list = []
                self.prioritize_foreground = True
                self.minimize_on_unfreeze = True

    def on_edit_layout(self) -> None:
        """OnEditLayout hook.  The FreezeEditPanel drives the single boolean
        toggle directly, so there is nothing to pre-build here."""
        pass

    # -- rendering ----------------------------------------------------
    def _btn_text(self, where: str, idx: int) -> str:
        if where == "cand":
            key = self.CAND_SLOT_TO_KEY.get(idx, "")
            if idx < len(self.candidates):
                p = self.candidates[idx]
                return f"{key}  ❄ {getattr(p, 'name', '?')}  {int(getattr(p, 'memory_mb', 0) or 0)}MB"
            return ""
        key = self.FROZ_SLOT_TO_KEY.get(idx, "")
        if idx < len(self.frozen):
            e = self.frozen[idx]
            mem = f"  {e.mem}MB" if e.mem else ""
            extra = f"  (+{len(e.pids) - 1})" if len(e.pids) > 1 else ""
            return f"{key}  🔥 {e.name or e.pid}{extra}{mem}"
        return ""

    def _render(self) -> None:
        for i, b in enumerate(self.cand_btns):
            b.text = self._btn_text("cand", i)
        for i, b in enumerate(self.froz_btns):
            b.text = self._btn_text("froz", i)
        if self.highlight_key is not None:
            self._apply_highlight(self.highlight_key, self.highlight_color)

    def refresh_panel(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._tick_highlight(now)
        procs = self.pm.enumerate() if self.pm.available else []
        self.candidates = self.resolve_candidates(procs)
        self._validate_frozen(procs)
        # Keep the frozen list ordered by memory (descending) so slot keys
        # 1,2,3,4,5 map onto entries from largest to smallest footprint.
        self.frozen.sort(key=lambda e: (e.mem or 0), reverse=True)
        if self.needs_refresh:
            self.needs_refresh = False
        self._render()

    def _validate_frozen(self, procs: list[Any]) -> None:
        """Mirror the PS1 ``Freeze-ValidatePids``: drop entries whose
        process has exited, and backfill ``name``/``mem`` for entries loaded
        from the PID file (which only stored the pid).  The PID file is the
        persistence source of truth; here we keep the in-memory list in sync
        with what is actually still alive and show real process names."""
        if not self.frozen:
            return
        live = {getattr(p, "pid", 0): p for p in procs}
        changed = False
        for g in self.frozen:
            # Backfill name/mem from the primary, else from any still-alive member.
            src = live.get(g.pid)
            if src is None:
                for mpid in g.pids:
                    if mpid in live:
                        src = live[mpid]
                        break
            if src is None:
                continue
            nm = getattr(src, "name", "") or ""
            if nm and not g.name:
                g.name = nm
                changed = True
            mem = int(getattr(src, "memory_mb", 0) or 0)
            if mem and not g.mem:
                g.mem = mem
                changed = True
        # A group is alive if ANY member still runs OR is still reported
        # suspended.  The latter is authoritative: a slow enumeration snapshot
        # taken right after a suspend (+ window move) can momentarily omit the
        # just-frozen process, and we must never drop a process we know we
        # suspended.  Without a pm we fall back to the enumeration alone.
        def _alive(g: _FrozenGroup) -> bool:
            for mpid in g.pids:
                if mpid in live:
                    return True
                pm = self.pm
                if pm is not None and getattr(pm, "available", False):
                    try:
                        if pm.is_suspended(mpid):
                            return True
                    except Exception:
                        pass
            return False

        dropped = [g for g in self.frozen if not _alive(g)]
        if dropped or changed:
            self.frozen = [g for g in self.frozen if _alive(g)]
            self._save_pids()

    def layout_panel(self) -> None:
        self._render()

    # -- highlight (two-press) -------------------------------------
    def _resolve_target(self, key: int) -> tuple[str, int] | None:
        if key in self.CAND_KEYS:
            return ("cand", self.CAND_KEYS[key])
        if key in self.FROZ_KEYS:
            return ("froz", self.FROZ_KEYS[key])
        return None

    def _clear_highlight(self) -> None:
        for b in self.cand_btns + self.froz_btns:
            b.border_color = self.lt_blue
            b.border_thickness = 2
        self.highlight_key = None

    def _apply_highlight(self, key: int, color: Color) -> None:
        tgt = self._resolve_target(key)
        if tgt is None:
            return
        where, idx = tgt
        btns = self.cand_btns if where == "cand" else self.froz_btns
        if 0 <= idx < len(btns):
            btns[idx].border_color = color
            btns[idx].border_thickness = 4

    def _tick_highlight(self, now: float) -> None:
        if self.highlight_key is None:
            return
        if now - self.highlight_ts > self.HIGHLIGHT_TIMEOUT_S:
            self.highlight_key = None
            self.needs_refresh = True

    def handle_hotkey(self, key: int, now: float | None = None) -> None:
        """``Handle-FreezeHotkey`` — two-press freeze/unfreeze."""
        now = time.time() if now is None else now
        tgt = self._resolve_target(key)
        if tgt is None:
            return
        if self.highlight_key == key:
            self._execute(tgt)
            self._clear_highlight()
        else:
            self.highlight_key = key
            self.highlight_ts = now
            self._apply_highlight(key, self.highlight_color)
            where, idx = tgt
            if where == "cand" and 0 <= idx < len(self.candidates):
                name = getattr(self.candidates[idx], "name", "?")
                self.show_toast(f"再次点击以冻结 {name}", success=True)
            elif where == "froz" and 0 <= idx < len(self.frozen):
                name = self.frozen[idx].name or self.frozen[idx].pid
                self.show_toast(f"再次点击以解冻 {name}", success=True)

    # -- execute freeze / unfreeze ---------------------------------
    def _move_failure_hint(self) -> str:
        """Explain why a window move failed, when we can tell (e.g. module
        missing because the app runs elevated while the module is per-user)."""
        vd = self.vd
        if vd is None or not getattr(vd, "available", False):
            return ""
        try:
            if not vd.module_ready():
                return ("（VirtualDesktop 模块未找到：请以与 App 相同权限运行 "
                        "vdesktop-doctor.bat 安装）")
        except Exception:
            pass
        path = getattr(vd, "log_path", None)
        loc = f" 日志：{path}" if path else ""
        return (f"（找不到该进程的可见窗口：可能由渲染子进程持有、独占全屏，"
                f"或窗口尚未创建。{loc}）")

    # -- async plumbing --------------------------------------------------
    def set_async_runner(self, runner: Any) -> None:
        """Inject ``runner(work, done)``: run ``work()`` on a background thread,
        then call ``done(result)`` on the UI thread.

        The GUI supplies a Qt-backed runner so the (potentially slow / hung)
        window move never freezes the app.  When left unset the freeze runs
        synchronously — headless tools and the test-suite rely on that."""
        self._async_runner = runner

    def set_on_change(self, cb: Any) -> None:
        """Register a callback invoked whenever the frozen list changes
        (freeze/unfreeze completed) so the GUI can repaint immediately instead
        of waiting for the next periodic refresh tick."""
        self._on_change = cb

    def set_minimize_self_callback(self, cb: Any) -> None:
        """Register the host-app minimise hook used by ``minimize_on_unfreeze``.

        The GUI injects a callable that hides / minimises the whole Ningbingjue
        window (e.g. ``QMainWindow._minimize_to_tray``).  When unset — headless
        tools and the test-suite — un-freeze never tries to minimise anything,
        so behaviour stays deterministic / controllable."""
        self._minimize_self = cb

    def set_refocus_callback(self, cb: Any) -> None:
        """Register the host-app re-focus hook invoked after a FREEZE completes.

        Freezing minimises the target's windows (see ``ProcessManager.suspend``
        — the window must be minimised BEFORE ``NtSuspendProcess`` because a
        suspended UI thread can no longer handle SC_MINIMIZE).  Minimising a
        foreground window makes Windows transfer focus to some other window; a
        top-most overlay is NOT the same as the focused window, so the HUD goes
        dim / loses keyboard input.  The GUI injects a Qt-based re-activation
        (raise + activateWindow + a deferred raw-HWND foreground steal) so the
        user can keep operating the HUD right after freezing.  When unset —
        headless tools and the test-suite — nothing happens."""
        self._refocus_cb = cb

    def _maybe_minimize_self(self) -> None:
        """Hide the host app after a successful un-freeze, if enabled and a
        minimise hook was injected.  Best-effort; any failure is swallowed so a
        broken minimise can never abort the un-freeze bookkeeping."""
        if not self.minimize_on_unfreeze:
            return
        cb = self._minimize_self
        if cb is None:
            return
        try:
            cb()
        except Exception:
            pass

    def _refocus_self(self) -> None:
        """Re-activate the host window after a successful freeze, if a re-focus
        hook was injected.

        Freezing minimises the target's windows (``ShowWindow(SW_MINIMIZE)``
        before ``NtSuspendProcess``), and minimising a foreground window makes
        Windows hand focus to another window — so the HUD loses keyboard focus
        even though it stays top-most.  Called only on the SUCCESS path of a
        freeze (never on un-freeze, which deliberately minimises the app back
        to the game).  Best-effort: any failure is swallowed so a broken
        re-focus can never abort the freeze bookkeeping."""
        cb = self._refocus_cb
        if cb is None:
            return
        try:
            cb()
        except Exception:
            pass

    def _notify_change(self) -> None:
        cb = self._on_change
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def _run_async(self, work: Any, done: Any) -> None:
        runner = self._async_runner
        if runner is None:
            try:
                res = work()
            except Exception:
                res = None
            done(res)
            return
        try:
            runner(work, done)
        except Exception:
            # If dispatch itself fails, degrade to synchronous so the action is
            # never silently lost (freeze must always happen).
            try:
                res = work()
            except Exception:
                res = None
            done(res)

    def _execute(self, tgt: tuple[str, int]) -> None:
        where, idx = tgt
        if where == "cand" and 0 <= idx < len(self.candidates):
            p = self.candidates[idx]
            pid = getattr(p, "pid", 0)
            name = getattr(p, "name", "")
            mem = int(getattr(p, "memory_mb", 0) or 0)
            if pid and pid in self._pending:
                return  # already being frozen; ignore the double press
            # Gather the primary + every alive family member (e.g. all
            # steamwebhelper.exe for Steam) so a multi-process app is frozen
            # coherently — suspending steam.exe alone leaves Big Picture alive
            # and desyncs Steam's IPC (→ error on resume).  Standalone processes
            # (e.g. a game) expand to their launch group via related_pids so the
            # WHOLE app (launcher -> game -> render child) freezes and moves
            # together, but related_pids STOPS at a known platform root (Steam),
            # so a game launched through Steam is never widened to Steam's tree.
            targets = self._gather_family_targets(pid, name, mem)
            family_key = self._family_key(name)
            # The exact set we will suspend + move + resume as ONE unit.  Captured
            # here (not re-derived at unfreeze) so it can never cross-contaminate
            # a sibling app that merely shares a process-tree ancestor.
            if family_key:
                group_info: dict[int, tuple[str, int]] = dict(targets)
            else:
                rel = {int(x) for x in related_pids(pid)}
                group_info = {pid: (name, mem)}
                if self.pm.available:
                    for p in self.pm.enumerate():
                        pp = getattr(p, "pid", 0)
                        if pp in rel:
                            group_info[pp] = (getattr(p, "name", ""),
                                             int(getattr(p, "memory_mb", 0) or 0))
            group_pids = sorted(group_info.keys())
            for tpid in group_pids:
                self._pending.add(tpid)
            # R2 业务层防御：即便候选列表被绕过（如手动冻结 / 编辑面板直输 PID），
            # 仍拒绝关键系统进程。底层 ProcessManager.suspend 另有「系统目录硬保护」
            # 作双保险，二者互补覆盖名单遗漏场景。
            if pid and self._norm_name(name) in self.EXCLUDED:
                for tpid in group_pids:
                    self._pending.discard(tpid)
                self.show_toast(f"⛔ 已拒绝冻结受保护系统进程 {name}", success=False)
                self._render()
                return
            self.show_toast(f"正在冻结 {name or pid}…", success=True)

            def work() -> dict[str, Any]:
                # 内核挂起是权威动作：先 suspend，成功后才把窗口移到后台桌面。
                # 这样「无法挂起的进程」绝不会被移到后台桌面却又不进已冻结列表
                # （看起来像冻结、实则未挂起、列表里也没有）—— 正是掌机实测的
                # 第二类现象。窗口搬迁是 best-effort，永远不阻塞真正的冻结。
                # 注意：``pm.suspend`` 会在进程仍响应时先最小化窗口（挂起后线程
                # 无法处理 SC_MINIMIZE），所以最终窗口以最小化态落在后台桌面，
                # 而不是还原态。
                # 进程族/启动组：先挂起主进程（如 steam.exe / game.exe），成功后才
                # 逐个挂起全部成员（如 steamwebhelper / 渲染子进程）。主进程挂起
                # 失败则不挂起任何成员（避免孤立挂起助手进程），整体 ok 由主进程决定。
                # 窗口搬迁用 ``move_pids`` 做 PID 收敛：只搬这个冻结单元精确包含的
                # 进程，绝不波及同进程树的其他应用（修复「解冻 Steam 把另一个已冻结
                # 的 MO2 也拽到当前桌面」的串扰 bug）。
                entries: list[tuple[int, str, int]] = []
                ok_primary = True
                if self.pm.available:
                    ok_primary = self.pm.suspend(pid) if pid else False
                    if ok_primary:
                        entries.append((pid, name, mem))
                        for gpid in group_pids:
                            if gpid == pid:
                                continue
                            gname, gmem = group_info.get(gpid, (str(gpid), 0))
                            if self._norm_name(gname) in self.EXCLUDED:
                                continue
                            if self.pm.suspend(gpid):
                                entries.append((gpid, gname, gmem))
                moved = False
                vd = self.vd
                if vd is not None and getattr(vd, "available", False):
                    moved = bool(vd.move_pids(group_pids, True))
                hint = "" if moved else self._move_failure_hint()
                return {"primary_pid": pid, "name": name, "mem": mem,
                        "entries": entries, "family": family_key or "",
                        "group_pids": list(group_pids),
                        "moved": moved, "ok": ok_primary, "hint": hint}

            self._run_async(work, self._on_freeze_done)

        elif where == "froz" and 0 <= idx < len(self.frozen):
            e = self.frozen[idx]
            pid = e.pid
            name = e.name
            # The whole unit we froze together (steam.exe + every steamwebhelper,
            # or a game's launch group).  Resuming / moving ONLY this set keeps
            # Steam coherent AND can never drag in a sibling app the user froze
            # separately (the MO2 bug).  group_pids is the authoritative set
            # captured at freeze time — we never re-derive it here.
            group_pids = list(e.pids)
            if pid and pid in self._pending:
                return  # already being unfrozen; ignore the double press
            for gpid in group_pids:
                self._pending.add(gpid)
            self.show_toast(f"正在解冻 {name or pid}…", success=True)

            self._run_async(self._build_unfreeze_work(pid, name, group_pids),
                            self._on_unfreeze_done)
        self._render()

    def _build_unfreeze_work(self, pid: int, name: str,
                             group_pids: list[int]) -> Any:
        """构造『解冻一个冻结单元』的 worker（与 ``_execute`` 的 froz 分支
        共用同一套顺序，避免逻辑分叉）。返回可直接交给 ``_run_async`` 的可
        调用对象。

        根因修复（2026-08-21，绝区零「解冻卡住」）：先只恢复线程（不还原/
        激活窗口）→ 进程活了窗口才响应 → 再把整组窗口移回当前桌面 → 最后
        统一 SW_RESTORE 还原。窗口还原时已在当前桌面，绝不会跳到恢复进程
        所在虚拟桌面。``move_pids`` 失败再走 ``move_to_current`` 逐成员补移。"""

        def work() -> dict[str, Any]:
            ok = True
            fail_pids: list[int] = []
            if self.pm.available:
                for gpid in group_pids:
                    r = self.pm.resume(gpid, restore_window=False)
                    # 先调用再更新标志：`ok and r` 在 ok 已为 False 时会
                    # 短路跳过后续进程，导致游戏只解冻一半（部分进程仍
                    # 挂起、游戏假死）。单独一行求值保证每个成员都恢复。
                    ok = ok and r
                    if not r:
                        fail_pids.append(gpid)
            moved = False
            vd = self.vd
            if vd is not None and getattr(vd, "available", False):
                # 进程已恢复 → find_hwnds_for_pids 找到窗口后 Move-Window
                # 快速完成（实测 ~0.7 s/窗口，15 s 总预算兜底）。
                # ``move_pids`` 是 PID 受限的，只搬本组窗口 —— 一个
                # 无关的已冻结应用（如 MO2）绝不会被拖到当前桌面（移动
                # 串扰修复不变式，见 _FrozenGroup.pids 的注释）。
                moved = bool(vd.move_pids(group_pids, False))
                if not moved:
                    # 兜底：EnumWindows 找不到窗口（全屏独占 / 特殊渲染
                    # 窗口），改用 PowerShell 的 PID→HWND 递归解析逐个
                    # 成员补移，两条路径互补。
                    for gpid in group_pids:
                        if gpid in fail_pids:
                            continue  # 已死/无法打开的进程没有窗口可移
                        try:
                            if vd.move_to_current(gpid):
                                moved = True
                        except Exception:
                            pass
            # 窗口已移回当前桌面，最后统一还原+激活（SW_RESTORE）。
            # 仅在全部恢复成功时执行；失败时保持最小化停在原桌面
            # （至少不触发桌面跳转），具体原因由 hint 给出。
            if ok and self.pm.available:
                for gpid in group_pids:
                    self.pm.restore_window(gpid)
            details: list[str] = []
            if not moved:
                details.append(self._move_failure_hint() or "窗口未移回当前桌面")
            if fail_pids:
                details.append("进程未恢复："
                               + ",".join(str(p) for p in fail_pids))
            hint = "；".join(d for d in details if d)
            return {"primary_pid": pid, "group_pids": list(group_pids),
                    "moved": moved, "ok": ok, "hint": hint}

        return work

    def unfreeze_by_pid(self, pid: int) -> bool:
        """Public API for the Start 'return to game' path.

        If the remembered game window's owning process is still suspended
        (frozen), kick off an unfreeze of the *whole* frozen unit it belongs to,
        so that pressing Start actually returns to a *responsive* game instead
        of focusing a dead, suspended window (which previously looked like the
        whole app had hung — the 'frozen process + Start = 卡死' bug).

        Reuses ``_build_unfreeze_work`` + ``_on_unfreeze_done`` (the exact same
        proven unfreeze sequence as the freeze card's own unfreeze button), so
        behaviour stays coherent.  Returns True if an unfreeze was started
        (or was already in flight), False if the PID isn't a known frozen unit
        or the process manager is unavailable.  Best-effort: never raises."""
        try:
            if not getattr(self.pm, "available", False):
                return False
            target = None
            for e in self.frozen:
                if e.pid == pid or pid in e.pids:
                    target = e
                    break
            if target is None:
                return False
            if pid in self._pending:
                return True  # already being unfrozen
            group_pids = list(target.pids)
            for gpid in group_pids:
                self._pending.add(gpid)
            self.show_toast(f"正在解冻 {target.name or pid}…", success=True)
            self._run_async(
                self._build_unfreeze_work(target.pid, target.name, group_pids),
                self._on_unfreeze_done)
            return True
        except Exception:  # noqa: BLE001
            return False

    # -- terminate (close) a selected process ---------------------------
    def _is_protected_pid(self, pid: int) -> bool:
        """Defense-in-depth: refuse to terminate a process whose image lives in
        a Windows system directory (csrss/lsass/services/winlogon …).  Reuses
        the same hard protection the underlying ``ProcessManager.suspend``
        applies, so closing a process can never blue-screen / crash the session
        even when the business-layer ``EXCLUDED`` name list misses one."""
        fn = getattr(self.pm, "_is_protected_system_process", None)
        if callable(fn):
            try:
                return bool(fn(pid))
            except Exception:
                return False
        return False

    def close_process(self, proc: Any) -> None:
        """Force-terminate a process selected in the freeze list (gamepad X /
        keyboard Backspace).  Handles both a candidate ``ProcessInfo`` and a
        frozen ``_FrozenGroup``:

        * a frozen unit (e.g. the Steam family: client + every webhelper) is
          killed together and removed from the frozen list so the HUD stops
          showing a dead process as "已冻结";
        * a lone candidate is killed by PID alone.

        Mirrors the freeze protection: ``EXCLUDED`` system processes, our own
        PID, and any process under a Windows system directory are refused — so a
        stray X / Backspace can never take down Windows or this app.  Killing
        uses ``taskkill /F`` via ``ProcessManager.stop`` (best-effort), run on
        the async worker so an unresponsive process can't freeze the HUD.
        """
        if not self.pm.available:
            self.show_toast("无法关闭进程：进程管理器不可用", success=False)
            return
        self_pid = os.getpid()
        if isinstance(proc, _FrozenGroup):
            pids = [int(x) for x in getattr(proc, "pids", []) or []]
            name = getattr(proc, "name", "") or getattr(proc, "pid", 0)
            is_frozen = True
        else:
            pid = int(getattr(proc, "pid", 0) or 0)
            if not pid:
                return
            pids = [pid]
            name = getattr(proc, "name", "") or pid
            is_frozen = False

        # Split into "must not kill" vs "killable" before touching anything.
        blocked: list[int] = []
        killable: list[int] = []
        for tpid in pids:
            if tpid == self_pid:
                blocked.append(tpid)
                continue
            tname = ""
            if is_frozen and tpid != int(getattr(proc, "pid", 0) or 0):
                # Resolve a name for the EXCLUDED check of secondary family
                # members (e.g. steamwebhelper) from the live enumeration.
                for p in (self.pm.enumerate() if self.pm.available else []):
                    if int(getattr(p, "pid", 0) or 0) == tpid:
                        tname = getattr(p, "name", "")
                        break
            else:
                tname = name if not is_frozen else getattr(proc, "name", "")
            if self._norm_name(tname) in self.EXCLUDED:
                blocked.append(tpid)
                continue
            if self._is_protected_pid(tpid):
                blocked.append(tpid)
                continue
            killable.append(tpid)

        if not killable:
            if blocked:
                self.show_toast(f"⛔ 已拒绝关闭受保护系统进程 {name}", success=False)
            else:
                self.show_toast(f"无法关闭 {name}：无可用进程", success=False)
            self._render()
            return

        kill_pids = list(killable)
        is_froz = is_frozen
        proc_ref = proc
        name_ref = name

        def work() -> dict[str, Any]:
            ok = True
            for tpid in kill_pids:
                try:
                    if not self.pm.stop(tpid):
                        ok = False
                except Exception:
                    ok = False
            return {"ok": ok, "pids": kill_pids}

        def done(res: dict[str, Any] | None) -> None:
            if not res:
                self.show_toast(f"关闭失败：内部错误 {name_ref}", success=False)
                self._render()
                return
            # A killed frozen unit is gone — drop it from the frozen list and
            # persist, so it no longer shows as "已冻结".
            if is_froz and res.get("ok"):
                self.frozen = [g for g in self.frozen if g is not proc_ref]
                self._save_pids()
            if res.get("ok"):
                self.show_toast(f"已关闭 {name_ref}", success=True)
            else:
                self.show_toast(f"部分进程未关闭：{name_ref}", success=False)
            # Re-enumerate + repaint (a killed candidate simply won't reappear).
            self.refresh_panel()
            self._notify_change()

        self._run_async(work, done)

    # -- completion callbacks (ALWAYS on the UI thread) ------------------
    def _on_freeze_done(self, res: dict[str, Any] | None) -> None:
        if not res:
            self.show_toast("冻结失败：内部错误", success=False)
            self._render()
            return
        # 清掉本次尝试过的**全部** PID 的 in-flight 标记（而非只清成功挂起的
        # entries）——否则任何成员 suspend 失败时该 PID 会永久留在 _pending，
        # 之后包含它的任何冻结/解冻按键都被 ``if pid in self._pending: return``
        # 静默吞掉，表现为「卡住/无响应」。
        for tpid in res.get("group_pids", []) or []:
            self._pending.discard(int(tpid))
        name = res.get("name") or res.get("primary_pid", 0)
        if res.get("ok"):
            # Collapse the whole suspended unit into ONE frozen-group entry, so
            # Steam (client + every helper) shows as a single row and unfreezing
            # it removes the entire family coherently — never a lone member.
            store_pids = [t[0] for t in res.get("entries", [])]
            if not store_pids:
                store_pids = res.get("group_pids", []) or []
            self.frozen.append(_FrozenGroup(
                pid=res.get("primary_pid", 0),
                name=res.get("name") or "",
                mem=res.get("mem", 0),
                family=res.get("family", ""),
                pids=list(store_pids)))
            # Drop from the live candidate list by PID (the index may be stale:
            # a background refresh can rebuild the list while the move ran).
            self.candidates = [c for c in self.candidates
                               if getattr(c, "pid", 0) not in set(store_pids)]
            self._save_pids()
            if res.get("moved"):
                suffix = " · 已移入后台桌面"
            else:
                hint = res.get("hint") or ""
                suffix = f" · 窗口未移动{hint}" if hint else " · 窗口未移动"
            self.show_toast(f"已冻结 {name}{suffix}", success=True)
        else:
            self.show_toast(f"冻结失败：无法挂起 {name}", success=False)
        self._render()
        self._notify_change()
        # 冻结成功后把焦点抢回凝冰结魄本体（若注入过 re-focus 回调）。
        # 冻结会最小化目标窗口 → Windows 把焦点交给别的窗口 → HUD 失焦变暗；
        # 用户刚按下冻结热键，显然还想继续在 HUD 上操作，故在此拉回焦点。
        # 解冻路径不调用（解冻后有意最小化 HUD 回到游戏，见 _on_unfreeze_done）。
        if res.get("ok"):
            self._refocus_self()

    def _on_unfreeze_done(self, res: dict[str, Any] | None) -> None:
        if not res:
            self.show_toast("解冻失败：内部错误", success=False)
            self._render()
            return
        grp = set(res.get("group_pids", []))
        for xpid in grp:
            self._pending.discard(xpid)
        # Resolve a human-readable name from the (still-present) frozen entry.
        primary = res.get("primary_pid", 0)
        name = None
        for e in self.frozen:
            if e.pid == primary:
                name = e.name
                break
        name = name or primary
        if res.get("ok"):
            # Drop any group that shares at least one PID with the unfrozen unit
            # (a single Steam family entry covers every member PID).
            self.frozen = [e for e in self.frozen
                           if not (set(e.pids) & grp)]
            self._save_pids()
            if res.get("moved"):
                suffix = " · 已移回当前桌面"
            else:
                hint = res.get("hint") or ""
                suffix = f" · 窗口未移回{hint}" if hint else " · 窗口未移回"
            self.show_toast(f"已解冻 {name}{suffix}", success=True)
        else:
            hint = res.get("hint") or ""
            suffix = f" · {hint}" if hint else ""
            self.show_toast(f"解冻失败：无法恢复 {name}{suffix}", success=False)
        self.refresh_panel()
        self._notify_change()
        # 解冻成功后自动最小化凝冰结魄本体：用户刚把游戏/应用解冻，通常想立刻
        # 回到它那里，而不是还停在我们的 HUD 上。仅成功时触发；最小化由宿主
        # 程序注入的回调执行（托盘/任务栏最小化），headless 下回调为空则无操作。
        if res.get("ok"):
            self._maybe_minimize_self()

    def dispose_vdesktop(self) -> None:
        """App-exit cleanup: only tear down the dedicated background desktop when
        nothing is still frozen on it (otherwise Windows would forcibly relocate
        the stranded window).  Best-effort; never raises."""
        if self.frozen:
            return
        vd = self.vd
        if vd is None or not getattr(vd, "available", False):
            return
        try:
            vd.dispose_background()
        except Exception:
            pass

    # -- toast ---------------------------------------------------------
    def show_toast(self, text: str, success: bool = True) -> None:
        panel = self.ui.get("toast_panel")
        label = self.ui.get("toast_label")
        if panel is not None and hasattr(panel, "set_tone"):
            panel.set_tone(success)
            panel.visible = True
        if label is not None:
            label.text = text
            label.fore_color = Color.from_rgb(0, 230, 120) if success else Color.from_rgb(255, 80, 80)

    # -- item-mode navigation ---------------------------------------
    def on_activate_item(self) -> None:
        self.nav_list = "cand"
        self.nav_idx.value = 0
        if self.cand_btns:
            set_control_focus_visual(self.cand_btns[0], True)

    def on_deactivate_item(self) -> None:
        clear_block_focus([b for b in self.cand_btns + self.froz_btns if b is not None])
        self.nav_idx.value = -1
        if self.needs_refresh:
            self.refresh_panel()

    # -- theme -------------------------------------------------------
    def _set_title_color(self, color: Color) -> None:
        title = self.ui.get("title")
        if title is not None:
            title.fore_color = color

    def on_theme_change(self, accent: dict[str, Any]) -> None:
        self.theme_color = get_theme_accent()
        self.update_highlight_color()
        self._set_title_color(self.theme_color)
        self._render()

    # -- registration --------------------------------------------------
    def register(self, registry: Any) -> None:
        bp = BlockBlueprint(
            block_type="freeze",
            name="► 进程冻结",
            color=COLOR_PEACH,
            card=self.ui.get("card"),
            hooks={
                "GetCardHeight": lambda: 474,
                "OnRefresh": self.refresh_panel,
                "OnLayout": self.layout_panel,
                "OnSyncTitleColor": self._set_title_color,
                "OnThemeChange": self.on_theme_change,
                "OnSaveConfig": self.save_config,
                "OnLoadConfig": self.load_config,
                "OnEditLayout": self.on_edit_layout,
                "OnActivateItem": self.on_activate_item,
                "OnDeactivateItem": self.on_deactivate_item,
            },
        )
        registry.register(bp.to_block())

    def status(self) -> str:
        """Short live readout for the smoke report / GUI panel."""
        return f"{len(self.candidates)} 候选 · {len(self.frozen)} 已冻结"