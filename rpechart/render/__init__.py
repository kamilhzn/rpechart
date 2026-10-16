"""渲染子系统：把谱面变成画面。

分层（与 Remotion 的思路一致：声明式描述 -> 执行）：

* :mod:`geometry` —— 坐标换算与**按秒积分**的下落曲线；
* :mod:`scene` —— 与后端无关的绘制指令（纯数据）；
* :mod:`timeline` —— ``RenderModel.scene_at(t)`` 纯函数，谱面 -> 场景；
* :mod:`raster` —— 把场景画成 PIL 图像；
* :mod:`renderer` —— 把上面几层拼成 ``Renderer.frame(t)``；
* :mod:`videostream` —— 流式读原片帧（视频叠底）；
* :mod:`cli` —— ``python -m rpechart.render`` 的命令行；
* :mod:`assets` —— 官方贴图与字体定位；
* :mod:`theme` —— 颜色、尺寸、HUD 开关。
"""

from .assets import find_resources, find_texture_dir, has_official_assets, load_font, load_texture
from .geometry import (DEFAULT_FLOOR_STEP, DEFAULT_LINE_SPEED, HEIGHT_RATIO,
                       NOTE_WIDTH_RATIO_BASE, RPE_HEIGHT, RPE_WIDTH, SPEED_RATIO,
                       Y_PER_SPEED_SECOND, FloorCurve, note_alpha, note_delta_rpe,
                       note_fade, note_width_px, px_per_rpe_x, px_per_rpe_y,
                       rotate_point, screen_angle, screen_x, screen_y)
from .raster import rasterize
from .renderer import Renderer
from .scene import (HoldDraw, HudState, ImageDraw, LineDraw, NoteDraw, Scene, TextDraw)
from .theme import DEFAULT_THEME, HudTheme, Theme
from .timeline import EventTrack, LineRuntime, RenderModel, scene_at
from .videostream import VideoStream, load_video_frames, open_video_stream

__all__ = [
    "DEFAULT_FLOOR_STEP", "DEFAULT_LINE_SPEED", "DEFAULT_THEME", "EventTrack",
    "FloorCurve", "HEIGHT_RATIO", "HoldDraw", "HudState", "HudTheme", "ImageDraw",
    "LineDraw", "LineRuntime", "NOTE_WIDTH_RATIO_BASE", "NoteDraw", "RPE_HEIGHT",
    "RPE_WIDTH", "RenderModel", "Renderer", "SPEED_RATIO", "Scene", "TextDraw",
    "Theme", "VideoStream", "Y_PER_SPEED_SECOND", "find_resources",
    "find_texture_dir", "has_official_assets", "load_font", "load_texture",
    "load_video_frames", "note_alpha", "note_delta_rpe", "note_fade",
    "note_width_px", "open_video_stream", "px_per_rpe_x", "px_per_rpe_y",
    "rasterize", "rotate_point", "scene_at", "screen_angle", "screen_x", "screen_y",
]