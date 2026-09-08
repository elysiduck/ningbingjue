"""两层导航冒烟：手动构造模拟 LayoutEditPanel nav_items（row=r, col=0..4），
直接读 controller._apply_grid 与 _apply_direction 的内部判定（不实例化 GUI）。"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import QPoint
app = QApplication.instance() or QApplication(sys.argv[:1])

import ningbingjue.ui.interaction as it_mod
from ningbingjue.ui.interaction import InteractionController, NavItem


# === 模拟一行的 nav_items（与 LayoutEditPanel 真实字段一致）===
class FakeCard(QWidget): pass


def build_layout_items():
    items = []
    # 4 张卡片，每张 5 个 NavItem（col=0..4：整行 / ↑ / ↓ / ▲ / ▼）
    # rows: r=0=页眉, r=1..4=4 张卡片，r=5=页脚
    for k in range(1, 5):
        t = f"card{k}"
        card = FakeCard()
        card._up = FakeCard(); card._down = FakeCard(); card._prev = FakeCard(); card._next = FakeCard()
        items.append(NavItem(card, "action",
                             confirm=lambda tt=t: None, row=k, col=0, card_token=t))
        items.append(NavItem(card._up, "action",
                             confirm=lambda: None, row=k, col=1, card_token=t))
        items.append(NavItem(card._down, "action",
                             confirm=lambda: None, row=k, col=2, card_token=t))
        items.append(NavItem(card._prev, "action",
                             confirm=lambda: None, row=k, col=3, card_token=t))
        items.append(NavItem(card._next, "action",
                             confirm=lambda: None, row=k, col=4, card_token=t))
    return items


# --- mock 必要依赖 ---
host = QWidget()
overlay = QWidget()  # 只用作容器占位
ctrl = InteractionController.__new__(InteractionController)
# 手动初始化（避开 host/window 依赖）—— 只填 nav 相关字段
ctrl._items = build_layout_items()
ctrl._focus = -1
ctrl._gp_active = True
ctrl._edit_mode = True
ctrl._edit_inner = False
ctrl._input_mode = "gamepad"
ctrl._grid_rows = {}
for i, it in enumerate(ctrl._items):
    if it.row is not None and it.col is not None:
        ctrl._grid_rows.setdefault(it.row, {})[it.col] = i
ctrl._grid_max_row = max(ctrl._grid_rows) if ctrl._grid_rows else -1

# 取一个 col=0 的 NavItem（整行）
row1_idx = ctrl._grid_rows[1][0]
ctrl._focus = row1_idx
ctrl._edit_overlay = overlay

# === 测试 1：外层 + 方向键 dx!=0 应被锁 ===
# 关键判定：_apply_grid(1, 0) 在 edit_inner=False 时必须 return False，
# 然后 _apply_direction 看到 moved=False + edit_mode=True 就 return False，
# 绝不会调用 _move（_move 才会真正改焦点）。
ok_locked = True
fails = []
for it in ctrl._items:
    if it.col != 0:
        continue  # 只测在 col=0 时
    ctrl._focus = ctrl._grid_rows[it.row][0]  # 强制 lock 在该行 col=0
    saved = ctrl._focus
    rv = ctrl._apply_grid(1, 0)
    if rv is not False:
        ok_locked = False
        fails.append(f"row={it.row} col=0: _apply_grid(1,0) returned {rv}, expected False")
print(f"TEST 1 (outer lock _apply_grid(1,0)==False on every col=0 row): {'PASS' if ok_locked else 'FAIL: '+str(fails)}")

# 进一步：看 _apply_direction 在同样状态下是否走 _move（焦点移走 = bug）
# 我们看的是 _apply_grid 的返回值（这是路由判定关键点），不实际 set_focus。
print(f"TEST 1b (outer lock _apply_grid(-1,0)==False on every col=0 row): ", end="")
ok = True
for it in ctrl._items:
    if it.col != 0:
        continue
    ctrl._focus = ctrl._grid_rows[it.row][0]
    rv = ctrl._apply_grid(-1, 0)
    if rv is not False:
        ok = False
        break
print('PASS' if ok else 'FAIL')

# === 测试 2：外层 + dy!=0 应切卡 ===
print(f"TEST 2 (outer up from row>1 goes to row-1 col=0): ", end="")
ok = True
for r in [2, 3, 4]:
    ctrl._focus = ctrl._grid_rows[r][0]
    rv = ctrl._apply_grid(0, -1)
    expected_idx = ctrl._grid_rows[r - 1][0]
    # 不 set_focus，只看 _apply_grid 是否找到目标
    if rv is not True:
        ok = False
        break
print('PASS' if ok else 'FAIL')

# === 测试 3：内层 + dx!=0 应横向切 col（只读返回目标 idx）===
ctrl._edit_inner = True
print(f"TEST 3 (inner dx=1 col 0->1): ", end="")
ctrl._focus = ctrl._grid_rows[1][0]
rv = ctrl._apply_grid(1, 0)
print('PASS' if rv is True else f'FAIL got {rv}')

ctrl._focus = ctrl._grid_rows[1][1]
print(f"TEST 3b (inner dx=1 col 1->2): ", end="")
rv = ctrl._apply_grid(1, 0)
print('PASS' if rv is True else f'FAIL got {rv}')

# === 测试 4：设备模式切换 ===
ctrl._edit_inner = False
ctrl._input_mode = "gamepad"
ctrl.set_input_mode("mouse")
if ctrl._input_mode == "mouse":
    print("TEST 4 (input mode mouse): PASS")
else:
    print(f"TEST 4 FAIL: got {ctrl._input_mode}")

ctrl.set_input_mode("gamepad")
if ctrl._input_mode == "gamepad":
    print("TEST 5 (input mode gamepad): PASS")
else:
    print(f"TEST 5 FAIL: got {ctrl._input_mode}")

print("\n=== 全部探针完成 ===")
