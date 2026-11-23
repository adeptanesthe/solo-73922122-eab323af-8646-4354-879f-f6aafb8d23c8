"""需求逐条核对脚本（不依赖网络，直接调用内部模块）。"""
import io
import sys

from PIL import Image

from app import images, solver
from tests.test_app import png_bytes, stripes_grid, checker_grid

RED = (220, 60, 60)
BLUE = (60, 90, 220)
GREEN = (60, 180, 90)
YELLOW = (230, 200, 70)
ok = 0


def check(name, cond):
    global ok
    assert cond, f"FAIL: {name}"
    ok += 1
    print(f"[PASS] {name}")


# 1. 参考图最多 4 色、每边 <=64
ref = images.parse_reference(png_bytes(stripes_grid(8)))
check("参考图调色板 <=4 色", len(ref.colors) <= 4)

# 2. 2×2 图块统计：总数=像素数，跨边跨角
check("图块统计总数=参考图像素数", sum(ref.tile_counts.values()) == 64)
# 手工验证一个跨角图块（右下角为左上 -> 环绕到 (0,0)）
grid = stripes_grid(8)
corner_tile = (grid[7][7], grid[7][0], grid[0][7], grid[0][0])
check("跨角图块被统计", corner_tile in ref.tile_counts)

# 3. 同格异色返回两处原坐标
points = {(0, 0): RED, (4, 4): GREEN, (1, 1): BLUE}
fixed, conflicts = images.project_points(points, 4, 4)
check("同格异色检测", len(conflicts) == 1)
check("返回两处原坐标", set(conflicts[0].positions) == {(0, 0), (4, 4)})
check("冲突格坐标", conflicts[0].cell == (0, 0))

# 4. 同格同色合并
f2, c2 = images.project_points({(0, 0): RED, (4, 4): RED}, 4, 4)
check("同格同色合并无冲突", c2 == [] and len(f2) == 1)

# 5. 完好色点保留 + 每图块都在参考图出现（含跨边跨角）
r = solver.solve(ref.colors, ref.tile_counts, 6, 6, {(2, 3): RED}, 5.0)
check("求解有候选", r.grid is not None)
check("完好色点保留", r.grid[3][2] == RED)
W = H = 6
for y in range(H):
    for x in range(W):
        key = (r.grid[y][x], r.grid[y][(x + 1) % W],
               r.grid[(y + 1) % H][x],
               r.grid[(y + 1) % H][(x + 1) % W])
        assert key in ref.tile_counts, (x, y)
check("每格 2×2 图块均在参考图出现（含环绕）", True)

# 6. 得分=图块频次之和
manual = sum(
    ref.tile_counts[(
        r.grid[y][x], r.grid[y][(x + 1) % W],
        r.grid[(y + 1) % H][x],
        r.grid[(y + 1) % H][(x + 1) % W])]
    for y in range(H) for x in range(W))
check("得分=图块频次之和", manual == r.score)

# 7. 四种状态都可达
check("已证最优可达", r.status in (solver.STATUS_OPTIMAL, solver.STATUS_FEASIBLE))
ri = solver.solve(ref.colors, ref.tile_counts, 4, 4, {(0, 0): (1, 2, 3)}, 5.0)
check("已证无解可达", ri.status == solver.STATUS_INFEASIBLE and ri.grid is None)
ru = solver.solve(ref.colors, ref.tile_counts, 12, 12, {}, 0.02)
check("待定可达", ru.status in (solver.STATUS_UNKNOWN, solver.STATUS_FEASIBLE,
                                solver.STATUS_OPTIMAL))

# 8. 限时 5 秒
import time
t0 = time.perf_counter()
solver.solve(ref.colors, ref.tile_counts, 10, 10, {}, 5.0)
check("5 秒限时", time.perf_counter() - t0 <= 5.1)

# 9. 单元尺寸边界 4~12
for bad in (3, 13):
    try:
        images.validate_unit_size(bad, 8)
        raise AssertionError("应拒绝")
    except images.ImageValidationError:
        pass
check("单元尺寸 4~12 边界校验", True)

# 10. 透明=缺损
frag = images.parse_fragment(
    png_bytes(stripes_grid(4), transparent_pred=lambda x, y, c: y >= 2))
check("透明像素标缺损", len(frag.points) == 8)

# 11. 64 像素上限
big = png_bytes([[RED] * 65 for _ in range(4)])
try:
    images.parse_reference(big)
    raise AssertionError("应拒绝超宽图")
except images.ImageValidationError:
    check("每边 <=64 像素上限", True)

# 12. 回溯兜底与 CP-SAT 在小案例结论一致
bt = solver._solve_backtrack(ref.colors, ref.tile_counts, 6, 6, {(2, 3): RED}, 5.0)
cp = solver._solve_cpsat(ref.colors, ref.tile_counts, 6, 6, {(2, 3): RED}, 5.0)
check("回溯/CP-SAT 最优性一致", bt.status == cp.status == solver.STATUS_OPTIMAL)
check("回溯/CP-SAT 得分一致", bt.score == cp.score)

print(f"\n全部 {ok} 项需求核对通过")
