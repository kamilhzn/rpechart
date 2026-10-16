"""RPE 谱面的数据模型。

设计原则（按重要性排序）：

1. **无损往返**：任何 RPE 写出的字段，即使本库还不认识，也要能原样带回
   （``_extra`` 机制）。这样用 RPE 编辑器改过的谱面，进来再出去不会掉东西。
2. **宽容读取**：RPE 各版本字段缺失/为 null/为空数组的含义不同，读写都按
   phira-docs 与 RPE 1.4.1/1.6.0 的实测语义处理。
3. **精确时间**：一切时间字段都是 :class:`~rpechart.beat.Beat`（精确有理数）。
4. **写出的 JSON 尽量贴近 RPE 自己的输出**，便于 diff 和人工检查。
"""

from __future__ import annotations

import copy
import math
import typing as _t
from dataclasses import dataclass, field, fields, is_dataclass

from .beat import Beat, beat
from .easing import clamp_easing

__all__ = [
    "NOTE_TAP",
    "NOTE_HOLD",
    "NOTE_FLICK",
    "NOTE_DRAG",
    "NOTE_TYPE_NAMES",
    "NOTE_TYPE_IDS",
    "EVENT_MOVE_X",
    "EVENT_MOVE_Y",
    "EVENT_ROTATE",
    "EVENT_ALPHA",
    "EVENT_SPEED",
    "COMMON_EVENT_FIELDS",
    "SPECIAL_EVENT_FIELDS",
    "Note",
    "Event",
    "SpeedEvent",
    "EventLayer",
    "ExtendedEvents",
    "ControlKeyframe",
    "Controls",
    "JudgeLine",
    "BPMItem",
    "Meta",
    "TimeTag",
    "Chart",
]

# ---------------------------------------------------------------- 常量
NOTE_TAP, NOTE_HOLD, NOTE_FLICK, NOTE_DRAG = 1, 2, 3, 4

NOTE_TYPE_NAMES: dict[int, str] = {
    NOTE_TAP: "tap",
    NOTE_HOLD: "hold",
    NOTE_FLICK: "flick",
    NOTE_DRAG: "drag",
}
NOTE_TYPE_IDS: dict[str, int] = {v: k for k, v in NOTE_TYPE_NAMES.items()}
#: 中文别名
NOTE_TYPE_ALIASES: dict[str, int] = {
    "蓝": NOTE_TAP,
    "蓝键": NOTE_TAP,
    "tap": NOTE_TAP,
    "黄": NOTE_DRAG,
    "黄键": NOTE_DRAG,
    "drag": NOTE_DRAG,
    "红": NOTE_FLICK,
    "红键": NOTE_FLICK,
    "flick": NOTE_FLICK,
    "长按": NOTE_HOLD,
    "长条": NOTE_HOLD,
    "hold": NOTE_HOLD,
}

EVENT_MOVE_X = "moveXEvents"
EVENT_MOVE_Y = "moveYEvents"
EVENT_ROTATE = "rotateEvents"
EVENT_ALPHA = "alphaEvents"
EVENT_SPEED = "speedEvents"

#: 普通事件（eventLayers 内）的五个字段名，顺序即 RPE 的写出顺序
COMMON_EVENT_FIELDS: tuple[str, ...] = (
    EVENT_MOVE_X,
    EVENT_MOVE_Y,
    EVENT_ROTATE,
    EVENT_ALPHA,
    EVENT_SPEED,
)

#: 特殊事件（extended 内）的字段名
SPECIAL_EVENT_FIELDS: tuple[str, ...] = (
    "scaleXEvents",
    "scaleYEvents",
    "colorEvents",
    "textEvents",
    "paintEvents",
    "gifEvents",
    "inclineEvents",
)

#: 事件坐标的合理范围（仅用于校验器，不强制夹取）
EVENT_RANGES: dict[str, tuple[float, float]] = {
    EVENT_MOVE_X: (-675.0, 675.0),
    EVENT_MOVE_Y: (-450.0, 450.0),
    EVENT_ALPHA: (0.0, 255.0),
}

_EXTRA_KEY = "_extra"


def _as_beat(value: _t.Any, default: _t.Any = None) -> Beat:
    if value is None:
        if default is None:
            return Beat(0)
        return Beat.of(default)
    return Beat.of(value)


class _JsonMixin:
    """给 dataclass 提供统一的 ``to_json``：驼峰键名 + ``_extra`` 透传。"""

    def to_json(self) -> _t.Any:  # pragma: no cover - 由子类覆盖
        raise NotImplementedError


def _dump_typed(value: _t.Any) -> _t.Any:
    """把 Beat / 嵌套对象统一转成可 JSON 序列化的值。"""
    if isinstance(value, Beat):
        return value.to_list()
    if hasattr(value, "to_json") and callable(value.to_json):
        return value.to_json()
    if isinstance(value, list):
        return [_dump_typed(v) for v in value]
    if isinstance(value, dict):
        return {k: _dump_typed(v) for k, v in value.items()}
    return value


def _read_extra(obj: dict, known: _t.Container[str]) -> dict:
    """收集字典里本库尚未建模的键，供无损往返。"""
    return {k: copy.deepcopy(v) for k, v in obj.items() if k not in known}


def _dump_easing(value: _t.Any) -> int:
    """写出缓动 id。

    整数**原样保留**：真实谱面里确实存在 ``easingType = 0``（temmie 的
    inclineEvents、若干 170 谱面），规范化只应发生在求值（:func:`ease`）时，
    不能顺手改写别人的文件。字符串 / 浮点这类便捷输入才走规范化。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return clamp_easing(value)
    return int(value)


def _intify_event_values(event: dict) -> None:
    """把事件字典里的 start / end 就地整数化。

    RPE 对 **alpha 事件与颜色事件**的值恒写整数（实测 26 份真实谱面：
    alphaEvents 的 start/end 共 21603 个值全部是 int，0 个 float；colorEvents
    同理）。本库内部一律用 float 存值（求值需要），写回时若不整数化就会得到
    "end" : 200.0 而 RPE 写 "end" : 200。

    -0.0 不整数化：它有符号语义，且真实谱面的 -0.0 只出现在 rotate/moveX/moveY
    事件与 note.positionX 上，不会落在 alpha/颜色事件里。
    """
    for key in ("start", "end"):
        value = event.get(key)
        if isinstance(value, float) and value.is_integer() and not (
            value == 0.0 and math.copysign(1.0, value) < 0
        ):
            event[key] = int(value)


def _load_easing(value: _t.Any) -> int:
    """读入缓动 id，容忍字符串/浮点写法（真实谱面都是整数，这里只做兜底）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return clamp_easing(value)
    return int(value)


def _present_keys(obj: _t.Optional[dict]) -> frozenset:
    """记下载入时真正出现过的键。

    RPE 各版本对同一字段的写法差别很大：1.4 写整数、1.5+ 写 ``.0`` 浮点；有的版本
    总是写出 ``bezier``/``linkgroup`` 这类默认值，有的版本整键省略；``anchor``、
    ``isGif``、``rotateWithFather`` 更是只在特定版本之后才出现。所以「写不写这个键」
    不能由我们替 RPE 决定 —— 载入时见过的键一律原样保留。
    """
    return frozenset(obj.keys()) if obj else frozenset()


def _emit(present: _t.Container[str], key: str, value: _t.Any, default: _t.Any) -> bool:
    """该键是否应当写进 JSON。

    载入过的谱面：字段本来在就写（哪怕值等于默认值）。
    程序化新建的对象：只在值偏离默认时才写，保持输出干净。
    """
    if key in present:
        return True
    if isinstance(value, float) and isinstance(default, (int, float)):
        return abs(value - float(default)) > 1e-12
    if isinstance(value, (list, tuple)) and isinstance(default, (list, tuple)):
        if len(value) != len(default):
            return True
        return any(
            (abs(float(a) - float(b)) > 1e-12)
            if isinstance(a, (int, float)) and isinstance(b, (int, float))
            else a != b
            for a, b in zip(value, default)
        )
    return value != default


def _is_loaded(obj: _t.Any) -> bool:
    """该对象是否由 ``from_json`` 载入，而非程序化新建。

    新建对象从不设置 ``_present``（恒为空集）；载入对象至少含有 RPE 必定写出的
    基础键。少数字段需要区分这两种情况：「载入但缺该键」要**省略**，而「程序化
    新建」要写出默认值 —— 单看值与默认值是否相等无法区分，例如
    ``rotateWithFather``（新建默认 True，载入缺键时语义为 False）。
    """
    return bool(getattr(obj, "_present", None))


#: ``_present`` 字段的统一定义，保证 dataclass 之间行为一致（不参与相等比较）。
_PRESENT_FIELD = dict(
    default_factory=frozenset, repr=False, compare=False,
)


@dataclass
class Note(_JsonMixin):
    """一个音符。

    ``positionX`` 是相对判定线中心的横坐标；``size`` 在 RPE 里实际只控制**宽度**；
    ``yOffset`` 的真实偏移量是 ``yOffset * speed``（``speed == 0`` 时偏移恒为 0）。
    """

    type: int = NOTE_TAP
    startTime: Beat = field(default_factory=lambda: Beat(0))
    endTime: Beat = field(default_factory=lambda: Beat(0))
    positionX: float = 0.0
    above: int = 1
    alpha: int = 255
    isFake: int = 0
    size: float = 1.0
    speed: float = 1.0
    visibleTime: float = 999999.0
    yOffset: float = 0.0
    hitsound: _t.Optional[str] = None
    judgeArea: float = 1.0
    tint: _t.Optional[list[int]] = None
    tintHitEffects: _t.Optional[list[int]] = None
    _extra: dict = field(default_factory=dict, repr=False)
    _present: frozenset = field(**_PRESENT_FIELD)
    # 170 谱面里 `color` 与 `tint` 是互斥的两个键名（实测两者同时出现 0 次），
    # 所以载入时必须记住用的是哪个名字，原样写回，否则等于替谱师改名。
    _tint_key: str = field(default="tint", repr=False, compare=False)

    # ------------------------------------------------------------ 便捷属性
    @property
    def is_hold(self) -> bool:
        return self.type == NOTE_HOLD

    @property
    def is_fake(self) -> bool:
        return bool(self.isFake)

    @property
    def duration(self) -> Beat:
        return self.endTime - self.startTime

    @property
    def type_name(self) -> str:
        return NOTE_TYPE_NAMES.get(self.type, f"unknown({self.type})")

    @property
    def real_offset(self) -> float:
        """实际纵向偏移（RPE 语义：``yOffset * speed``）。"""
        return self.yOffset * self.speed

    def copy(self) -> "Note":
        return copy.deepcopy(self)

    def moved_to(self, start: _t.Any) -> "Note":
        """整体平移（Hold 会带着尾判一起移动），返回新音符。"""
        delta = Beat.of(start) - self.startTime
        out = self.copy()
        out.startTime = Beat.of(start)
        out.endTime = self.endTime + delta
        return out

    def shifted(self, delta: _t.Any) -> "Note":
        out = self.copy()
        out.startTime = self.startTime + Beat.of(delta)
        out.endTime = self.endTime + Beat.of(delta)
        return out

    # ------------------------------------------------------------ 序列化
    def to_json(self) -> dict:
        data: dict[str, _t.Any] = {
            "above": int(self.above),
            "alpha": int(self.alpha),
            "endTime": self.endTime.to_list(),
            "isFake": int(self.isFake),
            "positionX": float(self.positionX),
            "size": float(self.size),
            "speed": float(self.speed),
            "startTime": self.startTime.to_list(),
            "type": int(self.type),
            "visibleTime": float(self.visibleTime),
            "yOffset": float(self.yOffset),
        }
        if self.hitsound is not None:
            data["hitsound"] = self.hitsound
        # judgeArea 的默认值 1.0 与「未出现该字段」在 JSON 里都是合法状态，
        # 所以要靠载入时记录的字段存在性来区分，否则真实 170 谱面的 judgeArea 会丢。
        if self.judgeArea != 1.0 or "judgeArea" in self._present:
            data["judgeArea"] = float(self.judgeArea)
        if self.tint is not None:
            data[self._tint_key] = list(self.tint)
        if self.tintHitEffects is not None:
            data["tintHitEffects"] = list(self.tintHitEffects)
        data.update(copy.deepcopy(self._extra))
        return data

    @classmethod
    def from_json(cls, obj: dict) -> "Note":
        obj = obj or {}
        known = {
            "type",
            "startTime",
            "endTime",
            "positionX",
            "above",
            "alpha",
            "isFake",
            "size",
            "speed",
            "visibleTime",
            "yOffset",
            "hitsound",
            "judgeArea",
            "tint",
            "color",
            "tintHitEffects",
        }
        # color 是 170 之前的字段名，tint 是新名，两者都兼容
        tint = obj.get("tint")
        tint_key = "tint"
        if tint is None:
            tint = obj.get("color")
            if tint is not None:
                tint_key = "color"
        st = _as_beat(obj.get("startTime"), 0)
        et_raw = obj.get("endTime")
        ntype = int(obj.get("type", NOTE_TAP))
        et = _as_beat(et_raw, st) if et_raw is not None else st
        if ntype != NOTE_HOLD:
            # 非 Hold 的 endTime 应等于 startTime；旧文件里偶有不一致，这里归位
            et = st
        return cls(
            type=ntype,
            startTime=st,
            endTime=et,
            positionX=float(obj.get("positionX", 0.0)),
            above=int(obj.get("above", 1)),
            alpha=int(obj.get("alpha", 255)),
            isFake=int(obj.get("isFake", 0)),
            size=float(obj.get("size", 1.0)),
            speed=float(obj.get("speed", 1.0)),
            visibleTime=float(obj.get("visibleTime", 999999.0)),
            yOffset=float(obj.get("yOffset", 0.0)),
            hitsound=obj.get("hitsound"),
            judgeArea=float(obj.get("judgeArea", 1.0)),
            tint=list(tint) if tint is not None else None,
            tintHitEffects=list(obj["tintHitEffects"])
            if obj.get("tintHitEffects") is not None
            else None,
            _extra=_read_extra(obj, known),
            _present=_present_keys(obj),
            _tint_key=tint_key,
        )


@dataclass
class Event(_JsonMixin):
    """判定线事件（普通或特殊，两者字段几乎一致）。

    ``start`` / ``end`` 的类型随事件种类变化：float（移动/旋转/透明度/缩放/…）、
    ``int[3]``（颜色）、``str``（文字）。
    """

    startTime: Beat = field(default_factory=lambda: Beat(0))
    endTime: Beat = field(default_factory=lambda: Beat(0))
    start: _t.Any = 0.0
    end: _t.Any = 0.0
    easingType: int = 1
    easingLeft: float = 0.0
    easingRight: float = 1.0
    linkgroup: int = 0
    bezier: int = 0
    bezierPoints: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    font: _t.Optional[str] = None
    _extra: dict = field(default_factory=dict, repr=False)
    _present: frozenset = field(**_PRESENT_FIELD)

    #------------------------------------------------------------ 属性
    @property
    def is_color(self) -> bool:
        return isinstance(self.start, (list, tuple)) and len(self.start) == 3

    @property
    def is_text(self) -> bool:
        return isinstance(self.start, str)

    @property
    def duration(self) -> Beat:
        return self.endTime - self.startTime

    def copy(self) -> "Event":
        return copy.deepcopy(self)

    def t(self, time: _t.Any) -> float:
        """把绝对时刻映射到该事件的归一化进度 ``[0, 1]``。"""
        st, et = self.startTime.value, self.endTime.value
        if et <= st:
            return 1.0
        return (Beat.of(time).value - st) / (et - st)

    def value_at(self, time: _t.Any) -> _t.Any:
        """求该事件在 ``time`` 时刻的值（不考虑同层其它事件）。"""
        from .interp import interpolate_value

        return interpolate_value(self, time)

    # ------------------------------------------------------------ 序列化
    def to_json(self) -> dict:
        data: dict[str, _t.Any] = {
            "startTime": self.startTime.to_list(),
            "endTime": self.endTime.to_list(),
            "start": _dump_typed(self.start),
            "end": _dump_typed(self.end),
        }
        if self.linkgroup or "linkgroup" in self._present:
            data["linkgroup"] = int(self.linkgroup)
        # 原样写回：真实谱面的 inclineEvents 就有 easingType=0，
        # 规范化只应发生在求值（ease()/clamp_easing()）时，不能污染文件。
        data["easingType"] = _dump_easing(self.easingType)
        # RPE 写出的普通事件恒含 bezier / bezierPoints / easingLeft / easingRight /
        # linkgroup（实测 26 份真实谱面无一例外）。载入过的谱面必须逐个写回，
        # 否则「RPE 打开 → 保存」会平白少掉这些键；程序化新建时才按需省略。
        if self.easingLeft != 0.0 or "easingLeft" in self._present:
            data["easingLeft"] = float(self.easingLeft)
        if self.easingRight != 1.0 or "easingRight" in self._present:
            data["easingRight"] = float(self.easingRight)
        # bezier 与 bezierPoints 是两个独立键：v113 的 inclineEvents 只有前者缺失、
        # 后者仍在，所以分别按存在性决定。
        if self.bezier or "bezier" in self._present:
            data["bezier"] = int(self.bezier)
        if self.bezier or "bezierPoints" in self._present or "bezier" in self._present:
            data["bezierPoints"] = [float(v) for v in self.bezierPoints]
        if self.font is not None:
            data["font"] = self.font
        data.update(copy.deepcopy(self._extra))
        return data

    @classmethod
    def from_json(cls, obj: dict) -> "Event":
        obj = obj or {}
        known = {
            "startTime",
            "endTime",
            "start",
            "end",
            "easingType",
            "easingLeft",
            "easingRight",
            "linkgroup",
            "bezier",
            "bezierPoints",
            "font",
        }
        st = _as_beat(obj.get("startTime"), 0)
        et = _as_beat(obj.get("endTime"), st)
        raw_start = obj.get("start", 0.0)
        raw_end = obj.get("end", raw_start)
        if isinstance(raw_start, str):
            start, end = str(raw_start), str(raw_end)
        elif isinstance(raw_start, (list, tuple)):
            start = [int(v) for v in raw_start]
            end = [int(v) for v in (raw_end if raw_end is not None else raw_start)]
        else:
            # 不能写 float(raw_start or 0.0)：-0.0 是 falsy，会被悄悄换成 0.0。
            # 真实谱面里 -0.0 很常见（IN-v1.1.pez 共 111 处，分布在 rotate/moveX/
            # moveY 的 start/end 与 note.positionX 上），它是有符号的浮点值，
            # 属于原文件的一部分，必须原样写回。
            start = float(raw_start) if raw_start is not None else 0.0
            end = float(raw_end) if raw_end is not None else start
        return cls(
            startTime=st,
            endTime=et,
            start=start,
            end=end,
            easingType=_load_easing(obj.get("easingType", 1)),
            easingLeft=float(obj.get("easingLeft", 0.0)),
            easingRight=float(obj.get("easingRight", 1.0)),
            linkgroup=int(obj.get("linkgroup", 0)),
            bezier=int(obj.get("bezier", 0)),
            bezierPoints=[float(v) for v in obj.get("bezierPoints", [0.0, 0.0, 0.0, 0.0])],
            font=obj.get("font"),
            _extra=_read_extra(obj, known),
            _present=_present_keys(obj),
        )


@dataclass
class SpeedEvent(_JsonMixin):
    """流速事件。

    RPE 162 起支持缓动字段，但**始终不支持 bezier**；1.6.0 的 RPE 只写四个基本字段。
    """

    startTime: Beat = field(default_factory=lambda: Beat(0))
    endTime: Beat = field(default_factory=lambda: Beat(0))
    start: float = 1.0
    end: float = 1.0
    easingType: int = 1
    easingLeft: float = 0.0
    easingRight: float = 1.0
    linkgroup: int = 0
    _extra: dict = field(default_factory=dict, repr=False)
    _present: frozenset = field(**_PRESENT_FIELD)

    @property
    def duration(self) -> Beat:
        return self.endTime - self.startTime

    def copy(self) -> "SpeedEvent":
        return copy.deepcopy(self)

    def value_at(self, time: _t.Any) -> float:
        from .interp import interpolate_value

        return float(interpolate_value(self, time))

    def to_json(self) -> dict:
        data: dict[str, _t.Any] = {
            "startTime": self.startTime.to_list(),
            "endTime": self.endTime.to_list(),
            "start": float(self.start),
            "end": float(self.end),
        }
        # 实测真实谱面里的速度事件恒含 easingType/easingLeft/easingRight/linkgroup
        # （但没有 bezier/bezierPoints）。载入过的按原样写回，新建的才省默认值。
        if self.easingType != 1 or "easingType" in self._present:
            data["easingType"] = _dump_easing(self.easingType)
        if self.easingLeft != 0.0 or "easingLeft" in self._present:
            data["easingLeft"] = float(self.easingLeft)
        if self.easingRight != 1.0 or "easingRight" in self._present:
            data["easingRight"] = float(self.easingRight)
        if self.linkgroup or "linkgroup" in self._present:
            data["linkgroup"] = int(self.linkgroup)
        data.update(copy.deepcopy(self._extra))
        return data

    @classmethod
    def from_json(cls, obj: dict) -> "SpeedEvent":
        obj = obj or {}
        known = {
            "startTime",
            "endTime",
            "start",
            "end",
            "easingType",
            "easingLeft",
            "easingRight",
            "linkgroup",
        }
        st = _as_beat(obj.get("startTime"), 0)
        et = _as_beat(obj.get("endTime"), st)
        return cls(
            startTime=st,
            endTime=et,
            start=float(obj.get("start", 1.0)),
            end=float(obj.get("end", obj.get("start", 1.0))),
            easingType=_load_easing(obj.get("easingType", 1)),
            easingLeft=float(obj.get("easingLeft", 0.0)),
            easingRight=float(obj.get("easingRight", 1.0)),
            linkgroup=int(obj.get("linkgroup", 0)),
            _extra=_read_extra(obj, known),
            _present=_present_keys(obj),
        )


@dataclass
class EventLayer(_JsonMixin):
    """事件层（最多 5 层，索引 0~4）。"""

    moveXEvents: list[Event] = field(default_factory=list)
    moveYEvents: list[Event] = field(default_factory=list)
    rotateEvents: list[Event] = field(default_factory=list)
    alphaEvents: list[Event] = field(default_factory=list)
    speedEvents: list[SpeedEvent] = field(default_factory=list)
    _extra: dict = field(default_factory=dict, repr=False)

    def events(self, kind: str) -> list:
        return getattr(self, kind)

    def is_empty(self) -> bool:
        return not any(
            getattr(self, k) for k in COMMON_EVENT_FIELDS if hasattr(self, k)
        ) and not self._extra

    def to_json(self, drop_empty: bool = True) -> _t.Optional[dict]:
        data: dict[str, _t.Any] = {}
        for key in COMMON_EVENT_FIELDS:
            items = getattr(self, key, None)
            if not items:
                continue
            data[key] = [_dump_typed(e) for e in items]
        # alpha 事件的值 RPE 恒写整数（见 _intify_event_values）。
        for event in data.get(EVENT_ALPHA, []):
            _intify_event_values(event)
        data.update(copy.deepcopy(self._extra))
        if not data and drop_empty:
            return None
        return data

    @classmethod
    def from_json(cls, obj: _t.Optional[dict]) -> "EventLayer":
        if not obj:
            return cls()
        known = set(COMMON_EVENT_FIELDS)
        layer = cls(
            moveXEvents=[Event.from_json(e) for e in obj.get(EVENT_MOVE_X, [])],
            moveYEvents=[Event.from_json(e) for e in obj.get(EVENT_MOVE_Y, [])],
            rotateEvents=[Event.from_json(e) for e in obj.get(EVENT_ROTATE, [])],
            alphaEvents=[Event.from_json(e) for e in obj.get(EVENT_ALPHA, [])],
            speedEvents=[SpeedEvent.from_json(e) for e in obj.get(EVENT_SPEED, [])],
            _extra=_read_extra(obj, known),
        )
        return layer


@dataclass
class ExtendedEvents(_JsonMixin):
    """``judgeLine.extended``：故事板类特殊事件。"""

    scaleXEvents: list[Event] = field(default_factory=list)
    scaleYEvents: list[Event] = field(default_factory=list)
    colorEvents: list[Event] = field(default_factory=list)
    textEvents: list[Event] = field(default_factory=list)
    paintEvents: list[Event] = field(default_factory=list)
    gifEvents: list[Event] = field(default_factory=list)
    inclineEvents: list[Event] = field(default_factory=list)
    _extra: dict = field(default_factory=dict, repr=False)

    def is_empty(self) -> bool:
        return not any(getattr(self, k) for k in SPECIAL_EVENT_FIELDS) and not self._extra

    def to_json(self) -> _t.Optional[dict]:
        data: dict[str, _t.Any] = {}
        for key in SPECIAL_EVENT_FIELDS:
            items = getattr(self, key, None)
            if not items:
                continue
            data[key] = [_dump_typed(e) for e in items]
        # 颜色事件的值 RPE 恒写整数（与 alpha 事件同理，见 _intify_event_values）。
        for event in data.get("colorEvents", []):
            _intify_event_values(event)
        data.update(copy.deepcopy(self._extra))
        return data or None

    @classmethod
    def from_json(cls, obj: _t.Optional[dict]) -> "ExtendedEvents":
        if not obj:
            return cls()
        known = set(SPECIAL_EVENT_FIELDS)
        return cls(
            scaleXEvents=[Event.from_json(e) for e in obj.get("scaleXEvents", [])],
            scaleYEvents=[Event.from_json(e) for e in obj.get("scaleYEvents", [])],
            colorEvents=[Event.from_json(e) for e in obj.get("colorEvents", [])],
            textEvents=[Event.from_json(e) for e in obj.get("textEvents", [])],
            paintEvents=[Event.from_json(e) for e in obj.get("paintEvents", [])],
            gifEvents=[Event.from_json(e) for e in obj.get("gifEvents", [])],
            inclineEvents=[Event.from_json(e) for e in obj.get("inclineEvents", [])],
            _extra=_read_extra(obj, known),
        )


@dataclass
class ControlKeyframe(_JsonMixin):
    """属性控制关键帧。

    ``x`` 是与判定线的纵向距离（即音符的 floor position），``value`` 是该处的倍率。
    键名随控制类型变化：alphaControl 用 ``alpha``、sizeControl 用 ``size``、
    posControl 用 ``pos``、yControl 用 ``y``、skewControl 用 ``skew``。
    """

    x: float = 0.0
    easing: int = 1
    value: float = 1.0
    value_key: str = "value"
    _extra: dict = field(default_factory=dict, repr=False)

    def to_json(self) -> dict:
        data = {
            "x": float(self.x),
            self.value_key: float(self.value),
            "easing": int(self.easing),
        }
        data.update(copy.deepcopy(self._extra))
        return data

    @classmethod
    def from_json(cls, obj: dict, value_key: str = "value") -> "ControlKeyframe":
        obj = obj or {}
        known = {"x", "easing", value_key}
        value = obj.get(value_key)
        if value is None:
            value = obj.get("value", 1.0)
        return cls(
            x=float(obj.get("x", 0.0)),
            easing=int(obj.get("easing", 1)),
            value=float(value),
            value_key=value_key,
            _extra=_read_extra(obj, known),
        )


@dataclass
class Controls(_JsonMixin):
    """五种属性控制；``None`` 表示该字段在 JSON 中不存在。"""

    alphaControl: _t.Optional[list[ControlKeyframe]] = None
    sizeControl: _t.Optional[list[ControlKeyframe]] = None
    posControl: _t.Optional[list[ControlKeyframe]] = None
    yControl: _t.Optional[list[ControlKeyframe]] = None
    skewControl: _t.Optional[list[ControlKeyframe]] = None

    #: JSON 键 -> 关键帧内数值键
    VALUE_KEYS: _t.ClassVar[dict[str, str]] = {
        "alphaControl": "alpha",
        "sizeControl": "size",
        "posControl": "pos",
        "yControl": "y",
        "skewControl": "skew",
    }

    def get(self, key: str) -> _t.Optional[list[ControlKeyframe]]:
        return getattr(self, key)

    def set(self, key: str, frames: _t.Optional[list[ControlKeyframe]]) -> None:
        setattr(self, key, frames)

    def to_json(self) -> _t.Optional[dict]:
        data: dict[str, _t.Any] = {}
        for key in self.VALUE_KEYS:
            frames = getattr(self, key, None)
            if frames is None:
                continue
            data[key] = [_dump_typed(f) for f in frames]
        return data or None

    @classmethod
    def from_json(cls, obj: _t.Optional[dict]) -> "Controls":
        if not obj:
            return cls()
        kwargs: dict[str, _t.Any] = {}
        for key, value_key in cls.VALUE_KEYS.items():
            raw = obj.get(key)
            if raw is None:
                continue
            kwargs[key] = [
                ControlKeyframe.from_json(f, value_key)
                if f.get(value_key) is None and f.get("value") is not None
                else ControlKeyframe.from_json(f, value_key)
                for f in raw
            ]
        return cls(**kwargs)


@dataclass
class JudgeLine(_JsonMixin):
    """一条判定线。"""

    notes: list[Note] = field(default_factory=list)
    eventLayers: list[EventLayer] = field(default_factory=list)
    extended: ExtendedEvents = field(default_factory=ExtendedEvents)
    controls: Controls = field(default_factory=Controls)
    Group: int = 0
    Name: str = "Untitled"
    Texture: str = "line.png"
    anchor: list[float] = field(default_factory=lambda: [0.5, 0.5])
    father: int = -1
    isCover: int = 1
    zOrder: int = 0
    bpmfactor: float = 1.0
    attachUI: _t.Optional[str] = None
    isGif: bool = False
    rotateWithFather: bool = True
    _extra: dict = field(default_factory=dict, repr=False)
    _present: frozenset = field(**_PRESENT_FIELD)
    # 实测 577 条真实判定线里有 42 条**完全没有 numOfNotes 键**（Hold-only 行、
    # 空行），且 v113/v140 旧存档写成 len(notes)（含 Hold）、另有个别手工值。
    # 该字段是谱师填写的参考值，RPE 不强制重算，所以载入时必须原样带过去。
    _raw_numOfNotes: _t.Optional[int] = field(default=None, repr=False, compare=False)
    # 真实谱面的 eventLayers 数组里第 5 个元素常是 JSON null 占位（实测 88 处），
    # 槽位本身是语义的一部分，序列化时要还原。
    _null_layers: frozenset = field(
        default_factory=frozenset, repr=False, compare=False
    )

    # ------------------------------------------------------------ 便捷访问
    @property
    def numOfNotes(self) -> int:
        """RPE 口径：含 FakeNote，**不含 Hold**。"""
        return sum(1 for n in self.notes if n.type != NOTE_HOLD)

    @property
    def real_note_count(self) -> int:
        """真正参与判定的物量（排除 Fake 与 Hold）。"""
        return sum(
            1 for n in self.notes if n.type != NOTE_HOLD and not n.isFake
        )

    def new_layer(self, index: int) -> EventLayer:
        """取第 ``index`` 层，不足则补齐中间的空层。"""
        while len(self.eventLayers) <= index:
            self.eventLayers.append(EventLayer())
        return self.eventLayers[index]

    def add_events(self, kind: str, events: _t.Iterable, layer: int = 0) -> None:
        layer_obj = self.new_layer(layer)
        getattr(layer_obj, kind).extend(events)

    def add_note(self, note: Note) -> Note:
        self.notes.append(note)
        return note

    def sort(self) -> None:
        """按 RPE 的标准输出顺序排序音符与各层事件。

        音符的顺序 RPE 并不强制，但它自己生成的标准谱面是「非 Hold 全体按时间
        升序 + Hold 全体按时间升序」（实测 v150+ 的 190 行样本 0 违例，而纯时间
        升序有 101 行违例）。v113/v140 的旧存档是纯时间升序，载入后不会被自动
        重排 —— 这个方法是显式调用的。
        """
        self.notes.sort(key=lambda n: (n.type == NOTE_HOLD, n.startTime.frac))
        for layer in self.eventLayers:
            for key in COMMON_EVENT_FIELDS:
                getattr(layer, key).sort(key=lambda e: e.startTime.frac)
        for key in SPECIAL_EVENT_FIELDS:
            getattr(self.extended, key).sort(key=lambda e: e.startTime.frac)

    # ------------------------------------------------------------ 序列化
    def to_json(self) -> dict:
        data: dict[str, _t.Any] = {
            "Group": int(self.Group),
            "Name": self.Name,
            "Texture": self.Texture,
            "father": int(self.father),
            "isCover": int(self.isCover),
            "zOrder": int(self.zOrder),
            "bpmfactor": float(self.bpmfactor),
        }
        # numOfNotes：v150+ 口径是「非 Hold 数（含 Fake）」，v113/v140 旧存档写
        # len(notes)（含 Hold），还有 4 例手工值，且 42 条真实判定线整键省略。
        # 该字段是谱师填的参考值，RPE 不强制重算 → 载入过的原样带过去。
        if _is_loaded(self):
            if self._raw_numOfNotes is not None:
                data["numOfNotes"] = int(self._raw_numOfNotes)
        else:
            data["numOfNotes"] = self.numOfNotes
        # 实测：真实谱面在判定线没有音符时**整键省略 notes**（temmie 24 线里 5 条如此，
        # IN-v1.1 12 线里 3 条如此）。载入过的按原样保留，新建的空线也省略。
        if self.notes or "notes" in self._present:
            data["notes"] = [_dump_typed(n) for n in self.notes]
        if self.anchor != [0.5, 0.5] or "anchor" in self._present:
            data["anchor"] = [float(v) for v in self.anchor]
        # eventLayers 里的 null 是占位语义（实测 88 处，全在 5 层行的第 5 元素），
        # 不能简单过滤掉，否则数组长度会从 5 变成 4。载入过的严格按槽位还原；
        # 程序化新建的则丢掉空层（真实谱面里 1~5 层都出现过，短数组完全合法）。
        serialized: list[_t.Any] = []
        for index, layer in enumerate(self.eventLayers):
            if index in self._null_layers:
                serialized.append(None)
            else:
                serialized.append(layer.to_json())
        if not _is_loaded(self):
            serialized = [item for item in serialized if item is not None]
        if serialized:
            data["eventLayers"] = serialized
        ext = self.extended.to_json()
        if ext:
            data["extended"] = ext
        ctrl = self.controls.to_json()
        if ctrl:
            data.update(ctrl)
        if self.attachUI is not None:
            data["attachUI"] = self.attachUI
        # 实测真实谱面（含 150+ 的）一律显式写出 isGif，哪怕值是 false；
        # 载入过的照原样保留，程序化新建时才省略。
        if self.isGif or "isGif" in self._present:
            data["isGif"] = bool(self.isGif)
        # v113/v140 根本没有这个键；载入过的按存在性决定（且 False 时不凭空补出），
        # 程序化新建的一律显式写出（显式优于依赖版本相关的缺省语义）。
        if (
            not _is_loaded(self)
            or self.rotateWithFather
            or "rotateWithFather" in self._present
        ):
            data["rotateWithFather"] = bool(self.rotateWithFather)
        data.update(copy.deepcopy(self._extra))
        return data

    @classmethod
    def from_json(cls, obj: dict) -> "JudgeLine":
        obj = obj or {}
        known = {
            "Group",
            "Name",
            "Texture",
            "anchor",
            "eventLayers",
            "extended",
            "father",
            "isCover",
            "notes",
            "numOfNotes",
            "zOrder",
            "attachUI",
            "isGif",
            "bpmfactor",
            "rotateWithFather",
            "posControl",
            "alphaControl",
            "sizeControl",
            "skewControl",
            "yControl",
        }
        raw_layers = obj.get("eventLayers")
        layers: list[EventLayer] = []
        null_layers: set[int] = set()
        if isinstance(raw_layers, list):
            # null 层表示空层；143+ 直接不写该层，两种都要保留位置
            for index, raw in enumerate(raw_layers):
                if raw is None:
                    null_layers.add(index)
                layers.append(EventLayer.from_json(raw))
        return cls(
            notes=[Note.from_json(n) for n in obj.get("notes", [])],
            eventLayers=layers,
            extended=ExtendedEvents.from_json(obj.get("extended")),
            controls=Controls.from_json(obj),
            Group=int(obj.get("Group", 0)),
            Name=str(obj.get("Name", "Untitled")),
            Texture=str(obj.get("Texture", "line.png")),
            anchor=[float(v) for v in obj.get("anchor", [0.5, 0.5])],
            father=int(obj.get("father", -1)),
            isCover=int(obj.get("isCover", 1)),
            zOrder=int(obj.get("zOrder", 0)),
            bpmfactor=float(obj.get("bpmfactor", 1.0)) or 1.0,
            attachUI=obj.get("attachUI"),
            isGif=bool(obj.get("isGif", False)),
            rotateWithFather=bool(obj.get("rotateWithFather", False)),
            _raw_numOfNotes=(
                int(obj["numOfNotes"])
                if isinstance(obj.get("numOfNotes"), (int, float))
                else None
            ),
            _null_layers=frozenset(null_layers),
            _extra=_read_extra(obj, known),
            _present=_present_keys(obj),
        )


@dataclass
class BPMItem(_JsonMixin):
    """一段 BPM。``startTime`` 是该段开始的拍号。"""

    bpm: float = 120.0
    startTime: Beat = field(default_factory=lambda: Beat(0))

    def to_json(self) -> dict:
        return {"bpm": float(self.bpm), "startTime": self.startTime.to_list()}

    @classmethod
    def from_json(cls, obj: dict) -> "BPMItem":
        return cls(
            bpm=float(obj.get("bpm", 120.0)),
            startTime=_as_beat(obj.get("startTime"), 0),
        )


@dataclass
class Meta(_JsonMixin):
    """``META`` 段。"""

    RPEVersion: int = 160
    name: str = "Unknow"
    id: str = "0"
    song: str = "song.mp3"
    background: str = "background.png"
    composer: str = "Unknow"
    charter: str = "Unknow"
    illustrator: _t.Optional[str] = None
    level: str = "Unknow"
    offset: int = 0
    # 音乐时长（秒），RPEVersion >= 150 才写这个键（实测语料 113/140 无、150/160/170 有）。
    # 注意：RPE 1.6.0 自己保存时**总是**写这个键，且刚新建的谱面写 ``0.0``
    # （实测 ``Resources/<id>/AutoSave_*.json`` 里 ``"duration": 0.0``）。
    # 该键与音频实际时长无关时 RPE 并不报错，所以它更像编辑器的缓存字段。
    # 保留 ``None`` 表示「没有这个键」，以便对旧谱面做无损往返。
    duration: _t.Optional[float] = None
    _extra: dict = field(default_factory=dict, repr=False)
    _present: frozenset = field(**_PRESENT_FIELD)
    # v113 的 ``id`` 是 JSON 数字（实测 5/24 份语料如此），其余版本是字符串。
    # 类型本身是别人文件的一部分，不该由本库统一改写。
    _id_is_int: bool = field(default=False, repr=False, compare=False)

    def to_json(self) -> dict:
        data: dict[str, _t.Any] = {
            "RPEVersion": int(self.RPEVersion),
            "charter": self.charter,
            "composer": self.composer,
            "id": int(self.id) if self._id_is_int else self.id,
            "level": self.level,
            "name": self.name,
            "offset": int(self.offset),
            "song": self.song,
        }
        # v113/v140 的部分 META 没有 background 键（实测 pezc-80867 的 8 键 META）；
        # 载入过的按存在性决定，但用户显式改过的值仍要写出来。
        if (
            not _is_loaded(self)
            or "background" in self._present
            or self.background != "background.png"
        ):
            data["background"] = self.background
        if self.illustrator is not None:
            data["illustration"] = self.illustrator
        if self.duration is not None:
            data["duration"] = float(self.duration)
        data.update(copy.deepcopy(self._extra))
        return data

    @classmethod
    def from_json(cls, obj: dict) -> "Meta":
        obj = obj or {}
        known = {
            "RPEVersion",
            "background",
            "charter",
            "composer",
            "id",
            "level",
            "name",
            "offset",
            "song",
            "illustration",
            "duration",
        }
        # 注意：语料里 v140 谱面的 META 还带一个 ``Malody2Phigros`` 键（转换器留下的）。
        # 它**不能**加进 known —— 否则会从 _extra 里被剔除而丢失；留在这里由 _read_extra
        # 收进 _extra 再原样写回，才有无损往返。
        raw_id = obj.get("id", "0")
        raw_duration = obj.get("duration")
        return cls(
            RPEVersion=int(obj.get("RPEVersion", 160)),
            name=str(obj.get("name", "Unknow")),
            id=str(raw_id),
            song=str(obj.get("song", "song.mp3")),
            background=str(obj.get("background", "background.png")),
            composer=str(obj.get("composer", "Unknow")),
            charter=str(obj.get("charter", "Unknow")),
            illustrator=obj.get("illustration"),
            level=str(obj.get("level", "Unknow")),
            offset=int(obj.get("offset", 0)),
            duration=float(raw_duration) if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool) else None,
            _extra=_read_extra(obj, known),
            _present=_present_keys(obj),
            _id_is_int=isinstance(raw_id, int) and not isinstance(raw_id, bool),
        )


@dataclass
class TimeTag(_JsonMixin):
    name: str = ""
    time: Beat = field(default_factory=lambda: Beat(0))

    def to_json(self) -> dict:
        return {"name": self.name, "time": self.time.to_list()}

    @classmethod
    def from_json(cls, obj: dict) -> "TimeTag":
        return cls(name=str(obj.get("name", "")), time=_as_beat(obj.get("time"), 0))


@dataclass
class Chart(_JsonMixin):
    """一整张 RPE 谱面。"""

    BPMList: list[BPMItem] = field(default_factory=lambda: [BPMItem()])
    META: Meta = field(default_factory=Meta)
    judgeLineList: list[JudgeLine] = field(default_factory=list)
    chartTime: float = 0.0
    #: 分组**名**数组；每条判定线的 ``Group`` 是它的下标。RPE 初始只有一个
    #: ``Default`` 组（说明书「分组初始有一个 Default」），26 份真实谱面里这个
    #: 数组没有一份是空的，RPE 自己改写谱面时也会补上 ``["Default"]``。
    #: 若留空，线默认的 ``Group=0`` 会指向不存在的组名。
    judgeLineGroup: list[str] = field(default_factory=lambda: ["Default"])
    timeTags: list[TimeTag] = field(default_factory=list)
    multiLineString: str = ""
    multiScale: float = 1.0
    xybind: bool = True
    _extra: dict = field(default_factory=dict, repr=False)
    _present: frozenset = field(**_PRESENT_FIELD)

    # ------------------------------------------------------------ 便捷访问
    def line(self, index: int) -> JudgeLine:
        return self.judgeLineList[index]

    def add_line(self, **kwargs) -> JudgeLine:
        line = JudgeLine(**kwargs)
        self.judgeLineList.append(line)
        return line

    @property
    def lines(self) -> list[JudgeLine]:
        return self.judgeLineList

    @property
    def note_count(self) -> int:
        return sum(line.numOfNotes for line in self.judgeLineList)

    @property
    def hold_count(self) -> int:
        return sum(
            1 for line in self.judgeLineList for n in line.notes if n.type == NOTE_HOLD
        )

    @property
    def fake_count(self) -> int:
        return sum(
            1 for line in self.judgeLineList for n in line.notes if n.isFake
        )

    def duration_beats(self) -> Beat:
        end = Beat(0)
        for line in self.judgeLineList:
            for n in line.notes:
                if n.endTime > end:
                    end = n.endTime
                if n.startTime > end:
                    end = n.startTime
            for layer in line.eventLayers:
                for key in COMMON_EVENT_FIELDS:
                    for e in getattr(layer, key):
                        if e.endTime > end:
                            end = e.endTime
        return end

    def sort(self) -> None:
        self.BPMList.sort(key=lambda b: b.startTime.frac)
        for line in self.judgeLineList:
            line.sort()

    def copy(self) -> "Chart":
        return copy.deepcopy(self)

    # ------------------------------------------------------------ 序列化
    def to_json(self) -> dict:
        data: dict[str, _t.Any] = {
            "BPMList": [_dump_typed(b) for b in self.BPMList],
            "META": self.META.to_json(),
            "judgeLineGroup": list(self.judgeLineGroup),
            "judgeLineList": [_dump_typed(l) for l in self.judgeLineList],
        }
        # 这几个键是后来才加入的：chartTime 是 141+、multiLineString/multiScale 是
        # 1.4 时代、xybind 只在 170 谱面里见到、timeTags 是 130+。旧谱面整键不存在，
        # 载入过的一律按存在性决定，程序化新建时才补出来。
        loaded = _is_loaded(self)
        if self.chartTime or not loaded or "chartTime" in self._present:
            data["chartTime"] = float(self.chartTime)
        if self.multiLineString or not loaded or "multiLineString" in self._present:
            data["multiLineString"] = self.multiLineString
        if self.multiScale != 1.0 or not loaded or "multiScale" in self._present:
            data["multiScale"] = float(self.multiScale)
        if not self.xybind or not loaded or "xybind" in self._present:
            data["xybind"] = bool(self.xybind)
        if self.timeTags or "timeTags" in self._present:
            data["timeTags"] = [_dump_typed(t) for t in self.timeTags]
        data.update(copy.deepcopy(self._extra))
        return data

    @classmethod
    def from_json(cls, obj: dict) -> "Chart":
        obj = obj or {}
        known = {
            "BPMList",
            "META",
            "chartTime",
            "judgeLineGroup",
            "judgeLineList",
            "multiLineString",
            "multiScale",
            "timeTags",
            "xybind",
        }
        # 只有「字段不存在」才补默认 BPM；显式的空数组保留原样交给校验器报错，
        # 否则一张 BPM 丢失的谱面会被静默改成 120 BPM 的错谱。
        raw_bpm = obj.get("BPMList")
        if isinstance(raw_bpm, list):
            bpm_list = [BPMItem.from_json(b) for b in raw_bpm]
        else:
            bpm_list = [BPMItem()]
        chart = cls(
            BPMList=bpm_list,
            META=Meta.from_json(obj.get("META", {})),
            judgeLineList=[JudgeLine.from_json(l) for l in obj.get("judgeLineList", [])],
            chartTime=float(obj.get("chartTime", 0.0)),
            judgeLineGroup=[str(g) for g in obj.get("judgeLineGroup", [])],
            timeTags=[TimeTag.from_json(t) for t in obj.get("timeTags", [])],
            multiLineString=str(obj.get("multiLineString", "")),
            multiScale=float(obj.get("multiScale", 1.0)),
            xybind=bool(obj.get("xybind", True)),
            _extra=_read_extra(obj, known),
            _present=_present_keys(obj),
        )
        chart.BPMList.sort(key=lambda b: b.startTime.frac)
        return chart
