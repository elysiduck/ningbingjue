# PS1 配色与页面布局编辑深度分析报告

> 源文件：`ds8ZZZ.ps1`（Build-EditLayout §5362-5564、主题按钮 §2957-2978）
> `button-editor.ps1`（全 849 行）
> `colors.ps1`（基于《凝冰结魄-颜色配置系统技术剖析.md》）

---

## 一、Build-EditLayout — 页面布局编辑器完整实现

### 1.1 入口分派（L5362-5372）
```
Build-EditLayout
  ├─ 遍历 BlockRegistry → OnCheckEditMode=true → 分派 OnEditLayout
  ├─ $TileEditMode=true → Build-TileEditLayout
  └─ 否则 → 构建布局编辑器
```

### 1.2 UI 结构（L5385-5564）
```
pnlEdit.ContentPanel:
├─ lblEditTitle: "// LAYOUT_EDITOR //"（大标题，EditTitleColor）
├─ btnNewTileBlock: 右上角 "◫ 新建磁贴区块"（L5423，位置 clientW-110）
├─ btnNewPage: 右上角 "＋ 新建页面"（L5427，位置 clientW-60）
│
├─ themeRow（L5430-5462）：主题配色网格
│  ├─ lblThemeTitle: "// THEME_GALLERY //"
│  └─ pnlThemeFlow（FlowLayoutPanel）：
│     └─ N 个 themeBtns（90×50px RoundedButton）
│        └─ 每个按钮：theme.Name + BgApp 背景 + 点击→设全局色+Apply-Theme
│
├─ 每页（L5477-5501）：
│  ├─ New-PageSeparator: "═══ 第X页 ═══"（24px 高）
│  └─ N 个 EditCardRow（54px 高，gap=6px）：
│     ├─ 名称 Label（角色色，bold）
│     ├─ ↑ 按钮 / ↓ 按钮
│     ├─ 显示/隐藏 按钮
│     └─ 编辑项 按钮（仅 procmon/tile）
│
├─ btnEditApply（160px）："应用"（Green 风格）
├─ btnEditCancel（160px）："取消"（Red 风格）
└─ editHelpBar：操作提示
```

### 1.3 关键参数
| 参数 | 值 | 说明 |
|------|-----|------|
| cardH | 54px | 每行卡片高度 |
| gap | 6px | 卡片间距 |
| sepH | 24px | 页面分隔条高度 |
| themeBtnW×H | 90×50px | 主题按钮尺寸 |

---

## 二、新建磁贴区块 / 新建页面

### 2.1 新建磁贴区块（btnNewTileBlock）
```powershell
$btnNewTileBlock.Add_Click({ New-TileBlock })
```
`New-TileBlock` → `New-TileBlockPanel` 创建空磁贴面板 → 追加到 `$global:TilePanels` → 重建编辑器

**Python 实现**：`_new_tile_block()` → `TileBlock.add_tile("新磁贴", "")` → 内部 `tiles.append` + `_rebuild` + `save_config`（同步 `TileBlock.tiles` → `config.tiles`）→ `refresh_panel`

**关键 bug 修复**：之前直接改 `config.tiles` 后调 `save_all()`，`save_all()` 触发 `TileBlock.OnSaveConfig` → `save_config()` 用旧 `TileBlock.tiles` 覆盖 `config.tiles`，新磁贴丢失。改为通过 `TileBlock.add_tile()` 保持同步。

### 2.2 新建页面（btnNewPage）
```powershell
$btnNewPage.Add_Click({
    $global:PageBreaks += $global:AllCards.Count
    $global:EmptyPageSlot = $true
    $form.BeginInvoke({ Build-EditLayout })
})
```
**Python 实现**：`_add_page_break()` → `_page_breaks.append(len(order))` → `_render_rows()` 显示分隔条 → toast 反馈

---

## 三、主题配色系统

### 3.1 主题按钮创建（L2957-2978）
```powershell
$global:themeBtns = @()
for ($ti = 0; $ti -lt $themes.Count; $ti++) {
    $theme = $themes[$ti]
    $btn = New-Object RoundedButton
    $btn.Text = $theme.Name
    $btn.Size = 90, 50
    $btn.SetColors($theme.BgApp, $theme.BgCard, Transparent, $theme.Fg)
    $btn.Tag = $theme
    $btn.Add_Click({
        $t = $this.Tag
        $global:BgApp = $t.BgApp
        $global:BgCard = $t.BgCard
        $global:FgPrimary = $t.Fg
        $global:SelectionBorderColor = $global:FgPrimary
        $global:ActiveThemeName = $t.Name
        Apply-Theme
        Apply-ThemeAccents
        $form.BeginInvoke({ Build-EditLayout })
    })
    $global:themeBtns += $btn
}
```

**核心流程**：点击主题按钮 → 设置全局色 → `Apply-Theme` → `Apply-ThemeAccents` → `BeginInvoke(Build-EditLayout)` 重建编辑器

**Python 实现**：`_theme_cb.currentTextChanged` → `_on_theme_changed(name)` → `theme_manager.apply(name)` → `save_all()` → `_render_rows()` 重建行

### 3.2 10 个主题
从 `themes.py` 中每个主题返回完整 accent dict（34-60 键），涵盖：区块标题色、焦点边框色、退出按钮四态、斜杠发光色、LS 按钮主题名、编辑界面按钮主题名。

---

## 四、按钮风格编辑器（button-editor.ps1 完全对照）

### 4.1 主面板结构（880×700 Form）
```
├─ 左面板（470×655）：canvas FlowLayoutPanel
│  └─ 按区块分组显示所有已注册按钮（mini 预览，点击选中）
│
├─ 右面板（382×655）：
│  ├─ 整体风格：ComboBox + 新建/复制/删除/应用
│  ├─ 定制预览：预览面板 + 预览按钮
│  ├─ 定制预设：ListBox + 新建/复制/删除
│  ├─ 绑定：绑定到选中按钮 / 取消绑定
│  └─ 属性编辑面板（378×290）：
│     ├─ 名称 TextBox
│     ├─ 切角 TrackBar（0-30）
│     ├─ 圆角 TrackBar（0-20）
│     ├─ 边框 TrackBar（0-6）
│     ├─ 复选框：链接主题 / 仅悬停边框 / 悬停渐变
│     ├─ 11 个色块（4 列×3 行）：
│     │  背景 | 悬停 | 边框 | 前景
│     │  悬停前景 | 渐变顶 | 渐变底 | 激活背景
│     │  激活边框 | 未激活背景 | 未激活边框
│     └─ 4 个操作按钮：保存预设 / 覆盖预设 / 提交风格 / 应用刷新
```

### 4.2 Python 实现对照
| PS1 功能 | Python 实现 | 状态 |
|----------|-------------|------|
| 风格 ComboBox | `_style_cb` + `_load_btn_style_editor` | ✅ |
| 切角/圆角/边框滑块 | `_cut_slider` / `_corner_slider` / `_border_slider` | ✅ |
| 链接主题复选框 | `_chk_link_theme` | ✅ |
| 仅悬停边框复选框 | `_chk_hover_only` | ✅ |
| 悬停渐变复选框 | `_chk_hover_grad` | ✅ |
| 11 色块编辑 | `_btn_color_editors`（11 个 QLineEdit） | ✅ |
| 新建/复制/删除风格 | （待实现） | ❌ |
| 定制预设管理 | （待实现） | ❌ |
| 按钮绑定 | （待实现） | ❌ |
| 画布预览 | （待实现） | ❌ |

---

## 五、呼吸灯可调参数详解

### 5.1 原 PS1 实现
原脚本 `effects/scanline.ps1`：
- **周期**：约 2.36 秒（正弦波）
- **驱动**：30ms Timer tick → phase 推进 → sin(phase) → alpha
- **alpha 范围**：0.2 ~ 1.0
- **渲染位置**：窗口顶部 y≈[12, 30] 渐变扫描线

### 5.2 Python 实现参数
| 参数 | 范围 | 默认值 | 说明 |
|------|------|--------|------|
| `speed` | 10-100 | 50 | 呼吸速度：10=最快（15ms 间隔），100=最慢（60ms 间隔） |
| `_scan_phase` | 0.0-2π | 0.0 | 正弦相位角 |
| `_slash_alpha` | 0.0-1.0 | 0.0 | 斜杠发光 alpha |
| `_slash_target` | 0.0-1.0 | 0.0 | 斜杠发光目标值 |

**速度映射**：`ms_interval = max(15, min(60, speed * 0.6))`
- speed=10 → 15ms（~66.7 fps）→ 最快呼吸
- speed=50 → 30ms → 正常速度
- speed=100 → 60ms（~16.7 fps）→ 最慢呼吸

### 5.3 编辑器控制
- `QSlider(10, 100)`：滑块直接控制 speed
- `_breath_changed(val)`：写 `config.breath_speed = val` + `save_all()`
- `BreathingController(speed)`：构造时从 config 读取

---

## 六、焦点光标渲染

### 6.1 PS1 实现
- `SelectionBorderColor = FgPrimary`（荧光黄 #E6FF1E）
- 焦点框绘制在控件上方，2px 边框

### 6.2 Python 实现
- `_FOCUS_STYLE`：`rgba(230,255,30,0.25)` 背景 + `2px solid #e6ff1e` 边框
- `_place_rect()` 中 `self._rect.raise_()` 确保在兄弟控件上方渲染
- 子控件属于 scrollable content widget，自然在 toast/对话框之下
