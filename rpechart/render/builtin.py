"""内置键形：官方贴图缺失时程序化生成的 Tap / Drag / Flick / Hold。

仓库不附带第三方贴图（见 docs/data-safety.md），但预览和导出不能只剩白色
色块。这里用 Pillow 画一套与官方纹理同比例、同色系的简化键形：白色描边 +
标准填充色，长按是一条带白色描边的竖向渐变长条。所有图像只生成一次并缓存，
渲染循环里仍然走 raster 原有的缩放/旋转缓存。
"""

from __future__ import annotations

import typing as _t

from PIL import Image, ImageDraw

_RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS

#: 官方 Tap2 / Drag2 / Flick2 / Hold 的主色（用 PIL 从 Texture/ 采样得到）。
TAP_COLOR = (10, 195, 255)
DRAG_COLOR = (240, 237, 105)
FLICK_COLOR = (255, 66, 101)
HOLD_COLOR = (153, 230, 252)

#: 生成时的超采样倍数：先画大图再缩回去，斜边和描边不会锯齿。
_SCALE = 4

#: name -> (宽, 高, 形状)。宽高比对齐官方贴图，免得内置键比官方键胖/瘦一截。
_SPECS = {
    "Tap2.png": (512, 47, "tap"),
    "Drag2.png": (512, 28, "drag"),
    "Flick2.png": (512, 94, "flick"),
    # 长按体按 raster._draw_hold 的约定做成横向条纹：渲染时先按判定线角度
    # 旋转，再拉成头到尾的长度。竖向的 256x492 会在旋转后把渐变拧到侧边。
    "Hold.png": (512, 96, "hold"),
    "HoldHead.png": (512, 24, "head"),
    "HoldEnd.png": (512, 24, "tail"),
}

_CACHE: dict = {}
_MISSING = object()


def texture(name: str) -> _t.Optional[Image.Image]:
    """返回内置键形 RGBA；不是已知键形时返回 None。"""
    key = str(name)
    cached = _CACHE.get(key, _MISSING)
    if cached is not _MISSING:
        return cached
    spec = _SPECS.get(key)
    if spec is None:
        _CACHE[key] = None
        return None
    image = _render(*spec)
    _CACHE[key] = image
    return image


def clear_cache() -> None:
    _CACHE.clear()


def _render(width: int, height: int, shape: str) -> Image.Image:
    w = int(width) * _SCALE
    h = int(height) * _SCALE
    image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if shape == "tap":
        _draw_arrow_key(draw, w, h, TAP_COLOR)
    elif shape == "drag":
        _draw_arrow_key(draw, w, h, DRAG_COLOR)
    elif shape == "flick":
        _draw_arrow_key(draw, w, h, FLICK_COLOR, split=True)
    elif shape == "hold":
        _draw_hold_body(image, draw, w, h)
    elif shape in ("head", "tail"):
        _draw_hold_cap(draw, w, h, HOLD_COLOR if shape == "head"
                       else (238, 250, 255))
    return image.resize((int(width), int(height)), _RESAMPLE)


def _draw_arrow_key(draw: ImageDraw.ImageDraw, w: int, h: int, fill,
                    split: bool = False) -> None:
    """横向键：中间一根尖头长条 + 两侧各一个白色箭头（与官方轮廓同构）。"""
    # 中间长条：左右两端向中点收成尖角。
    x0, x1 = 0.092 * w, 0.908 * w
    bevel = 0.022 * w
    cy = h * 0.5
    outer = [(x0 + bevel, 0), (x1 - bevel, 0), (x1, cy),
             (x1 - bevel, h), (x0 + bevel, h), (x0, cy)]
    draw.polygon(outer, fill=(255, 255, 255, 255))

    # 内部彩色区域：四周留出一圈白边；上下边比左右边厚，和官方贴图一致。
    ix0, ix1 = x0 + 0.046 * w, x1 - 0.046 * w
    iy0, iy1 = 0.22 * h, 0.78 * h
    ib = 0.012 * w
    inner = [(ix0 + ib, iy0), (ix1 - ib, iy0), (ix1, cy),
             (ix1 - ib, iy1), (ix0 + ib, iy1), (ix0, cy)]
    draw.polygon(inner, fill=tuple(fill) + (255,))
    if split:
        # 红色 Flick 的中间缺口：用一块白色「闪电」把彩色区分成左右两半。
        zig = [(0.455 * w, iy0), (0.545 * w, iy0), (0.515 * w, cy),
               (0.545 * w, iy1), (0.455 * w, iy1), (0.485 * w, cy)]
        draw.polygon(zig, fill=(255, 255, 255, 255))
        # 缺口两侧再各点一个小三角，读起来更像官方分裂箭头。
        draw.polygon([(0.500 * w, iy0 + (cy - iy0) * 0.10),
                      (0.530 * w, iy0 + (cy - iy0) * 0.42),
                      (0.500 * w, iy0 + (cy - iy0) * 0.74)],
                     fill=tuple(fill) + (255,))
        draw.polygon([(0.500 * w, iy1 - (iy1 - cy) * 0.10),
                      (0.470 * w, iy1 - (iy1 - cy) * 0.42),
                      (0.500 * w, iy1 - (iy1 - cy) * 0.74)],
                     fill=tuple(fill) + (255,))

    # 两侧箭头：顶部在 x≈0.068/0.932，中部伸到 x≈0.046/0.954。
    draw.polygon([(0.046 * w, cy), (0.068 * w, 0), (0.091 * w, 0),
                  (0.069 * w, cy), (0.091 * w, h), (0.068 * w, h)],
                 fill=(255, 255, 255, 255))
    draw.polygon([(0.954 * w, cy), (0.932 * w, 0), (0.909 * w, 0),
                  (0.931 * w, cy), (0.909 * w, h), (0.932 * w, h)],
                 fill=(255, 255, 255, 255))


def _draw_hold_body(image: Image.Image, draw: ImageDraw.ImageDraw,
                    w: int, h: int) -> None:
    """长按体：横向白描边条纹，内部沿长轴从浅青渐变到蓝色。"""
    radius = max(2, int(h * 0.30))
    border = max(1, int(h * 0.045))

    # 先画渐变，再用圆角矩形做蒙版，避免渐变填满方角。
    grad = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    px = grad.load()
    head = (247, 253, 255, 150)
    mid = (200, 243, 254, 210)
    tail = (150, 230, 252, 255)
    for x in range(w):
        t = x / max(1, w - 1)
        if t < 0.5:
            u = t * 2.0
            color = tuple(int(round(head[i] + (mid[i] - head[i]) * u))
                          for i in range(4))
        else:
            u = (t - 0.5) * 2.0
            color = tuple(int(round(mid[i] + (tail[i] - mid[i]) * u))
                          for i in range(4))
        for y in range(h):
            px[x, y] = color

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1],
                                           radius=radius, fill=255)
    image.paste(grad, (0, 0), mask)
    # 两条很淡的侧高光；旋转后正好落在长条的两侧。
    # 注意：ImageDraw 在 RGBA 上直接写 alpha 不会和底色混合，会把高光画成
    # 透明缝；先画到独立图层再 alpha_composite。
    highlight = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    hd = ImageDraw.Draw(highlight)
    for fy in (0.15, 0.85):
        hd.line([(int(w * 0.04), int(fy * h)),
                 (int(w * 0.96), int(fy * h))],
                fill=(255, 255, 255, 60), width=max(1, int(h * 0.018)))
    image.alpha_composite(highlight)
    draw.rounded_rectangle([border // 2, border // 2,
                            w - 1 - border // 2, h - 1 - border // 2],
                           radius=radius, outline=(255, 255, 255, 255),
                           width=border)


def _draw_hold_cap(draw: ImageDraw.ImageDraw, w: int, h: int, fill) -> None:
    """长按头/尾：一条细白描边圆角条。"""
    border = max(1, int(h * 0.14))
    radius = max(1, h // 2)
    draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius,
                           fill=(255, 255, 255, 255))
    draw.rounded_rectangle([border, border, w - 1 - border, h - 1 - border],
                           radius=max(1, radius - border),
                           fill=tuple(fill) + (255,))
