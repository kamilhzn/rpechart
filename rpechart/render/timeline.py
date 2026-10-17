"""时间线：把一张谱面在某一时刻的状态算成一幅 :class:`~rpechart.render.scene.Scene`。

``scene_at`` 是**纯函数**：给定 ``(谱面, 秒, 画布尺寸, 主题)`` 一定得到同一幅场景，
不依赖任何全局状态。这样它可以单独测试、可以并行、可以从任意时刻续渲。
"""

from __future__ import annotations

import bisect
import math
import typing as _t

from ..beat import Beat
from ..easing import bezier as _bezier_curve
from ..easing import ease as _ease
from ..interp import interpolate_value
from ..model import EVENT_ALPHA, EVENT_MOVE_X, EVENT_MOVE_Y, EVENT_ROTATE, EVENT_SPEED
from . import geometry as G
from .scene import (HitDraw, HoldDraw, HudState, ImageDraw, LineDraw, NoteDraw,
                    Scene, TextDraw)
from .theme import Theme

_BEAT = Beat.of
_MISSING = object()


def _clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return float(value)


def _progress_float(event, frac: float) -> float:
    """与 :func:`rpechart.interp._progress` 同义，但直接用拍的小数，省掉 Beat 构造。"""
    st = event.startTime.frac
    et = event.endTime.frac
    if et <= st:
        return 1.0
    raw = (frac - st) / (et - st)
    raw = 0.0 if raw < 0.0 else (1.0 if raw > 1.0 else raw)
    left = 0.0 if event.easingLeft is None else float(event.easingLeft)
    right = 1.0 if event.easingRight is None else float(event.easingRight)
    if left != 0.0 or right != 1.0:
        span = right - left
        if span <= 0.0:
            return 1.0
        raw = _clamp01((raw - left) / span)
    points = getattr(event, "bezierPoints", None)
    if getattr(event, "bezier", 0) and points is not None and len(points) >= 4:
        return _bezier_curve(raw, points)
    return _ease(int(event.easingType), raw)


def _lerp_value(f: float, start, end):
    if isinstance(start, str) or isinstance(end, str):
        return start
    if isinstance(start, (list, tuple)) or isinstance(end, (list, tuple)):
        a = list(start) if isinstance(start, (list, tuple)) else [start]
        b = list(end) if isinstance(end, (list, tuple)) else [end]
        return [round(x + (y - x) * f, 6) for x, y in zip(a, b)]
    return start + (end - start) * f


class EventTrack:
    """一条按 ``startTime`` 排好序的事件轨，用 float 二分定位。

    语义与 :func:`rpechart.interp.value_at_resolved` 一致：先找覆盖 ``t`` 的事件；
    没有覆盖则取最后一个 ``startTime <= t`` 的事件的 ``end``（RPE 的「保持到下一段」）。
    """

    __slots__ = ("events", "starts", "ends")

    def __init__(self, events: _t.Sequence) -> None:
        evs = sorted(events or (), key=lambda e: (e.startTime.frac, e.endTime.frac))
        self.events = list(evs)
        self.starts = [e.startTime.frac for e in evs]
        self.ends = [e.endTime.frac for e in evs]

    def __bool__(self) -> bool:
        return bool(self.events)

    def __len__(self) -> int:
        return len(self.events)

    def at_beat(self, frac: float, default=0.0):
        if not self.events:
            return default
        idx = bisect.bisect_right(self.starts, frac) - 1
        if idx < 0:
            return default
        if frac < self.ends[idx]:
            return _lerp_value(_progress_float(self.events[idx], frac),
                               self.events[idx].start, self.events[idx].end)
        return self.events[idx].end


def _collect(line, attr: str) -> list:
    out: list = []
    for layer in getattr(line, "eventLayers", None) or ():
        out.extend(getattr(layer, attr, None) or ())
    return out


def _collect_extended(line, attr: str) -> list:
    ext = getattr(line, "extended", None)
    if ext is None:
        return []
    return list(getattr(ext, attr, None) or ())


class LineRuntime:
    """一条判定线预编译好的求值器。"""

    __slots__ = ("index", "line", "x", "y", "rot", "alpha", "scale_x", "scale_y",
                 "color", "incline", "text", "floor", "father", "rotate_with_father",
                 "draw_below", "texture", "is_gif")

    def __init__(self, index: int, line, timing, max_seconds: float, *,\
                 floor_step: float) -> None:
        self.index = int(index)
        self.line = line
        self.x = EventTrack(_collect(line, EVENT_MOVE_X))
        self.y = EventTrack(_collect(line, EVENT_MOVE_Y))
        self.rot = EventTrack(_collect(line, EVENT_ROTATE))
        self.alpha = EventTrack(_collect(line, EVENT_ALPHA))
        self.scale_x = EventTrack(_collect_extended(line, "scaleXEvents"))
        self.scale_y = EventTrack(_collect_extended(line, "scaleYEvents"))
        self.color = EventTrack(_collect_extended(line, "colorEvents"))
        self.incline = EventTrack(_collect_extended(line, "inclineEvents"))
        self.text = EventTrack(_collect_extended(line, "textEvents"))
        self.floor = G.FloorCurve(_collect(line, EVENT_SPEED), timing, max_seconds,
                                  step=floor_step)
        # 注意：**不能**写成 `getattr(...) or -1` —— father=0 是合法的
        # 「父线是第 0 条」，裸 or 会把它吞成 -1，父链在渲染时静默断开
        # （tests/fixtures/charts/multi.json 的 child 线正是 father: 0）。
        try:
            self.father = int(getattr(line, "father", -1))
        except (TypeError, ValueError):
            self.father = -1
        self.rotate_with_father = bool(getattr(line, "rotateWithFather", True))
        self.draw_below = int(getattr(line, "isCover", 1) or 0) != 1
        self.texture = str(getattr(line, "Texture", "line.png") or "line.png")
        self.is_gif = bool(getattr(line, "isGif", False))


class RenderModel:
    """一张谱面的可渲染模型：预编译事件轨、下落曲线与内容长度。"""

    def __init__(self, chart, *, floor_step: float = G.DEFAULT_FLOOR_STEP,
                 pad_beats: float = 8.0) -> None:
        from ..timing import Timing

        self.chart = chart
        self.timing = Timing(chart.BPMList)
        self.floor_step = float(floor_step)
        self.pad_beats = float(pad_beats)
        max_beat = self._max_beat(chart)
        self.content_beats = float(max_beat)
        self.n_beats = float(max_beat) + self.pad_beats
        seconds = self.timing.beat_to_second(Beat(self.n_beats))
        self.max_seconds = float(seconds) + 1.0
        self.lines = [LineRuntime(i, ln, self.timing, self.max_seconds,
                                  floor_step=self.floor_step)
                      for i, ln in enumerate(chart.judgeLineList)]
        #: 官方打击特效序列帧名缓存（渲染时才查资源目录，构造时不碰磁盘）
        self._hit_frame_cache = None
        #: Beat.frac -> 秒 的换算记忆。事件轨在相邻帧反复问同一批拍号，而音符的
        #: 起点/终点更是在整段播放里被反复问 —— 换算只依赖 BPMList，谱面构造之后
        #: 不可变，所以这个缓存永远不需要失效。
        self._seconds_cache: dict = {}
        #: 每个音符的 (起秒, 止秒)，与 chart.judgeLineList 展平后的顺序一一对应；
        #: _line_spans[i] 是第 i 条判定线在其中的切片。这样「拍 -> 秒」整谱只算一次
        #: （同一拍号共享），每帧退化成查表，而不是 O(音符数) 的分段累加。
        note_seconds: list = []
        line_spans: list = []
        combo_seconds: list = []
        cursor = 0
        for ln in chart.judgeLineList:
            ln_notes = ln.notes or ()
            line_spans.append((cursor, cursor + len(ln_notes)))
            for note in ln_notes:
                start_s, end_s = self._seconds(note.startTime, note.endTime)
                note_seconds.append((start_s, end_s))
                if not note.isFake:
                    combo_seconds.append(start_s)
            cursor += len(ln_notes)
        self._note_seconds = note_seconds
        self._line_spans = line_spans
        notes = [n for ln in chart.judgeLineList for n in (ln.notes or ())]
        self.note_count = len(notes)
        self.judgeable_count = sum(1 for n in notes if not n.isFake)
        #: 多押（双押/多押）的音符起点（Beat.frac）。
        #: 口径逐字对齐 phira prpr/src/parse.rs:20-71 的 process_lines：
        #: 把所有判定线的音符起点汇到一起，**出现次数 >= 2** 的时间点算多押；
        #: 命中的音符全部标记（含 fake，phira 也不排除 fake）。
        #: 注意这是**跨线全局**的判据，不是「同一条线同一时刻」——
        #: 真谱里大量多押是不同判定线上的 Tap+Flick+Drag 同时出现。
        self._multi_fracs = self._multi_hint_fracs(chart)
        #: 参与连击（非 fake）的音符起秒，升序 —— hud_state 只对它做一次二分。
        combo_seconds.sort()
        self._combo_seconds = combo_seconds

    @staticmethod
    def _frac(value):
        """取一个时间的 Beat.frac（容忍 int/float/Fraction 形状的输入）。"""
        return value.frac if isinstance(value, Beat) else Beat.of(value).frac

    def _multi_hint_fracs(self, chart) -> frozenset:
        """同一个起点上 >= 2 个音符的那些起点（口径见 __init__ 的注释）。"""
        counts = {}
        for ln in chart.judgeLineList:
            for note in (ln.notes or ()):
                frac = self._frac(note.startTime)
                counts[frac] = counts.get(frac, 0) + 1
        return frozenset(frac for frac, n in counts.items() if n >= 2)

    # ------------------------------------------------------------ 拍 -> 秒
    def _seconds(self, *beats) -> tuple:
        """把一组 Beat 换算成秒（按 frac 记忆；语义与逐次 beat_to_second 相同）。"""
        cache = self._seconds_cache
        out = []
        for value in beats:
            frac = value.frac if isinstance(value, Beat) else Beat.of(value).frac
            seconds = cache.get(frac, _MISSING)
            if seconds is _MISSING:
                # 传 Fraction 而不是 Beat：Beat.of 对 Fraction 是恒等映射，结果与
                # beat_to_second(Beat(...)) 完全一致，但省掉一次 Beat 构造。
                seconds = self.timing.beat_to_second(frac)
                cache[frac] = seconds
            out.append(seconds)
        return tuple(out)

    # ------------------------------------------------------------ 长度
    @staticmethod
    def _max_beat(chart) -> float:
        best = 0.0
        for ln in chart.judgeLineList:
            for note in ln.notes or ():
                best = max(best, float(note.startTime.frac), float(note.endTime.frac))
            for layer in ln.eventLayers or ():
                for field in (EVENT_MOVE_X, EVENT_MOVE_Y, EVENT_ROTATE, EVENT_ALPHA,
                              EVENT_SPEED):
                    for ev in getattr(layer, field, None) or ():
                        best = max(best, float(ev.endTime.frac))
            ext = getattr(ln, "extended", None)
            if ext is not None:
                for field in ("scaleXEvents", "scaleYEvents", "colorEvents",
                              "textEvents", "paintEvents", "gifEvents", "inclineEvents"):
                    for ev in getattr(ext, field, None) or ():
                        best = max(best, float(ev.endTime.frac))
        return best

    def second_of(self, beat_value) -> float:
        return self._seconds(beat_value)[0]

    # ------------------------------------------------------------ 线状态

    #: 父链跟踪的硬上限。合法谱面远远到不了（嵌套十几层已是极端），但坏谱面
    #: 必须**有界**：`validate` 会把父线成环报成 FatherLoop，而渲染路径从不
    #: 调用 validate —— 没有这道闸，谱面里一旦有 A→B→C→B 这种环，`frame()`
    #: 每一帧都 RecursionError，比把画面画错更糟（整个 render video 直接崩）。
    FATHER_DEPTH_LIMIT = 256

    def _parent_of(self, rt: LineRuntime, seen: frozenset):
        """父线下标；越界、自环、回边、链过深都算「没有父线」。"""
        father = rt.father
        if father < 0 or father >= len(self.lines) or father in seen:
            return None
        if len(seen) >= self.FATHER_DEPTH_LIMIT:
            return None
        return father

    def _line_pose(self, rt: LineRuntime, seconds: float,
                   _seen: frozenset = frozenset()):
        frac = self.timing.second_to_beat(seconds).frac
        x = float(rt.x.at_beat(frac, 0.0))
        y = float(rt.y.at_beat(frac, 0.0))
        rot = float(rt.rot.at_beat(frac, 0.0))
        father = self._parent_of(rt, _seen)
        if rt.rotate_with_father and father is not None:
            prot, _, _ = self._line_pose(self.lines[father], seconds,
                                         _seen | {rt.index})
            rot += prot
        return rot, x, y

    def line_origin(self, index: int, seconds: float, width: int, height: int,
                    _seen: frozenset = frozenset()):
        """判定线原点的屏幕像素坐标与旋转角（度）。"""
        rt = self.lines[index]
        rot, x, y = self._line_pose(rt, seconds, _seen)
        father = self._parent_of(rt, _seen)
        if father is not None:
            px, py, prot = self.line_origin(father, seconds, width, height,
                                            _seen | {rt.index})
            dx = x * G.px_per_rpe_x(width)
            dy = -y * G.px_per_rpe_y(height)
            rdx, rdy = G.rotate_point(dx, dy, prot)
            return px + rdx, py + rdy, G.screen_angle(rot)
        return (G.screen_x(x, width), G.screen_y(y, height), G.screen_angle(rot))

    def line_alpha(self, rt: LineRuntime, seconds: float) -> float:
        frac = self.timing.second_to_beat(seconds).frac
        return _clamp01(float(rt.alpha.at_beat(frac, 255.0)) / 255.0)

    def line_scale(self, rt: LineRuntime, seconds: float):
        frac = self.timing.second_to_beat(seconds).frac
        sx = float(rt.scale_x.at_beat(frac, 1.0)) if rt.scale_x else 1.0
        sy = float(rt.scale_y.at_beat(frac, 1.0)) if rt.scale_y else 1.0
        return sx, sy

    def line_color(self, rt: LineRuntime, seconds: float, default):
        if not rt.color:
            return tuple(default)
        frac = self.timing.second_to_beat(seconds).frac
        value = rt.color.at_beat(frac, None)
        return _as_rgb(value, default)

    # ------------------------------------------------------------ 可见性 / 统计
    def visible_notes(self, index: int, seconds: float, *,
                      margin_y: float = 160.0, margin_x: float = 300.0):
        """屏幕上该画出来的音符列表 ``[(note, delta_rpe), ...]``。"""
        rt = self.lines[index]
        out = []
        for note in rt.line.notes or ():
            note_t = self._seconds(note.startTime)[0]
            if note.is_hold:
                end_t = self._seconds(note.endTime)[0]
                if seconds >= end_t:
                    continue
                head = G.note_delta_rpe(rt.floor, note, note_t, seconds)
                tail = G.note_delta_rpe(rt.floor, note, end_t, seconds)
                if head < -0.02 and tail < -0.02:
                    continue
                out.append((note, head))
                continue
            if seconds > note_t:
                if float(note.isFake):
                    continue
                if seconds - 0.16 >= note_t:
                    continue
            delta = G.note_delta_rpe(rt.floor, note, note_t, seconds)
            if delta < -0.02:
                continue
            if abs(delta) > G.RPE_HEIGHT + margin_y:
                continue
            if abs(float(note.positionX)) > G.RPE_WIDTH + margin_x:
                continue
            out.append((note, delta))
        return out

    def visible_count(self, index: int, seconds: float) -> int:
        return len(self.visible_notes(index, seconds))

    def note_count_between(self, index: int, start_beat: float, end_beat: float) -> int:
        """``[start_beat, end_beat)`` 内的音符数（含 Hold）。"""
        return sum(1 for n in (self.lines[index].line.notes or ())
                   if start_beat <= float(n.startTime.frac) < end_beat)

    def stats(self, step_beats: float = 4.0):
        """每 ``step_beats`` 拍一行：``(秒, 拍, 物量, 屏上, 分线物量)``。

        只统计到 :attr:`content_beats`，不把渲染尾巴当成「谱面缺了一段」。
        """
        step = float(step_beats)
        rows = []
        b = 0.0
        while b < self.content_beats:
            seconds = self._seconds(Beat(b))[0]
            cnt = {i: self.note_count_between(i, b, b + step)
                   for i in range(len(self.lines))}
            shown = sum(self.visible_count(i, seconds) for i in range(len(self.lines)))
            rows.append((seconds, b, sum(cnt.values()), shown, cnt))
            b += step
        return rows

    # ------------------------------------------------------------ 场景
    def scene_at(self, seconds: float, width: int, height: int,
                 theme: _t.Optional[Theme] = None) -> Scene:
        theme = theme or Theme()
        scene = Scene(seconds, width, height, background=theme.background)
        notes_by_line = []
        for i, rt in enumerate(self.lines):
            ox, oy, ang = self.line_origin(i, seconds, width, height)
            alpha = self.line_alpha(rt, seconds)
            sx, sy = self.line_scale(rt, seconds)
            color = self.line_color(rt, seconds, theme.line_color)
            self._emit_line(scene, rt, i, ox, oy, ang, alpha, sx, sy, color,
                            width, height, theme)
            self._emit_notes(scene, rt, i, ox, oy, ang, alpha, seconds, width, height,
                             theme)
            self._emit_hits(scene, rt, i, seconds, width, height, theme)
        self._emit_hud(scene, seconds, width, height, theme)
        return scene

    # ------------------------------------------------------------ 打击特效
    def _hit_frames(self) -> list:
        """官方打击特效序列帧名（缓存；没有资源时为空列表）。"""
        cached = getattr(self, '_hit_frame_cache', None)
        if cached is None:
            from . import assets
            try:
                cached = assets.hit_texture_names()
            except Exception:
                cached = []
            self._hit_frame_cache = cached
        return cached

    def _emit_hits(self, scene, rt, index, seconds, width, height, theme) -> None:
        """音符命中瞬间在判定位置上播一帧官方判定环（Texture/HE/img-N.png）。

        真机截图里那个「正方形框 + 菱形 + 两段圆弧」的旋转环就是这个序列；
        judgeball.png（纯白圆环）形态对不上。序列 31 帧、约 0.3s 播完。
        """
        if not getattr(theme, 'hit_effects', True):
            return
        frames = self._hit_frames()
        if not frames:
            return
        duration = max(1e-3, float(getattr(theme, 'hit_duration', 0.32)))
        size = float(getattr(theme, 'hit_size', 0.25)) * height
        if size <= 1.0:
            return
        start, _end = self._line_spans[index]
        for offset, note in enumerate(rt.line.notes or ()):
            if float(note.isFake):
                continue
            note_t = self._note_seconds[start + offset][0]
            age = seconds - note_t
            if age < 0.0 or age >= duration:
                continue
            u = age / duration
            frame = frames[min(len(frames) - 1, int(u * len(frames)))]
            # 命中瞬间的判定位置：用**命中时刻**的线状态，而不是当前时刻
            ox, oy, ang = self.line_origin(index, note_t, width, height)
            delta = G.note_delta_rpe(rt.floor, note, note_t, note_t)
            dx = float(note.positionX) * G.px_per_rpe_x(width)
            dy = -delta * G.px_per_rpe_y(height)
            if not bool(note.above):
                dy = -dy
            rdx, rdy = G.rotate_point(dx, dy, ang)
            cx, cy = ox + rdx, oy + rdy
            margin = size
            if not (-margin <= cx <= width + margin and -margin <= cy <= height + margin):
                continue
            fade = 1.0 if u < 0.75 else max(0.0, (1.0 - u) / 0.25)
            if fade <= 0.0:
                continue
            scene.hits.append(HitDraw(cx, cy, 'HE/' + frame, size=size,
                                      alpha=fade, angle=0.0,
                                      color=getattr(theme, 'hit_color', None),
                                      kind=int(note.type)))

    def _emit_line(self, scene, rt, index, ox, oy, ang, alpha, sx, sy, color,
                   width, height, theme) -> None:
        if alpha <= 0.0:
            return
        text_track = rt.text
        if rt.texture == "line.png" and not text_track:
            # phira：draw_line(-len, 0, len, 0) 里 len = info.line_length = 6 世界单位；
            # 世界 x 的 [-1, 1] 对应整屏，故 1 世界单位 = W/2 像素。
            half = theme.line_length_world * (width / 2.0) * max(0.02, abs(sx))
            dx, dy = G.rotate_point(half, 0.0, ang)
            scene.lines.append(LineDraw(index, ox - dx, oy - dy, ox + dx, oy + dy,
                                        thickness=max(1.0, theme.line_thickness * height),
                                        alpha=alpha, color=color, texture="",
                                        label=str(index)))
            return
        if rt.texture == "line.png" and text_track:
            frac = self.timing.second_to_beat(scene.t).frac
            text = text_track.at_beat(frac, "")
            if text:
                size = max(1, int(0.08 * height))
                scene.texts.append(TextDraw(ox, oy, str(text), font="cmdysj", size=size,
                                            color=color, anchor="mm", alpha=alpha))
            return
        texture = rt.texture
        if theme.textures and texture in theme.textures:
            texture = theme.textures[texture]
        # 非 line.png 的装饰贴图：按宽度 0.5W 等比摆放（真实语料里只占 50/601 条线）。
        image = None
        try:
            from .assets import load_texture
            image = load_texture(texture)
        except Exception:
            image = None
        tex_w = float(getattr(image, "width", 0) or 0)
        tex_h = float(getattr(image, "height", 0) or 0)
        draw_w = 0.5 * width * max(0.02, abs(sx))
        draw_h = draw_w * (tex_h / tex_w) if tex_w > 0 and tex_h > 0 else draw_w
        scene.images.append(ImageDraw(ox - draw_w * 0.5, oy - draw_h * 0.5, texture,
                                      width=draw_w, height=draw_h,
                                      alpha=alpha, angle=ang))

    def _emit_notes(self, scene, rt, index, ox, oy, ang, line_alpha, seconds,
                    width, height, theme) -> None:
        line = rt.line
        note_w = G.note_width_px(width) * theme.note_scale
        fadeout = theme.fadeout
        # 屏外剔除：真实谱面的线速常到 10+，任意时刻绝大多数音符都远在屏幕外
        # （实测最远 y ≈ -77000px）。不剔除会让光栅化白干，也会让 scene.notes
        # 变成一个没有意义的「整谱音符表」。
        margin = max(note_w, 64.0)
        below_visible = rt.draw_below
        incline_deg = 0.0
        if rt.incline:
            frac = self.timing.second_to_beat(seconds).frac
            incline_deg = float(rt.incline.at_beat(frac, 0.0))
        incline_sin = math.sin(math.radians(incline_deg))
        start, _end = self._line_spans[index]
        for offset, note in enumerate(line.notes or ()):
            note_t = self._note_seconds[start + offset][0]
            above = bool(note.above)
            if not above and not below_visible:
                continue
            delta = G.note_delta_rpe(rt.floor, note, note_t, seconds)
            if seconds > note_t:
                # 对齐 phira core/note.rs:236-241 的剔除条件：过了判定线的音符
                # 不立刻消失，而是继续下落、用 FADEOUT_TIME(0.16s) 淡出。
                if float(note.isFake) and not note.is_hold:
                    continue
                if not note.is_hold and seconds - fadeout >= note_t:
                    continue
            if note.is_hold:
                self._emit_hold(scene, note, rt, ox, oy, ang, line_alpha, seconds,
                                note_t, self._note_seconds[start + offset][1], width,
                                height, theme, note_w, above, incline_sin,
                                theme.multi_hint
                                and self._frac(note.startTime) in self._multi_fracs)
                continue
            # 不再按 delta<0 剔除：phira core/note.rs:236-241 允许音符越过判定线后
            # 继续下落并在 FADEOUT_TIME 内淡出，剔除交给上面的时间判据与 note_fade。
            dx = float(note.positionX) * G.px_per_rpe_x(width)
            if incline_sin:
                dx *= max(-4.0, 1.0 - incline_sin * delta / 360.0)
            dy = -delta * G.px_per_rpe_y(height)
            if not above:
                dy = -dy
            rdx, rdy = G.rotate_point(dx, dy, ang)
            cx = ox + rdx
            cy = oy + rdy
            if not (-margin <= cx <= width + margin
                    and -margin <= cy <= height + margin):
                continue
            note_ang = ang + (0.0 if above else 180.0)
            a = line_alpha * G.note_alpha(note, seconds, note_t)
            a *= G.note_fade(note_t, seconds, fadeout)
            if a <= 0.0:
                continue
            kind = int(note.type)
            texture = theme.note_textures.get(kind, "Tap2.png")
            multi = theme.multi_hint and self._frac(note.startTime) in self._multi_fracs
            # 多押时换成官方高亮贴图；没有官方 HL 变体（长按）的留给 raster
            # 现场合成外泛光，所以 texture_mh 用空串表达「没有」而不是缺键。
            texture_mh = theme.note_textures_mh.get(kind, "") if multi else ""
            scene.notes.append(NoteDraw(kind, cx, cy, angle=note_ang,
                                        width=note_w * float(note.size),
                                        height=note_w * float(note.size),
                                        alpha=a, color=_note_color(note),
                                        above=above, fake=bool(note.isFake),
                                        x_scale=1.0, texture=texture,
                                        texture_mh=texture_mh, multi=multi))

    def _emit_hold(self, scene, note, rt, ox, oy, ang, line_alpha, seconds, note_t,
                   end_t, width, height, theme, note_w, above, incline_sin,
                   multi: bool = False) -> None:
        if seconds >= end_t:
            return
        head_delta = G.note_delta_rpe(rt.floor, note, note_t, seconds)
        tail_delta = G.note_delta_rpe(rt.floor, note, end_t, seconds)
        if head_delta < -0.02 and tail_delta < -0.02:
            return
        def place(delta):
            dx = float(note.positionX) * G.px_per_rpe_x(width)
            dy = -delta * G.px_per_rpe_y(height)
            if not above:
                dy = -dy
            rdx, rdy = G.rotate_point(dx, dy, ang)
            return ox + rdx, oy + rdy
        hx, hy = place(head_delta)
        tx, ty = place(tail_delta)
        margin = max(note_w, 64.0)
        if ((hy < -margin and ty < -margin)
                or (hy > height + margin and ty > height + margin)
                or (hx < -margin and tx < -margin)
                or (hx > width + margin and tx > width + margin)):
            return
        a = line_alpha * G.note_alpha(note, seconds, note_t)
        a *= G.note_fade(note_t, seconds, theme.fadeout)
        if a <= 0.0:
            return
        scene.holds.append(HoldDraw(x_head=hx, y_head=hy, x_tail=tx, y_tail=ty,
                                    angle=ang + (0.0 if above else 180.0),
                                    width=note_w * float(note.size), alpha=a,
                                    color=_note_color(note), above=above,
                                    fake=bool(note.isFake),
                                    texture_head=theme.hold_textures.get("head", "HoldHead.png"),
                                    texture_body=theme.hold_textures.get("body", "Hold.png"),
                                    texture_tail=theme.hold_textures.get("tail", "HoldEnd.png"),
                                    multi=bool(multi)))

    # ------------------------------------------------------------ HUD
    def hud_state(self, seconds: float) -> HudState:
        # 原来这里每帧遍历整谱所有音符（真谱上千个）；现在所有音符的起秒在
        # __init__ 里就算好了，combo 只是「起秒 <= seconds 的非 fake 音符数」。
        # 二分边界与原来的逐项比较完全一致（都是 <=，含等号）。
        combo = bisect.bisect_right(self._combo_seconds, float(seconds))
        total = max(1, self.judgeable_count)
        score = int(round(1000000.0 * combo / total))
        meta = self.chart.META
        return HudState(combo=combo, score=score, song=str(getattr(meta, "name", "") or ""),
                        level=str(getattr(meta, "level", "") or ""), paused=True)

    def _emit_hud(self, scene, seconds, width, height, theme) -> None:
        state = self.hud_state(seconds)
        scene.hud = state
        hud = theme.hud
        if hud is None or not hud.enabled:
            return
        if state.paused:
            pause = theme.textures.get("pause", "Pause.png")
            ph = hud.pause["h"] * height * hud.pause_scale
            scene.images.append(ImageDraw(hud.pause["nx"] * width, hud.pause["ny"] * height,
                                          pause, width=ph * 0.55, height=ph, alpha=1.0))
        if state.combo > 0:
            size = max(1, int(hud.combo["size"] * height))
            label_size = max(1, int(hud.combo["label_size"] * height))
            scene.texts.append(TextDraw(hud.combo["nx"] * width, hud.combo["ny"] * height,
                                        str(state.combo), font=hud.combo_font, size=size,
                                        color=(255, 255, 255), anchor="ma"))
            scene.texts.append(TextDraw(hud.combo["nx"] * width,
                                        hud.combo["label_ny"] * height, "combo",
                                        font=hud.text_font, size=label_size,
                                        color=(247, 247, 247), anchor="ma"))
        score_size = max(1, int(hud.score["size"] * height))
        digits = int(hud.score["digits"])
        scene.texts.append(TextDraw(hud.score["nx"] * width, hud.score["ny"] * height,
                                    str(state.score).zfill(digits), font=hud.combo_font,
                                    size=score_size, color=(255, 255, 255), anchor="ra"))
        if state.song:
            scene.texts.append(TextDraw(hud.song["nx"] * width, hud.song["ny"] * height,
                                        state.song, font=hud.text_font,
                                        size=max(1, int(hud.song["size"] * height)),
                                        color=(235, 235, 235), anchor="ld"))
        if state.level:
            scene.texts.append(TextDraw(hud.level["nx"] * width, hud.level["ny"] * height,
                                        state.level, font=hud.text_font,
                                        size=max(1, int(hud.level["size"] * height)),
                                        color=(245, 245, 245), anchor="rd"))
        if hud.show_line_numbers:
            size = max(1, int(hud.line_number["size"] * height))
            dy = float(hud.line_number.get("dy", 0.010)) * height
            for i in range(len(self.lines)):
                ox, oy, _ = self.line_origin(i, seconds, width, height)
                # 真机截图（978x642）里编号画在判定线**下方**：线 y=383..389、
                # label「2」y=396..411。phira core/line.rs:241-245 也是
                # ui.text(id).pos(0., -0.01).anchor(0.5, 1.)（世界 y 向下为负）。
                scene.texts.append(TextDraw(ox, oy + dy, str(i + 1),
                                            font=hud.text_font, size=size,
                                            color=(238, 238, 238), anchor="ms"))


def _as_rgb(value, default):
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return (int(value[0]), int(value[1]), int(value[2]))
    if isinstance(value, str):
        return tuple(default)
    return tuple(default)


def _note_color(note):
    """音符颜色。官方贴图自带颜色，所以默认返回白色 = 不着色。"""
    tint = getattr(note, "tint", None)
    if isinstance(tint, (list, tuple)) and len(tint) >= 3:
        return (int(tint[0]), int(tint[1]), int(tint[2]))
    return (255, 255, 255)


def scene_at(chart, seconds: float, width: int, height: int,
             theme: _t.Optional[Theme] = None, *, model=None) -> Scene:
    """便捷入口：``scene_at(chart, t, w, h, theme)``。"""
    model = model or RenderModel(chart)
    return model.scene_at(seconds, width, height, theme)