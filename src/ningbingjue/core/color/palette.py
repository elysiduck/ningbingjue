"""§1 Base palette — global background/foreground and the 9 role colors.

Mirrors the ``$global:*`` assignments in ``colors.ps1`` verbatim.
"""

from __future__ import annotations

from .rgb import Color

# -- background / foreground ------------------------------------------------
BG_APP = Color.from_rgb(14, 15, 19)          # 主背景：深邃科技感街头暗夜黑
BG_CARD = Color.from_rgb(28, 30, 38)         # 卡片背景：+12 提亮
BG_CARD_HOVER = Color.from_rgb(40, 43, 52)   # 卡片悬停：再 +12
FG_PRIMARY = Color.from_rgb(230, 255, 30)    # 主核心色：ZZZ 荧光酸性黄
FG_SECONDARY = Color.from_rgb(172, 180, 194) # 次级文字
SELECTION_BORDER_COLOR = FG_PRIMARY          # 焦点边框：荧光黄

# -- 9 block role colors ---------------------------------------------------
COLOR_BLUE = Color.from_rgb(95, 175, 235)    # 冰蓝（锁帧控制）
COLOR_GREEN = Color.from_rgb(130, 255, 30)   # 激活荧光绿
COLOR_RED = Color.from_rgb(230, 75, 90)      # 警示红
COLOR_PEACH = Color.from_rgb(255, 130, 40)   # 潮流橙（绳网风）
COLOR_MAUVE = Color.from_rgb(175, 130, 235)  # 雾紫（Power/磁贴）
COLOR_TEAL = Color.from_rgb(45, 215, 175)    # 薄荷绿（备用）
COLOR_YELLOW = Color.from_rgb(245, 215, 75)  # 纯暖黄（LS 预设/编辑标题）
COLOR_ORANGE = Color.from_rgb(255, 150, 45)  # 霓虹橙（新建页面按钮）
COLOR_CYAN = Color.from_rgb(90, 215, 230)    # 青蓝（进程监控）

# -- card gradient (top light -> bottom dark) ------------------------------
GRAD_CARD_TOP = Color.from_rgb(34, 36, 44)
GRAD_CARD_BOTTOM = Color.from_rgb(22, 24, 32)

# -- neon focus inner-glow width -------------------------------------------
FOCUS_GLOW_WIDTH = 3

# -- tile (磁贴) surface / focus / toast ------------------------------------
# Extracted from tile.ps1's scattered hard-coded RGB offsets (report §6.2-4)
# so every tile colour flows through the central palette API instead of
# being inlined at call sites.
TILE_BG = Color.from_rgb(28, 30, 38)             # 磁贴默认背景（与 BG_CARD 一致）
TILE_BG_HOVER = Color.from_rgb(40, 43, 52)       # 磁贴悬停背景（与 BG_CARD_HOVER 一致）
TILE_FOCUS_BORDER = FG_PRIMARY                   # 磁贴焦点边框：荧光酸性黄
TOAST_OK = Color.from_rgb(0, 230, 120)           # 启动成功提示：绿
TOAST_BAD = Color.from_rgb(255, 80, 80)          # 启动失败提示：红
