"""高层构建 API —— 程序化制谱的「语法糖」。

这一层把 RPE 的 JSON 结构包装成写谱时顺手的形式，覆盖用户关心的全部要素：

============  ==========================================================
判定线数量    :meth:`ChartBuilder.lines` / :meth:`ChartBuilder.add_line`
判定线位置    :meth:`~JudgeLineBuilder.at` / :meth:`~JudgeLineBuilder.move_to`
判定线移动    :meth:`~JudgeLineBuilder.move_to` / :meth:`~JudgeLineBuilder.move_by`
              / :meth:`~JudgeLineBuilder.rotate_to` / :meth:`~JudgeLineBuilder.spin`
判定线其它    :meth:`~JudgeLineBuilder.fade_to` / :meth:`~JudgeLineBuilder.scale_to`
              / :meth:`~JudgeLineBuilder.color_to` / :meth:`~JudgeLineBuilder.text`
音符下落位置  :meth:`~JudgeLineBuilder.tap` 等的 ``x=`` 参数
音符时间      :meth:`~JudgeLineBuilder.tap` 等的 ``t=`` / ``dur=`` 参数
音符速度      :meth:`~JudgeLineBuilder.tap` 等的 ``speed=`` 参数
音符流速事件  :meth:`~JudgeLineBuilder.line_speed`
其它          :meth:`~JudgeLineBuilder.curve` 曲线轨迹、:meth:`~JudgeLineBuilder.pattern` 预制型
============  ==========================================================

所有时间参数都接受 :class:`~rpechart.beat.Beat`、``float``、``"a:b/c"`` 字符串或
``[a, b, c]`` 列表，内部统一转成精确有理数。
"""

from __future__ import annotations

import math
import typing as _t

from .beat import Beat
from .model import (
    EVENT_ALPHA,
    EVENT_MOVE_X,
    EVENT_MOVE_Y,
    EVENT_ROTATE,
    EVENT_SPEED,
    NOTE_DRAG,
    NOTE_FLICK,
    NOTE_HOLD,
    NOTE_TAP,
    NOTE_TYPE_ALIASES,
    Chart,
    Event,
    EventLayer,
    JudgeLine,
    Meta,
    Note,
    SpeedEvent,
)
from .timing import Timing

__all__ = ["ChartBuilder", "JudgeLineBuilder", "NoteSpec", "note_type_id"]


def note_type_id(value: _t.Any) -> int:
    """把 ``"tap"`` / ``"蓝"`` / ``1`` 之类统一成整数类型。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    key = str(value).strip().lower()
    if key in NOTE_TYPE_ALIASES:
        return NOTE_TYPE_ALIASES[key]
    raise ValueError(f"未知音符类型: {value!r}（可用 tap/drag/flick/hold 或 1-4）")


class NoteSpec:
    """音符的默认样式，用于批量生成时统一风格。"""

    __slots__ = (
        "size",
        "speed",
        "alpha",
        "above",
        "is_fake",
        "y_offset",
        "visible_time",
        "judge_area",
        "tint",
    )

    def __init__(
        self,
        size: float = 1.0,
        speed: float = 1.0,
        alpha: int = 255,
        above: int = 1,
        is_fake: bool = False,
        y_offset: float = 0.0,
        visible_time: float = 999999.0,
        judge_area: float = 1.0,
        tint: _t.Optional[list[int]] = None,
    ) -> None:
        self.size = size
        self.speed = speed
        self.alpha = alpha
        self.above = above
        self.is_fake = is_fake
        self.y_offset = y_offset
        self.visible_time = visible_time
        self.judge_area = judge_area
        self.tint = tint

    def apply(self, note: Note) -> Note:
        note.size = self.size
        note.speed = self.speed
        note.alpha = self.alpha
        note.above = self.above
        note.isFake = 1 if self.is_fake else 0
        note.yOffset = self.y_offset
        note.visibleTime = self.visible_time
        note.judgeArea = self.judge_area
        note.tint = list(self.tint) if self.tint else None
        return note


# ================================================================== 判定线
#: ``at()`` 在 t=0 处放置的常量事件延伸多远（拍）。后续事件会自动裁剪它。
_SEED_HOLD = Beat(64)


class JudgeLineBuilder:
    """一条判定线的构建器。

    所有方法都返回 ``self``，可以链式调用。
    """

    def __init__(
        self,
        chart_builder: "ChartBuilder",
        line: JudgeLine,
        index: int,
    ) -> None:
        self._cb = chart_builder
        self.line = line
        self.index = index
        #: 该线当前「已知」的 X/Y（来自事件，用于相对移动）
        self._pos: list[float] = [0.0, 0.0]
        self._size: list[float] = [1.0, 1.0]
        self._angle: float = 0.0
        self._alpha: float = 255.0
        #: 该线的默认音符样式；``None`` 表示沿用谱面级 ``ChartBuilder.note_spec``。
        #: 普通属性而非 property —— 直接 ``lb.note_spec = NoteSpec(speed=3)`` 即可，
        #: :meth:`note` 每次都会重新读取它。
        self.note_spec: _t.Optional[NoteSpec] = None

    # ------------------------------------------------------------ 身份
    def name(self, value: str) -> "JudgeLineBuilder":
        self.line.Name = value
        return self

    def group(self, value: int) -> "JudgeLineBuilder":
        self.line.Group = int(value)
        return self

    def z_order(self, value: int) -> "JudgeLineBuilder":
        """绘制层级；值越大越靠前（RPE 范围 ±100）。"""
        self.line.zOrder = int(value)
        return self

    def texture(self, value: str) -> "JudgeLineBuilder":
        self.line.Texture = value
        return self

    def cover(self, value: bool = True) -> "JudgeLineBuilder":
        """遮罩：位于判定线背面的音符不渲染。"""
        self.line.isCover = 1 if value else 0
        return self

    def bpm_factor(self, value: float) -> "JudgeLineBuilder":
        """该线的时间缩放：实际 BPM = 谱面 BPM / bpmfactor。"""
        self.line.bpmfactor = float(value) or 1.0
        return self

    def attach_ui(self, value: _t.Optional[str]) -> "JudgeLineBuilder":
        """绑定 UI 元素：pause/combonumber/combo/score/bar/name/level。"""
        self.line.attachUI = value
        return self

    def father(self, index: int) -> "JudgeLineBuilder":
        """指定父线：该线的坐标与角度会叠加到父线上。"""
        self.line.father = int(index)
        return self

    def rotate_with_father(self, value: bool = True) -> "JudgeLineBuilder":
        self.line.rotateWithFather = bool(value)
        return self

    def anchor(self, x: float, y: float) -> "JudgeLineBuilder":
        self.line.anchor = [float(x), float(y)]
        return self

    # ------------------------------------------------------------ 事件基元
    def _layer(self, layer: int) -> EventLayer:
        while len(self.line.eventLayers) <= layer:
            self.line.eventLayers.append(EventLayer())
        return self.line.eventLayers[layer]

    def event(
        self,
        kind: str,
        start_time: _t.Any,
        end_time: _t.Any,
        start: _t.Any,
        end: _t.Any,
        *,
        easing: _t.Any = 1,
        layer: int = 0,
        linkgroup: int = 0,
        bezier: int = 0,
        bezier_points: _t.Optional[_t.Sequence[float]] = None,
        easing_left: float = 0.0,
        easing_right: float = 1.0,
    ) -> Event:
        """通用事件添加。

        ``kind`` 取 ``moveXEvents`` / ``moveYEvents`` / ``rotateEvents`` /
        ``alphaEvents``；特殊事件请用 :meth:`special_event`。

        同一层同一类事件**不允许重叠**（RPE 会报错），但手工写谱时又常常「接着
        上一个事件继续」。所以这里做两件自动处理：

        * 若新事件与前面的事件重叠，就**裁剪**前面那个（把它的 endTime 收到新事件的
          起点，并去掉多余的中间段）—— 这也是 RPE 编辑器里拉伸事件的默认行为；
        * 若两个事件首尾恰好相接，直接缝上，不留空隙。
        """
        if kind == EVENT_SPEED:
            # 缓动窗口与 linkgroup 必须一起转发：旧实现只给了 easing，
            # ``easing_left``/``easing_right`` 被静默丢掉。bezier 不转发 ——
            # SpeedEvent 从模型层就没有这个字段（测试
            # speed_event_has_no_bezier_fields_of_its_own 钉住了这一条）。
            return self.line_speed(
                start_time, end_time, start, end,
                easing=easing, layer=layer, linkgroup=linkgroup,
                easing_left=easing_left, easing_right=easing_right,
            )
        st, et = Beat.of(start_time), Beat.of(end_time)
        if et <= st:
            raise ValueError(f"事件的结束时间必须晚于开始时间: {st} -> {et}")
        if not isinstance(start, str) and not isinstance(start, (list, tuple)):
            start, end = float(start), float(end)
        ev = Event(
            startTime=st,
            endTime=et,
            start=start,
            end=end,
            easingType=easing,  # 由 Event.to_json 规范化
            linkgroup=int(linkgroup),
            bezier=int(bezier),
            bezierPoints=[float(v) for v in (bezier_points or [0.0, 0.0, 0.0, 0.0])],
            easingLeft=float(easing_left),
            easingRight=float(easing_right),
        )
        seq = self._layer(layer).events(kind)
        self._splice(seq, ev)
        if kind == EVENT_MOVE_X:
            self._pos[0] = float(end)
        elif kind == EVENT_MOVE_Y:
            self._pos[1] = float(end)
        elif kind == EVENT_ROTATE:
            self._angle = float(end)
        elif kind == EVENT_ALPHA:
            self._alpha = float(end)
        return ev

    @staticmethod
    def _splice(seq: list, ev: Event) -> None:
        """把 ``ev`` 插入已排序的事件序列，并消解重叠/空隙。"""
        from .model import Event as _Event, SpeedEvent as _SpeedEvent

        cls = _SpeedEvent if isinstance(ev, _SpeedEvent) else _Event
        # 1) 裁剪被完全覆盖或部分覆盖的旧事件
        kept: list = []
        for old in seq:
            if old.endTime <= ev.startTime or old.startTime >= ev.endTime:
                kept.append(old)  # 无交集
                continue
            if old.startTime >= ev.startTime and old.endTime <= ev.endTime:
                continue  # 被完全包住，丢弃
            if old.startTime < ev.startTime and old.endTime > ev.endTime:
                # 新事件落在旧事件内部：切成前后两段
                tail = old.copy()
                tail.startTime = ev.endTime
                tail.start = ev.end
                # 尾部是「之后保持不动」的残留段：必须恒定在新事件的终值上，
                # 否则 old.end 会留下一段凭空下降/上升的斜坡（例如 400 → 0）。
                tail.end = ev.end
                tail.easingType = 1
                tail.bezier = 0
                head = old.copy()
                head.endTime = ev.startTime
                head.end = ev.start
                kept.append(head)
                kept.append(tail)
                continue
            if old.startTime < ev.startTime:
                old.endTime = ev.startTime
                old.end = ev.start
                kept.append(old)
            else:
                old.startTime = ev.endTime
                old.start = ev.end
                # 同上：被削掉头部的旧事件只作为尾段残留，恒定在新事件的终值上
                old.end = ev.end
                old.easingType = 1
                old.bezier = 0
                kept.append(old)
        kept.append(ev)
        kept.sort(key=lambda e: (e.startTime.frac, e.endTime.frac))
        seq[:] = kept

        # 2) 填补空隙（用前一个事件的终值），保证时间线处处有值
        filled: list = []
        for i, cur in enumerate(kept):
            filled.append(cur)
            if i + 1 < len(kept):
                nxt = kept[i + 1]
                if cur.endTime < nxt.startTime:
                    filler = cur.copy()
                    filler.startTime = cur.endTime
                    filler.endTime = nxt.startTime
                    filler.start = cur.end
                    filler.end = cur.end
                    filler.easingType = 1
                    filler.bezier = 0
                    filler.easingLeft = 0.0
                    filler.easingRight = 1.0
                    filler.linkgroup = 0
                    filled.append(filler)
        filled.sort(key=lambda e: (e.startTime.frac, e.endTime.frac))
        seq[:] = filled

        # 3) 去掉零长度事件（RPE 会报 illegal Event）
        seq[:] = [e for e in seq if e.endTime > e.startTime]

    def special_event(
        self,
        kind: str,
        start_time: _t.Any,
        end_time: _t.Any,
        start: _t.Any,
        end: _t.Any,
        *,
        easing: _t.Any = 1,
        layer: int = 4,
        font: _t.Optional[str] = None,
        **kwargs: _t.Any,
    ) -> Event:
        """添加 ``extended`` 里的特殊事件（缩放/颜色/文字/GIF/倾斜）。

        RPE 只在**第 5 层（索引 4）**读取特殊事件，所以默认 ``layer=4``。
        """
        st, et = Beat.of(start_time), Beat.of(end_time)
        if et <= st:
            raise ValueError(f"事件的结束时间必须晚于开始时间: {st} -> {et}")
        if isinstance(start, str) or isinstance(end, str):
            s_val, e_val = str(start), str(end)
        elif isinstance(start, (list, tuple)):
            s_val = [int(v) for v in start]
            e_val = [int(v) for v in end]
        else:
            s_val, e_val = float(start), float(end)
        ev = Event(
            startTime=st,
            endTime=et,
            start=s_val,
            end=e_val,
            easingType=easing,
            font=font,
            **kwargs,
        )
        # extended 直接挂在 line.extended 上，同时保证第 5 层存在（RPE 的习惯）
        self._layer(layer)
        seq = getattr(self.line.extended, kind)
        self._splice(seq, ev)
        return ev

    # ------------------------------------------------------------ 位置与移动
    def at(self, x: float, y: float, *, at: _t.Any = 0) -> "JudgeLineBuilder":
        """把判定线**瞬时**放到 (x, y)。

        * ``at=0``：写一个从第 0 拍起的常量事件，之后所有未覆盖的时间都用它；
        * ``at>0``：先在 ``[0, at)`` 保持原位置（相对坐标 0,0），到 ``at`` 时**跳**
          到 (x, y) —— 等价于 RPE 里「在某时刻把线直接挪过去」。
        """
        t = Beat.of(at)
        if t <= Beat(0):
            self.event(EVENT_MOVE_X, Beat(0), _SEED_HOLD, float(x), float(x))
            self.event(EVENT_MOVE_Y, Beat(0), _SEED_HOLD, float(y), float(y))
        else:
            first = not self.line.eventLayers or all(
                not getattr(layer, k) for layer in self.line.eventLayers for k in (EVENT_MOVE_X, EVENT_MOVE_Y)
            )
            if not first:
                # 先让之前的位置持续到 t
                self.event(EVENT_MOVE_X, Beat(0), t, self._pos[0], self._pos[0])
                self.event(EVENT_MOVE_Y, Beat(0), t, self._pos[1], self._pos[1])
            self.event(EVENT_MOVE_X, t, _SEED_HOLD, float(x), float(x))
            self.event(EVENT_MOVE_Y, t, _SEED_HOLD, float(y), float(y))
        self._pos = [float(x), float(y)]
        return self

    def move_to(
        self,
        x: float,
        y: float,
        t: _t.Any,
        dur: _t.Any,
        *,
        easing: _t.Any = 1,
        layer: int = 0,
    ) -> "JudgeLineBuilder":
        """从当前位置**平滑移动**到 (x, y)。

        ``t`` 为开始时刻、``dur`` 为持续拍数。
        """
        st, et = Beat.of(t), Beat.of(t) + Beat.of(dur)
        self.event(EVENT_MOVE_X, st, et, self._pos[0], float(x), easing=easing, layer=layer)
        self.event(EVENT_MOVE_Y, st, et, self._pos[1], float(y), easing=easing, layer=layer)
        self._pos = [float(x), float(y)]
        return self

    def move_by(
        self,
        dx: float,
        dy: float,
        t: _t.Any,
        dur: _t.Any,
        *,
        easing: _t.Any = 1,
        layer: int = 0,
    ) -> "JudgeLineBuilder":
        """相对当前位置移动。"""
        return self.move_to(self._pos[0] + dx, self._pos[1] + dy, t, dur, easing=easing, layer=layer)

    def move_x(
        self, x: float, t: _t.Any, dur: _t.Any, *, easing: _t.Any = 1, layer: int = 0
    ) -> "JudgeLineBuilder":
        st, et = Beat.of(t), Beat.of(t) + Beat.of(dur)
        self.event(EVENT_MOVE_X, st, et, self._pos[0], float(x), easing=easing, layer=layer)
        self._pos[0] = float(x)
        return self

    def move_y(
        self, y: float, t: _t.Any, dur: _t.Any, *, easing: _t.Any = 1, layer: int = 0
    ) -> "JudgeLineBuilder":
        st, et = Beat.of(t), Beat.of(t) + Beat.of(dur)
        self.event(EVENT_MOVE_Y, st, et, self._pos[1], float(y), easing=easing, layer=layer)
        self._pos[1] = float(y)
        return self

    # ------------------------------------------------------------ 旋转
    def rotate_to(
        self,
        angle: float,
        t: _t.Any,
        dur: _t.Any,
        *,
        easing: _t.Any = 1,
        layer: int = 0,
    ) -> "JudgeLineBuilder":
        """旋转到指定角度（角度制，**0 度 = 竖直向上**，顺时针为正）。"""
        st, et = Beat.of(t), Beat.of(t) + Beat.of(dur)
        self.event(EVENT_ROTATE, st, et, self._angle, float(angle), easing=easing, layer=layer)
        self._angle = float(angle)
        return self

    def spin(
        self,
        degrees: float,
        t: _t.Any,
        dur: _t.Any,
        *,
        easing: _t.Any = 1,
        layer: int = 0,
    ) -> "JudgeLineBuilder":
        """相对当前角度再转 ``degrees`` 度。"""
        return self.rotate_to(self._angle + degrees, t, dur, easing=easing, layer=layer)

    def rotate_forever(
        self,
        degrees_per_beat: float,
        *,
        start: _t.Any = 0,
        end: _t.Any = 64,
        layer: int = 0,
    ) -> "JudgeLineBuilder":
        """持续旋转（用两个端点相差 ``degrees_per_beat * 拍数`` 的线性事件实现）。"""
        st, et = Beat.of(start), Beat.of(end)
        total = degrees_per_beat * (et - st).value
        self.event(EVENT_ROTATE, st, et, self._angle, self._angle + total, easing=1, layer=layer)
        self._angle += total
        return self

    # ------------------------------------------------------------ 透明度/缩放/颜色/文字
    def fade_to(
        self,
        alpha: float,
        t: _t.Any,
        dur: _t.Any,
        *,
        easing: _t.Any = 1,
        layer: int = 0,
    ) -> "JudgeLineBuilder":
        """透明度渐变（0~255）。"""
        st, et = Beat.of(t), Beat.of(t) + Beat.of(dur)
        self.event(EVENT_ALPHA, st, et, self._alpha, float(alpha), easing=easing, layer=layer)
        self._alpha = float(alpha)
        return self

    def scale_to(
        self,
        sx: float,
        sy: _t.Optional[float] = None,
        t: _t.Any = 0,
        dur: _t.Any = 1,
        *,
        easing: _t.Any = 1,
    ) -> "JudgeLineBuilder":
        """scaleX / scaleY 特殊事件（第 5 层）。"""
        if sy is None:
            sy = sx
        st, et = Beat.of(t), Beat.of(t) + Beat.of(dur)
        self.special_event("scaleXEvents", st, et, self._size[0], float(sx), easing=easing)
        self.special_event("scaleYEvents", st, et, self._size[1], float(sy), easing=easing)
        self._size = [float(sx), float(sy)]
        return self

    def color_to(
        self,
        rgb: _t.Sequence[int],
        t: _t.Any,
        dur: _t.Any,
        *,
        easing: _t.Any = 1,
    ) -> "JudgeLineBuilder":
        """颜色渐变事件（RGB 0~255），作用于判定线/音符的着色。"""
        st, et = Beat.of(t), Beat.of(t) + Beat.of(dur)
        self.special_event("colorEvents", st, et, list(rgb), list(rgb), easing=easing)
        return self

    def color_fade(
        self,
        rgb_from: _t.Sequence[int],
        rgb_to: _t.Sequence[int],
        t: _t.Any,
        dur: _t.Any,
        *,
        easing: _t.Any = 1,
    ) -> "JudgeLineBuilder":
        st, et = Beat.of(t), Beat.of(t) + Beat.of(dur)
        self.special_event("colorEvents", st, et, list(rgb_from), list(rgb_to), easing=easing)
        return self

    def text(
        self,
        content: str,
        t: _t.Any,
        dur: _t.Any,
        *,
        font: _t.Optional[str] = None,
        easing: _t.Any = 1,
    ) -> "JudgeLineBuilder":
        """文字事件；``content`` 里的 ``%P%`` 会显示事件进度数字。

        注意：有文字事件的判定线**不再渲染自身纹理**（RPE 的既定行为）。
        """
        st, et = Beat.of(t), Beat.of(t) + Beat.of(dur)
        self.special_event("textEvents", st, et, content, content, easing=easing, font=font)
        return self

    def text_morph(
        self,
        from_text: str,
        to_text: str,
        t: _t.Any,
        dur: _t.Any,
        *,
        font: _t.Optional[str] = None,
    ) -> "JudgeLineBuilder":
        st, et = Beat.of(t), Beat.of(t) + Beat.of(dur)
        self.special_event("textEvents", st, et, from_text, to_text, easing=1, font=font)
        return self

    def gif_progress(
        self, to_value: float, t: _t.Any, dur: _t.Any, *, from_value: float = 0.0
    ) -> "JudgeLineBuilder":
        """GIF 纹理的播放进度（0.0~1.0，越界自动循环）。"""
        st, et = Beat.of(t), Beat.of(t) + Beat.of(dur)
        self.special_event("gifEvents", st, et, float(from_value), float(to_value))
        return self

    def incline(
        self, angle: float, t: _t.Any, dur: _t.Any, *, from_angle: float = 0.0
    ) -> "JudgeLineBuilder":
        """判定线绕自身 X 轴倾斜（Z 轴旋转；RPE 内不可编辑，只能靠改谱面文件）。"""
        st, et = Beat.of(t), Beat.of(t) + Beat.of(dur)
        self.special_event("inclineEvents", st, et, float(from_angle), float(angle))
        return self

    # ------------------------------------------------------------ 流速
    def line_speed(
        self,
        start_time: _t.Any,
        end_time: _t.Any,
        start: float,
        end: float,
        *,
        easing: _t.Any = 1,
        layer: int = 0,
        linkgroup: int = 0,
        easing_left: float = 0.0,
        easing_right: float = 1.0,
    ) -> SpeedEvent:
        """判定线流速事件：**乘**在音符自身 speed 之上。

        流速为负时音符向上飞。
        """
        st, et = Beat.of(start_time), Beat.of(end_time)
        if et <= st:
            raise ValueError(f"流速事件结束时间必须晚于开始时间: {st} -> {et}")
        ev = SpeedEvent(
            startTime=st,
            endTime=et,
            start=float(start),
            end=float(end),
            easingType=easing,
            easingLeft=float(easing_left),
            easingRight=float(easing_right),
            linkgroup=int(linkgroup),
        )
        # 仍然直接 append（不过 _splice）：见 gen/motion.py 的模块 docstring ——
        # 线速区间由生成层用 assert_disjoint 自证互不重叠，_splice 的自动填隙
        # 反而会给「音量为零的段」补出多余事件。需要 RPE 标准顺序时走
        # JudgeLine.sort() / save_chart()。
        self._layer(layer).speedEvents.append(ev)
        return ev

    # ------------------------------------------------------------ 音符
    def note(
        self,
        kind: _t.Any,
        t: _t.Any,
        x: float = 0.0,
        *,
        dur: _t.Any = 0,
        speed: _t.Optional[float] = None,
        size: _t.Optional[float] = None,
        alpha: _t.Optional[int] = None,
        above: _t.Optional[int] = None,
        is_fake: _t.Optional[bool] = None,
        y_offset: _t.Optional[float] = None,
        visible_time: _t.Optional[float] = None,
        judge_area: _t.Optional[float] = None,
        tint: _t.Optional[_t.Sequence[int]] = None,
        hitsound: _t.Optional[str] = None,
    ) -> Note:
        """添加单个音符。

        :param kind: ``"tap"``/``"drag"``/``"flick"``/``"hold"`` 或 1~4，也认中文
        :param t: 起始时刻（拍）
        :param x: 相对判定线中心的横坐标（左负右正）
        :param dur: Hold 的持续拍数；对非 Hold 无意义
        :param speed: 音符自身流速倍率；省略则取线级 :attr:`note_spec`，
            线级为 ``None`` 时再取谱面级 :attr:`ChartBuilder.note_spec`

        除 ``x``/``dur``/``hitsound`` 外的参数默认都是 ``None``，含义是「本次调用
        没有指定」。这样 :class:`NoteSpec` 才能充当默认值，而显式传入的参数
        会覆盖它 —— 若用 ``1.0`` 之类的具体默认值，就分不清「没传」和「传了 1.0」，
        ``NoteSpec(speed=3)`` 会被静默吃掉。

        优先级：**显式参数 > 线级 ``note_spec`` > 谱面级 ``note_spec``**。
        """
        spec = self.note_spec if self.note_spec is not None else self._cb.note_spec
        ntype = note_type_id(kind)
        st = Beat.of(t)
        et = st + Beat.of(dur) if ntype == NOTE_HOLD else st
        if ntype == NOTE_HOLD and et <= st:
            raise ValueError("Hold 需要 dur > 0")
        note = Note(
            type=ntype,
            startTime=st,
            endTime=et,
            positionX=float(x),
            hitsound=hitsound,
        )
        # 先套用谱面级默认样式，再用本次调用显式给出的值覆盖它们。
        spec.apply(note)
        if speed is not None:
            note.speed = float(speed)
        if above is not None:
            note.above = int(above)
        if is_fake is not None:
            note.isFake = 1 if is_fake else 0
        if y_offset is not None:
            note.yOffset = float(y_offset)
        if visible_time is not None:
            note.visibleTime = float(visible_time)
        if size is not None:
            note.size = float(size)
        if alpha is not None:
            note.alpha = int(alpha)
        if judge_area is not None:
            note.judgeArea = float(judge_area)
        if tint is not None:
            note.tint = [int(v) for v in tint]
        self.line.notes.append(note)
        return note

    # ---- 四种音符的快捷方法
    def tap(self, t: _t.Any, x: float = 0.0, **kw: _t.Any) -> Note:
        """蓝键。"""
        return self.note(NOTE_TAP, t, x, **kw)

    def drag(self, t: _t.Any, x: float = 0.0, **kw: _t.Any) -> Note:
        """黄键。"""
        return self.note(NOTE_DRAG, t, x, **kw)

    def flick(self, t: _t.Any, x: float = 0.0, **kw: _t.Any) -> Note:
        """红键。"""
        return self.note(NOTE_FLICK, t, x, **kw)

    def hold(self, t: _t.Any, x: float = 0.0, dur: _t.Any = 1, **kw: _t.Any) -> Note:
        """长条。"""
        return self.note(NOTE_HOLD, t, x, dur=dur, **kw)

    def notes_at(
        self,
        kind: _t.Any,
        t: _t.Any,
        xs: _t.Sequence[float],
        **kw: _t.Any,
    ) -> list[Note]:
        """同一时刻一次放多个音符（多押）。"""
        return [self.note(kind, t, x, **kw) for x in xs]

    def stream(
        self,
        kind: _t.Any,
        t: _t.Any,
        count: int,
        interval: _t.Any,
        *,
        x: float = 0.0,
        x_from: _t.Optional[float] = None,
        x_to: _t.Optional[float] = None,
        **kw: _t.Any,
    ) -> list[Note]:
        """等间隔连打；``x_from``/``x_to`` 给出时会做 X 线性渐变。"""
        step = Beat.of(interval)
        out: list[Note] = []
        for i in range(int(count)):
            if x_from is not None and x_to is not None:
                frac = i / (count - 1) if count > 1 else 0.0
                xi = x_from + (x_to - x_from) * frac
            else:
                xi = x
            out.append(self.note(kind, Beat.of(t) + step * i, xi, **kw))
        return out

    # ------------------------------------------------------------ 曲线轨迹
    def curve(
        self,
        t_start: _t.Any,
        t_end: _t.Any,
        x_from: float,
        x_to: float,
        y_from: float,
        y_to: float,
        *,
        beats: _t.Optional[int] = None,
        step: _t.Any = None,
        easing: _t.Any = 1,
        rotate_from: _t.Optional[float] = None,
        rotate_to: _t.Optional[float] = None,
        layer: int = 0,
        arc: _t.Optional[float] = None,
    ) -> "JudgeLineBuilder":
        """让判定线沿一条轨迹运动。

        RPE 编辑器的「曲线轨迹生成」用的是「把整段切成若干首尾相接的事件」的做法，
        这里实现同样的思路（也顺便避免了单事件线性插值在大角度旋转下的误差）：

        * ``beats``：切成多少段（默认按 ``step`` 或 16 段）；
        * ``step``：每段多少拍（优先于 ``beats``）；
        * ``arc``：给定时走一条抛物线，X 从 ``x_from`` 到 ``x_to``，Y 在中间拱起 ``arc``。
        """
        st, et = Beat.of(t_start), Beat.of(t_end)
        if et <= st:
            raise ValueError("曲线终点必须晚于起点")
        span = et - st
        if step is not None:
            n = max(1, int(round(span.value / Beat.of(step).value)))
        else:
            n = int(beats) if beats else 16
        n = max(1, n)
        seg = span / n

        for i in range(n):
            p0 = i / n
            p1 = (i + 1) / n
            if arc is not None:
                def _y(p: float) -> float:
                    return y_from + (y_to - y_from) * p + arc * math.sin(math.pi * p)
            else:
                def _y(p: float) -> float:
                    return y_from + (y_to - y_from) * p

            def _x(p: float) -> float:
                return x_from + (x_to - x_from) * p

            a0 = Beat.of(st.value + span.value * p0)
            a1 = Beat.of(st.value + span.value * p1)
            self.event(EVENT_MOVE_X, a0, a1, _x(p0), _x(p1), easing=easing, layer=layer)
            self.event(EVENT_MOVE_Y, a0, a1, _y(p0), _y(p1), easing=easing, layer=layer)
            if rotate_from is not None and rotate_to is not None:
                r0 = rotate_from + (rotate_to - rotate_from) * p0
                r1 = rotate_from + (rotate_to - rotate_from) * p1
                self.event(EVENT_ROTATE, a0, a1, r0, r1, easing=easing, layer=layer)

        self._pos = [_x(1.0), _y(1.0)]
        if rotate_to is not None:
            self._angle = float(rotate_to)
        return self

    def event_curve(
        self,
        kind: str,
        t: _t.Any,
        dur: _t.Any,
        fn: _t.Callable[[float], float],
        *,
        samples: int = 32,
        easing: _t.Any = 1,
        layer: int = 0,
    ) -> "JudgeLineBuilder":
        """用任意函数 ``fn(p)``（``p`` 从 0 到 1）生成一条事件曲线。

        这等价于 RPE 编辑器的「参数方程」功能，但用 Python 写更自由。
        """
        st = Beat.of(t)
        total = Beat.of(dur)
        n = max(1, int(samples))
        seg = total / n
        for i in range(n):
            a0 = st + seg * i
            a1 = st + seg * (i + 1)
            self.event(kind, a0, a1, fn(i / n), fn((i + 1) / n), easing=easing, layer=layer)
        return self

    # ------------------------------------------------------------ 音符图案
    def pattern(
        self,
        kind: _t.Any,
        t: _t.Any,
        count: int,
        *,
        interval: _t.Any = 0.5,
        x_start: float = -300.0,
        x_end: float = 300.0,
        dur: _t.Any = 0,
        **kw: _t.Any,
    ) -> list[Note]:
        """一条从左到右（或反向）的等间隔音符序列。"""
        return self.stream(
            kind, t, count, interval, x_from=x_start, x_to=x_end, dur=dur, **kw
        )

    def zigzag(
        self,
        kind: _t.Any,
        t: _t.Any,
        count: int,
        *,
        interval: _t.Any = 0.5,
        x_left: float = -300.0,
        x_right: float = 300.0,
        **kw: _t.Any,
    ) -> list[Note]:
        """左右往复的音符序列。"""
        out: list[Note] = []
        step = Beat.of(interval)
        for i in range(int(count)):
            x = x_left if i % 2 == 0 else x_right
            out.append(self.note(kind, Beat.of(t) + step * i, x, **kw))
        return out

    # ------------------------------------------------------------ 收尾
    def done(self) -> "ChartBuilder":
        """回到谱面构建器。"""
        return self._cb

    def __repr__(self) -> str:
        return f"<JudgeLineBuilder #{self.index} {self.line.Name!r} notes={len(self.line.notes)}>"


# ================================================================== 谱面
class ChartBuilder:
    """一张谱面的构建器。

    基本用法::

        cb = ChartBuilder(bpm=174, name="My Chart", charter="me")
        l0 = cb.line()                       # 新建一条判定线
        l0.at(0, 0)
        l0.move_to(0, 400, 0, 8, easing="in_out_sine")
        for i in range(16):
            l0.tap(i * 0.5, x=-400 + i * 50)
        cb.save("chart.json")
    """

    def __init__(
        self,
        bpm: _t.Union[float, _t.Sequence, _t.Mapping] = 120.0,
        *,
        name: str = "Unknow",
        charter: str = "Unknow",
        composer: str = "Unknow",
        level: str = "Unknow",
        song: str = "song.mp3",
        background: str = "background.png",
        illustrator: _t.Optional[str] = None,
        offset: int = 0,
        rpe_version: int = 160,
        chart_id: _t.Optional[str] = None,
        note_spec: _t.Optional[NoteSpec] = None,
        existing: _t.Optional[Chart] = None,
    ) -> None:
        if existing is not None:
            self.chart = existing
        else:
            bpm_list = self._make_bpm_list(bpm)
            self.chart = Chart(
                BPMList=bpm_list,
                META=Meta(
                    RPEVersion=int(rpe_version),
                    name=name,
                    id=chart_id or "0",
                    song=song,
                    background=background,
                    composer=composer,
                    charter=charter,
                    illustrator=illustrator,
                    level=level,
                    offset=int(offset),
                ),
            )
        self.note_spec = note_spec or NoteSpec()

    # ---------------------------------------------------------------- 基础
    @staticmethod
    def _make_bpm_list(bpm: _t.Any) -> list:
        from .timing import build_bpm_list

        if isinstance(bpm, (int, float)):
            return build_bpm_list([(float(bpm), Beat(0))])
        if isinstance(bpm, dict):
            return build_bpm_list([bpm])
        return build_bpm_list(list(bpm))

    @property
    def timing(self) -> Timing:
        return Timing(self.chart.BPMList)

    @property
    def meta(self) -> Meta:
        return self.chart.META

    def set_bpm(self, bpm: float, at: _t.Any = 0) -> "ChartBuilder":
        """在 ``at`` 拍处插入一段新 BPM。"""
        from .model import BPMItem

        t = Beat.of(at)
        for i, item in enumerate(self.chart.BPMList):
            if item.startTime == t:
                item.bpm = float(bpm)
                break
        else:
            self.chart.BPMList.append(BPMItem(bpm=float(bpm), startTime=t))
        self.chart.BPMList.sort(key=lambda b: b.startTime.frac)
        return self

    def bpm_at(self, t: _t.Any = 0) -> float:
        """某拍处的实际 BPM。"""
        return 60.0 / self.timing.seconds_per_beat(t)

    def seconds(self, t: _t.Any) -> float:
        """拍 -> 秒（谱面自身 BPM）。"""
        return self.timing.beat_to_second(t)

    def beats(self, seconds: float) -> Beat:
        """秒 -> 拍。"""
        return self.timing.second_to_beat(seconds)

    # ---------------------------------------------------------------- 判定线
    #: ``line()`` 认识的身份参数 → :class:`JudgeLineBuilder` 上的同名方法。
    #: 旧实现只认 ``name``/``x``/``y``，其余 ``**kwargs`` 被静默吞掉
    #: （``cb.line(z_order=7)`` 什么也不做，却不报错）。
    _LINE_ATTRS: _t.ClassVar[dict] = {
        "group": "group",
        "z_order": "z_order",
        "texture": "texture",
        "cover": "cover",
        "bpm_factor": "bpm_factor",
        "attach_ui": "attach_ui",
        "father": "father",
        "rotate_with_father": "rotate_with_father",
    }

    def line(
        self,
        *,
        name: _t.Optional[str] = None,
        x: _t.Optional[float] = None,
        y: _t.Optional[float] = None,
        at: _t.Any = 0,
        **kwargs: _t.Any,
    ) -> JudgeLineBuilder:
        """新建一条判定线并返回它的构建器。

        ``x``/``y`` 给了的话会立即在 ``at`` 处放好位置。

        其余身份参数按 :data:`_LINE_ATTRS` 转发给对应的 builder 方法
        （``group`` / ``z_order`` / ``texture`` / ``cover`` / ``father`` /
        ``bpm_factor`` / ``attach_ui`` / ``rotate_with_father``）；认不出的键
        直接 ``TypeError``，绝不静默吞掉。
        """
        unknown = [k for k in kwargs if k not in self._LINE_ATTRS]
        if unknown:
            raise TypeError(
                f"ChartBuilder.line() 不认识的参数 {sorted(unknown)}；"
                f"可用: {sorted(self._LINE_ATTRS)}"
            )
        index = len(self.chart.judgeLineList)
        line = JudgeLine(Name=name or f"Line {index}")
        self.chart.judgeLineList.append(line)
        builder = JudgeLineBuilder(self, line, index)
        if x is not None or y is not None:
            builder.at(x or 0.0, y or 0.0, at=at)
        for key, value in kwargs.items():
            getattr(builder, self._LINE_ATTRS[key])(value)
        return builder

    def line_builder(self, index: int) -> JudgeLineBuilder:
        """取回已有判定线的构建器（用于二次编辑）。"""
        b = JudgeLineBuilder(self, self.chart.judgeLineList[index], index)
        return b

    @property
    def lines(self) -> list[JudgeLineBuilder]:
        return [
            JudgeLineBuilder(self, line, i)
            for i, line in enumerate(self.chart.judgeLineList)
        ]

    @property
    def chart_lines(self) -> list[JudgeLine]:
        return self.chart.judgeLineList

    def remove_line(self, index: int) -> None:
        """删除一条判定线，并修正所有父线引用。"""
        del self.chart.judgeLineList[index]
        for line in self.chart.judgeLineList:
            if line.father == index:
                line.father = -1
            elif line.father > index:
                line.father -= 1

    # ---------------------------------------------------------------- 校验
    def validate(self, *, song_duration: _t.Optional[float] = None):
        from .validate import validate

        return validate(self.chart, song_duration=song_duration)

    # ---------------------------------------------------------------- 输出
    def to_json(self) -> dict:
        return self.chart.to_json()

    def save(self, path: _t.Union[str, _t.Any], *, indent: int = 2):
        from .io import save_chart

        self.chart.sort()
        return save_chart(self.chart, path, indent=indent)

    def save_json(self, path: _t.Union[str, _t.Any], *, indent: int = 2):
        return self.save(path, indent=indent)

    def summary(self) -> str:
        """一句话概括这张谱面。"""
        c = self.chart
        return (
            f"《{c.META.name}》 谱师:{c.META.charter} BPM:{self.timing.base_bpm:g} "
            f"线数:{len(c.judgeLineList)} 物量:{c.note_count} Hold:{c.hold_count} "
            f"时长:{self.seconds(c.duration_beats()):.2f}s"
        )

    def __repr__(self) -> str:
        return f"<ChartBuilder {self.chart.META.name!r} lines={len(self.chart.judgeLineList)}>"
