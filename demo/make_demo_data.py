"""生成演示用的参考 PNG 与残片 PNG。

运行后在 demo/ 目录生成：

* ``reference_stripes.png``  8×8 双色竖条纹参考图（图块种类少、容易最优）
* ``fragment_stripes.png``   同尺寸残片，部分像素透明表示缺损
* ``reference_checker.png``  8×8 棋盘格参考图（含跨边/跨角图块）
* ``fragment_checker.png``   棋盘格残片
* ``reference_conflict.png`` 6×6 参考图，用于制造同格异色冲突
* ``fragment_conflict.png``  周期投影后同格异色的残片

直接执行：``python demo/make_demo_data.py``
"""

from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

from PIL import Image

OUT_DIR = Path(__file__).resolve().parent

RED = (220, 60, 60, 255)
BLUE = (60, 90, 220, 255)
GREEN = (60, 180, 90, 255)
YELLOW = (230, 200, 70, 255)
TRANSPARENT = (0, 0, 0, 0)


def save(img: Image.Image, name: str) -> Path:
    path = OUT_DIR / name
    img.save(path, format="PNG")
    return path


def stripes_reference(size: int = 8) -> Image.Image:
    img = Image.new("RGBA", (size, size))
    for y in range(size):
        for x in range(size):
            img.putpixel((x, y), RED if x % 2 == 0 else BLUE)
    return img


def stripes_fragment(size: int = 8) -> Image.Image:
    img = stripes_reference(size)
    # 右下半区挖空为透明缺损
    for y in range(size // 2, size):
        for x in range(size // 2, size):
            img.putpixel((x, y), TRANSPARENT)
    # 再挖几个零散格
    for x, y in [(1, 1), (3, 2), (6, 1)]:
        img.putpixel((x, y), TRANSPARENT)
    return img


def checker_reference(size: int = 8) -> Image.Image:
    img = Image.new("RGBA", (size, size))
    for y in range(size):
        for x in range(size):
            img.putpixel((x, y), GREEN if (x + y) % 2 == 0 else YELLOW)
    return img


def checker_fragment(size: int = 8) -> Image.Image:
    img = checker_reference(size)
    for y in range(size):
        for x in range(size):
            # 保留两条对角带，其余缺损
            if not (abs(x - y) <= 1 or abs(x + y - (size - 1)) <= 1):
                img.putpixel((x, y), TRANSPARENT)
    return img


def conflict_artifacts(size: int = 6) -> tuple[Image.Image, Image.Image]:
    """参考图为纯色竖条；残片在同一投影格放两种颜色制造冲突。"""
    ref = Image.new("RGBA", (size, size))
    for y in range(size):
        for x in range(size):
            ref.putpixel((x, y), RED if x % 2 == 0 else BLUE)

    frag = Image.new("RGBA", (size, size), TRANSPARENT)
    # 单元宽 4：像素 (0,0) 与 (4,0) 投影到同一格 (0,0)，故意异色
    frag.putpixel((0, 0), RED)
    frag.putpixel((4, 0), GREEN)  # GREEN 也在参考调色板之外，先触发同格冲突
    # 其余给一些正常完好色点
    for y in range(1, size):
        frag.putpixel((0, y), RED)
        frag.putpixel((1, y), BLUE)
    return ref, frag


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    artifacts = [
        (stripes_reference(), "reference_stripes.png"),
        (stripes_fragment(), "fragment_stripes.png"),
        (checker_reference(), "reference_checker.png"),
        (checker_fragment(), "fragment_checker.png"),
    ]
    ref_conflict, frag_conflict = conflict_artifacts()
    artifacts.append((ref_conflict, "reference_conflict.png"))
    artifacts.append((frag_conflict, "fragment_conflict.png"))

    for img, name in artifacts:
        path = save(img, name)
        print(f"wrote {path} ({img.size[0]}x{img.size[1]})")

    # 简单自检：确认参考图不透明、残片含透明像素
    for _, name in artifacts[:4]:
        im = Image.open(OUT_DIR / name)
        alpha = im.getchannel("A")
        lo, hi = alpha.getextrema()
        print(f"{name}: alpha range=({lo},{hi})")


if __name__ == "__main__":
    main()
