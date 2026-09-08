# 凝冰结魄 (ningbingjue) Python 端口 — 代码结构与功能梳理

> 分析日期: 2026-07-19
> 源项目: ds8ZZZ.ps1 (PowerShell) + blocks/*.ps1 + buttons/button-editor.ps1
> Python 端口规模: 34 个源文件（不含测试）

---

## 一、项目架构总览

```
ningbingjue/
├── src/ningbingjue/
│   ├── __init__.py              # 包入口，版本 0.1.0
│   ├── app.py                   # Application 编排器（启动/配置/boot）
│   ├── core/                    # 核心引擎层
│   │   ├── config.py            # 配置管理（layout.json 加载/保存）
│   │   ├── registry.py          # Block 插件注册表
│   │   ├── theme.py             # 主题管理器（10 套主题）
│   │   ├── timers.py            # 后台定时器 + 呼吸灯数学
│   │   ├── navigation.py        # 导航状态机（page/zone/item-mode）
│   │   └── color/               # 颜色子系统
│   │       ├── __init__.py
│   │       ├── rgb.py           # Color 类型（不可变 ARGB）
│   │       ├── palette.py       # 9 种角色颜色基础调色板
│   │       ├── api.py           # 统一颜色派生 API（7 个 API）
│   │       ├── state.py         # 可变颜色/主题全局状态
│   │       ├── themes.py        # 10 套完整主题定义
│   │       ├── semantic.py      # 语义颜色 API
│   │       └── styles.py        # 按钮风格引擎（L2/L1 两层预设系统）
│   ├── blocks/                  # 功能模块层 — 对应 PS1 blocks/*.ps1
│   │   ├── base.py              # Block 基类 + STANDARD_HOOKS
│   │   ├── rtss.py              # 帧率限制 (RtssBlock)
│   │   ├── procmon.py           # 进程监控 (ProcMonBlock)
│   │   ├── freeze.py            # 进程冻结 (FreezeBlock)
│   │   ├── ls.py                # Lossless Scaling (LSBlock)
│   │   ├── power.py             # Ryzen APU 电源 (PowerBlock)
│   │   └── tile.py              # 启动磁贴 (TileBlock)
│   ├── ui/                      # PyQt6 自定义绘制 UI 层
│   │   ├── app_window.py        # Overlay 主窗口 + BlockPanel 聚合
│   │   ├── button_edit.py       # 按钮风格编辑器（L2+L1 统一面板）
│   │   ├── edit_overlay.py      # 布局编辑器 + 磁贴编辑器 + 进程监控编辑器
│   │   ├── panels.py            # 每个 Block 的交互式面板
│   │   ├── rounded_button.py    # 自定义切角多边形按钮
│   │   ├── tile_button.py       # 磁贴按钮（RoundedButton 子类）
│   │   ├── interaction.py       # 统一交互层（NavItem + 焦点光标）
│   │   ├── gamepad.py           # XInput 手柄后端
│   │   ├── gamepad_hint.py      # 手柄提示徽标
│   │   ├── breathing.py         # 呼吸灯动画
│   │   ├── slider.py            # Slider 组件
│   │   └── rounded_panel.py     # RoundedPanel 组件
│   └── platform/                # 平台适配层
│       ├── base.py              # ExternalAdapter ABC
│       ├── rtss_cli.py          # RTSS CLI 适配器
│       ├── lossless_scaling.py  # Lossless Scaling 适配器
│       ├── window.py            # 窗口管理
│       ├── process_manager.py   # 进程管理器
│       ├── ryzenadj.py          # RyzenAdj 适配器
│       └── battery.py           # 电池状态读取
└── tests/                       # pytest 测试套件（~25 个测试文件）
```

## 二、按钮编辑器功能对照

### 2.1 ButtonEditPanel（button_edit.py, 126-966 行）

PS1 对应：`Show-ButtonStyleEditor` + `Show-PerButtonEditor`（两个函数合并为一个统一面板）

| 功能区 | PS1 实现 | Python 实现 |
|--------|---------|------------|
| L2 风格列表 | ListBox（纯文本） | QComboBox（下拉框） |
| L2 风格操作 | 新建/复制/删除按钮 | 同（4 个 QPushButton） |
| 定制预览 | 独立 RoundedButton（200×36，左栏） | 独立 RoundedButton（220×30，控件区） |
| L1 预设列表 | 无（在 PerButtonEditor 中） | QListWidget（固定高度 88px） |
| L1 预设操作 | 新建/复制/删除 | 同 + 绑定/解绑 |
| 按钮预览列表 | PerButtonEditor：ListBox 纯文本 `"索引 : 文本 [*]"` | **QFrame 中渲染克隆 QPushButton/TileButton**（扩展设计） |
| 属性编辑 | 3 个 TrackBar + 11 个 Panel（色板）+ 3 个 Checkbox | 3 个 QSlider（3 列并排）+ 11 个 QFrame（色板）+ 3 个 QCheckBox |
| 颜色选择 | 点击 Panel → ColorDialog | 点击 QFrame → QColorDialog |
| 底部按钮 | 保存为预设 / 覆盖 / 提交 / 应用 | 同（4 个 QPushButton） |
| 风格持久化 | Commit-AndSaveStyles → Save-ButtonStylesToFile | `_commit_style` → `_persist` → `save_all_button_data` |

### 2.2 styles.py 功能模块

| 函数 | PS1 对应 | 描述 |
|------|---------|------|
| `ButtonStyle` dataclass | PS1 hashtable `@{CutSize=...}` | 按钮风格数据结构 |
| `get_style_default_colors` | `Get-StyleDefaultColors` | 按风格名+主题返回默认颜色 |
| `apply_button_style` | `Apply-ButtonStyle` | 推送到单个 duck-typed 控件 |
| `set_global_button_style` | `Set-GlobalButtonStyle` | 应用到所有注册按钮 |
| `apply_styles_to_buttons` | 扩展功能 | L2 风格 + L1 预设叠加 |
| `bind_button_preset` | PS1 `ActivePerButtonPreset[$idx] = name` | 绑定按钮 key → 预设 |
| `unbind_button_preset` | PS1 `ActivePerButtonPreset.Remove($idx)` | 取消绑定 |
| `save_all_button_data` | `Save-ButtonStylesToFile` + 预设文件 | 三文件持久化 |
| `load_all_button_data` | `Load-ButtonStylesFromFile` + 预设文件 | 三文件加载 |
| `apply_per_button_preset` | PS1 per-button 覆盖逻辑 | 应用 L1 预设到单个按钮 |

### 2.3 已完全移植的功能

| 功能 | 状态 |
|------|------|
| 4 个默认 L2 按钮风格（暗黑科技/柔和圆角/极简线条/霓虹边框） | ✅ |
| 风格新建/复制/删除 | ✅ |
| 11 个颜色字段编辑（BackColor ~ InactiveBorderColor） | ✅ |
| 3 个几何滑块（切角/圆角/边框）+ 3 个 Checkbox | ✅ |
| LinkTheme 自动加载主题颜色 | ✅ |
| 定制预览 RoundedButton（实时） | ✅ |
| L1 预设（PER_BUTTON_PRESETS）保存/加载/应用 | ✅ |
| 按钮绑定映射（BUTTON_PRESET_MAP） | ✅ |
| 数据持久化（三文件：button-styles.json, button-presets.json, button-preset-map.json） | ✅ |
| 按钮预览列表（含克隆渲染 + 分组头 + 过滤规则） | ✅ |
| 列表项悬停/点击交互 | ✅ |
| 上下排列布局（适配窄页面） | ✅ |
| 视图过滤（tile 排除 + freeze ≤2 代表） | ✅ |
| 滚动条隐藏 + 预览列表边框 | ✅ |
| freeze 按钮排除风格应用 | ✅ |
| 定制预览按钮居中 | ✅ |
| 定制预览↔定制预设区块互换 | ✅ |

## 三、`collect_button_groups` 收集机制

```python
# 收集流程
collect_button_groups(window, exclude=edit_overlay)
  → findChildren(RoundedButton)  # 含 TileButton 子类
  → findChildren(QPushButton)
  → 按 block_type 分组（_BLOCK_ORDER 排序）
  → 跳过 exclude 子树（编辑器自身控件）
  → 返回 OrderedDict[block_type] → [(key, btn), ...]
  # key 格式: "block_type#local_index"（确定性，跨 rebuild 稳定）
```

## 四、样式应用链路

```
启动 / rebuild:
  app_window._apply_button_styles()
    → collect_button_groups(win, exclude=edit_overlay)
    → apply_styles_to_buttons(live, key_map)
      → 先 L2: _apply_style_to_ctrl(btn, ACTIVE_STYLE)
      → 后 L1: apply_per_button_preset(btn, BUTTON_PRESET_MAP[key])

按钮编辑器操作:
  提交风格: _commit_style() → 写 BUTTON_STYLES → _persist()
  应用并刷新: _apply_and_refresh() → _commit_style() → apply_styles_to_buttons → render_canvas()
  绑定预设: _bind() → bind_button_preset(key, preset) → apply_per_button_preset(btn, preset)
```

## 五、数据持久化文件

| 文件 | 路径 | 格式 |
|------|------|------|
| button-styles.json | `{script_root}/buttons/button-styles.json` | L2 风格字典 + ACTIVE_STYLE_NAME |
| button-presets.json | `{script_root}/buttons/button-presets.json` | L1 预设字典 |
| button-preset-map.json | `{script_root}/buttons/button-preset-map.json` | 按钮 key → 预设绑定映射 |

## 六、待识别：PS1 功能对照

以下功能在 PS1 中存在，需要确认 Python 端口是否覆盖：

| PS1 功能 | Python 端口状态 |
|----------|---------------|
| `Copy-ButtonStyles` 深拷贝 | `_build_default_styles` 替代 |
| `Show-ButtonStyleEditor` L2 风格编辑器 | `ButtonEditPanel`（统一面板） ✅ |
| `Show-PerButtonEditor` L1 独立编辑器 | 简化到 `ButtonEditPanel` L1 部分 ✅ |
| `Get-StyleDefaultColors` 主题颜色 | `get_style_default_colors` ✅ |
| `Load-ThemeColors` 加载主题色到色板 | `_chk_link` toggle → `Load-ThemeColors` 逻辑 |
| `Scan-AllButtons` 扫描注册按钮 | `collect_button_groups`（按需）— 无全局注册表 |
| `Set-GlobalButtonStyle` 全局应用 | `apply_styles_to_buttons` ✅ |
| `PerButtonOverrides` 单按钮覆盖 | `PER_BUTTON_PRESETS` + `BUTTON_PRESET_MAP` ✅ |
| `Save-CurrentEdit` 保存编辑 | `_commit_style` + `_save_preset` ✅ |
| `Refresh-StyleList` 刷新风格列表 | `refresh()` ✅ |
| `Update-PreviewFromEditors` 实时预览 | `_apply_to_preview` ✅ |
| PS1 `RoundedButton` 控件（含 Text/SetColors/paintEvent） | `rounded_button.py` ✅ |
| PS1 `TileButton` 控件（含图标/磁贴名） | `tile_button.py` ✅ |
