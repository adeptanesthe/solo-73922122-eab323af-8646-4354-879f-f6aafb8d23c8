"""PNG 解析、校验与参考图 2×2 图块统计。

约定
----
* 坐标统一使用 ``(x, y)``，原点在左上角，x 向右、y 向下。
* 颜色用归一化后的 RGB 三元组 ``(r, g, b)`` 表示。
* 残片的透明像素（alpha < 128）表示缺损；不透明像素是完好色点。
* 重复单元按周期对齐：残片像素 ``(x, y)`` 对应纹版格 ``(x % W, y % H)``。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from typing import Iterable

from PIL import Image, UnidentifiedImageError

MAX_SIDE = 64
MIN_UNIT = 4
MAX_UNIT = 12
MAX_REF_COLORS = 4
ALPHA_THRESHOLD = 128

# 2×2 图块按 (左上, 右上, 左下, 右下) 顺序展平
TileKey = tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int],
                tuple[int, int, int]]
RGB = tuple[int, int, int]


class ImageValidationError(ValueError):
    """输入图片不符合接口约定（对应 HTTP 400）。"""


@dataclass(frozen=True)
class ReferenceImage:
    """参考图解析结果。"""

    width: int
    height: int
    colors: tuple[RGB, ...]
    grid: tuple[tuple[RGB, ...], ...]
    tile_counts: dict[TileKey, int]

    def tile_frequency(self, key: TileKey) -> int:
        return self.tile_counts.get(key, 0)


@dataclass(frozen=True)
class FragmentImage:
    """残片解析结果。

    ``points`` 为完好色点，键是残片原图坐标，值是颜色。
    """

    width: int
    height: int
    points: dict[tuple[int, int], RGB]


@dataclass(frozen=True)
class CellConflict:
    """同格异色冲突（对应 HTTP 409）。"""

    cell: tuple[int, int]
    positions: tuple[tuple[int, int], ...]
    colors: tuple[RGB, ...]


def _load_rgba(data: bytes) -> Image.Image:
    try:
        img = Image.open(BytesIO(data))
    except UnidentifiedImageError as exc:
        raise ImageValidationError("无法识别的图片文件，需要 PNG") from exc
    img.load()
    if img.format not in (None, "PNG"):
        # BytesIO 中 Pillow 通常能识别 PNG；非 PNG 直接拒绝
        raise ImageValidationError(f"仅接受 PNG 图片，收到 {img.format}")
    return img.convert("RGBA")


def _check_size(width: int, height: int, name: str) -> None:
    if width <= 0 or height <= 0:
        raise ImageValidationError(f"{name}尺寸非法：{width}×{height}")
    if width > MAX_SIDE or height > MAX_SIDE:
        raise ImageValidationError(
            f"{name}每边至多 {MAX_SIDE} 像素，收到 {width}×{height}")


def parse_reference(data: bytes) -> ReferenceImage:
    """解析参考 PNG：必须完全不透明，且最多 4 种颜色。"""
    img = _load_rgba(data)
    width, height = img.size
    _check_size(width, height, "参考图")

    grid: list[tuple[RGB, ...]] = []
    colors: set[RGB] = set()
    for y in range(height):
        row: list[RGB] = []
        for x in range(width):
            r, g, b, a = img.getpixel((x, y))
            if a < ALPHA_THRESHOLD:
                raise ImageValidationError(
                    f"参考图必须全部不透明，像素 ({x}, {y}) 为透明")
            color = (int(r), int(g), int(b))
            row.append(color)
            colors.add(color)
        grid.append(tuple(row))

    if len(colors) > MAX_REF_COLORS:
        raise ImageValidationError(
            f"参考图最多 {MAX_REF_COLORS} 色，实际 {len(colors)} 色")

    ordered_colors = tuple(sorted(colors))
    tile_counts = count_tiles(tuple(grid), width, height)
    return ReferenceImage(
        width=width,
        height=height,
        colors=ordered_colors,
        grid=tuple(grid),
        tile_counts=tile_counts,
    )


def parse_fragment(data: bytes) -> FragmentImage:
    """解析残片 PNG：透明=缺损，不透明=完好色点。"""
    img = _load_rgba(data)
    width, height = img.size
    _check_size(width, height, "残片图")

    points: dict[tuple[int, int], RGB] = {}
    for y in range(height):
        for x in range(width):
            r, g, b, a = img.getpixel((x, y))
            if a >= ALPHA_THRESHOLD:
                points[(x, y)] = (int(r), int(g), int(b))
    return FragmentImage(width=width, height=height, points=points)


def count_tiles(grid: tuple[tuple[RGB, ...], ...], width: int,
                height: int) -> dict[TileKey, int]:
    """统计参考图中每个左上角位置的 2×2 图块出现次数（跨边、跨角环绕）。

    参考图每格都可作为图块左上角，共 ``width × height`` 个图块；
    越界坐标按环绕（toroidal）取模处理。
    """
    counter: Counter[TileKey] = Counter()
    for y in range(height):
        for x in range(width):
            key = (
                grid[y][x],
                grid[y][(x + 1) % width],
                grid[(y + 1) % height][x],
                grid[(y + 1) % height][(x + 1) % width],
            )
            counter[key] += 1
    return dict(counter)


def project_points(
    points: dict[tuple[int, int], RGB],
    unit_w: int,
    unit_h: int,
) -> tuple[dict[tuple[int, int], RGB], list[CellConflict]]:
    """把残片像素按周期投影到纹版格，返回 (格约束, 冲突列表)。

    同一格被多个完好色点覆盖且颜色一致时只保留一条约束；
    颜色不一致时产生 :class:`CellConflict`，并给出两处（或多处）原坐标。
    """
    cells: dict[tuple[int, int], RGB] = {}
    by_cell: dict[tuple[int, int], list[tuple[int, int]]] = {}
    conflict_colors: dict[tuple[int, int], set[RGB]] = {}
    for (x, y), color in points.items():
        cell = (x % unit_w, y % unit_h)
        by_cell.setdefault(cell, []).append((x, y))
        if cell in cells:
            if cells[cell] != color:
                conflict_colors.setdefault(cell, {cells[cell]}).add(color)
        else:
            cells[cell] = color

    conflicts: list[CellConflict] = []
    for cell, extra_colors in conflict_colors.items():
        positions = tuple(sorted(by_cell[cell]))
        # 该格实际出现过的全部颜色，按色值排序保证响应稳定
        seen = sorted({points[pos] for pos in positions})
        conflicts.append(CellConflict(cell=cell, positions=positions,
                                      colors=tuple(seen)))
    return cells, conflicts


def validate_unit_size(unit_w: int, unit_h: int) -> None:
    for name, value in (("unit_w", unit_w), ("unit_h", unit_h)):
        if not isinstance(value, int) or not (MIN_UNIT <= value <= MAX_UNIT):
            raise ImageValidationError(
                f"{name} 必须是 {MIN_UNIT}～{MAX_UNIT} 的整数，收到 {value}")


def grid_to_png(grid: Iterable[Iterable[RGB]], scale: int = 1) -> bytes:
    """把纹版（RGB 网格）编码为不透明 PNG。"""
    rows = [list(row) for row in grid]
    height = len(rows)
    width = len(rows[0]) if rows else 0
    if scale < 1:
        raise ValueError("scale 必须 >= 1")
    img = Image.new("RGBA", (width * scale, height * scale))
    for y, row in enumerate(rows):
        for x, color in enumerate(row):
            for dy in range(scale):
                for dx in range(scale):
                    img.putpixel((x * scale + dx, y * scale + dy),
                                 (*color, 255))
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
