"""几何：RPE 谱面坐标 -> 屏幕像素。

权威依据是 phira 的 RPE 解析器（``prpr/src/parse/rpe.rs``）与相机（``core/resource.rs``）：

* 下落高度按 **秒** 积分 —— ``height += speed * (end_time - start_time)``（rpe.rs:363 / :483），
  变量是秒不是拍。这一点最容易写错：按「拍」积分的实现看起来也能跑，但下落速度
  会整体快 ``BPM / 106.5`` 倍（BPM 越高错得越多）。
* ``1 RPE 速度单位 = 450 * SPEED_RATIO ≈ 120.229 RPE-Y / 秒``；手册写的 120 与之差 +0.19%。
* 屏幕映射与宽高比无关：``px_x = X * W / 1350``、``px_y = Y * H / 900``。
* 音符旋转 = 判定线旋转 + （``above`` 为假时 180 度）；RPE 的 rotate 事件在解析时取负。
"""

from __future__ import annotations

import bisect
import math
import typing as _t

from ..interp import interpolate_value

# ---------------------------------------------------------------- 常量（phira 真值）
RPE_WIDTH = 1350.0
RPE_HEIGHT = 900.0
HEIGHT_RATIO = 0.83175
NOTE_WIDTH_RATIO_BASE = 0.13175016
SPEED_RATIO = 10.0 / 45.0 / HEIGHT_RATIO
"""``prpr/src/parse/rpe.rs:24``：``10 / 45 / HEIGHT_RATIO``。"""

Y_PER_SPEED_SECOND = 450.0 * SPEED_RATIO
"""线速 1.0 时每秒下落的 RPE-Y 数（≈120.229）。"""

DEFAULT_LINE_SPEED = 1.0
"""判定线没有 speedEvents 时按 RPE 编辑器的默认线速 1.0 处理。"""

DEFAULT_FLOOR_STEP = 1.0 / 240.0
"""下落积分表的时间步长（秒）。"""


# ---------------------------------------------------------------- 坐标换算
def px_per_rpe_x(width: float) -> float:
    """1 个 RPE-X 单位等于多少像素（横向）。"""
    return float(width) / RPE_WIDTH


def px_per_rpe_y(height: float) -> float:
    """1 个 RPE-Y 单位等于多少像素（纵向）。"""
    return float(height) / RPE_HEIGHT


def screen_x(x_rpe: float, width: float) -> float:
    """RPE-X -> 屏幕 x（像素，原点左上）。"""
    return float(width) * 0.5 + float(x_rpe) * px_per_rpe_x(width)


def screen_y(y_rpe: float, height: float) -> float:
    """RPE-Y -> 屏幕 y（像素，原点左上；RPE 的 y 向上）。"""
    return float(height) * 0.5 - float(y_rpe) * px_per_rpe_y(height)


def note_width_px(width: float) -> float:
    """音符的**贴图宽度**（像素）—— ``NOTE_WIDTH_RATIO_BASE * W``。"""
    return NOTE_WIDTH_RATIO_BASE * float(width)


def screen_angle(rpe_rotate: float) -> float:
    """RPE 的 rotate 值（度）-> 屏幕旋转角（度，逆时针为正）。"""
    return -float(rpe_rotate)


def rotate_point(dx: float, dy: float, angle_deg: float) -> tuple[float, float]:
    """在**屏幕坐标系**（y 向下）里把 ``(dx, dy)`` 绕原点旋转 ``angle_deg`` 度。

    ``angle_deg`` 用 :func:`screen_angle` 的口径（逆时针为正）。y 向下时
    ``[[cos, -sin], [sin, cos]]`` 表现为顺时针，所以这里先取负。
    """
    a = math.radians(-float(angle_deg))
    ca, sa = math.cos(a), math.sin(a)
    return dx * ca - dy * sa, dx * sa + dy * ca


# ---------------------------------------------------------------- 下落积分
def speed_segments(events: _t.Sequence, timing) -> list[tuple[float, float, object]]:
    """把速度事件换算成 ``[(起秒, 止秒, 事件), ...]`` 并按起秒排序。"""
    out: list[tuple[float, float, object]] = []
    for ev in events or ():
        t0 = timing.beat_to_second(ev.startTime)
        t1 = timing.beat_to_second(ev.endTime)
        if t1 < t0:
            t0, t1 = t1, t0
        out.append((t0, t1, ev))
    out.sort(key=lambda s: (s[0], s[1]))
    return out


class FloorCurve:
    """判定线的下落高度 ``∫ v(τ) dτ``（RPE-Y 单位），**以秒为积分变量**。

    表用中点法以 :data:`DEFAULT_FLOOR_STEP` 步长预计算，查询时线性插值。
    音符与判定线共用同一张表，所以 ``t == note.startTime`` 时位移恒为 0。
    """

    __slots__ = ("step", "default", "_timing", "_segs", "_starts", "_table", "_last", "_span")

    def __init__(self, events: _t.Sequence, timing, max_seconds: float, *,
                 step: float = DEFAULT_FLOOR_STEP,
                 default: float = DEFAULT_LINE_SPEED) -> None:
        self.step = float(step)
        self.default = float(default)
        self._timing = timing
        self._segs = speed_segments(events, timing)
        self._starts = [s[0] for s in self._segs]
        self._span = max(float(max_seconds), self.step * 2.0)
        n = int(self._span / self.step) + 2
        table = [0.0] * n
        h = 0.0
        k = self.step * Y_PER_SPEED_SECOND
        for i in range(1, n):
            h += self.speed_at((i - 0.5) * self.step) * k
            table[i] = h
        self._table = table
        self._last = self.speed_at(self._span)

    # ------------------------------------------------------------ 速度查询
    def speed_at(self, seconds: float) -> float:
        """某秒处的线速（RPE 速度单位，未乘 :data:`Y_PER_SPEED_SECOND`）。"""
        segs = self._segs
        if not segs:
            return self.default
        idx = bisect.bisect_right(self._starts, seconds) - 1
        if idx < 0:
            return float(segs[0][2].start)
        t0, t1, ev = segs[idx]
        if seconds >= t1:
            return float(ev.end)
        return float(interpolate_value(ev, self._timing.second_to_beat(seconds)))

    # ------------------------------------------------------------ 高度查询
    def at(self, seconds: float) -> float:
        """``∫ v dτ``（RPE-Y 单位）。表外按末速线性外推。"""
        s = float(seconds)
        if s <= 0.0:
            return 0.0
        i = int(s / self.step)
        table = self._table
        if i >= len(table) - 1:
            return table[-1] + (s - (len(table) - 1) * self.step) * self._last * Y_PER_SPEED_SECOND
        f = s / self.step - i
        return table[i] + (table[i + 1] - table[i]) * f

    def __call__(self, seconds: float) -> float:
        return self.at(seconds)


def note_delta_rpe(floor: FloorCurve, note, note_seconds: float, now_seconds: float) -> float:
    """音符相对判定线的纵向位移（RPE-Y，正数在判定线上方）。

    phira：``world_y = y_offset + (note_height - line_height) * note.speed``，
    其中 ``y_offset = note.yOffset * note.speed``（``parse/rpe.rs:538``）。
    """
    delta = floor.at(note_seconds) - floor.at(now_seconds)
    return (delta + float(note.yOffset)) * float(note.speed)


def note_alpha(note, now_seconds: float, note_seconds: float) -> float:
    """音符自身的 alpha（0~1），含 ``visibleTime`` 淡入。

    phira ``parse/rpe.rs:578-587``：``visibleTime >= 音符时刻`` 时是常量；
    否则在 ``(音符时刻 - visibleTime)`` 秒里从 0 线性升到 ``alpha/255``。
    """
    base = max(0.0, min(255.0, float(note.alpha))) / 255.0
    vt = float(note.visibleTime)
    if vt >= note_seconds:
        return base
    span = note_seconds - vt
    if span <= 0.0:
        return base
    u = (now_seconds - vt) / span
    if u <= 0.0:
        return 0.0
    if u >= 1.0:
        return base
    return base * u


def note_fade(note_seconds: float, now_seconds: float, fadeout: float) -> float:
    """音符落到判定线之后的 0.16 秒淡出（phira ``FADEOUT_TIME``）。"""
    if now_seconds <= note_seconds:
        return 1.0
    if fadeout <= 0.0:
        return 0.0
    return max(0.0, 1.0 - (now_seconds - note_seconds) / fadeout)
