# PS1 button-editor.ps1 按钮列表显示与渲染逻辑分析

> 源文件: `D:\下载\localsend\凝冰结魄-覆盖任务栏-修复-完全解耦\buttons\button-editor.ps1`
> 行数: 1245 行
> 分析日期: 2026-07-19

---

## 一、架构概览

`button-editor.ps1` 是一个**独立模块**，被 `ds8ZZZ.ps1` 通过 dot-source 引入。它定义了两个主要编辑表单：

| 表单 | 函数 | 用途 | 窗口大小 |
|------|------|------|----------|
| **L2 风格编辑器** | `Show-ButtonStyleEditor` (line 51) | 管理全局按钮风格（`$global:ButtonStyles`） | 760×640 |
| **L1 按钮编辑器** | `Show-PerButtonEditor` (line 694) | 管理单个按钮的颜色覆盖/预设（`PerButtonOverrides`） | 770×620 |

**核心特征**：两个编辑器的按钮列表均使用 **纯文本 `System.Windows.Forms.ListBox`**，**没有任何视觉克隆渲染**。这与此前的 Python 端口（clone RoundedButton/QPushButton 到列表中）有本质区别。

---

## 二、按钮注册机制（ds8ZZZ.ps1）

### 2.1 `Scan-AllButtons`（line 3201–3219）

```powershell
function global:Scan-AllButtons {
    $global:RegisteredButtons = [System.Collections.ArrayList]::new()
    try { _ScanCtrlRecursive $form } catch {}
}
function global:_ScanCtrlRecursive($parent) {
    if (-not $parent -or $parent.IsDisposed) { return }
    foreach ($c in $parent.Controls) {
        if ($c -is [RoundedButton]) {
            if ($global:RegisteredButtons -notcontains $c) {
                [void]$global:RegisteredButtons.Add($c)
            }
        }
        if ($c.Controls -and $c.Controls.Count -gt 0) {
            _ScanCtrlRecursive $c
        }
    }
}
```

- **收集对象**：只收集 `[RoundedButton]` 类型（包括其子类如 TileButton）
- **收集方式**：递归遍历主窗体（`$form`）的完整控件树
- **去重**：`-notcontains` 检查，已存在的跳过
- **结构**：**扁平 `ArrayList`，0-based 索引**——不按区块分组，不区分磁贴/冻结/其他


### 2.2 `RegisteredButtons` 使用场景

L1 按钮编辑器通过 `$global:RegisteredButtons` 访问所有已注册按钮：

```powershell
# 列表填充 — 遍历所有按钮
foreach ($pbBtn2 in $global:RegisteredButtons) { ... }

# 选中某一项 — 按索引获取实际按钮控件
$myT = $global:RegisteredButtons[$myBI]

# 检查边界
if ($myBI -lt 0 -or $myBI -ge $global:RegisteredButtons.Count) { return }
```

---

## 三、L2 风格编辑器列表（`Show-ButtonStyleEditor`）

### 3.1 列表控件与布局

```
┌─────────────────────────────────────────────────┐
│  760 × 640 (FixedDialog)                        │
│  ┌───────────────┐  ┌───────────────────────┐   │
│  │ Left (280px)  │  │ Right (420px)         │   │
│  │               │  │                       │   │
│  │ 风格列表      │  │ 属性 (28px↓)          │   │
│  │ ┌───────────┐ │  │ 名称 [TextField]      │   │
│  │ │ ListBox   │ │  │ 切角 [TrackBar+value] │   │
│  │ │ (280×160) │ │  │ 圆角 [TrackBar+value] │   │
│  │ │ 纯文本    │ │  │ 边框 [TrackBar+value] │   │
│  │ └───────────┘ │  │ □仅悬停边框 □悬停渐变  │   │
│  │               │  │ □链接主题             │   │
│  │ 实时预览      │  │                       │   │
│  │ ┌───────────┐ │  │ 11 颜色色板 (4×4)    │   │
│  │ │RoundedBtn │ │  │                       │   │
│  │ │ (200×36)  │ │  ├─── 操作按钮 ────────┤   │
│  │ └───────────┘ │  │ 新建 复制 删除 应用   │   │
│  │               │  │ 全部应用  保存        │   │
│  └───────────────┘  └───────────────────────┘   │
└─────────────────────────────────────────────────┘
```

### 3.2 列表数据

```powershell
# Line 82-94: ListBox 创建
$script:StyleList = New-Object System.Windows.Forms.ListBox
$script:StyleList.Size = New-Object System.Drawing.Size(280, 160)
$script:StyleList.Font = New-Object System.Drawing.Font("Microsoft YaHei", 11, [FontStyle]::Bold)
$script:StyleList.BackColor = [System.Drawing.Color]::FromArgb(38,40,48)  # 深色
$script:StyleList.ForeColor = [System.Drawing.Color]::White
```

- **数据源**：`$script:EditorStyles.Keys`（风格名称列表）
- **刷新函数**：`Refresh-StyleList`（line 549–551）

```powershell
function Refresh-StyleList {
    $script:StyleList.Items.Clear()
    foreach ($key in $script:EditorStyles.Keys) {
        [void]$script:StyleList.Items.Add($key)
    }
}
```

- **初始填充**：表单构造末尾调用 `Refresh-StyleList`（line 544）
- **增量刷新**：新建/复制/删除操作后调用 `Refresh-StyleList`（line 510, 530, 541）

### 3.3 选择处理链

```
用户点击 ListBox 条目
  │
  ├─ SelectedIndexChanged 事件触发
  │
  ├─ Save-CurrentEdit（保存当前编辑到 EditorStyles[oldName]）
  │    └─ 将 TrackBar/Swatch/CheckBox 值写回哈希表
  │
  └─ Load-StyleToEditor（加载新选中项到界面控件）
       ├─ 从 EditorStyles[name] 读取所有属性
       ├─ 设置 TrackBar.Value, CheckBox.Checked
       ├─ 更新 11 个色板 .BackColor
       ├─ Update-ColorSwatchesEnabled
       └─ Update-PreviewFromEditors（更新预览 RoundedButton）
            ├─ 根据 ChkLinkTheme 决定颜色来源
            │    ├─ linkTheme=true → Get-StyleDefaultColors
            │    └─ linkTheme=false → 读取 Swatch 控件 .BackColor
            ├─ RoundedButton.SetColors(back, hover, border, fore, hoverFore)
            ├─ RoundedButton.SetActiveColors(ab, abb, ib, ibb)
            └─ RoundedButton.Invalidate()
```

### 3.4 关键：预览按钮的文字

```powershell
# Line 128-133: 预览按钮创建
$script:PreviewBtn = New-Object RoundedButton
$script:PreviewBtn.Text = "预览"    # ← 固定的标签文字
$script:PreviewBtn.Size = 200×36
$script:PreviewBtn.Location = (40, 17)  # 在 previewPanel 内居中（panel 280px，按钮200px）
```

- 预览按钮的文字固定为 `"预览"`，**不跟随列表选择变化**
- `RoundedButton` 在 PS1 中是一个自绘制控件，其 `Text` 属性通过 `OnPaint` 绘制在按钮内部

---

## 四、L1 按钮编辑器列表（`Show-PerButtonEditor`）

### 4.1 列表控件与布局

```
┌─────────────────────────────────────────────────────┐
│  770 × 620 (FixedDialog)                            │
│  ┌─────────────────┐  ┌─────────────────────────┐   │
│  │ Left (340px)    │  │ Right (390px)           │   │
│  │                 │  │                         │   │
│  │ 已注册按钮      │  │ 覆盖颜色                │   │
│  │ ┌─────────────┐ │  │ 未选择按钮              │   │
│  │ │ ListBox     │ │  │                         │   │
│  │ │ (340×280)   │ │  │ 预设管理               │   │
│  │ │ 纯文本条目   │ │  │ [预设下拉] 保存 删除   │   │
│  │ └─────────────┘ │  │                         │   │
│  │                 │  │ □背景 □悬停 □边框       │   │
│  │ 当前样式预览     │  │ □前景 □悬停前景        │   │
│  │ ┌─────────────┐ │  │ (5 个颜色色板)          │   │
│  │ │RoundedBtn   │ │  │                         │   │
│  │ │ (240×36)    │ │  ├─── 操作按钮 ──────────┤ │
│  │ └─────────────┘ │  │ 应用   重置   关闭     │   │
│  │                 │  │                         │   │
│  │ [刷新列表] 提示  │  │                         │   │
│  └─────────────────┘  └─────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

### 4.2 列表条目格式

```powershell
# Line 1146–1157: 列表填充
$pbBtnList.Items.Clear()
[int]$pbI = 0
foreach ($pbBtn2 in $global:RegisteredButtons) {
    if ($pbBtn2 -and -not $pbBtn2.IsDisposed) {
        $pbT = $pbBtn2.Text; if (-not $pbT) { $pbT = "(空)" }
        $pbM = if ($global:PerButtonOverrides.ContainsKey("$pbI")) { " [*]" } else { "" }
        [void]$pbBtnList.Items.Add("$pbI : $pbT$pbM")
    } else {
        [void]$pbBtnList.Items.Add("$pbI : (已释放)")
    }
    $pbI++
}
```

条目格式：`"索引 : 按钮文本[覆盖标记]"`

| 索引 | 按钮文本 | 覆盖标记 | 示例 |
|------|---------|---------|------|
| 0 | `RoundedButton.Text` | `[*]` 如有覆盖 | `"0 : 确定"` |
| 1 | `RoundedButton.Text` | `[*]` 如有覆盖 | `"1 : 取消 [*]"` |
| 2 | — | — | `"2 : (已释放)"` |

### 4.3 刷新按钮

```powershell
# Line 1159–1172: "刷新列表" 按钮 Click 事件
$pbBtnRefresh.Add_Click({
    $global:__pbEditor.BtnList.Items.Clear()
    [int]$pbJ = 0
    foreach ($pbBtn3 in $global:RegisteredButtons) {
        # ... 同上逻辑，重扫 RegisteredButtons ...
    }
})
```

- "刷新列表" 按钮位于左栏底部（120×28），用于重新扫描 `RegisteredButtons` 重建列表
- 不再次调用 `Scan-AllButtons`——`RegisteredButtons` 由主窗口在必要时更新

### 4.4 选择处理链

```powershell
# Line 1174–1232: SelectedIndexChanged 事件
$pbBtnList.Add_SelectedIndexChanged({
    $stt  = $global:__pbEditor
    $myBI = $stt.BtnList.SelectedIndex          # ← 获取选中索引

    if ($myBI 超出范围) {
        # 清空所有色板为灰色，预设设为 "（无预设）"
        return
    }

    # 1. 获取实际按钮控件
    $myT = $global:RegisteredButtons[$myBI]
    $myTxt = $myT.Text
    $stt.LblInfo.Text = "按钮 #$myBI : $myTxt"  # ← 更新信息标签

    # 2. 更新实时预览按钮
    $stt.LivePrevBtn.Text = "$myI : $myTxt"     # ← 预览按钮文字 = 按钮的 Text
    $stt.LivePrevBtn.CutSize = $myT.CutSize
    $stt.LivePrevBtn.CornerRadius = $myT.CornerRadius
    $stt.LivePrevBtn.BorderThickness = $myT.BorderThickness
    # 颜色来自按钮的当前实际颜色属性
    try { $stt.LivePrevBtn.SetColors($myT.BackColor, $myT.BackColor,
                                      $myT.BorderColor, $myT.ForeColor,
                                      [System.Drawing.Color]::White) } catch {}
    $stt.LivePrevBtn.Invalidate()

    # 3. 加载覆盖数据到色板
    $myOv2 = $global:PerButtonOverrides["$myBI"]
    foreach ($cf in $stt.Fields) {
        $myHas = ($myOv2 -and $myOv2[$cf] -and $myOv2[$cf] -ne "00000000")
        $stt.Chks[$cf].Checked = $myHas
        if ($myHas) {
            $stt.Swatches[$cf].BackColor = HexToColor $myOv2[$cf]
        } else {
            $stt.Swatches[$cf].BackColor = [System.Drawing.Color]::Gray
        }
    }

    # 4. 刷新预设下拉列表
    $stt.PresetCbo.Items.Clear()
    [void]$stt.PresetCbo.Items.Add("(无预设)")
    foreach ($pk in $global:PerButtonPresets.Keys) {
        $parts = $pk.Split(":", 2)
        if ($parts[0] -eq "$myBI") { [void]$stt.PresetCbo.Items.Add($parts[1]) }
    }
    # 选中当前激活的预设（如有）
})
```

### 4.5 预览按钮说明

```powershell
# Line 764-772: 预览按钮创建
$pbLivePreviewBtn = New-Object RoundedButton
$pbLivePreviewBtn.Text = "预览"                  # ← 初始文字
$pbLivePreviewBtn.Size = New-Object System.Drawing.Size(240, 36)
$pbLivePreviewBtn.Location = (50, 12)            # ← 在 340×62 面板内居中
$pbLivePreviewBtn.BorderThickness = 2
$pbLivePreviewBtn.TabStop = $false               # ← 不接受 Tab 焦点
```

- 选中列表项时，预览按钮的 `Text` 更新为选中的按钮文本（行 1195）
- 预览按钮的 `SetColors` 使用选中按钮的实际背景/边框/前景色（行 1199）
- 预览按钮不可焦点（`TabStop = $false`）

---

## 五、PS1 与 Python 端口关键差异对照

| 维度 | PS1 button-editor.ps1 | Python button_edit.py（当前） |
|------|----------------------|------------------------------|
| **列表控件类型** | `ListBox`（纯文本） | 自定义 `QFrame` 组件（含克隆控件） |
| **按钮视觉呈现** | **无**——列表中只有文字条目 | 字节拷贝克隆（`_clone_button` → QPushButton/TileButton/RoundedButton） |
| **文字位置** | 在列表条目文本中（如 `"0 : 确定"`） | 在克隆按钮内部（如 QPushButton 原生文字） |
| **按钮分组** | **无分组**——扁平索引（0..N） | 按 `block_type` 分组（含组头 `QLabel`） |
| **按钮过滤** | **不过滤**——显示全部注册的 RoundedButton | tile 块排除、freeze 块限 2 个代表 |
| **列宽** | ListBox 固定宽度 340px，文字可能截断 | 克隆按自然宽度（`stretch=0`），meta 右对齐 |
| **预览** | 单个独立 RoundedButton（200×36 / 240×36） | 每个列表项单独克隆控件 |
| **悬停效果** | ListBox 原生蓝色选择高亮 | 克隆按钮原生悬停动画（enterEvent/leaveEvent） |
| **选择方式** | 点击 ListBox 条目 | 点击克隆按钮（connected `clicked` signal） |
| **编辑器定位** | 独立 Dialog 弹窗（`FixedDialog`） | 内嵌子页（EditOverlay `QStackedWidget`） |
| **color swatches** | 11 个 `Panel`（带 `Click` 事件 → 打开 `ColorDialog`） | 11 个 `QFrame`（带 `mousePressEvent` → 打开 `QColorDialog`） |
| **滑块** | `TrackBar`（3 个，带 `value_label`） | `QSlider`（3 列并排，带 `value_label`） |
| **LinkTheme** | 选中后自动加载主题预设颜色并禁用色板 | 同（`_chk_link` toggle → `Toggle-GradientSwatches` 效果） |

---

## 六、关键设计决策总结

### 6.1 列表条目的设计思路

PS1 的按钮列表**完全不需要渲染视觉克隆**，因为：

1. **按钮已经有文字标识**：`RoundedButton.Text` 属性存储了按钮的语义文字（如 "确定"、"取消"、"加载" 等），足以在列表中识别按钮身份
2. **颜色信息通过覆盖标记传达**：`[*]` 标记已足够说明该按钮有颜色覆盖，不需要渲染实际颜色
3. **预览功能由独立控件承担**：左侧的预览 RoundedButton（`LivePrevBtn`）集中处理视觉预览，列表中的每个条目不需要单独渲染
4. **WinForms ListBox 的限制**：ListBox 本身不支持嵌入自定义控件，只能显示纯文本

### 6.2 Python 端口的设计偏离

Python 端口在以下方面对 PS1 原始设计做了扩展：

- **克隆渲染**：通过 `_clone_button` 直接复制源按钮的 QColor 属性和几何属性到列表中——这是 PS1 没有的，但提供了更好的视觉反馈
- **按区块分组**：PS1 按平坦索引排列，Python 按 `block_type` 分块并加了组头 `QLabel`
- **显示过滤**：PS1 显示全部，Python 过滤 tile/freeze
- **内嵌子页**：PS1 用独立 `Form`（dialog），Python 用 `EditOverlay` 内的 `QStackedWidget` 子页

### 6.3 列表条目宽度

PS1 的 ListBox 有固定宽度（280px 或 340px），所有条目在统一列宽内左对齐排列。每个条目是单行文本。如果按钮文本超过列宽，会被截断。

Python 端口当前的 HBox 布局（`addWidget(clone, 0, AlignVCenter)` + `addLayout(meta, 0)`）实现了按钮按自然宽度显示，meta 信息右对齐——这在概念上更接近"按钮列表"的语义（每个按钮保持其实际比例），而非 PS1 的"文本索引目录"。
