# `button-editor.ps1` 逻辑与编辑实现分析

> 配套源文件：`C:\Users\13567\WorkBuddy\2026-07-09-20-21-04\button-editor.ps1`
> 目标：为 Python 端口（`ningbingjue`）实现等价的「按钮风格编辑器」提供逻辑蓝本。

---

## 一、定位与整体架构

`button-editor.ps1` 实现了原版 `Show-ButtonStyleEditor` 函数：一个**独立 WinForms 窗口**（`System.Windows.Forms.Form`，`FormBorderStyle=FixedDialog`），用于集中编辑所有 RoundedButton 的视觉风格。

它的设计是**两层预设体系（Two-Layer Preset System）**：

| 层级 | 变量 | 含义 | 作用域 |
|------|------|------|--------|
| **L2 整体风格** | `$global:ButtonStyles` | 全局按钮风格字典，按风格名索引 | 应用到**全部**按钮（经 `Set-GlobalButtonStyle`） |
| **L1 定制预设** | `$global:PerButtonPresets` | 单按钮/单组预设字典，按预设名索引 | 绑定到**特定按钮**（经 `ButtonPresetMap`） |
| **绑定映射** | `$global:ButtonPresetMap` | 按钮索引 → 预设名 | 决定哪些按钮用哪个 L1 预设 |
| **当前激活** | `$global:ActiveButtonStyle` | 当前生效的 L2 风格名 | 全局默认风格 |

窗口左右分栏：

- **左侧（Canvas）**：遍历 `$global:RegisteredButtons`，按区块（LS预设/锁帧控制/电源设置/进程冻结/页面布局编辑/其他）分组，渲染每个按钮的**迷你预览**（复制其真实颜色/形状），选中项加黄色边框，已绑定预设的显示青色 `[预设名]`。
- **右侧（Controls）**：整体风格选择 + 定制预览 + 定制预设列表 + 属性编辑面板。

---

## 二、核心数据结构

### 2.1 L2 风格条目（`$global:ButtonStyles[$name]`）

```powershell
@{
    CutSize           = 8      # 切角（0-30）
    CornerRadius      = 0      # 圆角（0-20）
    BorderThickness   = 3      # 边框厚度（0-6）
    HoverOnlyBorder   = $false # 仅悬停显示边框
    HoverGradient     = $false # 悬停渐变色
    LinkTheme         = $false # 链接主题颜色（跟随主题自动变）
    BackColor         = "101418"
    HoverColor        = "1A202A"
    BorderColor       = "CC66A0"
    ForeColor         = "CC66A0"
    HoverForeColor    = "FFFFFF"
    GradientHoverTop  = "2A3550"
    GradientHoverBottom = "0D1015"
    ActiveBackColor   = "1A2535"
    ActiveBorderColor = "CC66A0"
    InactiveBackColor = "0A0D10"
    InactiveBorderColor = "1A2530"
}
```

**11 个颜色字段**（`$script:ColorFields`）：
`BackColor, HoverColor, BorderColor, ForeColor, HoverForeColor, GradientHoverTop, GradientHoverBottom, ActiveBackColor, ActiveBorderColor, InactiveBackColor, InactiveBorderColor`

### 2.2 L1 预设条目（`$global:PerButtonPresets[$presetName]`）

结构与 L2 风格一致（几何 + 11 色 + 三个开关），由「保存为定制预设」从当前属性编辑器值生成。

### 2.3 绑定映射（`$global:ButtonPresetMap`）

`键 = 按钮在 RegisteredButtons 中的索引（字符串）`，`值 = 预设名`。
```powershell
$global:ButtonPresetMap["12"] = "预设#3"   # 第 12 个按钮绑定到 预设#3
```

---

## 三、关键函数与执行流程

### 3.1 打开编辑器 `Show-ButtonStyleEditor`

1. `Copy-ButtonStyles`：深拷贝 `$global:ButtonStyles` 到 `$script:EditorStyles`（编辑器工作副本，避免改一半污染全局）。
2. 记录 `$script:_IsLight`（当前主题是否为 girlpink 浅色）。
3. 构建左侧 Canvas 与右侧控件，填充风格下拉框。
4. 自动选中当前 `ActiveButtonStyle` 并 `LoadSE` 载入属性。
5. `RenderCanvas` 渲染全部按钮预览。
6. `ShowDialog()` 模态显示。

### 3.2 画布渲染 `RenderCanvas`

- 遍历 `$global:RegisteredButtons`，用 `GetBlockLabel` 推断每个按钮所属区块（沿父链匹配文本/Label/Block 属性：`RTSS|锁帧|FPS`→锁帧控制，`LS|Lossless`→LS预设，`Power|电源|APU`→电源设置，`Freeze|冻结`→进程冻结，`Layout|布局|Tile`→页面布局编辑，`进程|Process|CPU`→进程监控，`EXIT|退出`→主页面顶端）。
- 按固定顺序（`LS预设, 锁帧控制, 电源设置, 进程冻结, 页面布局编辑, 其他`）分组，空组跳过。
- 每组一个标题 `>> 区块名 (数量)`，其下用 `FlowLayoutPanel` 横向排列迷你 `RoundedButton`：
  - 文本取原按钮 `Text`（超 10 字截断）。
  - 颜色/形状从原按钮 `normalBackColor/hoverBackColor/BorderColor/CutSize/CornerRadius/BorderThickness` 复制。
  - 选中项（`$s.SelBtn`）边框变黄、厚度 2。
  - 若 `ButtonPresetMap["$idx"]` 存在，文本追加 `[预设名]`、前景青色。
  - 点击 → 设置 `$s.SelBtn`、刷新画布、刷新预览、`LoadPresetForSelected`。

### 3.3 属性编辑面板

- **名称**：`txtNm`（编辑的是风格/预设名）。
- **三个滑块**：切角(0–30)、圆角(0–20)、边框(0–6)，`ValueChanged` → `ApplyToPreview`（实时更新预览按钮）。
- **三个复选框**：链接主题颜色 / 仅悬停显示边框 / 悬停渐变色，`CheckedChanged` → `ApplyToPreview`。
- **11 个色块**：每个是一块 `Panel`，点击弹出 `ColorDialog`，选色后 `ApplyToPreview`。
- **底部四个按钮**：
  - `保存为定制预设`（`bS1`）：把编辑器当前值存为一个新的 L1 预设（名取按钮文本，避免重名）。
  - `覆盖当前预设`（`bS2`）：写回列表当前选中的 L1 预设。
  - `提交到当前风格`（`bS3`）：`SaveCE` + `CommitAndSave`（写回 L2 风格并落盘）。
  - `应用并刷新画布`（`bS4`）：`SaveCE`+`CommitAndSave`+`Set-GlobalButtonStyle`+`RenderCanvas`。

### 3.4 实时预览 `ApplyToPreview`

用属性编辑器的 11 色块 + 3 滑块值，调用预览按钮的 `SetColors(...)` 与 `SetActiveColors(...)`，并设置 `CutSize/CornerRadius/BorderThickness`。`HoverGradient` 关闭时渐变顶/底传 `Transparent`。

### 3.5 载入/保存助手

| 函数 | 作用 |
|------|------|
| `LoadSE` | 把选中的 L2 风格载入属性编辑器（滑块/复选/色块） |
| `LoadPE` | 把选中的 L1 预设载入属性编辑器 |
| `SavePE($pn)` | 把属性编辑器值写入名为 `$pn` 的 L1 预设 |
| `SaveCE` | 把属性编辑器值写回当前 L2 风格 |
| `CommitAndSave` | 把 `EditorStyles` 全量写回 `$global:ButtonStyles`，设置 `ActiveButtonStyle`，`Save-ButtonStylesToFile` |
| `RefreshPL` | 刷新 L1 预设列表框 |
| `RenderPresetPreview` | 在预览按钮上显示选中的 L1 预设 |
| `Set-GlobalButtonStyle` | 把某 L2 风格应用到全部 RegisteredButtons |

### 3.6 绑定

- `绑定到选中按钮`（`bBd`）：把当前选中的 L1 预设绑定到画布中当前选中的按钮索引，`Save-ButtonPresetMap`，并立即 `SetColors` 应用到该真实按钮。
- `取消绑定`（`bUb`）：从 `ButtonPresetMap` 移除该索引，`Save-ButtonPresetMap`，`RenderCanvas`。

---

## 四、持久化

| 数据 | 文件 | 函数 |
|------|------|------|
| L2 全局风格 + 激活名 | `buttons/button-styles.json`（`__Active__` 键存激活名） | `Save-ButtonStylesToFile` / `Load-ButtonStylesFromFile` |
| L1 单按钮预设 | `buttons/button-presets.json` | `Save-PerButtonPresets` / 加载 |
| 绑定映射 | `buttons/button-preset-map.json` | `Save-ButtonPresetMap` / `Load-ButtonPresetMap` |

加载失败均 `try/catch` 静默吞掉（**注意**：原版不保留原始文本，Python 端口已在 `ConfigManager.load` 改进为保留 `last_error_raw`，此处 JSON 也应遵循同样原则）。

---

## 五、对 Python 端口的映射建议

| PS1 | Python 端口对应 |
|-----|----------------|
| `$global:ButtonStyles` | `core/color/styles.py::BUTTON_STYLES`（已存在，需接通 load/save） |
| `$global:ActiveButtonStyle` | `ACTIVE_STYLE_NAME`（已存在） |
| `$global:PerButtonPresets` | **新增** `PER_BUTTON_PRESETS: dict[str, ButtonStyle]` |
| `$global:ButtonPresetMap` | **新增** `BUTTON_PRESET_MAP: dict[str, str]` |
| `$global:RegisteredButtons` | 改为收集窗口内 `RoundedButton`（含 `TileButton`）+ 功能面板 `QPushButton`，按 `panel.block_type` 分组 |
| `Show-ButtonStyleEditor`（弹窗） | **内嵌** `ButtonEditPanel`（`QStackedWidget` 子页，非对话框），与布局/磁贴/进程编辑同级的独立模式 |
| `Set-GlobalButtonStyle` | `apply_styles_to_buttons(buttons)`：先应用激活 L2 风格，再按 `BUTTON_PRESET_MAP` 叠加 L1 预设 |
| `RenderCanvas` | `ButtonEditPanel.render_canvas()`：按区块分组渲染迷你预览，支持点选绑定 |
| `ColorDialog` 取色 | `QColorDialog` |
| 11 色块 / 3 滑块 / 3 复选框 | `ButtonEditPanel` 属性编辑区 |
| 四个底部按钮 | 对应「保存为定制预设 / 覆盖当前预设 / 提交到当前风格 / 应用并刷新画布」 |
| `buttons/*.json` 持久化 | `app.save_all()` 中调用 `save_*_to_file`，`load_config` 中调用 `load_*_from_file` |

### 关键差异与注意点

1. **无需弹窗**：用户明确要求按钮编辑窗口不要新建窗口，直接显示在页面内、作为和「各功能卡片编辑」类似的独立界面。因此 `ButtonEditPanel` 应作为 `EditOverlay` 的一个 `QStackedWidget` 子页（类似 `TileEditPanel`/`ProcMonEditPanel`），而非 `QDialog`。
2. **分组依据**：Python 端口中功能面板按钮多为原生 `QPushButton`（带文字），`RoundedButton` 本身不渲染文字。画布预览应同时收集两类按钮，按 `panel.block_type` 分组；L1 预设对 `RoundedButton` 用 `SetColors`，对 `QPushButton` 用样式表（背景/边框/前景）生效。
3. **绑定键稳定性**：PS1 用全局按钮索引；Python 端口每次重建会新建按钮对象，故绑定键采用 `(block_type, 同区块内序号)`，顺序确定性保证重建/重启后键稳定。
4. **重建后重应用**：`app_window.rebuild()` 重建面板后，需对所有 `RoundedButton` 重新应用激活风格 + L1 预设，否则新建区块/重建后按钮会丢失自定义风格。

---

## 六、功能清单（验收对照）

- [x] L2 整体风格：选择 / 新建 / 复制 / 删除 / 应用
- [x] L1 定制预设：新建 / 复制 / 删除
- [x] 绑定：把 L1 预设绑定到指定按钮 / 解绑
- [x] 画布预览列表：按区块分组展示全部按钮，可点选
- [x] 属性编辑：名称 / 切角 / 圆角 / 边框 / 链接主题 / 仅悬停边框 / 悬停渐变 / 11 色（含激活态与未激活态颜色）
- [x] 实时预览：任何改动立即反映到预览按钮
- [x] 持久化：风格 / 预设 / 绑定映射均落盘并可回放
- [x] 内嵌非弹窗：作为 EditOverlay 独立子页
