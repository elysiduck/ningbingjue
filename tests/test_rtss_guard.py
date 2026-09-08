"""rtss_guard 守护线程逻辑（直接调用 _tick，不依赖真实 RTSS）。

验证：仅在 enabled 时守；拉起成功后恢复；连续失败达上限进入 deployed_failed
退避态；绝不无限刷进程（失败计数有上限）。
"""
import sys

import pytest

import ningbingjue.platform.rtss_guard as grd


class _FakeCli:
    def __init__(self):
        self.installed = True
        self.running = True
        self.dir = r"C:\RTSS"
        self.set_limiter_calls = []
        self.set_limit_calls = []
        self.launched = 0

    def is_rtss_installed(self):
        return self.installed

    def is_rtss_running(self):
        return self.running

    def _resolve_rtss_dir(self):
        return self.dir

    def set_limiter(self, en):
        self.set_limiter_calls.append(en)

    def set_limit(self, lim):
        self.set_limit_calls.append(lim)


class _FakeCli:
    def __init__(self):
        self.installed = True
        self._running = False      # 拉起前为 False，拉起（下发）后为 True
        self.force_down = False    # 失败用例：恒为 False，模拟拉起不开
        self.dir = r"C:\RTSS"
        self.set_limiter_calls = []
        self.set_limit_calls = []

    def is_rtss_installed(self):
        return self.installed

    def is_rtss_running(self):
        return self._running and not self.force_down

    def _resolve_rtss_dir(self):
        return self.dir

    def set_limiter(self, en):
        self.set_limiter_calls.append(en)

    def set_limit(self, lim):
        self.set_limit_calls.append(lim)
        # 模拟「下发成功即 RTSS 已起」：拉起后把它标记为在跑（失败用例靠
        # force_down 压制，保持 False）。
        if not self.force_down:
            self._running = True


@pytest.fixture
def fake_popen(monkeypatch):
    """替掉 subprocess.Popen（不真拉进程）与 time.sleep（不真等）；并让
    ``os.path.exists`` 对 RTSS.exe 返回 True（假路径在测试机不存在，否则
    _relaunch 的 exe 存在性检查会直接跳过拉起）。"""
    class _P:
        pass
    monkeypatch.setattr(grd.subprocess, "Popen", lambda *a, **k: _P())
    monkeypatch.setattr(grd.time, "sleep", lambda s: None)
    monkeypatch.setattr(grd.os.path, "exists", lambda p: str(p).endswith("RTSS.exe"))


def make_guard(cli, ctx):
    return grd.RtssGuard(cli, context=ctx)


def test_idle_when_lock_off(fake_popen):
    # context.enabled=False → 不探活、不拉起、状态 idle。
    cli = _FakeCli()
    g = make_guard(cli, lambda: {"enabled": False, "limit": 0, "deploy_dir": cli.dir})
    g._tick()
    assert g.state[0] == "idle"
    assert cli.set_limit_calls == []


def test_running_state_no_relaunch(fake_popen):
    # RTSS 在跑 → 不拉起，状态 running。
    cli = _FakeCli()
    cli._running = True
    g = make_guard(cli, lambda: {"enabled": True, "limit": 60, "deploy_dir": cli.dir})
    g._tick()
    assert g.state[0] == "running"
    assert cli.set_limit_calls == []


def test_relaunch_success(fake_popen):
    # RTSS 没跑 → 拉起 → 补下发 → 拉起后 running=True → 状态 running。
    cli = _FakeCli()
    g = make_guard(cli, lambda: {"enabled": True, "limit": 60, "deploy_dir": cli.dir})
    g._tick()
    assert g.state[0] == "running"
    # 补下发：先 set_limiter(True) 再 set_limit(60)
    assert cli.set_limiter_calls == [True]
    assert cli.set_limit_calls == [60]


def test_relaunch_failure_then_backoff(fake_popen):
    # RTSS 始终拉不起来 → 连续 max_retries 次失败后进入 deployed_failed 退避态。
    cli = _FakeCli()
    cli.force_down = True       # 拉起后仍为 False
    g = make_guard(cli, lambda: {"enabled": True, "limit": 60, "deploy_dir": cli.dir})
    for _ in range(g.max_retries - 1):
        g._tick()
        assert g.state[0] == "installed"   # 重试中
    g._tick()
    assert g.state[0] == "deployed_failed"
    # 失败计数有上限：仅尝试了 max_retries 次拉起（不无限刷）。
    assert len(cli.set_limit_calls) == g.max_retries


def test_missing_system_reports_missing(fake_popen):
    # 系统都没装 → 状态 missing，不尝试拉起。
    cli = _FakeCli()
    cli.installed = False
    g = make_guard(cli, lambda: {"enabled": True, "limit": 60, "deploy_dir": None})
    g._tick()
    assert g.state[0] == "missing"
    assert cli.set_limit_calls == []


def test_backoff_interval_when_failed(fake_popen):
    # deployed_failed 态（连续拉起失败）→ 退避；BACKOFF_INTERVAL 明显大于
    # 正常探测间隔（loop 据此用更长等待，避免无限刷进程）。
    cli = _FakeCli()
    cli.force_down = True
    g = make_guard(cli, lambda: {"enabled": True, "limit": 60, "deploy_dir": cli.dir})
    for _ in range(g.max_retries):
        g._tick()
    assert g.state[0] == "deployed_failed"
    assert grd.BACKOFF_INTERVAL > g.interval
