"""场景图：与渲染后端无关的绘制指令。

``timeline.scene_at`` 产出一个 :class:`Scene`，``raster`` 把它画成 PIL 图像。
这样求值与绘制彻底分开：几何/时间可以单独测，后端可以替换。
"""

from __future__ import annotations

import typing as _t


class NoteDraw:
    """一个音符（Tap / Flick / Drag）的绘制指令。全部是**屏幕像素**。"""

    __slots__ = ("kind", "x", "y", "angle", "width", "height", "alpha", "color",
                 "above", "fake", "x_scale", "texture")

    def __init__(self, kind: int, x: float, y: float, *, angle: float = 0.0,
                 width: float, height: float, alpha: float = 1.0,
                 color: _t.Tuple[int, int, int] = (255, 255, 255),
                 above: bool = True, fake: bool = False, x_scale: float = 1.0,
                 texture: str = "") -> None:
        self.kind = int(kind)
        self.x = float(x)
        self.y = float(y)
        self.angle = float(angle)
        self.width = float(width)
        self.height = float(height)
        self.alpha = float(alpha)
        self.color = tuple(color)
        self.above = bool(above)
        self.fake = bool(fake)
        self.x_scale = float(x_scale)
        self.texture = texture

    def __repr__(self) -> str:
        return (f"NoteDraw(kind={self.kind}, x={self.x:.1f}, y={self.y:.1f}, "
                f"alpha={self.alpha:.2f})")


class HoldDraw:
    """一条长按：头、身、尾三个位置都在屏幕上，身按头尾连线绘制。"""

    __slots__ = ("x_head", "y_head", "x_tail", "y_tail", "angle", "width", "alpha",
                 "color", "above", "fake", "texture_head", "texture_body", "texture_tail")

    def __init__(self, *, x_head: float, y_head: float, x_tail: float, y_tail: float,
                 angle: float = 0.0, width: float, alpha: float = 1.0,
                 color: _t.Tuple[int, int, int] = (255, 255, 255),
                 above: bool = True, fake: bool = False,
                 texture_head: str = "", texture_body: str = "",
                 texture_tail: str = "") -> None:
        self.x_head = float(x_head)
        self.y_head = float(y_head)
        self.x_tail = float(x_tail)
        self.y_tail = float(y_tail)
        self.angle = float(angle)
        self.width = float(width)
        self.alpha = float(alpha)
        self.color = tuple(color)
        self.above = bool(above)
        self.fake = bool(fake)
        self.texture_head = texture_head
        self.texture_body = texture_body
        self.texture_tail = texture_tail


class HitDraw:
    """一次打击特效（判定环）的绘制指令。

    贴图是官方 ``Texture/HE/img-N.png`` 序列（真机截图里的旋转判定环与它逐帧
    模板匹配得上，误差 0.071，远小于 ``judgeball.png`` 的形态差异）。
    ``size`` 是正方形的边长（像素）。
    """

    __slots__ = ("x", "y", "size", "alpha", "texture", "angle", "kind", "color")

    def __init__(self, x: float, y: float, texture: str, *, size: float,
                 alpha: float = 1.0, angle: float = 0.0, kind: int = 0,
                 color=(255, 255, 255)) -> None:
        self.x = float(x)
        self.y = float(y)
        self.texture = str(texture)
        self.size = float(size)
        self.alpha = float(alpha)
        self.angle = float(angle)
        self.kind = int(kind)
        self.color = tuple(color) if color is not None else (255, 255, 255)

    def __repr__(self) -> str:
        return (f"HitDraw({self.texture!r}, x={self.x:.1f}, y={self.y:.1f}, "
                f"size={self.size:.1f})")


class LineDraw:
    """一条判定线（屏幕像素端点）。"""

    __slots__ = ("index", "x0", "y0", "x1", "y1", "thickness", "alpha", "color",
                 "texture", "label")

    def __init__(self, index: int, x0: float, y0: float, x1: float, y1: float, *,
                 thickness: float, alpha: float = 1.0,
                 color: _t.Tuple[int, int, int] = (255, 255, 255),
                 texture: str = "", label: str = "") -> None:
        self.index = int(index)
        self.x0 = float(x0)
        self.y0 = float(y0)
        self.x1 = float(x1)
        self.y1 = float(y1)
        self.thickness = float(thickness)
        self.alpha = float(alpha)
        self.color = tuple(color)
        self.texture = texture
        self.label = label


class TextDraw:
    """一段 HUD 文本（屏幕像素）。``anchor`` 是 PIL 的锚点字符串。"""

    __slots__ = ("x", "y", "text", "font", "size", "color", "anchor", "alpha")

    def __init__(self, x: float, y: float, text: str, *, font: str = "cmdysj",
                 size: float, color: _t.Tuple[int, int, int] = (255, 255, 255),
                 anchor: str = "la", alpha: float = 1.0) -> None:
        self.x = float(x)
        self.y = float(y)
        self.text = str(text)
        self.font = font
        self.size = float(size)
        self.color = tuple(color)
        self.anchor = anchor
        self.alpha = float(alpha)


class ImageDraw:
    """一张 HUD 贴图（屏幕像素，左上角定位）。"""

    __slots__ = ("x", "y", "width", "height", "name", "alpha", "angle")

    def __init__(self, x: float, y: float, name: str, *, width: float, height: float,
                 alpha: float = 1.0, angle: float = 0.0) -> None:
        self.x = float(x)
        self.y = float(y)
        self.name = str(name)
        self.width = float(width)
        self.height = float(height)
        self.alpha = float(alpha)
        self.angle = float(angle)


class HudState:
    """HUD 上随谱面变化的数字。"""

    __slots__ = ("combo", "score", "song", "level", "paused")

    def __init__(self, *, combo: int = 0, score: int = 0, song: str = "", level: str = "",
                 paused: bool = True) -> None:
        self.combo = int(combo)
        self.score = int(score)
        self.song = str(song)
        self.level = str(level)
        self.paused = bool(paused)


class Scene:
    """某一时刻的完整画面描述（纯数据）。"""

    __slots__ = ("t", "width", "height", "background", "lines", "hits", "holds",
                 "notes", "texts", "images", "hud", "meta")

    def __init__(self, t: float, width: int, height: int, *, background=(0, 0, 0)) -> None:
        self.t = float(t)
        self.width = int(width)
        self.height = int(height)
        self.background = tuple(background)
        self.lines: list[LineDraw] = []
        self.hits: list[HitDraw] = []
        self.holds: list[HoldDraw] = []
        self.notes: list[NoteDraw] = []
        self.texts: list[TextDraw] = []
        self.images: list[ImageDraw] = []
        self.hud = HudState()
        self.meta: dict = {}

    def __repr__(self) -> str:
        return (f"Scene(t={self.t:.3f}, {self.width}x{self.height}, "
                f"lines={len(self.lines)}, holds={len(self.holds)}, notes={len(self.notes)})")