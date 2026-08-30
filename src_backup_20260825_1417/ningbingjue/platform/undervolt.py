"""platform/undervolt.py — 负载门控 Curve Optimizer 降压控制器。

把「录制分析」阶段定下的判定逻辑落成可运行的状态机。

判定信号分工（用户拍板）
======================
  - 首要：单核最大占用 (max_core)            —— 主触发，直接反映 CPU 当前负载
  - 配角：CPU 均值 (mean_core) + 前台进程占用 (prog_cpu)
                                              —— 辅助判断（均衡型游戏靠 mean 触发）
  - 防抖：GPU 占用 (gpu_pct)                  —— 不参与升级，只在要降级时当守卫
    （突开开始菜单/游戏菜单：CPU 降但 GPU 未必立刻跌 -> GPU 仍高则 hold 住不降级；
     地图加载/场景切换：CPU 极高但 GPU≈0 -> GPU 不参与升级，CPU 高仍保 HEAVY）

每 tick（dt≈1s，UV 定时器）的状态机
===========================
  1. 未命中白名单(失焦 sticky_s 后)   -> IDLE,   CO=0
  2. cpu_heavy = max>=Hmax OR mean>=Hmean -> 累计 heavy_streak；**持续
     >=heavy_confirm_s 才升 HEAVY**（升慢降快，避免单核瞬时尖峰误触发 -50）
  3. 否则 state==HEAVY 且 gpu>=Hgpu    -> hold HEAVY           (GPU 防抖，即时)
  4. 否则 light_streak+=dt; >=T_light  -> LIGHT,  CO=-20       (降级即时)
  5. HEAVY 落地双重门控：-50 需 heavy_enabled=True **且** heavy_validated=True，
     否则降一档用 LIGHT（见下方「验证流程」）

阈值（config/undervolt.json，默认值，待实战微调）
  Hmax=75  Hmean=35  Hgpu_hold=55  T_light=1.5s  sticky=30s
  （2026-07-29 据绝区零实机 2415 样本标定：原 50/25 使 HEAVY 占 90%、LIGHT 档失效；
   75/35 使 HEAVY 仅约 16% 的 P90 尖峰触发，正常游玩走 LIGHT(-20)。t_light 由 3s 降到
   1.5s 以落实「升慢降快」——降档不慢于升档确认，缩小 alt-tab 切出后 -50 滞留窗口。）
  heavy_confirm_s=2.0（≈2 个 tick，过滤单样本尖峰）
  offsets: idle=0  light=-20  heavy=-50

安全开关
========
  - enabled：总开关。False 时状态机照常运行（便于观察它会做什么），但**不写
    ryzenadj**。默认 False —— 避免未经验证就自动改电压。
  - heavy_enabled：是否允许给 -50。False 时即使判定 HEAVY 也只给 LIGHT(-20)，
    等用户做完「-50 键盘受控验证」再放开。默认 False。
  - heavy_validated：**-50 键盘受控验证**是否已通过。仅当 heavy_enabled=True
    且 heavy_validated=True 时才真正下发 -50；否则即便判定 HEAVY 也降 LIGHT
    并告警。默认 False —— 这是「未验证不动 -50」的硬门控，防止 CO 在轻载降压
    最深时（RyzenAdj 通病）导致掌机键盘失灵。用户完成验证后在 config 显式置 true。
  - dry_run：观察模式。True 时**逐 tick 把状态机「会做什么」写到日志文件，但
    绝不写 ryzenadj**（backend 同时置 dry_run 双重保险）。用于无风险看清状态机
    怎么切；日志路径默认 config/undervolt_dryrun.log。默认 False。

验证流程（放开 -50 前必做，对应修复⑥）
=====================================
  1) 标定阈值：游戏实机跑 `test-launch/py-script/measure_game_load.py --auto`
     （默认已开每核监控），看 CSV 的 cpu_max/cpu_avg 分布与 phase_hint 列，
     据此设定 hmax/hmean（phase_hint 现已与控制器同口径，可直接标定）。
  2) 安全观察：config 设 enabled=true, heavy_enabled=false, dry_run=true，
     跑几天看 config/undervolt_dryrun.log，确认状态机切档符合预期（不写电压）。
  3) -50 键盘受控验证：短期放开 heavy_enabled=true（此时仍因 heavy_validated
     =false 而只给 -20），手动在 config 把 heavy_validated 临时置 true，进重负载
     游戏连续玩 30+ 分钟，确认键盘不冻结、无 WHEA-Logger 欠压报错；若有异常立即
     把 heavy_validated 改回 false（自动回退 -20）。
  4) 正式放开：验证通过且稳定后，保持 heavy_validated=true，让 -50 长期生效。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .ryzenadj import RyzenAdj

try:
    from ..core.game_detect import find_foreground_game
except Exception:  # pragma: no cover
    find_foreground_game = None  # type: ignore[assignment]


# ryzenadj --set-coall 编码：base - |mV|。-50 -> 1048526；-20 -> 1048556；0 -> 1048576。
COALL_BASE = 1048576


def coall_value(mv: int) -> int:
    """把 mV 偏移编码成 --set-coall 接受的 1048576-|mV| 形式。"""
    return COALL_BASE - abs(int(mv))


def _fmt(v: float | None) -> str:
    """dry-run 日志用：把 None 显示成 '-'，数字保留 1 位小数。"""
    return f"{v:.1f}" if isinstance(v, (int, float)) else "-"


class RyzenAdjBackend:
    """把「应用 CO 偏移」封装成对 ryzenadj 的调用。

    ``dry_run=True`` 时**只记日志、绝不调用 ryzenadj**——用于「观察状态机怎么切」
    的无风险模式（配合 ``UndervoltController`` 的逐 tick 日志）。此时 ``apply_coall``
    返回 ``True``（假装下发成功），但不碰任何电压。
    """

    def __init__(self, ryzenadj: RyzenAdj, dry_run: bool = False) -> None:
        self.ra = ryzenadj
        self.dry_run = dry_run

    @property
    def available(self) -> bool:
        return self.ra.available

    def apply_coall(self, mv: int) -> bool:
        """应用 Curve Optimizer 全核偏移 mv（负值=降压）。

        经统一派发器下发：只把「期望 CO 偏移」写进共享 RyzenAdj 的 desired，
        再由 maybe_apply 在【组合状态变化】时与 GPU 锁频/TDP 合并成一次 ryzenadj
        调用——避免降压 tick（主线程）与 GPU 浮动（后台采样线程）各自独立 invoke
        导致两进程并发抢 WinRing0 驱动。GUI 线程安全。返回是否下发。
        """
        if not self.available:
            return False
        if self.dry_run:
            print(f"[DRY-RUN] 跳过 ryzenadj 调用：本应 --set-coall "
                  f"{coall_value(mv)} (offset={mv}mV)")
            return True
        self.ra.set_desired_coall_mv(mv)
        self.ra.maybe_apply()
        return True

    def reset(self) -> None:
        """强制复位到 0（退出/异常路径）。"""
        self.apply_coall(0)


@dataclass
class UndervoltConfig:
    """从 config/undervolt.json 加载；缺失键用默认值合并（向下兼容旧模板）。"""
    enabled: bool = False
    heavy_enabled: bool = False
    dry_run: bool = False
    offsets: dict[str, int] = field(default_factory=lambda: {
        "idle": 0, "light": -20, "heavy": -50,
    })
    hmax: float = 50.0
    hmean: float = 25.0
    hgpu_hold: float = 55.0
    t_light_s: float = 3.0
    sticky_s: float = 30.0
    heavy_confirm_s: float = 2.0
    heavy_validated: bool = False
    whitelist: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "UndervoltConfig":
        cfg = cls()
        if not path.is_file():
            return cfg
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cfg
        cfg.enabled = bool(d.get("enabled", cfg.enabled))
        cfg.heavy_enabled = bool(d.get("heavy_enabled", cfg.heavy_enabled))
        cfg.dry_run = bool(d.get("dry_run", cfg.dry_run))
        offs = d.get("offsets") or {}
        if isinstance(offs, dict):
            for k in ("idle", "light", "heavy"):
                if k in offs:
                    try:
                        cfg.offsets[k] = int(offs[k])
                    except (TypeError, ValueError):
                        pass
        th = d.get("thresholds") or {}
        # 新扁平键优先，否则回退到旧 thresholds 子字典
        cfg.hmax = float(d.get("hmax", th.get("hmax", cfg.hmax)))
        cfg.hmean = float(d.get("hmean", th.get("hmean", cfg.hmean)))
        cfg.hgpu_hold = float(d.get("hgpu_hold", th.get("hgpu_hold", cfg.hgpu_hold)))
        cfg.t_light_s = float(d.get("t_light_s", th.get("t_light_s", cfg.t_light_s)))
        cfg.sticky_s = float(d.get("sticky_s", th.get("sticky_s", cfg.sticky_s)))
        cfg.heavy_confirm_s = float(d.get("heavy_confirm_s",
                                          th.get("heavy_confirm_s", cfg.heavy_confirm_s)))
        cfg.heavy_validated = bool(d.get("heavy_validated",
                                         cfg.heavy_validated))
        wl = d.get("game_exe_whitelist") or []
        if isinstance(wl, list):
            cfg.whitelist = [str(x).strip().lower() for x in wl if str(x).strip()]
        return cfg

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "enabled": self.enabled,
            "heavy_enabled": self.heavy_enabled,
            "dry_run": self.dry_run,
            "offsets": self.offsets,
            "hmax": self.hmax, "hmean": self.hmean, "hgpu_hold": self.hgpu_hold,
            "t_light_s": self.t_light_s, "sticky_s": self.sticky_s,
            "heavy_confirm_s": self.heavy_confirm_s,
            "heavy_validated": self.heavy_validated,
            "game_exe_whitelist": self.whitelist,
        }, ensure_ascii=False, indent=2), encoding="utf-8")


IDLE, LIGHT, HEAVY = "idle", "light", "heavy"


class UndervoltController:
    """负载门控降压状态机。

    消费者把共享 ``MetricSampler`` 与 ``RyzenAdjBackend`` 注入；每 tick 调
    ``tick(dt)`` 即可。只在状态变化时写 ryzenadj（避免每 tick 都 spawn 进程）。
    """

    def __init__(self, backend: RyzenAdjBackend, sampler: Any,
                 config: UndervoltConfig,
                 dry_log_path: Any | None = None) -> None:
        self.backend = backend
        self.sampler = sampler
        self.config = config
        # 初始为哨兵（非任何正式状态），保证首帧必写一次当前档位——
        # 开机清掉上次会话可能残留的 -50。
        self.state = ""
        self.last_game_ts = -1e9
        self.light_streak = 0.0
        self.heavy_streak = 0.0
        self.last_applied_mv = 0
        self._last_metrics = None
        # 状态变化回调（主线程），用于 UI/日志。签名 (state, applied_mv)
        self.on_change: Callable[[str, int], None] | None = None
        # dry-run 逐 tick 日志（仅 dry_run 时启用，只看不做）
        self.dry_run = bool(config.dry_run)
        self._dry_fh = None
        if self.dry_run and dry_log_path is not None:
            try:
                import datetime as _dt
                p = Path(dry_log_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                self._dry_fh = open(p, "a", encoding="utf-8", buffering=1)
                self._dry_fh.write(
                    f"# === dry-run session "
                    f"{_dt.datetime.now():%Y-%m-%d %H:%M:%S} === "
                    f"(enabled={config.enabled} "
                    f"heavy_enabled={config.heavy_enabled})\n"
                )
                self._dry_fh.flush()
            except Exception as e:  # pragma: no cover
                print(f"[undervolt] dry-run 日志打开失败: {e}")
                self._dry_fh = None

    # -- 内部 ------------------------------------------------------------
    def _offset_for(self, st: str) -> int:
        return int(self.config.offsets.get(st, 0))

    def _set_state(self, new: str) -> None:
        if new == self.state:
            return
        self.state = new
        mv = self._offset_for(new)
        if self.config.enabled:
            # -50 双重门控：未放行 / 未完成 -50 键盘受控验证 -> 降一档用 LIGHT，
            # 保证不在未验证场景给 -50（CO 轻载降压最深，易致掌机键盘失灵）
            if new == HEAVY and not self.config.heavy_enabled:
                mv = self._offset_for(LIGHT)
            elif new == HEAVY and not self.config.heavy_validated:
                mv = self._offset_for(LIGHT)
                print("[undervolt] HEAVY(-50) 被拦截：heavy_validated=False。"
                      "请先完成「-50 键盘受控验证」并在 config 显式置 true"
                      "（见 platform/undervolt.py 顶部「验证流程」）。")
            self.backend.apply_coall(mv)
            self.last_applied_mv = mv
        if self.on_change is not None:
            try:
                self.on_change(new, mv)
            except Exception:  # pragma: no cover
                pass

    # -- 主循环 ----------------------------------------------------------
    def tick(self, dt: float) -> str:
        """推进一个 tick。返回当前状态名。

        dry_run 模式下会逐 tick 把「会做什么」写到日志文件
        （默认 ``config/undervolt_dryrun.log``），但**绝不写 ryzenadj**
        （backend 同时被置为 dry_run 双重保险）。
        """
        now = time.monotonic()
        cfg = self.config

        # 1) 游戏判定 + 失焦 sticky
        game = None
        if find_foreground_game is not None:
            game = find_foreground_game(cfg.whitelist)  # 白名单命中 或 关键字自动识别
        game_name = game[1] if game else None
        if game:
            self.last_game_ts = now
        in_game = (now - self.last_game_ts) <= cfg.sticky_s

        target: str = IDLE
        reason: str = ""
        metrics = None
        if not in_game:
            # 2) 未命中白名单（失焦 sticky 之后）-> IDLE, CO=0
            self.light_streak = 0.0
            target = IDLE
            reason = "not_in_game(失焦超过 sticky_s)"
        else:
            # 3) 读共享指标
            s = self.sampler.snapshot
            maxc, meanc, gpu, prog = s.max_core, s.mean_core, s.gpu_pct, s.prog_cpu
            metrics = (maxc, meanc, gpu, prog)

            # 4) 主触发：单核最大 或 均值 命中 -> 累计 heavy_streak；
            #    **持续 >= heavy_confirm_s 才升 HEAVY**（升慢降快，过滤单核瞬时
            #    尖峰——着色器编译/后台任务常 <1s，却会反复 -50↔-20 写 ryzenadj
            #    且重负载降压更易 WHEA）。降级（GPU 防抖 / light_streak）仍即时。
            cpu_heavy = (
                (maxc is not None and maxc >= cfg.hmax)
                or (meanc is not None and meanc >= cfg.hmean)
            )
            if cpu_heavy:
                self.heavy_streak += dt
                self.light_streak = 0.0
                if self.heavy_streak >= cfg.heavy_confirm_s:
                    target = HEAVY
                    reason = (f"cpu_heavy(max={_fmt(maxc)}>={cfg.hmax:g} "
                              f"or mean={_fmt(meanc)}>={cfg.hmean:g}) "
                              f"持续{self.heavy_streak:.1f}s>="
                              f"{cfg.heavy_confirm_s:g}")
                else:
                    # 尚未确认：维持现状（已 HEAVY 继续；否则 IDLE/LIGHT）
                    target = self.state if self.state in (IDLE, LIGHT, HEAVY) else IDLE
                    reason = (f"cpu_heavy_confirming({self.heavy_streak:.1f}s/"
                              f"{cfg.heavy_confirm_s:g})")
            # 5) GPU 防抖：仍在渲染 -> hold 住 HEAVY，不降级
            elif self.state == HEAVY and gpu is not None and gpu >= cfg.hgpu_hold:
                self.light_streak = 0.0
                self.heavy_streak = 0.0
                target = HEAVY
                reason = f"gpu_hold(gpu={_fmt(gpu)}>={cfg.hgpu_hold:g}，不降级)"
            else:
                # 6) 非对称去抖：CPU 不重 + GPU 不高 -> 累计，满 T_light 才落 LIGHT
                self.heavy_streak = 0.0
                self.light_streak += dt
                if self.light_streak >= cfg.t_light_s:
                    target = LIGHT
                    reason = (f"light_streak={self.light_streak:.1f}s>="
                              f"t_light={cfg.t_light_s:g}")
                else:
                    # 维持现状（已 LIGHT/IDLE，且无升级信号）
                    target = self.state if self.state in (IDLE, LIGHT, HEAVY) else IDLE
                    reason = f"stable(state={self.state or '-'}, streak={self.light_streak:.1f}s)"

        # 计算有效偏移（尊重双重门控：-50 未放行 / 未完成 -50 键盘验证 时降一档用 LIGHT）
        eff_mv = self._offset_for(target)
        gated = ""
        if target == HEAVY and not cfg.heavy_enabled:
            eff_mv = self._offset_for(LIGHT)
            gated = " [HEAVY 未放行 -> 用 LIGHT]"
        elif target == HEAVY and not cfg.heavy_validated:
            eff_mv = self._offset_for(LIGHT)
            gated = " [HEAVY 未验证 -> 用 LIGHT(需先完成-50键盘受控验证)]"

        # dry-run：逐 tick 落日志（只看不做）
        if self.dry_run:
            self._dry_log_tick(game_name, metrics, target, eff_mv, reason, gated)

        # 真正应用（受 enabled 控制；dry_run 时 backend 已不写）
        self._set_state(target)
        return self.state

    # -- dry-run 日志 -----------------------------------------------------
    def _dry_log_tick(self, game_name: str | None,
                      metrics: Any, target: str, eff_mv: int,
                      reason: str, gated: str) -> None:
        """写一行逐 tick 决策日志（仅 dry_run 模式调用）。"""
        if self._dry_fh is None:
            return
        ts = time.strftime("%H:%M:%S", time.localtime())
        if metrics is None:
            mstr = "max=- mean=- gpu=- prog=-"
        else:
            maxc, meanc, gpu, prog = metrics
            mstr = (f"max={_fmt(maxc)} mean={_fmt(meanc)} "
                    f"gpu={_fmt(gpu)} prog={_fmt(prog)}")
        line = (f"{ts} | game={game_name or '-'} | {mstr} | "
                f"-> {target.upper()} (eff_mv={eff_mv}){gated} | {reason}\n")
        try:
            self._dry_fh.write(line)
            self._dry_fh.flush()
        except Exception:  # pragma: no cover
            pass

    def close(self) -> None:
        """释放 dry-run 日志文件句柄（app 退出时）。"""
        if self._dry_fh is not None:
            try:
                self._dry_fh.close()
            except Exception:
                pass
            self._dry_fh = None

    def reset(self) -> None:
        """复位到 IDLE 并强制 CO=0（app 退出/异常路径）。"""
        self.state = IDLE
        self.light_streak = 0.0
        self.heavy_streak = 0.0
        self.last_game_ts = -1e9
        if self.config.enabled:
            self.backend.reset()
            self.last_applied_mv = 0

    def reapply(self) -> None:
        """运行时改了 enabled/heavy_enabled 后，立即按最新门控重应用当前档位。

        状态机只在「状态变化」时写 ryzenadj，所以仅切换开关不会触发重写——
        这里先把 state 临时清空再 ``_set_state`` 当前档位，强制走一遍应用逻辑
        （关闭总开关会经 backend.reset() 把电压归零；重开放行档位会立即下发）。
        """
        cur = self.state
        self.state = ""
        self._set_state(cur)
