"""渲染器入口：把 ``chart.json`` 变成一帧帧画面。

这一层把 :mod:`timeline`（纯函数场景）与 :mod:`raster`（PIL 光栅化）拼起来，
并负责**背景**：没给底图时用深色渐变，给了曲绘/视频帧就模糊压暗。

它同时是 :func:`rpechart.video.render_chart_video` 认得的那个 ``Renderer``：
只要暴露 ``timing`` / ``content_beats`` / ``frame(t, size=..., ...)`` 就够了。
"""

from __future__ import annotations

import typing as _t
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from ..model import Chart
from ..timing import Timing
from . import raster
from .theme import DEFAULT_THEME, Theme
from .timeline import RenderModel
from .videostream import VideoStream, open_video_stream

__all__ = ["Renderer", "load_chart_file", "VideoStream", "open_video_stream"]

#: 无曲绘时的兜底底色。真机截图（某一首成品曲，978x642）的背景是**纯黑**
#: （lum<40 占 97.3%，median RGB=(0,0,0)），所以兜底也从纯黑出发，只留一层
#: 极淡的中心提亮，避免整屏死黑。
BG_TOP = (0, 0, 0)
BG_BOTTOM = (0, 0, 0)
#: 中心提亮的强度（0 = 完全纯黑）
BG_VEIL = 18
#: 底图默认的模糊与压暗
DEFAULT_BLUR_RATIO = 1.0 / 240.0
DEFAULT_DIM = 0.45


def _mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def load_chart_file(path):
    """读谱面：``.json`` 直接读，``.pez`` 取包里最浅的那个 json。"""
    from ..io import load_chart

    return load_chart(path)


class Renderer:
    """离线渲染器：``Renderer(chart_path).frame(t, size=(w, h))``。

    ``chart`` 可以是路径，也可以直接给 :class:`~rpechart.model.Chart`。
    """

    def __init__(self, chart, *, theme: _t.Optional[Theme] = None,
                 floor_step=None) -> None:
        if isinstance(chart, Chart):
            self.chart = chart
            self.path = None
        else:
            self.path = str(chart)
            self.chart = load_chart_file(chart)
        self.theme = theme or DEFAULT_THEME
        self.timing = Timing(self.chart.BPMList)
        self.model = RenderModel(self.chart, floor_step=floor_step) \
            if floor_step is not None else RenderModel(self.chart)
        self.content_beats = self.model.content_beats
        self.n_beats = self.model.n_beats
        self._bg_cache: dict = {}
        #: 渐变兜底底图的 RGBA 版本。每帧都是同一张图，转换一次就够了；
        #: `frame` 把它交给 `raster.rasterize(..., base_changed=False)`，
        #: 由 raster 自己 copy()（写时复制）后再画，不会污染缓存。
        self._bg_rgba_cache: dict = {}

    # ------------------------------------------------------------ 背景
    def _gradient(self, w: int, h: int) -> Image.Image:
        key = ("grad", int(w), int(h))
        img = self._bg_cache.get(key)
        if img is not None:
            return img
        # 每行一个颜色再整体拉伸 —— 比逐像素快得多
        col = Image.new("RGB", (1, h))
        px = col.load()
        for y in range(h):
            px[0, y] = _mix(BG_TOP, BG_BOTTOM, y / max(1, h - 1))
        img = col.resize((w, h), Image.BILINEAR)
        veil = Image.new("L", (w, h), 0)
        ImageDraw.Draw(veil).ellipse(
            [w * 0.5 - w * 0.42, h * 0.5 - h * 0.34,
             w * 0.5 + w * 0.42, h * 0.5 + h * 0.34], fill=34)
        veil = veil.filter(ImageFilter.GaussianBlur(max(4.0, w / 24.0)))
        img = Image.composite(Image.new("RGB", (w, h), (BG_VEIL, BG_VEIL, BG_VEIL)),
                               img, veil)
        self._bg_cache[key] = img
        return img

    def _gradient_rgba(self, w: int, h: int) -> Image.Image:
        """渐变兜底的 RGBA 版本（按尺寸缓存），供 ``frame`` 复用同一块缓冲。"""
        key = ("grad_rgba", int(w), int(h))
        img = self._bg_rgba_cache.get(key)
        if img is None:
            img = self._gradient(w, h).convert("RGBA")
            self._bg_rgba_cache[key] = img
        return img

    def backdrop(self, w: int, h: int, bg=None, *, blur=None,
                 dim=None) -> Image.Image:
        """底图：``bg=None`` 用深色渐变，给了图就模糊 + 压暗。"""
        if bg is None:
            return self._gradient(w, h)
        base = bg.convert("RGB").resize((w, h), Image.BILINEAR)
        if blur is None:
            blur = max(1.0, min(w, h) * DEFAULT_BLUR_RATIO)
        if blur > 0:
            base = base.filter(ImageFilter.GaussianBlur(blur))
        if dim is None:
            dim = DEFAULT_DIM
        if dim <= 0:
            return base
        return Image.blend(base, Image.new("RGB", (w, h), (10, 10, 20)), dim)

    # ------------------------------------------------------------ 出图
    def frame(self, t_sec: float, size=(1920, 1080), *, bg=None, hud=None,
              grid: bool = False, blur=None, dim=None) -> Image.Image:
        """``t_sec`` 秒处的一帧。``hud=None`` 时用 theme 的开关。"""
        w, h = int(size[0]), int(size[1])
        theme = self.theme
        if hud is not None:
            theme = theme.copy(hud=theme.hud.copy(enabled=bool(hud)))
        scene = self.model.scene_at(float(t_sec), w, h, theme)
        if bg is None:
            # 兜底渐变每帧都是同一张：直接交出缓存的 RGBA 缓冲，
            # 让 raster 跳过每帧一次的 convert("RGBA")。
            image = raster.rasterize(scene, theme, base=self._gradient_rgba(w, h),
                                     base_changed=False)
        else:
            backdrop = self.backdrop(w, h, bg, blur=blur, dim=dim)
            image = raster.rasterize(scene, theme, base=backdrop)
        if grid:
            image = self._draw_grid(image, w, h)
        return image

    def _draw_grid(self, image: Image.Image, w: int, h: int) -> Image.Image:
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        dr = ImageDraw.Draw(overlay)
        for gy in (0.25, 0.5, 0.75):
            dr.line([(0, h * gy), (w, h * gy)], fill=(255, 255, 255, 28), width=1)
        for gx in (0.25, 0.5, 0.75):
            dr.line([(w * gx, 0), (w * gx, h)], fill=(255, 255, 255, 28), width=1)
        out = image.convert("RGBA")
        out.alpha_composite(overlay)
        return out.convert("RGB")

    def rasterize_at(self, t_sec: float, size=(1920, 1080), *,
                     base=None) -> Image.Image:
        """只画谱面本身，不含背景（默认纯黑底）—— 便于叠图与测试。"""
        w, h = int(size[0]), int(size[1])
        scene = self.model.scene_at(float(t_sec), w, h, self.theme)
        return raster.rasterize(scene, self.theme, base=base)

    # ------------------------------------------------------------ 统计
    def stats(self, step_beats: float = 4.0):
        return self.model.stats(step_beats)

    def visible_notes(self, index: int, seconds: float):
        return self.model.visible_notes(index, seconds)

    @property
    def note_count(self) -> int:
        return self.model.note_count

    def __repr__(self) -> str:  # pragma: no cover - 只为调试好看
        return (f"Renderer({self.path!r}, content_beats={self.content_beats:g}, "
                f"lines={len(self.model.lines)})")