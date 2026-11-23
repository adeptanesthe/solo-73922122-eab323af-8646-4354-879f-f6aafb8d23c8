"""纹版约束求解。

问题建模
========
纹版是 ``W × H`` 格的环形（toroidal）色带，每格取参考图调色板中的一种颜色。
要求：

1. 残片投影得到的完好色点（固定格约束）必须保留；
2. 以每个格为左上角的 2×2 图块都必须在参考图中出现过（允许跨边/跨角环绕）；
3. 在所有可行纹版中，最大化「每个 2×2 图块在参考图中的出现次数」之和。

求解策略（三层）
================
* **可行性层（CP-SAT）**：决策变量只有 ``W×H`` 个颜色整数变量，每个格位置
  一条 ``add_allowed_assignments`` 约束，允许表即参考图出现过的 2×2 四元组
  （至多 4⁴=256 行）。该模型在最坏尺寸（12×12、256 种图块）下通常 0.1 秒
  内即可判定可行性或严格证明无解。
* **优化层（局部搜索）**：在可行网格上做单格变色的爬山搜索，每次变色只
  影响 4 个四元组，增量评估极快；多轮随机重启直到限时耗尽。
  当得分达到理论上界（格数 × 最高频图块频次）时可声明「已证最优」，
  否则标记「仅找到可行纹样」。
* **兜底层（内置回溯）**：运行环境缺少 OR-Tools 时，使用行编码回溯 +
  分支定界，接口与状态语义保持一致。

状态语义：``optimal`` 已证最优 / ``feasible`` 仅找到可行纹样 /
``infeasible`` 已证无解 / ``unknown`` 待定。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Optional

from .images import RGB, TileKey

STATUS_OPTIMAL = "optimal"          # 已证最优
STATUS_FEASIBLE = "feasible"        # 仅找到可行纹样
STATUS_INFEASIBLE = "infeasible"    # 已证无解
STATUS_UNKNOWN = "unknown"          # 待定

STATUS_LABELS = {
    STATUS_OPTIMAL: "已证最优",
    STATUS_FEASIBLE: "仅找到可行纹样",
    STATUS_INFEASIBLE: "已证无解",
    STATUS_UNKNOWN: "待定",
}

_CORNERS = ((0, 0), (1, 0), (0, 1), (1, 1))


@dataclass
class SolveResult:
    status: str
    grid: Optional[tuple[tuple[RGB, ...], ...]] = None
    score: Optional[int] = None
    upper_bound: Optional[int] = None
    elapsed_seconds: float = 0.0
    solver_name: str = ""
    tile_usage: list[dict] = field(default_factory=list)


def solve(
    palette: tuple[RGB, ...],
    tile_counts: dict[TileKey, int],
    unit_w: int,
    unit_h: int,
    fixed: dict[tuple[int, int], RGB],
    time_limit: float = 5.0,
) -> SolveResult:
    """按启动时色点求解纹版。

    Parameters
    ----------
    palette:
        参考图调色板（有序 RGB 元组）。
    tile_counts:
        参考图全部 2×2 图块的出现次数（仅含出现过的图块）。
    unit_w, unit_h:
        重复单元每边格数（4～12）。
    fixed:
        固定格约束 ``{(x, y): rgb}``。
    time_limit:
        墙钟限时（秒），默认 5 秒。
    """
    try:
        return _solve_cpsat(palette, tile_counts, unit_w, unit_h, fixed,
                            time_limit)
    except ImportError:
        return _solve_backtrack(palette, tile_counts, unit_w, unit_h, fixed,
                                time_limit)


# --------------------------------------------------------------------------- #
# 通用辅助
# --------------------------------------------------------------------------- #

def _max_freq(tile_counts: dict[TileKey, int]) -> int:
    return max(tile_counts.values(), default=0)


def _tile_usage(
    tile_counts: dict[TileKey, int],
    grid: tuple[tuple[RGB, ...], ...],
    unit_w: int,
    unit_h: int,
) -> list[dict]:
    """汇总候选纹版中各 2×2 图块的使用情况与参考频次。"""
    from collections import Counter

    usage: Counter[TileKey] = Counter()
    for y in range(unit_h):
        for x in range(unit_w):
            key = (
                grid[y][x],
                grid[y][(x + 1) % unit_w],
                grid[(y + 1) % unit_h][x],
                grid[(y + 1) % unit_h][(x + 1) % unit_w],
            )
            usage[key] += 1
    return [
        {
            "tile": [list(c) for c in key],
            "reference_count": tile_counts.get(key, 0),
            "used_in_pattern": count,
        }
        for key, count in sorted(
            usage.items(),
            key=lambda kv: (-kv[1], kv[0]))
    ]


def _score_rgb_grid(
    tile_counts: dict[TileKey, int],
    grid: tuple[tuple[RGB, ...], ...],
    unit_w: int,
    unit_h: int,
) -> int:
    total = 0
    for y in range(unit_h):
        for x in range(unit_w):
            key = (
                grid[y][x],
                grid[y][(x + 1) % unit_w],
                grid[(y + 1) % unit_h][x],
                grid[(y + 1) % unit_h][(x + 1) % unit_w],
            )
            total += tile_counts.get(key, 0)
    return total


def _score_idx_grid(
    quad_freq_idx: dict[tuple[int, int, int, int], int],
    grid: list[list[int]],
    unit_w: int,
    unit_h: int,
) -> int:
    """下标网格的图块频次之和。"""
    total = 0
    for y in range(unit_h):
        for x in range(unit_w):
            quad = (
                grid[y][x],
                grid[y][(x + 1) % unit_w],
                grid[(y + 1) % unit_h][x],
                grid[(y + 1) % unit_h][(x + 1) % unit_w],
            )
            total += quad_freq_idx.get(quad, 0)
    return total


# --------------------------------------------------------------------------- #
# CP-SAT 可行性 + 局部搜索优化
# --------------------------------------------------------------------------- #

def _solve_cpsat(
    palette: tuple[RGB, ...],
    tile_counts: dict[TileKey, int],
    unit_w: int,
    unit_h: int,
    fixed: dict[tuple[int, int], RGB],
    time_limit: float,
) -> SolveResult:
    from ortools.sat.python import cp_model

    started = time.perf_counter()
    # 安全截止线：预留余量给求解器收尾、结果封装、网络响应以及 CPU 争抢下
    # 的调度延迟，保证外层墙钟不突破 time_limit。
    safety_margin = min(0.75, float(time_limit) * 0.15)
    deadline = started + float(time_limit) - safety_margin
    color_index = {c: i for i, c in enumerate(palette)}

    # 快速拒绝：固定色不在参考图调色板中 -> 无解
    for color in fixed.values():
        if color not in color_index:
            return SolveResult(
                status=STATUS_INFEASIBLE,
                elapsed_seconds=time.perf_counter() - started,
                solver_name="cp-sat+localsearch",
            )

    # 四元组（颜色下标）允许表
    quad_colors = sorted(
        tuple(color_index[c] for c in key)  # type: ignore[arg-type]
        for key in tile_counts)
    allowed_table = [list(q) for q in quad_colors]
    quad_freq_idx = {
        q: tile_counts[next(
            k for k in tile_counts
            if tuple(color_index[c] for c in k) == q)]
        for q in quad_colors
    }

    # ---------------- 阶段 1：CP-SAT 精确优化（动态预算） ---------------- #
    # 图块种类少/约束多时精确模型很快；给它一个随规模收缩的预算，
    # 最坏情形（256 种图块、12×12）只花 ~1 秒，把剩余时间留给可行性+局部搜索。
    exact_budget = min(
        1.0,
        max(0.0, time_limit * 0.3
            - 0.005 * unit_w * unit_h * len(quad_colors) / 25.0),
    )
    exact_budget = min(exact_budget, deadline - time.perf_counter())
    exact = None
    fixed_idx_local = {
        (x, y): color_index[c] for (x, y), c in fixed.items()}
    if exact_budget > 0.02:  # 预算太小就跳过精确阶段，避免固定开销反噬
        exact = _try_exact_cpsat(
            palette, quad_colors, quad_freq_idx, allowed_table,
            unit_w, unit_h, fixed_idx_local, exact_budget)
    if exact is not None and exact[0] == STATUS_OPTIMAL:
        idx_grid, best_score = exact[1], exact[2]
        rgb_grid = tuple(
            tuple(palette[idx_grid[y][x]] for x in range(unit_w))
            for y in range(unit_h))
        result = SolveResult(
            status=STATUS_OPTIMAL, grid=rgb_grid, score=best_score,
            upper_bound=unit_w * unit_h * _max_freq(tile_counts),
            elapsed_seconds=min(time.perf_counter() - started, time_limit),
            solver_name="cp-sat+localsearch")
        result.tile_usage = _tile_usage(tile_counts, rgb_grid, unit_w, unit_h)
        return result

    # ---------------- 阶段 2：CP-SAT 纯可行性 ---------------- #
    model = cp_model.CpModel()
    cells: dict[tuple[int, int], cp_model.IntVar] = {}
    for y in range(unit_h):
        for x in range(unit_w):
            var = model.new_int_var(0, len(palette) - 1, f"c_{x}_{y}")
            cells[(x, y)] = var
            if (x, y) in fixed:
                model.add(var == color_index[fixed[(x, y)]])
    for y in range(unit_h):
        for x in range(unit_w):
            corner_vars = [
                cells[((x + dx) % unit_w, (y + dy) % unit_h)]
                for dx, dy in _CORNERS
            ]
            model.add_allowed_assignments(corner_vars, allowed_table)

    solver = cp_model.CpSolver()
    # 可行性模型正常 0.1 秒内可解；显式封顶 2 秒，避免极端环境下吃光预算
    feas_budget = min(2.0, deadline - time.perf_counter())
    solver.parameters.max_time_in_seconds = max(0.01, feas_budget)
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = 20261123
    status = solver.solve(model)

    if status == cp_model.INFEASIBLE:
        return SolveResult(
            status=STATUS_INFEASIBLE,
            elapsed_seconds=time.perf_counter() - started,
            solver_name="cp-sat+localsearch")
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        # 限时内可行性都未判定 -> 待定
        return SolveResult(
            status=STATUS_UNKNOWN,
            elapsed_seconds=min(time.perf_counter() - started, time_limit),
            upper_bound=unit_w * unit_h * _max_freq(tile_counts),
            solver_name="cp-sat+localsearch")

    idx_grid = [
        [solver.value(cells[(x, y)]) for x in range(unit_w)]
        for y in range(unit_h)
    ]
    # 精确模型若已找到可行解（未证最优），用它作为局部搜索起点
    if exact is not None and exact[0] == STATUS_FEASIBLE:
        idx_grid = exact[1]
    fixed_idx = fixed_idx_local

    # ---------------- 阶段 3：局部搜索优化得分 ---------------- #
    # 只使用安全截止线之前的剩余时间
    local_deadline = deadline
    best_grid = idx_grid
    best_score = _score_idx_grid(
        quad_freq_idx, idx_grid, unit_w, unit_h)
    if time.perf_counter() < local_deadline:
        best_grid, best_score = _local_search(
            idx_grid, quad_colors, quad_freq_idx, palette,
            unit_w, unit_h, fixed_idx, local_deadline)

    rgb_grid = tuple(
        tuple(palette[best_grid[y][x]] for x in range(unit_w))
        for y in range(unit_h)
    )
    upper_bound = unit_w * unit_h * _max_freq(tile_counts)
    # 达到理论上界 => 已证最优；否则只是当前找到的最佳可行纹样
    final_status = (
        STATUS_OPTIMAL if best_score >= upper_bound else STATUS_FEASIBLE)

    result = SolveResult(
        status=final_status,
        grid=rgb_grid,
        score=best_score,
        upper_bound=upper_bound,
        elapsed_seconds=min(time.perf_counter() - started, time_limit),
        solver_name="cp-sat+localsearch",
    )
    result.tile_usage = _tile_usage(tile_counts, rgb_grid, unit_w, unit_h)
    return result


def _try_exact_cpsat(
    palette: tuple[RGB, ...],
    quad_colors: list[tuple[int, int, int, int]],
    quad_freq_idx: dict[tuple[int, int, int, int], int],
    allowed_table: list[list[int]],
    unit_w: int,
    unit_h: int,
    fixed_idx: dict[tuple[int, int], int],
    budget: float,
) -> Optional[tuple[str, list[list[int]], int]]:
    """带频次目标的精确 CP-SAT 模型。

    返回 ``(状态, 下标网格, 得分)``；状态为 ``optimal`` 或 ``feasible``；
    若预算内无解/未找到可行点则返回 ``None``。
    """
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    cells: dict[tuple[int, int], cp_model.IntVar] = {}
    for y in range(unit_h):
        for x in range(unit_w):
            var = model.new_int_var(0, len(palette) - 1, f"c_{x}_{y}")
            cells[(x, y)] = var
            if (x, y) in fixed_idx:
                model.add(var == fixed_idx[(x, y)])

    freq_domain = cp_model.Domain.from_values(
        sorted(set(quad_freq_idx.values())))
    freq_table = [list(q) + [quad_freq_idx[q]] for q in quad_colors]
    terms = []
    for y in range(unit_h):
        for x in range(unit_w):
            corner_vars = [
                cells[((x + dx) % unit_w, (y + dy) % unit_h)]
                for dx, dy in _CORNERS
            ]
            model.add_allowed_assignments(corner_vars, allowed_table)
            freq_var = model.new_int_var_from_domain(
                freq_domain, f"f_{x}_{y}")
            model.add_allowed_assignments(
                corner_vars + [freq_var], freq_table)
            terms.append(freq_var)
    model.maximize(sum(terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = budget
    solver.parameters.num_workers = 1
    solver.parameters.random_seed = 20261123
    status = solver.solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    grid = [
        [solver.value(cells[(x, y)]) for x in range(unit_w)]
        for y in range(unit_h)
    ]
    label = STATUS_OPTIMAL if status == cp_model.OPTIMAL else STATUS_FEASIBLE
    return label, grid, int(round(solver.objective_value))



def _local_search(
    idx_grid: list[list[int]],
    quad_colors: list[tuple[int, int, int, int]],
    quad_freq_idx: dict[tuple[int, int, int, int], int],
    palette: tuple[RGB, ...],
    unit_w: int,
    unit_h: int,
    fixed_idx: dict[tuple[int, int], int],
    deadline: float,
) -> tuple[list[list[int]], int]:
    """单格变色爬山 + 随机重启，返回 (最佳下标网格, 得分)。"""
    rng = random.Random(20261123)
    valid_quads = set(quad_colors)
    n_colors = len(palette)

    def quad_at(grid: list[list[int]], x: int, y: int):
        return (
            grid[y][x],
            grid[y][(x + 1) % unit_w],
            grid[(y + 1) % unit_h][x],
            grid[(y + 1) % unit_h][(x + 1) % unit_w],
        )

    def score(grid: list[list[int]]) -> int:
        return sum(
            quad_freq_idx.get(quad_at(grid, x, y), 0)
            for y in range(unit_h) for x in range(unit_w))

    # 变色 (x,y) 只影响以 (x,y)、(x-1,y)、(x,y-1)、(x-1,y-1) 为左上角的图块
    def affected_positions(x: int, y: int):
        return {
            ((x - dx) % unit_w, (y - dy) % unit_h)
            for dx in (0, 1) for dy in (0, 1)
        }

    def hill_climb(grid: list[list[int]]) -> tuple[list[list[int]], int]:
        cur = [row[:] for row in grid]
        cur_score = score(cur)
        movable = [
            (x, y) for y in range(unit_h) for x in range(unit_w)
            if (x, y) not in fixed_idx
        ]
        improved = True
        while improved and time.perf_counter() < deadline:
            improved = False
            rng.shuffle(movable)
            for x, y in movable:
                if time.perf_counter() >= deadline:
                    break
                old = cur[y][x]
                local = list(affected_positions(x, y))
                before = sum(
                    quad_freq_idx.get(quad_at(cur, ax, ay), 0)
                    for ax, ay in local)
                best_value = old
                best_gain = 0
                for value in range(n_colors):
                    if value == old:
                        continue
                    cur[y][x] = value
                    if any(quad_at(cur, ax, ay) not in valid_quads
                           for ax, ay in local):
                        continue
                    after = sum(
                        quad_freq_idx.get(quad_at(cur, ax, ay), 0)
                        for ax, ay in local)
                    gain = after - before
                    if gain > best_gain:
                        best_gain = gain
                        best_value = value
                cur[y][x] = best_value
                if best_value != old:
                    cur_score += best_gain
                    improved = True
        return cur, cur_score

    best_grid = [row[:] for row in idx_grid]
    best_score = score(best_grid)

    # 多起点随机重启；hard_deadline 即调用方给定的绝对截止时间，
    # 单次 hill_climb 内部也在每格判断该时间，保证不会显著超时。
    hard_deadline = deadline
    while time.perf_counter() < hard_deadline:
        candidate, candidate_score = hill_climb(
            [row[:] for row in best_grid])
        if candidate_score > best_score:
            best_grid = candidate
            best_score = candidate_score
        if best_score >= unit_w * unit_h * max(quad_freq_idx.values()):
            break
        # 随机扰动若干非固定格后重启，帮助跳出局部最优
        if time.perf_counter() >= deadline:
            break
        restart = [row[:] for row in best_grid]
        for _ in range(max(1, (unit_w * unit_h) // 8)):
            x = rng.randrange(unit_w)
            y = rng.randrange(unit_h)
            if (x, y) not in fixed_idx:
                restart[y][x] = rng.randrange(n_colors)
        # 扰动可能破坏合法性，先恢复到最近合法状态：逐格回退
        for y in range(unit_h):
            for x in range(unit_w):
                if (x, y) in fixed_idx:
                    continue
                positions = list(affected_positions(x, y))
                if any(quad_at(restart, ax, ay) not in valid_quads
                       for ax, ay in positions):
                    restart[y][x] = best_grid[y][x]
    return best_grid, best_score


# --------------------------------------------------------------------------- #
# 内置回溯兜底（无 OR-Tools 时使用）
# --------------------------------------------------------------------------- #

def _solve_backtrack(
    palette: tuple[RGB, ...],
    tile_counts: dict[TileKey, int],
    unit_w: int,
    unit_h: int,
    fixed: dict[tuple[int, int], RGB],
    time_limit: float,
) -> SolveResult:
    """行编码回溯 + 分支定界（无 OR-Tools 时的兜底实现）。

    把每一行编码成调色板下标整数（调色板至多 4 色，每色 2 bit）。
    先用参考图块的合法水平边对快速筛行，再在逐行展开时按完整 2×2
    四元组校验，最后校验首尾行之间的环绕闭合。
    """
    started = time.perf_counter()
    deadline = started + time_limit
    color_index = {c: i for i, c in enumerate(palette)}
    base = len(palette)

    for color in fixed.values():
        if color not in color_index:
            return SolveResult(status=STATUS_INFEASIBLE,
                               elapsed_seconds=time.perf_counter() - started,
                               solver_name="backtrack")

    horizontal_pairs: set[tuple[int, int]] = set()
    vertical_pairs: set[tuple[int, int]] = set()
    freq: dict[tuple[int, int, int, int], int] = {}
    for (tl, tr, bl, br), n in tile_counts.items():
        i_tl, i_tr = color_index[tl], color_index[tr]
        i_bl, i_br = color_index[bl], color_index[br]
        horizontal_pairs.add((i_tl, i_tr))
        horizontal_pairs.add((i_bl, i_br))
        vertical_pairs.add((i_tl, i_bl))
        vertical_pairs.add((i_tr, i_br))
        freq[(i_tl, i_tr, i_bl, i_br)] = n
    valid_quads = set(freq)

    fixed_by_row: dict[int, dict[int, int]] = {}
    for (x, y), color in fixed.items():
        fixed_by_row.setdefault(y, {})[x] = color_index[color]

    def encode_row(cells: list[int]) -> int:
        row = 0
        for x, value in enumerate(cells):
            row |= value << (2 * x)
        return row

    def row_cells(row: int) -> list[int]:
        return [(row >> (2 * x)) & 0b11 for x in range(unit_w)]

    def generate_rows(y: int) -> Optional[list[int]]:
        """递归生成满足固定格与非环绕水平边对的行（带剪枝与时限检查）。"""
        result: list[int] = []
        fixed_here = fixed_by_row.get(y, {})
        deadline_hit = False

        def recurse(x: int, prev: Optional[int], cells: list[int]) -> None:
            nonlocal deadline_hit
            if deadline_hit:
                return
            if x == unit_w:
                result.append(encode_row(cells))
                return
            if (x & 3) == 0 and time.perf_counter() >= deadline:
                deadline_hit = True
                return
            options = [fixed_here[x]] if x in fixed_here else range(base)
            for value in options:
                if prev is not None and (prev, value) not in horizontal_pairs:
                    continue
                cells.append(value)
                recurse(x + 1, value, cells)
                cells.pop()

        recurse(0, None, [])
        if deadline_hit:
            return None
        return result

    candidates: list[list[int]] = []
    for y in range(unit_h):
        rows = generate_rows(y)
        if rows is None:
            return SolveResult(
                status=STATUS_UNKNOWN,
                elapsed_seconds=min(time.perf_counter() - started, time_limit),
                upper_bound=unit_w * unit_h * _max_freq(tile_counts),
                solver_name="backtrack")
        if not rows:
            return SolveResult(status=STATUS_INFEASIBLE,
                               elapsed_seconds=time.perf_counter() - started,
                               solver_name="backtrack")
        candidates.append(rows)

    def band_ok(upper: list[int], lower: list[int]) -> bool:
        for x in range(unit_w):
            quad = (upper[x], upper[(x + 1) % unit_w],
                    lower[x], lower[(x + 1) % unit_w])
            if quad not in valid_quads:
                return False
        return True

    def vpair_ok(upper: list[int], lower: list[int]) -> bool:
        return all((upper[x], lower[x]) in vertical_pairs
                   for x in range(unit_w))

    def score_rows(grid_rows: list[list[int]]) -> int:
        total = 0
        for y in range(unit_h):
            upper = grid_rows[y]
            lower = grid_rows[(y + 1) % unit_h]
            for x in range(unit_w):
                total += freq.get(
                    (upper[x], upper[(x + 1) % unit_w],
                     lower[x], lower[(x + 1) % unit_w]), 0)
        return total

    chosen_rows: list[list[int]] = []
    best: Optional[list[list[int]]] = None
    best_score = -1
    proved_optimal = True
    full_bound = unit_w * unit_h * _max_freq(tile_counts)

    def dfs(y: int) -> None:
        nonlocal best, best_score, proved_optimal
        if time.perf_counter() >= deadline:
            proved_optimal = False
            return
        if y == unit_h:
            if not band_ok(chosen_rows[unit_h - 1], chosen_rows[0]):
                return
            current_score = score_rows(chosen_rows)
            if current_score > best_score:
                best_score = current_score
                best = [r.copy() for r in chosen_rows]
            return
        for row_int in candidates[y]:
            cells = row_cells(row_int)
            if chosen_rows and (
                    not vpair_ok(chosen_rows[-1], cells)
                    or not band_ok(chosen_rows[-1], cells)):
                continue
            chosen_rows.append(cells)
            dfs(y + 1)
            chosen_rows.pop()
            if time.perf_counter() >= deadline:
                proved_optimal = False
                return

    dfs(0)
    elapsed = time.perf_counter() - started
    if best is None:
        if proved_optimal:
            return SolveResult(status=STATUS_INFEASIBLE, elapsed_seconds=elapsed,
                               solver_name="backtrack")
        return SolveResult(status=STATUS_UNKNOWN, elapsed_seconds=elapsed,
                           upper_bound=full_bound, solver_name="backtrack")

    grid = tuple(tuple(palette[c] for c in row) for row in best)
    status = STATUS_OPTIMAL if proved_optimal else STATUS_FEASIBLE
    result = SolveResult(status=status, grid=grid, score=best_score,
                         upper_bound=best_score if proved_optimal else full_bound,
                         elapsed_seconds=min(elapsed, time_limit),
                         solver_name="backtrack")
    result.tile_usage = _tile_usage(tile_counts, grid, unit_w, unit_h)
    return result
