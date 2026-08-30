"""Pure (no-Qt) test: the page-hook firer must skip the static `tile` block.

The tile block is a static launcher with no volatile data; enrolling it in the
periodic/per-switch refresh rotation was the root cause of the intermittent
"磁贴全黑刷新" flash.  `_fire_page_hooks` must still fire volatile blocks
(e.g. `freeze`) but never `tile` / `tile#N`.
"""
from ningbingjue.core.navigation import NavigationController


class _Blk:
    def __init__(self, spy):
        self._spy = spy

    def hook(self, name):
        return self._spy if name in ("OnRefresh", "OnLayout") else None


class _Reg:
    def __init__(self, spies):
        self._spies = spies

    def get(self, k):
        return _Blk(self._spies[k]) if k in self._spies else None


class _Cfg:
    page_breaks = [0]


class _App:
    def render_pages(self):
        return [["tile", "tile#1", "freeze"]]


def test_fire_page_hooks_skips_tile_and_tileN():
    calls = {"tile": 0, "tile#1": 0, "freeze": 0}

    def make(k):
        def spy():
            calls[k] += 1
        return spy

    spies = {"tile": make("tile"), "freeze": make("freeze")}
    reg = _Reg(spies)
    nav = NavigationController(reg, _Cfg(), _App())
    nav.current_page = 0
    nav._fire_page_hooks()

    # tile + tile#N must NEVER be refreshed (2 hooks each would have been 0).
    assert calls["tile"] == 0, "tile was refreshed — it is static!"
    # freeze (volatile) must still get both OnRefresh + OnLayout.
    assert calls["freeze"] == 2, calls
