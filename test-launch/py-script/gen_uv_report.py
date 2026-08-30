"""gen_uv_report.py — 负载录制 CSV → 自包含 HTML 报表生成器。

读 measure_game_load.py 产出的 CSV（多进程 / 多核 / GPU），生成**单文件** HTML
报表（内联 SVG，零外部依赖、零 CDN），包含：

  1. 各游戏进程 CPU 占用率 —— 曲线图（每进程一条线）
  2. 各游戏进程 CPU 占用率 —— 热力图（行=进程，列=时间点）
  3. 系统 CPU 各核心占用率 —— 热力图（行=核心，列=时间点）
  4. 系统 CPU 核心占用率 —— 曲线图（平均核心 + 最大核心）
  5. GPU 占用率 —— 曲线图

“分别记录不同游戏进程（记得过滤）”由录制端 enum_game_processes() 保证（只收
is_game_process 命中的进程）；本脚本只负责把 CSV 里 `cpu__<exe>__<pid>` 列还原成
逐进程曲线/热力图。系统各核心数据来自录制端的 cpu_cores 列（系统级，非逐进程）。

用法：
  python gen_uv_report.py logs/measure_20260101_120000.csv
  python gen_uv_report.py logs/measure_20260101_120000.csv --out report.html
也可被 measure_game_load.py 以 `generate_report(csv_path)` 调用。
"""
from __future__ import annotations

import argparse
import colorsys
import csv
import re
import sys
from datetime import datetime
from pathlib import Path

_PROC_RE = re.compile(r"^cpu__(.+)__(\d+)$")

# 曲线配色（进程多时循环取用）
_SERIES_COLORS = [
    "#4be38a", "#ffd166", "#7fd1ff", "#ff6b6b", "#c792ea",
    "#ff9e64", "#5ee0d6", "#f78fb3", "#a3e635", "#62a0ea",
]


# --------------------------------------------------------------------------
# 颜色刻度（CPU% 0..100 → 冷蓝→红热）
# --------------------------------------------------------------------------
def _heat(v: float | None) -> str:
    """0% 冷蓝、100% 红热；空值返回透明深灰。"""
    if v is None:
        return "rgb(28,32,40)"
    v = max(0.0, min(100.0, v))
    hue = (1.0 - v / 100.0) * 0.66  # 0.66≈蓝, 0≈红
    light = 0.30 + (v / 100.0) * 0.32
    r, g, b = colorsys.hsv_to_rgb(hue, 0.72, light)
    return f"rgb({int(r*255)},{int(g*255)},{int(b*255)})"


def _series_color(i: int) -> str:
    return _SERIES_COLORS[i % len(_SERIES_COLORS)]


# --------------------------------------------------------------------------
# 数据读取
# --------------------------------------------------------------------------
def load_rows(path: Path):
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        rows = [r for r in reader]
    return header, rows


def _to_float(s: str | None):
    if s is None or s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_csv(path: Path):
    header, rows = load_rows(path)

    # 逐进程列
    procs: list[tuple[str, str, int]] = []  # (col, exe, pid)
    for col in header:
        m = _PROC_RE.match(col)
        if m:
            procs.append((col, m.group(1), int(m.group(2))))

    # 兼容旧版单进程 CSV（仅有 cpu_pct，无 cpu__ 列）
    legacy = False
    if not procs and "cpu_pct" in header:
        procs = [("cpu_pct", "目标进程", 0)]
        legacy = True

    times: list[float] = []
    for r in rows:
        t = _to_float(r.get("elapsed_s"))
        times.append(t if t is not None else float(len(times)))

    # 逐进程序列
    proc_series: dict[int, list[tuple[float, float | None]]] = {pid: [] for _, _, pid in procs}
    for r, t in zip(rows, times):
        for col, _exe, pid in procs:
            proc_series[pid].append((t, _to_float(r.get(col))))

    # 系统各核心矩阵
    core_matrix: list[list[float | None]] = []
    n_cores = 0
    for r in rows:
        raw = r.get("cpu_cores")
        if raw:
            vals = [_to_float(x) for x in raw.split(",")]
            vals = [v if v is not None else 0.0 for v in vals]
            if vals:
                n_cores = max(n_cores, len(vals))
                core_matrix.append(vals)
    # 补齐成规整矩阵（统一列数）
    core_rows = [row + [0.0] * (n_cores - len(row)) for row in core_matrix] if n_cores else []

    # GPU 序列
    gpu_series: list[tuple[float, float | None]] = []
    for r, t in zip(rows, times):
        gpu_series.append((t, _to_float(r.get("gpu_pct"))))

    return {
        "header": header,
        "rows": rows,
        "times": times,
        "procs": procs,
        "legacy": legacy,
        "proc_series": proc_series,
        "core_rows": core_rows,
        "n_cores": n_cores,
        "gpu_series": gpu_series,
    }


# --------------------------------------------------------------------------
# 降采样（热力图列数封顶，避免 SVG 过大）
# --------------------------------------------------------------------------
def _aggregate_matrix(matrix: list[list[float | None]], times: list[float], max_cols: int):
    T = len(times)
    if T <= max_cols or T == 0:
        return matrix, times
    step = T / max_cols
    out_times = [times[(int(i * step) + int((i + 1) * step)) // 2] for i in range(max_cols)]
    out_rows = []
    for row in matrix:
        nr = []
        for i in range(max_cols):
            s, e = int(i * step), max(int((i + 1) * step), int(i * step) + 1)
            chunk = [v for v in row[s:e] if v is not None]
            nr.append(sum(chunk) / len(chunk) if chunk else None)
        out_rows.append(nr)
    return out_rows, out_times


def _aggregate_series(series: list[tuple[float, float | None]], max_n: int):
    n = len(series)
    if n <= max_n or n == 0:
        return series
    step = n / max_n
    out = []
    for i in range(max_n):
        s, e = int(i * step), max(int((i + 1) * step), int(i * step) + 1)
        chunk = [v for _, v in series[s:e] if v is not None]
        tmid = series[(s + e) // 2][0]
        out.append((tmid, sum(chunk) / len(chunk) if chunk else None))
    return out


# --------------------------------------------------------------------------
# SVG 构件
# --------------------------------------------------------------------------
def _line_chart(series: list[tuple[float, float | None]], color: str,
                label: str, vmax: float, W: int = 900, H: int = 240) -> str:
    pad_l, pad_r, pad_t, pad_b = 46, 12, 14, 26
    pw = W - pad_l - pad_r
    ph = H - pad_t - pad_b
    t0 = series[0][0] if series else 0.0
    tN = series[-1][0] if series else 1.0
    span = (tN - t0) or 1.0
    vmax = max(vmax, 1.0)

    def x(t: float) -> float:
        return pad_l + ((t - t0) / span) * pw

    def y(v: float) -> float:
        return pad_t + ph - (max(0.0, min(vmax, v)) / vmax) * ph

    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMid meet" '
             f'role="img" aria-label="{label}">']
    # 网格 + Y 轴刻度
    for g in (0, 0.25, 0.5, 0.75, 1.0):
        yy = pad_t + ph - g * ph
        val = vmax * g
        parts.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{W-pad_r}" y2="{yy:.1f}" '
                     f'stroke="#2a3140" stroke-width="1"/>')
        parts.append(f'<text x="{pad_l-6}" y="{yy+3:.1f}" fill="#7d8694" '
                     f'font-size="10" text-anchor="end">{val:.0f}</text>')
    # X 轴首尾标签
    parts.append(f'<text x="{pad_l}" y="{H-8}" fill="#7d8694" font-size="10">'
                 f'{t0:.0f}s</text>')
    parts.append(f'<text x="{W-pad_r}" y="{H-8}" fill="#7d8694" font-size="10" '
                 f'text-anchor="end">{tN:.0f}s</text>')
    # 折线
    pts = " ".join(f"{x(t):.1f},{y(v):.1f}" for t, v in series if v is not None)
    parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.6" '
                 f'stroke-linejoin="round" points="{pts}"/>')
    parts.append('</svg>')
    return "".join(parts)


def _heatmap(matrix: list[list[float | None]], row_labels: list[str],
             times: list[float], W: int = 900, cell_h: int = 22,
             max_cols: int = 360) -> str:
    if not matrix:
        return '<p style="color:#7d8694">（无核心数据）</p>'
    m, tm = _aggregate_matrix(matrix, times, max_cols)
    n_rows = len(m)
    n_cols = len(m[0]) if m else 0
    pad_l = 150
    pw = W - pad_l - 10
    H = cell_h * n_rows + 18
    cw = pw / n_cols if n_cols else 1.0

    parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMid meet" '
             f'role="img" aria-label="heatmap">']
    for ri, row in enumerate(m):
        yy = 14 + ri * cell_h
        label = row_labels[ri] if ri < len(row_labels) else f"#{ri}"
        parts.append(f'<text x="{pad_l-8}" y="{yy+cell_h/2+3:.1f}" fill="#9aa6b8" '
                     f'font-size="10" text-anchor="end">{label}</text>')
        for ci, v in enumerate(row):
            xx = pad_l + ci * cw
            parts.append(f'<rect x="{xx:.1f}" y="{yy:.1f}" width="{cw+0.6:.1f}" '
                         f'height="{cell_h-1:.1f}" fill="{_heat(v)}"/>')
    # 时间轴
    if tm:
        parts.append(f'<text x="{pad_l}" y="{H-2}" fill="#7d8694" font-size="10">'
                     f'{tm[0]:.0f}s</text>')
        parts.append(f'<text x="{W-10}" y="{H-2}" fill="#7d8694" font-size="10" '
                     f'text-anchor="end">{tm[-1]:.0f}s</text>')
    parts.append('</svg>')
    return "".join(parts)


# --------------------------------------------------------------------------
# 报表装配
# --------------------------------------------------------------------------
def generate_report(csv_path: Path, out_html: Path | None = None) -> Path:
    csv_path = Path(csv_path)
    if out_html is None:
        out_html = csv_path.with_suffix(".html")
    out_html = Path(out_html)

    data = parse_csv(csv_path)
    times = data["times"]
    procs = data["procs"]
    proc_series = data["proc_series"]
    core_rows = data["core_rows"]
    gpu_series = data["gpu_series"]

    # 降采样长序列（曲线图点过多无意义）
    max_pts = 900
    proc_series_ds = {pid: _aggregate_series(s, max_pts) for pid, s in proc_series.items()}
    gpu_ds = _aggregate_series(gpu_series, max_pts)

    # 统计
    dur = (times[-1] - times[0]) if len(times) > 1 else 0.0
    proc_stats = []
    for col, exe, pid in procs:
        vals = [v for _, v in proc_series[pid] if v is not None]
        avg = sum(vals) / len(vals) if vals else 0.0
        mx = max(vals) if vals else 0.0
        proc_stats.append((exe, pid, avg, mx))

    gpu_vals = [v for _, v in gpu_series if v is not None]
    gpu_avg = sum(gpu_vals) / len(gpu_vals) if gpu_vals else 0.0
    gpu_max = max(gpu_vals) if gpu_vals else 0.0

    # 核心平均/最大序列
    core_avg_series: list[tuple[float, float | None]] = []
    core_max_series: list[tuple[float, float | None]] = []
    if core_rows and times:
        for ri in range(len(core_rows)):
            t = times[ri] if ri < len(times) else float(ri)
            row = core_rows[ri]
            valid = [v for v in row if v is not None]
            core_avg_series.append((t, sum(valid) / len(valid) if valid else None))
            core_max_series.append((t, max(valid) if valid else None))

    # ---- SVG 块 ----
    proc_line_colors = {pid: _series_color(i) for i, (_, _, pid) in enumerate(procs)}
    proc_line_svg = ""
    if procs:
        # 合并到一张图：各进程线 + 100 上限
        combined = []
        for _, _, pid in procs:
            combined.extend(proc_series_ds[pid])
        vmax = min(100.0, max([v for _, v in combined if v is not None] + [1.0]) * 1.1)
        # 逐进程分别画（便于各自配色），用同一坐标系
        pad_l, pad_r, pad_t, pad_b = 46, 12, 14, 26
        W, H = 900, 240
        pw = W - pad_l - pad_r
        ph = H - pad_t - pad_b
        t0 = times[0] if times else 0.0
        tN = times[-1] if times else 1.0
        span = (tN - t0) or 1.0
        svg_parts = [f'<svg viewBox="0 0 {W} {H}" width="100%" '
                     f'preserveAspectRatio="xMidYMid meet" aria-label="proc-lines">']
        for g in (0, 0.5, 1.0):
            yy = pad_t + ph - g * ph
            svg_parts.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{W-pad_r}" '
                             f'y2="{yy:.1f}" stroke="#2a3140" stroke-width="1"/>')
            svg_parts.append(f'<text x="{pad_l-6}" y="{yy+3:.1f}" fill="#7d8694" '
                             f'font-size="10" text-anchor="end">{vmax*g:.0f}</text>')
        for _, _, pid in procs:
            pts = " ".join(
                f"{pad_l+((t-t0)/span)*pw:.1f},"
                f"{pad_t+ph-(max(0.0,min(vmax,v))/vmax)*ph:.1f}"
                for t, v in proc_series_ds[pid] if v is not None)
            svg_parts.append(f'<polyline fill="none" stroke="{proc_line_colors[pid]}" '
                             f'stroke-width="1.6" stroke-linejoin="round" points="{pts}"/>')
        svg_parts.append('</svg>')
        proc_line_svg = "".join(svg_parts)

    legend_html = "".join(
        f'<span style="display:inline-flex;align-items:center;margin-right:14px">'
        f'<span style="width:11px;height:11px;border-radius:2px;display:inline-block;'
        f'background:{proc_line_colors[pid]};margin-right:5px"></span>'
        f'{exe} ({pid})</span>'
        for _, exe, pid in procs) or '<span style="color:#7d8694">（无进程数据）</span>'

    proc_heat = _heatmap(
        [[v for _, v in proc_series[pid]] for _, _, pid in procs],
        [f"{exe} ({pid})" for _, exe, pid in procs],
        times,
    ) if procs else '<p style="color:#7d8694">（无进程数据）</p>'

    core_labels = [f"核心 {i}" for i in range(data["n_cores"])] if data["n_cores"] else []
    core_heat = _heatmap(core_rows, core_labels, times) if core_rows else \
        '<p style="color:#7d8694">（无每核数据，录制时未加 --no-cores）</p>'

    core_line_svg = ""
    if core_avg_series:
        cavg = _aggregate_series(core_avg_series, max_pts)
        cmax = _aggregate_series(core_max_series, max_pts)
        allv = [v for _, v in cavg + cmax if v is not None]
        vmax = min(100.0, max(allv + [1.0]) * 1.1)
        core_line_svg = (
            _line_chart(cavg, "#7fd1ff", "平均核心", vmax)
            + '<div style="height:6px"></div>'
            + _line_chart(cmax, "#ff6b6b", "最大核心", vmax)
        )

    gpu_svg = _line_chart(gpu_ds, "#ffd166", "GPU", 100.0) if gpu_ds else \
        '<p style="color:#7d8694">（无 GPU 数据）</p>'

    # ---- 统计卡片 ----
    stat_cards = []
    stat_cards.append(_card("录制时长", f"{dur:.0f}s"))
    stat_cards.append(_card("游戏进程数", f"{len(procs)}"))
    stat_cards.append(_card("GPU 平均", f"{gpu_avg:.1f}%"))
    stat_cards.append(_card("GPU 峰值", f"{gpu_max:.1f}%"))
    for exe, pid, avg, mx in proc_stats:
        stat_cards.append(_card(f"{exe} CPU", f"均{avg:.1f}% / 峰{mx:.1f}%"))

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>降压标定报表 · {csv_path.stem}</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ background:#0e1116; color:#c8d2e0; font-family:-apple-system,"Segoe UI",
         "Microsoft YaHei",sans-serif; margin:0; padding:24px; }}
  h1 {{ font-size:20px; margin:0 0 4px; color:#e8eef7; }}
  .sub {{ color:#7d8694; font-size:12px; margin-bottom:18px; }}
  .cards {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:20px; }}
  .card {{ background:#161b23; border:1px solid #232b38; border-radius:8px;
          padding:10px 14px; min-width:120px; }}
  .card .k {{ color:#7d8694; font-size:11px; }}
  .card .v {{ color:#e8eef7; font-size:18px; font-weight:600; margin-top:2px; }}
  section {{ background:#121721; border:1px solid #1d2531; border-radius:10px;
            padding:16px; margin-bottom:16px; }}
  section h2 {{ font-size:14px; margin:0 0 10px; color:#9fd0ff; font-weight:600; }}
  .legend {{ font-size:12px; color:#9aa6b8; margin-bottom:8px; }}
  .note {{ color:#6b7686; font-size:11px; margin-top:6px; }}
</style></head><body>
<h1>降压标定报表</h1>
<div class="sub">源文件：{csv_path.name} · 生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
<div class="cards">{''.join(stat_cards)}</div>

<section><h2>① 各游戏进程 CPU 占用率（曲线图）</h2>
  <div class="legend">{legend_html}</div>
  {proc_line_svg}
  <div class="note">纵轴为该进程 %Processor Time 按总逻辑核数归一化（0–100%）。</div>
</section>

<section><h2>② 各游戏进程 CPU 占用率（热力图）</h2>
  {proc_heat}
  <div class="note">行=进程，列=时间点；颜色越红占用越高。若仅一个进程即单进程录制。</div>
</section>

<section><h2>③ 系统 CPU 各核心占用率（热力图）</h2>
  {core_heat}
  <div class="note">行=逻辑核心（系统级，非逐进程），列=时间点。</div>
</section>

<section><h2>④ 系统 CPU 核心占用率（曲线图）</h2>
  {core_line_svg}
  <div class="note">蓝=全部核心均值，红=单核最大值，用于对照“重载”阈值判定。</div>
</section>

<section><h2>⑤ GPU 占用率（曲线图）</h2>
  {gpu_svg}
  <div class="note">来自系统 GPU Engine 计数器。</div>
</section>
</body></html>
"""
    out_html.write_text(html, encoding="utf-8")
    return out_html


def _card(k: str, v: str) -> str:
    return (f'<div class="card"><div class="k">{k}</div>'
            f'<div class="v">{v}</div></div>')


def main() -> int:
    ap = argparse.ArgumentParser(description="将负载录制 CSV 转为自包含 HTML 报表。")
    ap.add_argument("csv", help="measure_game_load.py 产出的 CSV 路径")
    ap.add_argument("--out", default=None, help="HTML 输出路径（默认 CSV 同名 .html）")
    args = ap.parse_args()
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"[report] CSV 不存在: {csv_path}", file=sys.stderr)
        return 2
    out = generate_report(csv_path, Path(args.out) if args.out else None)
    print(f"[report] 生成 -> {out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # pragma: no cover
        print(f"[report] FATAL: {e}", file=sys.stderr)
        sys.exit(1)
