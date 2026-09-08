# PS1 新建磁贴区块 & 分页逻辑深度分析

> 源文件：`blocks/tile.ps1` (New-TileBlock §534-559, New-TileBlockPanel §78-131)
> `ds8ZZZ.ps1` (Create-NewPage §4059-4070, Invoke-NewPage §6154-6160, Build-EditLayout §5362-5564)

---

## 一、新建磁贴区块 — `New-TileBlock`

### 1.1 触发入口

```powershell
# ds8ZZZ.ps1:5988-5997 — 右上角按钮创建
$btnNewTileBlock = New-Object RoundedButton
$btnNewTileBlock.Text     = "◫"
$btnNewTileBlock.Size     = New-Object System.Drawing.Size(44, 32)
$btnNewTileBlock.Add_Click({ New-TileBlock })
$pnlEdit.Controls.Add($btnNewTileBlock)
```

按钮在编辑器右上角，位于"＋分页"按钮左侧。点击调用 `New-TileBlock`。

### 1.2 `New-TileBlock` 主函数 (tile.ps1:534-559)

```powershell
function New-TileBlock {
    # 1) 生成唯一名称
    $blockCount = $global:ExtraTileBlocks.Count + 1
    $usedNames = @($global:ExtraTileBlocks | ForEach-Object { $_.BlockName })
    if ($lblTileTitle.Text -notin $usedNames) { $usedNames += $lblTileTitle.Text }
    [string]$newBlockName = "新区块_$blockCount"
    while ($newBlockName -in $usedNames) { $blockCount++; $newBlockName = "新区块_$blockCount" }

    # 2) 创建完整面板（见 §1.3）
    $block = New-TileBlockPanel -BlockName $newBlockName -BlockThemeColor "" -ShowBlockTitle $true

    # 3) 重建编辑器 UI
    $pnlEdit.SuspendLayout()
    try { Build-EditLayout }
    finally { $pnlEdit.ResumeLayout($false) }

    # 4) 选中新创建的区块行
    $newIdx = $global:AllCards.Count - 1
    for ($i = 0; $i -lt $global:EditCardPanels.Count; $i++) {
        $row = $global:EditCardPanels[$i]
        $d = $row.Tag
        if ($d -is [PSCustomObject] -and $d.GlobalIdx -eq $newIdx) {
            $global:EditSelCardIdx = $i
            Update-EditHighlight
            try { $pnlEdit.ContentPanel.ScrollControlIntoView($row) } catch {}
            break
        }
    }
}
```

**关键步骤分析**：

| 步骤 | 代码 | 说明 |
|------|------|------|
| 1. 计算序号 | `$blockCount = $global:ExtraTileBlocks.Count + 1` | 从已有额外区块数量 + 1 开始 |
| 2. 收集已用名 | `$usedNames = @($global:ExtraTileBlocks \| ForEach-Object { $_.BlockName })` | 遍历所有已存在的额外区块 |
| 3. 生成名称 | `"新区块_$blockCount"` | 如果冲突则递增直到唯一 |
| 4. 创建面板 | `New-TileBlockPanel -BlockName $newBlockName ...` | 创建完整 UI + 数据 |
| 5. 重建编辑器 | `Build-EditLayout` | 刷新整个编辑器，新行出现在列表中 |
| 6. 选中新行 | 遍历 `EditCardPanels` 找到 `GlobalIdx == AllCards.Count - 1` | 高亮并滚动到新行 |

### 1.3 `New-TileBlockPanel` — 完整区块面板创建 (tile.ps1:78-131)

```powershell
function New-TileBlockPanel {
    param([string]$BlockName = "新区块",
          [string]$BlockThemeColor = "",
          [bool]$ShowBlockTitle = $true)

    # ═══ UI 层 ═══
    $newPanel = New-Object RoundedPanel                    # 创建面板控件
    $newPanel.Size = New-Object System.Drawing.Size(528, 300)
    $newPanel.BackColor  = $global:BgCard                  # 卡片背景色
    $newPanel.GradientTop = $global:GradCardTop            # 渐变顶
    $newPanel.GradientBottom = $global:GradCardBottom      # 渐变底
    $newPanel.Visible = $false                             # 初始隐藏

    $newTitle = New-Object System.Windows.Forms.Label      # 标题标签
    $newTitle.Text = "► $BlockName"
    $newTitle.ForeColor = ...
    $newPanel.Controls.Add($newTitle)

    $newGrid = New-Object System.Windows.Forms.Panel       # 磁贴网格容器
    $newGrid.Size = New-Object System.Drawing.Size(500, 240)
    $newPanel.Controls.Add($newGrid)

    # ═══ 数据层 ═══
    $newTiles = [System.Collections.ArrayList]::new()      # 空磁贴列表

    $block = [PSCustomObject]@{                            # 区块数据结构
        Panel           = $newPanel
        Title           = $newTitle
        Grid            = $newGrid
        Tiles           = $newTiles                        # ← 每个区块独立的磁贴列表！
        TileButtons     = @()                              # ← 每个区块独立的按钮列表！
        BlockName       = $BlockName
        BlockThemeColor = $BlockThemeColor
        ShowBlockTitle  = $ShowBlockTitle
    }

    # ═══ 注册到全局 ═══
    $pnlScroll.Controls.Add($newPanel)                     # 添加到滚动容器
    Register-CardMouseSelect $newPanel                     # 注册鼠标选择
    $newPanel.Add_Resize({ param($s,$e) Layout-TilePanel $s })  # 自适应重绘

    $global:ExtraTileBlocks += $block                      # 追加到额外区块列表
    $global:AllCards += $newPanel                          # 追加到全局卡片列表
    $global:CardTypes[$newPanel] = 'tile'                  # 标记卡片类型
    $global:CardNames[$newPanel] = "► $BlockName"          # 记录显示名

    return $block
}
```

**关键设计**：

| 层次 | 内容 | 作用 |
|------|------|------|
| **UI 层** | `RoundedPanel` + `Label` + `Panel(Grid)` | 在 HUD 中渲染一个完整的可视卡片 |
| **数据层** | `PSCustomObject{Tiles, TileButtons, BlockName, ...}` | 每个区块有自己独立的磁贴数组和按钮数组 |
| **全局注册** | `$global:ExtraTileBlocks`, `$global:AllCards`, `$global:CardTypes`, `$global:CardNames` | 将新区块注册到全局数据中，参与排序/显示/隐藏/页面分配 |

### 1.4 数据流全景

```
用户点击 "◫" 按钮
  └─→ New-TileBlock
        ├─→ 计算唯一名称 "新区块_N"
        ├─→ New-TileBlockPanel
        │     ├─→ 创建 RoundedPanel UI（528×300，背景/渐变/标题/网格）
        │     ├─→ 创建空磁贴列表 ArrayList
        │     ├─→ 组装 PSCustomObject 数据块
        │     ├─→ 注册到 pnlScroll.Controls（主滚动容器）
        │     ├─→ 追加到 $global:ExtraTileBlocks
        │     ├─→ 追加到 $global:AllCards
        │     └─→ 标记 $global:CardTypes['tile']
        ├─→ $pnlEdit.SuspendLayout()
        ├─→ Build-EditLayout（重建编辑器所有行）
        ├─→ $pnlEdit.ResumeLayout()
        └─→ 选中新行（高亮 + 滚动到可视区域）
```

### 1.5 Python 端口对应

```python
def _new_tile_block(self) -> None:
    from ..core.config import ExtraTileBlock
    cfg = self._app.config_manager.config

    # 1) 生成唯一名称
    extra = list(getattr(cfg, "extra_tile_blocks", []) or [])
    used_names = {eb.block_name for eb in extra}
    n = len(extra) + 1
    block_name = f"新区块_{n}"
    while block_name in used_names:
        n += 1; block_name = f"新区块_{n}"

    # 2) 创建 ExtraTileBlock（数据层等价物）
    new_eb = ExtraTileBlock(
        block_name=block_name,
        block_theme_color="Rose",
        show_block_title=True,
    )
    cfg.extra_tile_blocks = extra + [new_eb]

    # 3) 注册到布局
    if "tile" not in cfg.cards:
        cfg.cards = list(cfg.cards) + ["tile"]

    # 4) 保存 + 重建
    self._app.save_all()
    self._render_rows()           # 编辑器行重建
    self._app._window.rebuild()   # HUD 面板重建
```

---

## 二、分页 — `Create-NewPage` + `Invoke-NewPage`

### 2.1 触发入口

```powershell
# ds8ZZZ.ps1:5979-5985 — 右上角按钮创建
$btnNewPage = New-Object RoundedButton
$btnNewPage.Text     = "＋"
$btnNewPage.Size     = New-Object System.Drawing.Size(44, 32)
$btnNewPage.Add_Click({ Invoke-NewPage })
$pnlEdit.Controls.Add($btnNewPage)
```

按钮在编辑器右上角，"新建磁贴区块"按钮右侧。

### 2.2 `Invoke-NewPage` (ds8ZZZ.ps1:6154-6160)

```powershell
function Invoke-NewPage {
    $pnlEdit.SuspendLayout()
    try {
        Create-NewPage | Out-Null        # 创建新页面数据
        Build-EditLayout                 # 立即重建编辑器 UI
    } finally { $pnlEdit.ResumeLayout($false) }
}
```

这是一个薄包装层：创建数据 → 重建 UI。

### 2.3 `Create-NewPage` — 核心逻辑 (ds8ZZZ.ps1:4059-4070)

```powershell
function Create-NewPage {
    # 创建新的空页面，有且只能最多存在一个空页面
    # 空页面保存（Apply-EditLayout）后自动消失

    # ═══ 守卫：只允许一个空页面 ═══
    if ($global:EmptyPageSlot) { return $false }

    [int]$allCnt = $global:AllCards.Count

    # ═══ 设置空页面标记 ═══
    $global:EmptyPageSlot = $true

    # ═══ 在末尾追加空页面分隔点 ═══
    $newBreaks = @($global:PageBreaks) + @($allCnt)

    # ═══ 去重排序 ═══
    $global:PageBreaks = @($newBreaks | Sort-Object -Unique)

    # ═══ 确保首项永远是 0 ═══
    if ($global:PageBreaks[0] -ne 0) {
        $global:PageBreaks = @(0) + $global:PageBreaks
    }

    return $true
}
```

**关键逻辑分析**：

| 步骤 | 代码 | 说明 |
|------|------|------|
| ① 守卫 | `if ($global:EmptyPageSlot) { return $false }` | 全局最多一个空页面，防止无限创建空白页 |
| ② 标记 | `$global:EmptyPageSlot = $true` | 设置标志，`Build-EditLayout` 据此渲染空页面占位 |
| ③ 追加 | `$newBreaks = @($global:PageBreaks) + @($allCnt)` | 在当前位置（行尾）之后断开 |
| ④ 排序去重 | `$newBreaks \| Sort-Object -Unique` | 保证 `PageBreaks` 严格递增且无重复 |
| ⑤ 首项修正 | `if ($global:PageBreaks[0] -ne 0) { @(0) + ... }` | 第 0 页永远从索引 0 开始 |

### 2.4 空页面的渲染（Build-EditLayout 中）

```powershell
# ds8ZZZ.ps1:5505-5530
if ($global:EmptyPageSlot) {
    [int]$lastBreak = $global:PageBreaks[-1]
    if ($lastBreak -lt $global:AllCards.Count) {
        # 空页面已被卡片填入 → 清除标记
        $global:EmptyPageSlot = $false
    }
}
if ($global:EmptyPageSlot) {
    [int]$emptyPg = $totalPg - 1
    # 渲染页面分隔条
    $sep = New-PageSeparator $emptyPg $rowW
    ...
    # 渲染占位文字
    $emptyRow = New-Object System.Windows.Forms.Label
    $emptyRow.Text = "（空页面 · 拖入卡片或保存后自动消失）"
    $emptyRow.ForeColor = [System.Drawing.Color]::FromArgb(80, 80, 90)
    $emptyRow.Font = New-Object System.Drawing.Font($fontFamily, 11, [System.Drawing.FontStyle]::Italic)
    ...
}
```

**智能检测**：如果 `PageBreaks[-1] < AllCards.Count`，说明已有卡片被移到了空页面，自动清除 `EmptyPageSlot`。

### 2.5 保存时的处理（退出编辑模式）

```powershell
# ds8ZZZ.ps1:6012-6015 — Exit-EditModeInternal
$global:EmptyPageSlot = $false       # 清除空页面标记
$pnlEdit.Visible = $false            # 隐藏编辑器
$global:EditMode = $false            # 退出编辑模式
# ... 随后 Refresh-UI / Adjust-MainLayout 应用新布局
```

保存时：
- `EmptyPageSlot` 被重置为 `false`
- 若空页面无卡片 → 编辑器中已标记但不会进入主页面
- 若用户拖了卡片进去 → `Break` 位置生效，新页面产生

### 2.6 数据流全景

```
用户点击 "＋" 按钮
  └─→ Invoke-NewPage
        ├─→ Create-NewPage
        │     ├─→ 检查 EmptyPageSlot（已存在则拒绝）
        │     ├─→ 设置 EmptyPageSlot = true
        │     ├─→ PageBreaks 追加 AllCards.Count
        │     ├─→ Sort-Object -Unique（排序去重）
        │     └─→ 确保 PageBreaks[0] == 0
        └─→ Build-EditLayout
              ├─→ 渲染每页分隔条 + 卡片行
              └─→ 检测 EmptyPageSlot：渲染空页面占位符

用户保存（应用编辑）
  └─→ Exit-EditModeInternal
        ├─→ EmptyPageSlot = false
        ├─→ 新 PageBreaks 写入 layout.json
        ├─→ Refresh-UI → Adjust-MainLayout
        └─→ 后续主页面按新分页渲染
```

### 2.7 Python 端口对应

```python
def _add_page_break(self) -> None:
    # ① 守卫：只允许一个空页面
    if getattr(self, "_empty_page_slot", False):
        toast("已存在空页面，请先拖入卡片或保存")
        return

    self._empty_page_slot = True

    # ② 追加当前卡片总数
    pos = len(self.order)
    if pos not in self._page_breaks:
        self._page_breaks.append(pos)

    # ③ Sort + Unique + 首项保证 0
    self._page_breaks = sorted(set(self._page_breaks))
    if not self._page_breaks or self._page_breaks[0] != 0:
        self._page_breaks = [0] + self._page_breaks

    # ④ 立即重建编辑器
    self._render_rows()
```

---

## 三、全局数据结构关系

### 3.1 PS1 数据全景

```
$global:AllCards          ← 所有区块面板的数组（含 rtss/freeze/..., 含多个 tile 面板）
$global:PageBreaks        ← 分页索引数组 [0, 6, 12] 表示第0页0-5, 第1页6-11, 第2页12+
$global:ExtraTileBlocks   ← 额外创建的磁贴区块数组（不含首个 tile 面板）
$global:TileConfigs       ← 磁贴数据数组（每个磁贴的 Name/Path/Col/Row/...）
$global:CardTypes         ← Hashtable: 面板 → 区块类型字符串
$global:CardNames         ← Hashtable: 面板 → 显示名称字符串
$global:CardColorOverrides← Hashtable: 面板 → 自定义颜色
$global:EmptyPageSlot     ← 布尔: 是否存在未填卡片的空页面
$global:EditCardPanels    ← 编辑器中的行控件数组（Build-EditLayout 产物）
$global:EditSelCardIdx    ← 编辑器中当前选中的行索引
```

### 3.2 多区块共存示意

```
┌─────────────────────────────────────────────┐
│ $pnlScroll (主滚动容器)                     │
│                                             │
│  ┌─ RoundedPanel (rtss)                     │
│  ├─ RoundedPanel (freeze)                   │
│  ├─ RoundedPanel (power)                    │
│  ├─ RoundedPanel (ls)                       │
│  ├─ RoundedPanel (procmon)                  │
│  ├─ RoundedPanel (主 tile 面板)  ← 首个     │
│  │   ├─ Title: "► 主磁贴"                  │
│  │   ├─ Grid: [TileBtn][TileBtn]...         │
│  │   └─ Tiles: [Tile, Tile, ...]           │
│  ├─ RoundedPanel (新区块_1)  ← New-TileBlock│
│  │   ├─ Title: "► 新区块_1"                │
│  │   ├─ Grid: (空)                          │
│  │   └─ Tiles: []                           │
│  └─ RoundedPanel (新区块_2)  ← New-TileBlock│
│      ├─ Title: "► 新区块_2"                │
│      ├─ Grid: [TileBtn]                     │
│      └─ Tiles: [Tile]                       │
└─────────────────────────────────────────────┘

AllCards = [rtss面板, freeze面板, ..., 主tile面板, 新区块_1面板, 新区块_2面板]
PageBreaks = [0, 6]   → 第0页: 0-5, 第1页: 6+
ExtraTileBlocks = [新区块_1数据, 新区块_2数据]
```

### 3.3 页面分页逻辑

```
AllCards (8个):
  索引 0: rtss       ─┐
  索引 1: freeze      │ 第0页 (PageBreaks[0]=0
  索引 2: power       │   → PageBreaks[1]-1=5)
  索引 3: ls          │
  索引 4: procmon     │
  索引 5: 主tile     ─┘
  ─────────────────────  PageBreaks[1]=6
  索引 6: 新区块_1   ─┐
  索引 7: 新区块_2    │ 第1页 (PageBreaks[1]=6
                       │   → AllCards.Count-1=7)
                      ─┘
```

---

## 四、关键差异总结：单个磁贴 vs 磁贴区块

| 概念 | PS1 术语 | 含义 | 按钮 |
|------|----------|------|------|
| **磁贴** (Tile) | `$global:TileConfigs[i]` | 一个可点击的启动项（名称/路径/图标/位置） | 编辑器 "＋磁贴" |
| **磁贴区块** (Tile Block) | `$global:ExtraTileBlocks[j]` | 一个完整的 UI 面板（标题/颜色/磁贴网格），可包含多个磁贴 | 编辑器 "◫ 新建磁贴区块" |

**每个磁贴区块**拥有：
- 独立的面板控件 (`RoundedPanel`)
- 独立的标题（`► 区块名`）
- 独立的磁贴列表（`Tiles: ArrayList`）
- 独立的按钮渲染器（`TileButtons: Array`）
- 独立的主题颜色（`BlockThemeColor`）
- 独立的分页位置（由在 `AllCards` 中的索引决定）

**新建一个磁贴区块**意味着创建上述全部内容，而非在已有区块中添加一个磁贴。
