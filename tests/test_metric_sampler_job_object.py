"""R4 修复验证：MetricSampler 的 Job Object 崩溃安全网。

背景（ningbingjue_风险点审计.md R4 残留）：
MetricSampler 的 2 个 PowerShell 死循环子进程在硬崩溃（Python 被强杀、跑不到
stop()）时会变孤儿。修复=挂「关闭即杀」Job Object，OS 在父进程句柄关闭时
自动回收子进程。本测试验证挂载/关闭/幂等/失败放弃逻辑，不真起 PowerShell
（offscreen 环境下不可靠）。
"""
import sys
import threading
from unittest import mock

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32",
                                reason="Job Object 仅 Windows")


def _make_bare_sampler():
    """用 __new__ 绕过 __init__，手动补 stop() 所需属性。"""
    from ningbingjue.core.metrics import MetricSampler
    s = MetricSampler.__new__(MetricSampler)
    s._job = None
    s._procs = []
    s._lock = threading.Lock()
    s._callbacks = []
    s._prog_h = 0
    s._prog_pid = None
    s._running = True
    return s


def test_make_kill_on_close_job_succeeds():
    """_make_kill_on_close_job() 在 Windows 应成功创建 Job Object。"""
    from ningbingjue.core.metrics import _make_kill_on_close_job, _CloseHandle
    job = _make_kill_on_close_job()
    assert job is not None, "Job Object 创建失败（kernel32 API 不可用？）"
    _CloseHandle(job)  # 清理空 Job


def test_stop_closes_job_handle():
    """stop() 必须关闭 Job 句柄并置 None（正常退出路径）。"""
    from ningbingjue.core.metrics import MetricSampler, _make_kill_on_close_job
    s = _make_bare_sampler()
    s._job = _make_kill_on_close_job()
    assert s._job is not None
    s.stop()
    assert s._job is None, "stop() 未关闭 Job 句柄"


def test_stop_with_no_job_is_safe():
    """stop() 在 _job=None 时不应报错（未 start 或挂靠失败后）。"""
    s = _make_bare_sampler()
    s.stop()  # 不应抛异常
    assert s._job is None


def test_register_atexit_idempotent():
    """_register_atexit 幂等：重复调用只注册一次。"""
    import ningbingjue.core.metrics as m
    from ningbingjue.core.metrics import MetricSampler
    m._METRIC_SAMPLER_ATEXIT_REGISTERED = False
    s = _make_bare_sampler()
    s.stop = lambda: None
    s._register_atexit()
    assert m._METRIC_SAMPLER_ATEXIT_REGISTERED is True
    # 第二次调用不应再注册
    with mock.patch("atexit.register") as reg:
        s._register_atexit()
        reg.assert_not_called()


def test_attach_gives_up_on_assign_failure():
    """_attach_kill_on_close：_assign_to_job 返回 False → 关闭 Job、置 None。"""
    from ningbingjue.core.metrics import MetricSampler
    s = _make_bare_sampler()
    with mock.patch("ningbingjue.core.metrics._make_kill_on_close_job",
                    return_value=999), \
         mock.patch("ningbingjue.core.metrics._assign_to_job",
                    return_value=False), \
         mock.patch("ningbingjue.core.metrics._CloseHandle") as ch:
        proc = mock.Mock()
        proc._handle = 12345
        s._attach_kill_on_close(proc)
        assert s._job is None, "挂载失败应放弃 Job（置 None）"
        ch.assert_called_once_with(999)


def test_attach_gives_up_on_no_handle():
    """_attach_kill_on_close：取不到子进程句柄 → 关闭 Job、置 None。"""
    from ningbingjue.core.metrics import MetricSampler
    s = _make_bare_sampler()
    with mock.patch("ningbingjue.core.metrics._make_kill_on_close_job",
                    return_value=999), \
         mock.patch("ningbingjue.core.metrics._CloseHandle") as ch:
        proc = mock.Mock()
        proc._handle = None
        proc.pid = 999999  # OpenProcess 取不到
        s._attach_kill_on_close(proc)
        assert s._job is None
        ch.assert_called_once_with(999)


def test_second_popen_reuses_same_job():
    """已有 Job 时，第二个子进程挂同一个 Job，不重复创建。"""
    from ningbingjue.core.metrics import _make_kill_on_close_job, _CloseHandle
    s = _make_bare_sampler()
    s._job = _make_kill_on_close_job()
    assert s._job is not None
    original_job = s._job
    with mock.patch("ningbingjue.core.metrics._make_kill_on_close_job") as mk, \
         mock.patch("ningbingjue.core.metrics._assign_to_job",
                    return_value=True):
        proc = mock.Mock()
        proc._handle = 54321
        s._attach_kill_on_close(proc)
        mk.assert_not_called()  # 已有 Job 不应重复创建
        assert s._job == original_job
    _CloseHandle(s._job)


def test_make_job_none_disables_attach():
    """_make_kill_on_close_job 返回 None（降级环境）→ _attach 直接返回，_job 仍 None。"""
    s = _make_bare_sampler()
    with mock.patch("ningbingjue.core.metrics._make_kill_on_close_job",
                    return_value=None):
        proc = mock.Mock()
        proc._handle = 12345
        s._attach_kill_on_close(proc)
        assert s._job is None  # 降级：无 Job 保护，不崩
