# PowerShell tile.ps1 深度分析报告

> 源文件：`blocks/tile.ps1`（约 1070 行）
> 定位：凝冰结魄项目磁贴区块插件

---

## 一、脚本整体架构与执行流程

### 1.1 模块分层

```
tile.ps1
├─ §1 全局变量与 UI 控件声明（L1-64）
│   ├─ 数据层：$global:TileConfigs, $global:ExtraTileBlocks, $global:SerializedTileConfigs
│   ├─ UI 层：$pnlTiles, $lblTileTitle, $pnlTileGrid, TileButton[]
│   └─ 状态层：$global:TileFocusIdx, $global:TileToastControl, $global:LastTileLaunchTime
│
├─ §2 数据管理函数（L67-131）
│   ├─ Get-TileBlockByCard：卡片 → ExtraTileBlock 映射
│   └─ New-TileBlockPanel：创建完整磁贴区块（UI + 数据 + 全局注册）
│
├─ §3 磁贴配置工厂（L133-197）
│   ├─ Find-FirstEmptySlot：AABB 碰撞检测找空位
│   └─ New-TileConfig：创建磁贴数据对象（含自动类型推断）
│
├─ §4 启动与反馈（L199-273）
│   ├─ Invoke-TileLaunch：磁贴启动（含 1.5s 防抖）
│   └─ Show-TileLaunchToast：1.2s 自动消失的启动提示
│
├─ §5 布局引擎（L275-479）
│   ├─ Layout-TileGrid：按 Col/Row 排列 TileButton 到网格
│   └─ Layout-TilePanel：自适应面板高度 + 标题同步
│
├─ §6 编辑器入口（L481-559）
│   ├─ Show-TileManager：进入磁贴编辑模式（备份/隐藏卡片/错误恢复）
│   └─ New-TileBlock：创建新磁贴区块（唯一名称 + 自动选中）
│
├─ §7 通用数据层（L561-629）
│   ├─ Get-TileEditTiles：获取当前正在编辑的磁贴列表
│   ├─ Save-TileConfigs：序列化到 $global:SerializedTileConfigs (JSON)
│   └─ Load-TileConfigs：从 JSON 反序列化重建 TileConfigs
│
├─ §8 编辑器属性面板（L631-807）
│   ├─ Load-TileToEditor：选中磁贴 → 填充属性表单
│   └─ Save-TileFromEditor：属性表单 → 写回磁贴对象
│
├─ §9 编辑网格预览（L809-906）
│   ├─ Add-TileWheelForward：滚轮事件转发到编辑器滚动
│   └─ Refresh-BlockPreview：可拖拽的二维磁贴预览网格
│
├─ §10 编辑器卡片构造（L908-1040+）
│   ├─ Update-PropFieldsVisibility：控制属性面板显隐
│   └─ New-TileEditBlockSettingsCard：区块设置卡片（名称/主题色）
│
└─ §11 Block 注册（文件末尾）
    └─ Register-Block 五字段注册到 BlockRegistry
```

### 1.2 执行流程全景

```
应用启动
  └─→ Load-TileConfigs (JSON → TileConfigs ArrayList)
  └─→ Layout-TilePanel (主面板 + 所有 ExtraTileBlocks)
  └─→ 注册到 BlockRegistry (OnRefresh / OnThemeChange / OnSaveConfig 等)

用户点击磁贴
  └─→ TileButton.Add_Click → Invoke-TileLaunch
        ├─ 1.5s 防抖检查
        ├─ URL/dir/exe/lnk 类型分发 → Start-Process
        └─ Show-TileLaunchToast (1.2s 中间浮现)

用户进入编辑 (E键 / 编辑按钮)
  └─→ Show-TileManager
        ├─ Save-TileFromEditor (保存当前编辑内容)
        ├─ 备份 AllCards / PageBreaks / CardColors / TileConfigs
        ├─ 隐藏所有 HUD 卡片
        ├─ 显示编辑器 pnlEdit (全窗口)
        └─ Build-EditLayout → 三卡片编辑界面
              ├─ New-TileEditBlockSettingsCard (区块名称/颜色)
              ├─ Refresh-BlockPreview (拖拽排序网格)
              └─ 属性表单 (名称/路径/图标/尺寸/颜色)
```

---

## 二、核心功能与处理逻辑

### 2.1 数据模型

#### TileConfig（单个磁贴）
```powershell
[PSCustomObject]@{
    Name          = "新磁贴"       # 显示名称
    LaunchPath    = ""             # 启动目标（URL/EXE/路径）
    LaunchType    = "FILE"         # URL|DIR|EXE|BAT|CMD|LNK|PS1|IMG|FILE
    IconPath      = ""             # 自定义图标路径
    Width         = 1              # 1-2 列宽
    Height        = 1              # 1-2 行高
    Col           = 0              # 网格列位置
    Row           = 0              # 网格行位置
    BlockThemeColor = ""           # 区块主题颜色名（Rose/Teal/Peach/...）
    BlockName     = ""             # 所属区块名称
    ShowBlockTitle = $true         # 是否显示区块标题
    TileNameColor = "255,255,255"  # 磁贴名称颜色 RGB
    ShowTileName  = $true          # 是否显示磁贴名称
}
```

#### ExtraTileBlock（完整磁贴区块）
```powershell
[PSCustomObject]@{
    Panel          = RoundedPanel  # UI 面板控件
    Title          = Label         # 标题标签 "► 区块名"
    Grid           = Panel         # 磁贴网格容器
    Tiles          = ArrayList     # 该区块独立的磁贴列表
    TileButtons    = @()           # 该区块独立的按钮数组
    BlockName      = "新区块_1"    # 区块名称
    BlockThemeColor = "Rose"       # 区块主题色
    ShowBlockTitle = $true         # 是否显示标题
}
```

**关键设计**：每个 `ExtraTileBlock` 拥有独立的 `Tiles` 和 `TileButtons`，与主 `$global:TileConfigs` / `$global:TileButtons` 完全隔离。

### 2.2 网格布局算法

#### Find-FirstEmptySlot（AABB 碰撞检测）
```
输入：TileW, TileH (1-2)
过程：
  1. 扫描已占用的 Col/Row + Width/Height
  2. 按行优先顺序逐格检查
  3. 双重循环检测子格是否与任何已有磁贴重叠
  4. 未重叠 → 返回 {Col, Row}
输出：{Col: int, Row: int}
```

#### Layout-TileGrid 计算
```
effSize = max(40, min(120, floor((gridW - (MAX_COLS-1) * GAP) / MAX_COLS)))
pxW = tw * effSize + (tw-1) * GAP
pxH = th * effSize + (th-1) * GAP
px  = col * (effSize + GAP)
py  = row * (effSize + GAP)
```

### 2.3 磁贴启动

```powershell
Invoke-TileLaunch($tileConfig)
  ├─ LaunchPath 为空 → MessageBox "未设置启动目标"
  ├─ 时间差 < 1.5s → 防抖静默拒绝
  ├─ URL 匹配 → Start-Process $path (浏览器)
  ├─ Test-Path 存在 → Start-Process $path
  ├─ 文件不存在 → MessageBox "找不到目标文件"
  └─ Show-TileLaunchToast (1.2s 绿色圆角提示)
```

### 2.4 区块标题同步

`Layout-TilePanel` 中标题读取优先级：
1. `block.Tiles[0].BlockName` (第一个磁贴的区块名)
2. `block.BlockName` (区块自身名称)
3. 默认 `"► 磁贴"`

颜色同理：`Tiles[0].BlockThemeColor` → `block.BlockThemeColor` → `$global:ColorMauve`

### 2.5 编辑器的拖拽排序

`Refresh-BlockPreview` 中每个预览按钮绑定 `MouseDown`：
- 记录拖拽起始偏移 `_TileDragOffsetX/Y`
- 设置 `Capture = true` 到网格面板
- 后续 `MouseMove` / `MouseUp` 处理 AABB 重叠检测 → 自动交换位置
- 拖拽时边框变为黄色 (`$global:ColorYellow`)

---

## 三、关键函数与参数

| 函数 | 参数 | 返回值 | 核心职责 |
|------|------|--------|----------|
| `New-TileBlockPanel` | `$BlockName, $BlockThemeColor, $ShowBlockTitle` | `PSCustomObject` (block) | 创建完整 RoundedPanel + 注册到 ExtraTileBlocks/AllCards |
| `Find-FirstEmptySlot` | `$TileW, $TileH` | `{Col, Row}` | AABB 碰撞检测找第一个空位 |
| `New-TileConfig` | 12 个参数（Name, LaunchPath, IconPath, Width, Height, Col, Row, BlockThemeColor, BlockName, ShowBlockTitle, TileNameColor, ShowTileName） | `PSCustomObject` | 创建磁贴数据对象 + 自动类型推断 |
| `Invoke-TileLaunch` | `$tileConfig` | void | 启动磁贴（含防抖 + 类型分发） |
| `Layout-TileGrid` | `$card` (可选) | void | 排列 TileButton 到 Panel 网格 |
| `Layout-TilePanel` | `$card` (可选) | void | 自适应面板高度 + 标题/颜色同步 |
| `Save-TileConfigs` | 无 | void | 序列化 TileConfigs → JSON |
| `Load-TileConfigs` | 无 | void | JSON → 反序列化重建 TileConfigs |
| `Load-TileToEditor` | 无 | void | 选中磁贴 → 填充编辑器表单 |
| `Save-TileFromEditor` | 无 | void | 编辑器表单 → 写回磁贴对象 |
| `Refresh-BlockPreview` | 无 | void | 重建可拖拽预览网格 |
| `Show-TileLaunchToast` | `$tileName` | void | 1.2s 绿色圆角启动提示 |
| `New-TileBlock` | 无 | void | 创建新区块 + 重建布局编辑器 |

### 3.1 类型推断逻辑 (New-TileConfig / Save-TileFromEditor)

```
LaunchPath 为空 → ''
https:// 开头   → 'URL'
文件夹路径      → 'DIR'
.exe           → 'EXE'
.bat           → 'BAT'
.cmd           → 'CMD'
.lnk           → 'LNK'
.ps1           → 'PS1'
.png/jpg/...   → 'IMG'
其他           → 'FILE'
```

---

## 四、数据结构与持久化

### 4.1 运行时数据结构

| 变量 | 类型 | 作用 |
|------|------|------|
| `$global:TileConfigs` | ArrayList | 主磁贴区块的所有磁贴配置 |
| `$global:TileButtons` | Array | 主磁贴区块的所有 TileButton 控件 |
| `$global:ExtraTileBlocks` | Array | 额外区块（每个含 Panel/Title/Grid/Tiles/TileButtons） |
| `$global:SerializedTileConfigs` | String (JSON) | 序列化后的磁贴数据 |
| `$global:LastTileLaunchTime` | Float | 上次启动时间戳（防抖） |
| `$global:TileEditMode` | Bool | 是否处于磁贴编辑模式 |
| `$global:TileEditBlock` | PSCustomObject | 当前正在编辑的 ExtraTileBlock |
| `$global:TileEditIdx` | Int | 编辑器中选中的磁贴索引 |
| `$global:TileEditControls` | Hashtable | 编辑器控件引用 |
| `$global:TileFocusIdx` | Int | 手柄/键盘导航焦点索引 |

### 4.2 持久化格式

```json
// SerializedTileConfigs (单行压缩 JSON)
[
  {
    "Name": "浏览器",
    "LaunchPath": "https://www.example.com",
    "LaunchType": "URL",
    "IconPath": "D:/icons/browser.png",
    "Width": 1,
    "Height": 1,
    "Col": 0,
    "Row": 0,
    "BlockThemeColor": "Teal",
    "BlockName": "► 快速启动",
    "ShowBlockTitle": true,
    "TileNameColor": "255,255,255",
    "ShowTileName": true
  }
]
```

---

## 五、错误处理与边界条件

### 5.1 防御性检查

| 位置 | 检查内容 | 处理方式 |
|------|----------|----------|
| `Invoke-TileLaunch` | LaunchPath 为空 | MessageBox 提示 |
| `Invoke-TileLaunch` | 文件不存在 | MessageBox "找不到目标文件" |
| `Invoke-TileLaunch` | try/catch 全部 | MessageBox "启动失败: $_" |
| `New-TileBlockPanel` | BlockThemeColor 解析 | try/catch → fallback ColorMauve |
| `Layout-TilePanel` | 面板已 Dispose | 提前 return |
| `Layout-TileGrid` | gridW < 200 | max(200, ...) 保底 |
| `Layout-TileGrid` | maxBottom < 100 | min set 100 |
| `Load-TileToEditor` | idx 越界 | 清空表单，不崩溃 |
| `Save-TileFromEditor` | IsHandleCreated 检查 | try/catch 包裹每个控件 |
| `Show-TileManager` | 异常恢复 | 备份恢复 AllCards/PageBreaks + MessageBox |
| 全局异常 | foreach/switch 关键路径 | try/catch 包围，静默失败 |

### 5.2 边界处理

- **防抖**：`Invoke-TileLaunch` 1.5 秒内重复点击静默拒绝
- **Toast 清理**：每次显示前先 Dispose 旧 toast 和 timer
- **数组越界**：`_rebuild` 中 `focus_idx >= len(tiles)` → `focus_idx = 0`
- **Col/Row 非法值**：`if (col < 0) { col = 0 }`
- **Width/Height 范围**：`max(1, min(2, ...))`
- **IMPORTANT**: `Layout-TilePanel` 调用 `Layout-TileGrid` 时 _rebuild 之前 dispose 旧控件

---

## 六、潜在问题与优化建议

### 6.1 已知缺陷

| 问题 | 严重程度 | 说明 |
|------|----------|------|
| **ExtraTileBlock 不参与 Save/Load** | 🔴 高 | `Save-TileConfigs` 只序列化 `$global:TileConfigs`，额外区块的 `Tiles` 不持久化。刷新后额外区块磁贴丢失。 |
| **全局变量依赖** | 🟡 中 | 大量 `$global:` 变量（TileConfigs, TileButtons, ExtraTileBlocks, TileEditMode 等），插件化程度有限 |
| **主副区块代码重复** | 🟡 中 | `Layout-TileGrid` 中主区块和额外区块几乎相同的代码写了两次（L278-339 vs L341-402） |
| **硬编码颜色** | 🟡 中 | `(40, 60, 40, 20)` 等 RGB 偏移硬编码在多处 |
| **RoundedButton 闭包陷阱** | 🟡 中 | `Add_Click({...}.GetNewClosure())` 显式处理，但依赖开发者记得使用 |
| **JSON 错误静默** | 🟢 低 | `Load-TileConfigs` catch 中 `Clear()` 静默丢失数据 |
| **少女粉/暗色双路颜色** | 🟢 低 | `Get-GamepadButtonColors` 和编辑器中多处 `if ($isLight)` 分支 |

### 6.2 优化建议

1. **持久化 ExtraTileBlocks**：扩展 `Save-TileConfigs` / `Load-TileConfigs` 保存每个额外区块的独立磁贴列表
2. **统一网格布局**：抽取 `Layout-TileGrid` 主副区块共用逻辑到单个函数
3. **减少全局变量**：改用 Hashtable 打包 TileState，增加模块封装
4. **颜色管理集中化**：将 RGB 偏移常量提取到 `colors.ps1` 的 API
5. **JSON 错误保留**：加载失败时保留原始 `SerializedTileConfigs` 字符串以便恢复

---

## 七、与 Python 端口对照

| PS1 功能 | Python 对应 | 完整度 |
|----------|-------------|--------|
| TileConfig 数据模型 | `core/config.py::Tile` dataclass | ✅ |
| ExtraTileBlock 数据模型 | `core/config.py::ExtraTileBlock` dataclass | ✅ |
| 网格布局 | `panels.py::TilePanel._layout_grid` | ✅ |
| Find-FirstEmptySlot | `tile.py::TileBlock.find_first_empty_slot` | ✅ |
| 磁贴启动 | `tile.py::TileBlock.launch` | ✅ |
| 编辑器（区块设置卡） | `edit_overlay.py::TileEditPanel` (部分) | ⚠️ |
| 编辑器（拖拽预览） | `edit_overlay.py::TileEditPanel` (部分) | ⚠️ |
| Save-TileConfigs | `tile.py::TileBlock.save_config` | ✅ |
| Load-TileConfigs | `tile.py::TileBlock.load_config` | ✅ |
| New-TileBlockPanel | `app_window.py::_build_panels` + `_TileBlockPanelProxy` | ✅ |
| Layout-TilePanel 标题同步 | `panels.py::TilePanel._sync_block_title` | ✅ |
| Toast | `tile.py::TileBlock.show_toast` | ✅ |
| 防抖 | `tile.py::LAUNCH_DEBOUNCE_S = 1.5` | ✅ |
| ExtraTileBlock 持久化 | `ExtraTileBlock.to_dict/from_dict` (含 tiles) | ✅ |

---

## 八、优化项落地状态（2026-07-19 实施）

依据 §6.1 / §6.2 的缺陷与优化建议，已逐项在 Python 端口落实并通过自动化验证
（新增 `tests/test_tile_report_optimizations.py`，全量 `pytest` 130 passed，0 失败）。

| # | 优化项 | 状态 | 落地位置 |
|---|--------|------|----------|
| §6.2-1 | 持久化 ExtraTileBlocks | ✅ 已强化 | `tile.py::TileBlock.save_config` 现实时与 `config.extra_tile_blocks` 同步（仅读取活配置后回写，绝不覆盖编辑器已改内容）；端到端保存→重载 round-trip 已验证 |
| §6.2-2 | 统一网格布局 | ✅ 已抽取 | 新增模块级函数 `panels.py::layout_tile_grid`，基础区块与所有 ExtraTileBlock 代理面板共用同一代码路径（消除 PS1 中主/副网格重复） |
| §6.2-3 | 减少全局变量 | ✅ 天然满足 | Python 端口以 `AppConfig` / `ConfigManager` / `Block` 封装状态，无 `$global` 依赖，无需额外改动 |
| §6.2-4 | 颜色管理集中化 | ✅ 已抽取 | `core/color/palette.py` 新增 `TILE_BG` / `TILE_BG_HOVER` / `TILE_FOCUS_BORDER` / `TOAST_OK` / `TOAST_BAD`，替换 `panels.py` 与 `tile.py` 中的硬编码 RGB |
| §6.2-5 | JSON 错误保留 | ✅ 已实现 | `core/config.py::ConfigManager.load` 解析失败时保留原始文本（`last_error_raw`）并告警，不再静默丢数据 |

### 附：磁贴卡片显示内容同步（用户专项要求）
报告历史遗留的回归缺陷已修复，确保 HUD 磁贴卡片内容与数据一致：

- **`_edit_items("tile#N")` 不再直接 return** —— 额外区块「编辑项」按钮现可打开对应区块编辑器。
- **`TileEditPanel` 支持编辑 `ExtraTileBlock`** —— 区块名称 / 主题色 / 标题显示 / 磁贴列表均可编辑，并写回 `config.extra_tile_blocks`。
- **编辑即时同步 HUD** —— 每次编辑经代理面板 `instance._extra` 反查匹配面板并 `sync()`，磁贴名称、区块标题、主题色、标题显隐实时刷新。
- **新增磁贴继承区块元数据**（名称/主题色/标题显隐）—— 修复新建磁贴 `block_name` 为空的问题。
- **删除区块** —— 对额外区块执行真正的移除（此前仅清空基础区块磁贴）。
- 验证：`test_edit_extra_block_syncs_hud_and_config`、`test_delete_extra_block_removes_it`、`test_edit_items_opens_extra_block_editor` 均通过。

