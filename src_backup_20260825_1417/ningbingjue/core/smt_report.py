# -*- coding: utf-8 -*-
# =====================================================================
#  core/smt_report.py  --  SMT 热核监测记录 HTML 可视化生成器（自包含）
# =====================================================================
#  把一次热核监测会话的 record（拓扑 + 采样序列 + 判定 + 施加结果）渲染成
#  一个自包含 HTML 文件（内联 CSS + 内联 SVG，无外部依赖、无网络）。
#
#  由 编辑项页面的「调试/记录」按钮调用：模拟热核监测 -> 收集记录 -> 生成
#  HTML 报表 -> 打开。也可由真实施加流程复用（传入实际 apply 结果）。
# =====================================================================
from __future__ import annotations

import html
import os
import time


# --------------------------------------------------------------------------
# 调色：大核/小核/热核 配色
# --------------------------------------------------------------------------
_KIND_COLOR = {
    "big": "#ff7a7a",      # 性能核（Zen5）红
    "small": "#5cc8ff",    # 能效核（Zen5c）蓝
    "unknown": "#9aa6b8",
}
_HOT_COLOR = "#ffd166"     # 热核高亮（金黄）
_EXCLUDED_COLOR = "#7a5cff"  # 被排除的兄弟（紫）


def _esc(s):
    return html.escape(str(s))


def _bar_chart(topology, util, hot_lp, exclude_lps):
    """每个物理核一根竖向条形（同核两个兄弟并排），热核高亮。"""
    phys = topology.get("phys_cores", [])
    if not phys:
        return "<p style='color:#888'>无拓扑数据</p>"
    n = len(phys)
    W, H = 760, 220
    gap = 8
    bw = (W - gap * (n + 1)) / max(n, 1)
    maxv = 100.0
    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMid meet">']
    # 基线
    parts.append(f'<line x1="0" y1="{H-20}" x2="{W}" y2="{H-20}" stroke="#33415c" stroke-width="1"/>')
    for i, c in enumerate(phys):
        x = gap + i * (bw + gap)
        kind = c.get("kind", "unknown")
        lps = c.get("lps", [])
        subw = (bw - 4) / max(len(lps), 1)
        for j, lp in enumerate(lps):
            v = util.get(lp, 0.0) if util else 0.0
            bh = (v / maxv) * (H - 40)
            bx = x + j * (subw + 2)
            by = H - 20 - bh
            color = _KIND_COLOR.get(kind, "#9aa6b8")
            if lp == hot_lp:
                color = _HOT_COLOR
            elif lp in (exclude_lps or set()):
                color = _EXCLUDED_COLOR
            parts.append(
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{subw:.1f}" height="{bh:.1f}" '
                f'fill="{color}" rx="2"><title>LP{lp} {v:.1f}%</title></rect>')
            parts.append(
                f'<text x="{bx+subw/2:.1f}" y="{H-8}" font-size="9" fill="#9aa6b8" '
                f'text-anchor="middle">LP{lp}</text>')
        # 核号 + 类型
        label = f'#{c["index"]} '
        label += {"big": "大", "small": "小", "unknown": "?"} .get(kind, "?")
        parts.append(
            f'<text x="{x+bw/2:.1f}" y="14" font-size="10" fill="#cdd6e4" '
            f'text-anchor="middle">{_esc(label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _timeline(samples):
    """热核占用率随时间变化的折线（模拟监测的可视化）。"""
    if not samples:
        return "<p style='color:#888'>无采样序列</p>"
    W, H = 760, 160
    n = len(samples)
    pts = []
    for i, s in enumerate(samples):
        v = s.get("hot_val", 0.0)
        x = 10 + (W - 20) * (i / max(n - 1, 1))
        y = (H - 20) - (v / 100.0) * (H - 40)
        pts.append((x, y, v))
    body = ['<svg viewBox="0 0 %d %d" width="100%%" preserveAspectRatio="xMidYMid meet">' % (W, H)]
    body.append(f'<line x1="10" y1="{H-20}" x2="{W-10}" y2="{H-20}" stroke="#33415c" stroke-width="1"/>')
    # 100% / 80% 参考线
    y80 = (H - 20) - (80.0 / 100.0) * (H - 40)
    body.append(f'<line x1="10" y1="{y80:.1f}" x2="{W-10}" y2="{y80:.1f}" '
                f'stroke="#5a4a2a" stroke-width="1" stroke-dasharray="4 4"/>')
    body.append(f'<text x="12" y="{y80-3:.1f}" font-size="9" fill="#caa15a">80% 饱和线</text>')
    if len(pts) > 1:
        d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y, _ in pts)
        body.append(f'<path d="{d}" fill="none" stroke="#ffd166" stroke-width="2"/>')
    for x, y, v in pts:
        body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="#ffd166">'
                     f'<title>{v:.1f}%</title></circle>')
    body.append("</svg>")
    return "".join(body)


def _verdict_badge(verdict):
    if verdict == "APPLY":
        color = "#4be38a"
        txt = "APPLY（存在单线程瓶颈，建议施加）"
    else:
        color = "#9aa6b8"
        txt = "SKIP（负载均衡/未饱和，不建议施加）"
    return (f'<span style="display:inline-block;padding:4px 12px;border-radius:14px;'
            f'background:{color}22;color:{color};font-weight:bold;border:1px solid {color}">{_esc(txt)}</span>')


def generate_smt_html(record: dict, out_path: str | None = None) -> str:
    """生成自包含 HTML 报表；out_path 给定则写盘，返回最终路径（或 HTML 字符串）。"""
    topo = record.get("topology") or {}
    cfg = record.get("cfg") or {}
    samples = record.get("samples") or []
    final = record.get("final") or {}
    apply = record.get("apply")
    mode = record.get("mode", "模拟")
    game = record.get("game")
    ts = record.get("timestamp") or time.strftime("%Y-%m-%d %H:%M:%S")

    latest_util = samples[-1].get("util", {}) if samples else final.get("util", {})
    hot_lp = final.get("hot_lp")
    if hot_lp is None and samples:
        hot_lp = samples[-1].get("hot_lp")
    exclude_lps = final.get("exclude_lps") or set()
    if isinstance(exclude_lps, (set, list)):
        exclude_lps = set(exclude_lps)

    # 拓扑摘要
    phys = topo.get("phys_cores", [])
    topo_html = (
        f"<div class='kv'><span>物理核数</span><b>{len(phys)}</b></div>"
        f"<div class='kv'><span>逻辑处理器</span><b>{topo.get('total_lp', '?')}</b></div>"
        f"<div class='kv'><span>EfficiencyClass</span><b>{topo.get('eff_classes', '?')}</b></div>"
        f"<div class='kv'><span>隔离机制</span><b>{_esc(apply.get('mechanism','CPU集合') if apply else '仅检测')}</b></div>"
    )

    # 阈值表
    thr_rows = "".join(
        f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>"
        for k, v in [
            ("饱和阈值 saturated_thr", cfg.get("saturated_thr", "?")),
            ("离群下限 min_standout", cfg.get("min_standout", "?")),
            ("2σ 系数 k_sigma", cfg.get("k_sigma", "?")),
            ("激活下限 active_floor", cfg.get("active_floor", "?")),
            ("簇作用域 cluster_scope", cfg.get("cluster_scope", "?")),
        ]
    )

    # 判定结论
    verdict = final.get("is_bottleneck")
    verdict_txt = "APPLY" if verdict else ("SKIP" if verdict is not None else "?")
    stat_rows = "".join(
        f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>"
        for k, v in [
            ("最热逻辑核", f"LP{final.get('hot_lp','?')} ({final.get('hot_val',0):.1f}%)" if final else "?"),
            ("所属物理核/类型", f"#{final.get('hot_ci','?')} / {final.get('hot_kind','?')}" if final else "?"),
            ("簇内其余核均值", f"{final.get('mean_others',0):.1f}%" if final else "?"),
            ("热核超出均值", f"{final.get('standout',0):.1f} pp" if final else "?"),
            ("游戏进程", f"{game.get('name')}({game.get('pid')})" if game else "无（全部按非游戏排除热核）"),
        ]
    )
    if apply:
        stat_rows += (f"<tr><td>施加结果</td><td>施加 {apply.get('applied',0)} 个进程，"
                      f"跳过 {apply.get('skipped',0)} 个"
                      f"（游戏排除兄弟={apply.get('game_excluded')}，"
                      f"其他排除整核={apply.get('nongame_excluded')}）</td></tr>")

    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SMT 热核监测记录</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:#0f141c; color:#cdd6e4;
         font-family: "Microsoft YaHei", "Segoe UI", system-ui, sans-serif; padding:18px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  .sub {{ color:#8b97aa; font-size:12px; margin-bottom:14px; }}
  .card {{ background:#161c26; border:1px solid #232c3a; border-radius:10px;
          padding:14px 16px; margin-bottom:14px; }}
  .card h2 {{ font-size:14px; margin:0 0 10px; color:#9fd0ff; }}
  .kv {{ display:flex; justify-content:space-between; padding:3px 0;
         font-size:13px; border-bottom:1px dashed #232c3a; }}
  .kv span {{ color:#8b97aa; }} .kv b {{ color:#e6edf6; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  td {{ padding:5px 6px; border-bottom:1px solid #232c3a; }}
  td:first-child {{ color:#8b97aa; width:46%; }}
  .badge {{ margin:6px 0 10px; }}
  .legend {{ font-size:11px; color:#8b97aa; margin-top:6px; }}
  .legend i {{ display:inline-block; width:10px; height:10px; border-radius:2px;
              margin:0 4px 0 10px; vertical-align:middle; }}
</style></head>
<body>
  <h1>SMT 热核监测记录</h1>
  <div class="sub">{_esc(ts)} · 模式：{_esc(mode)}</div>

  <div class="card">
    <h2>拓扑概览</h2>
    {topo_html}
  </div>

  <div class="card">
    <h2>各核占用（最新采样）</h2>
    {_bar_chart(topo, latest_util, hot_lp, exclude_lps)}
    <div class="legend">
      <i style="background:{_HOT_COLOR}"></i>热核（最忙 LP）
      <i style="background:{_EXCLUDED_COLOR}"></i>被排除的兄弟
      <i style="background:{_KIND_COLOR['big']}"></i>性能核
      <i style="background:{_KIND_COLOR['small']}"></i>能效核
    </div>
  </div>

  <div class="card">
    <h2>热核占用时间线（模拟采样）</h2>
    {_timeline(samples)}
  </div>

  <div class="card">
    <h2>判定结论</h2>
    <div class="badge">{_verdict_badge(verdict_txt)}</div>
    <table>{stat_rows}</table>
  </div>

  <div class="card">
    <h2>判定阈值</h2>
    <table>{thr_rows}</table>
  </div>

  <div class="sub" style="margin-top:18px;">
    由 ningbingjue · SMT 功能卡片生成。本报表仅用于可视化调试记录，不含任何系统修改。
  </div>
</body></html>"""

    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_doc)
        return out_path
    return html_doc
