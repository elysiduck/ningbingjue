# 凝冰结魄 — 颜色系统 Python 实现报告

> 基于原 PS1 脚本 `colors.ps1`（1150行）、`button-editor.ps1`（849行）
> 以及《凝冰结魄-颜色配置系统技术剖析.md》报告

---

## 一、已实现的核心颜色体系

### 1.1 基础色板 → `core/color/palette.py`
- 9个区块角色色：`COLOR_BLUE`/`GREEN`/`RED`/`PEACH`/`MAUVE`/`TEAL`/`YELLOW`/`ORANGE`/`CYAN`
- 背景/前景色：`BG_APP`(14,15,19)、`BG_CARD`(28,30,38)、`FG_PRIMARY`(230,255,30)
- 梯度/聚焦色：`FOCUS_GLOW_WIDTH`=3

### 1.2 调色板API → `core/color/api.py`
- `get_color_derivative(Color, ratio, base)` — 线性混合派生（等价 `Get-ColorDerivative`）
- `get_button_state(Role, style, light)` — 返回 `{Back, Hover, Border, Fore}` (等价 `Get-ButtonState`)
- `get_theme_color(semantic, Role, Style)` — 语义颜色统一入口 (等价 `Get-ThemeColor`)

### 1.3 10 主题系统 → `core/color/themes.py`
| 主题 | 类型 | 条目数 |
|------|------|--------|
| 樱粉 | 暗色 | 20 |
| **少女粉** | **浅色(唯一)** | 29 |
| 原夜 | 暗色(默认) | 6 |
| 暗金/暮紫/深海/幽林/碳黑/琥珀/墨蓝 | 暗色 | 5-6 |

所有主题共享基 accent dict（`_base_accent()`，34键），各 `switch` 分支覆盖特色键。

### 1.4 按钮风格引擎 → `core/color/styles.py`
- 4 种预设风格：`暗黑科技`(切角8/圆角0/边框4)、`柔和圆角`(0/14/2)、`极简线条`(0/0/0)、`霓虹边框`(4/0/5)
- `ButtonStyle` dataclass：切角/圆角/边框/仅悬停边框/悬停渐变/链接主题 + 11色键
- `apply_button_style(ctrl, style)` — 等价 `Apply-ButtonStyle`
- `set_global_button_style(name)` — 等价 `Set-GlobalButtonStyle`
- JSON 持久化：`load_button_styles_from_file`/`save_button_styles_to_file`
- 光/暗主题自适应：`get_style_default_colors` 按 `girlpink` 切换粉彩底

### 1.5 主题应用流程
```
apply_theme(name)
  → get_theme_accent_colors(name)
  → STATE.theme_accent_colors = accent
  → update_freeze_highlight_color()
  → ThemeManager.apply(name)
    → 遍历 BlockRegistry
    → 每个block.hook("OnThemeChange")(accent)
```

---

## 二、按钮编辑器实现（布局编辑面板内）

### 2.1 风格选择
- `QComboBox` 列出 4 种风格，`currentTextChanged` 自动加载编辑器值

### 2.2 几何属性滑块
- **切角** (0-30)：`_cut_slider` → `ButtonStyle.cut_size`
- **圆角** (0-20)：`_corner_slider` → `ButtonStyle.corner_radius`
- **边框** (0-6)：`_border_slider` → `ButtonStyle.border_thickness`

### 2.3 颜色编辑
- 4 个关键色字段：背景(BackColor)、悬停(HoverColor)、边框(BorderColor)、前景(ForeColor)
- `QLineEdit` 输入6位十六进制色值

### 2.4 复选框
- **链接主题**：颜色跟随主题自动变化
- **仅悬停边框**：平时无边框
- **悬停渐变**：悬停时渐变效果

### 2.5 保存流程
```
_save() → _on_btn_geo_changed() → _apply_btn_style()
  → set_global_button_style(name)
  → save_button_styles_to_file(path)
  → apply_layout(order, visible, theme, style)
  → save_all()
```

---

## 三、呼吸灯自定义

### 3.1 速度控制
- `BreathingController(speed=50)` — 构造时接受 speed 参数
- speed 10(快) → 15ms 间隔，speed 100(慢) → 60ms 间隔
- `app_window.py` 从 `config.breath_speed` 读取并传入

### 3.2 编辑器滑块
- `QSlider(10-100)`：`valueChanged` → `_breath_changed(val)` → `config.breath_speed = val` → `save_all()`
- 手柄导航：nudge lambda `value + d*5`，范围限制 10-100

---

## 四、焦点光标优化

### 4.1 样式
- 从暗淡蓝 `rgba(127,209,255,0.10)` → 荧光黄 `rgba(230,255,30,0.15)` + `border: 2px solid #e6ff1e`
- 匹配 `$global:FgPrimary` (230,255,30) ZZZ 酸性荧光黄

### 4.2 层级
- 焦点矩形不再 `raise_()`，属于 scrollable content widget 子控件，自然在 toast/对话框之下

---

## 五、与原 PS1 的对照

| 特性 | PS1 实现 | Python 实现 | 状态 |
|------|---------|-------------|------|
| 基础色板 | `$global:ColorX` (9色) | `palette.py` COLOR_* | ✅ |
| 调色板API | 7个API函数 | `api.py` get_color_derivative/get_button_state | ✅ |
| 10主题 | `Get-ThemeAccentColors` switch | `themes.py` get_theme_accent_colors | ✅ |
| 语义颜色 | `Get-ThemeColor` (21语义) | `api.py` get_theme_color | ✅ |
| 按钮风格 | `ButtonStyles` + `button-editor.ps1` | `styles.py` ButtonStyle + edit_overlay按钮编辑面板 | ✅ |
| 呼吸灯 | `$breathSpeed` + scanline.ps1 | `breathing.py` BreathingController(speed) + 滑块 | ✅ |
| 焦点光标 | `SelectionBorderColor` = FgPrimary | `_FOCUS_STYLE` #e6ff1e 荧光黄 | ✅ |
| 主题切换 | `Apply-ThemeAccents` → `OnThemeChange` | `ThemeManager.apply()` → block hook | ✅ |
| 手柄提示色 | `SetHintColor` A/B/X/Y | `gamepad_hint.py` HINT_COLORS | ✅ |
| 每按钮预设 | `PerButtonPresets` + `ButtonPresetMap` | 风格级编辑（预设待后续） | ⚠️ 基础 |

---

## 六、未迁移特性（优先级排序）

1. **每按钮预设绑定** (`PerButtonPresets` / `ButtonPresetMap`) — 按钮编辑器中的"绑定到选中按钮"功能
2. **编辑器画布** (`RenderCanvas`) — 显示所有已注册按钮及其风格
3. **完整11色块编辑** — 当前仅编辑4个主色（Back/Hover/Border/Fore），省略了 HoverFore/GradientTop/Bottom/Active/Inactive
4. **色调分离导出** — `Export-ThemePalette` / hex 批量导出
5. **冻结高亮实时预览** — `Update-FreezeHighlightColor` 在编辑器中的视觉反馈
