"""事件的插值与求值。

RPE 播放器（以及 phira 的参考实现）在求某条判定线在某时刻的属性时是这样做的：

1. 把每类事件按 ``startTime`` 排序；
2. 相邻两个事件之间若有空隙，用前一个事件的**终值**补一段常量事件；
3. 末尾补一个哨兵事件，让最后一个值一直持续下去；
4. 求值时找**第一个**满足 ``startTime <= t <= endTime`` 的事件，按 ``easingType``
   在 ``[start, end]`` 之间插值。

:func:`resolve_timeline` 实现 1-3，:func:`value_at` 实现第 4 步。
"""

from __future__ import annotations

import bisect
import typing as _t

from .beat import Beat
from .easing import bezier as _bezier, clamp_easing, ease

__all__ = [
    "interpolate_value",
    "value_at",
    "value_at_resolved",
    "resolve_timeline",
    "SENTINEL_TIME",
    "SUPPORTED_VALUE_TYPES",
]

#: 哨兵事件的终点：足够远的未来（phira 参考实现用 31250000 拍）
SENTINEL_TIME = Beat(31_250_000)

SUPPORTED_VALUE_TYPES = (int, float, list, tuple, str)


def _lerp_value(f: float, start: _t.Any, end: _t.Any) -> _t.Any:
    """按缓动后的进度 ``f`` 在 ``start`` 与 ``end`` 之间求值。"""
    if isinstance(start, str):
        # 文字事件不做插值，直接返回起点文本；含 %P% 时由调用方自行替换
        return start
    if isinstance(start, (list, tuple)):
        out = []
        for a, b in zip(start, end):
            out.append(round(float(a) + (float(b) - float(a)) * f, 6))
        return out
    return float(start) + (float(end) - float(start)) * f


def _progress(event, time: Beat) -> float:
    """算归一化进度，并把 ``easingLeft`` / ``easingRight`` 窗口考虑进去。"""
    st, et = event.startTime.value, event.endTime.value
    if et <= st:
        return 1.0
    raw = (time.value - st) / (et - st)
    if raw < 0.0:
        raw = 0.0
    elif raw > 1.0:
        raw = 1.0
    left = getattr(event, "easingLeft", 0.0) or 0.0
    right = getattr(event, "easingRight", 1.0)
    right = 1.0 if right is None else right
    if left != 0.0 or right != 1.0:
        span = right - left
        if span <= 0:
            raw = 1.0
        else:
            raw = (raw - left) / span
            raw = 0.0 if raw < 0.0 else (1.0 if raw > 1.0 else raw)
    if getattr(event, "bezier", 0):
        points = getattr(event, "bezierPoints", None) or [0.0, 0.0, 0.0, 0.0]
        if len(points) >= 4:
            return _bezier(raw, (points[0], points[1], points[2], points[3]))
    return ease(getattr(event, "easingType", 1), raw)


def interpolate_value(event, time: _t.Any) -> _t.Any:
    """在单个事件内部插值（不做事件查找）。"""
    t = Beat.of(time)
    if t == event.startTime:
        return event.start
    if isinstance(event.start, str):
        return event.start
    return _lerp_value(_progress(event, t), event.start, event.end)


_MISSING = object()


def value_at(events: _t.Sequence, time: _t.Any, default: _t.Any = 0.0) -> _t.Any:
    """在事件序列上求值：返回**覆盖** ``t`` 的那个事件的结果。

    这是 RPE ``GetEventValue`` 的语义；若没有任何事件覆盖 ``t``，返回 ``default``。

    区间的闭合方向按播放器的实际行为来定：``[startTime, endTime)`` 优先，
    只有当没有任何事件「从 ``t`` 起还继续有效」时，才接受 ``t == endTime`` 的
    事件。原因是相邻事件共享边界时（例如把线从 A 瞬移到 B：``[0,4]`` 值 A、
    ``[4,64]`` 值 B），RPE/phira 的关键帧游标会继续前进到后一个事件，所以
    ``t == 4`` 必须取 B 而不是 A。

    **输入可以乱序**：函数在入口按 ``(startTime, endTime)`` 排序后再求值，
    因此 ``value_at(evs, t)`` 与 ``value_at(sorted(evs), t)`` 恒等。已排序的
    序列会跳过这次重排，所以更快。

    重叠时按 RPE 的遍历顺序返回**第一个**（起点最小的）覆盖事件。
    """
    if not events:
        return default
    t = Beat.of(time)
    # 入口按 (startTime, endTime) **联合键**检测乱序并整体重排。只比 startTime
    # 会漏掉「起点相同、终点乱序」的输入，而那正是旧实现的 bug：docstring 承诺
    # 过恒等，实际会静默取错值。
    keys = [(e.startTime.frac, e.endTime.frac) for e in events]
    if any(keys[i] > keys[i + 1] for i in range(len(keys) - 1)):
        order = sorted(range(len(events)), key=lambda i: keys[i])
        events = [events[i] for i in order]
        keys = [keys[i] for i in order]
    starts = [k[0] for k in keys]
    # 二分到最后一个 startTime <= t 的位置；只有下标 <= idx 的事件可能覆盖 t。
    idx = bisect.bisect_right(starts, t.frac) - 1
    if idx < 0:
        return default
    # 前缀最大 endTime 是非降序列，二分出「第一个 endTime > t 的下标」即为覆盖
    # t 的那个事件。旧实现每次都从 0 线性回扫到 idx（O(n)），事件链长时是热路径。
    prefmax: list = []
    running = None
    for _start, end in keys:
        running = end if running is None or end > running else running
        prefmax.append(running)
    hit = bisect.bisect_right(prefmax, t.frac, 0, idx + 1)
    if hit <= idx:
        return interpolate_value(events[hit], t)
    # 没有任何事件「从 t 起还继续有效」时，才接受 t == endTime 的事件，
    # 并取下标最小的那个（保持旧语义）。
    for i in range(idx + 1):
        if t == events[i].endTime:
            return interpolate_value(events[i], t)
    return default


def value_at_resolved(events: _t.Sequence, time: _t.Any, default: _t.Any = 0.0) -> _t.Any:
    """按**播放器语义**求值：事件链之外的时间也返回一个值。

    :func:`value_at` 是纯文件语义（没事件覆盖就返回 ``default``），而真实的
    RPE / phira 播放器在最后一个事件结束之后会**保持它的终值**（phaser 的
    ``Anim`` 在最后一个关键帧处停下不再外推），在第一个事件之前则取首值。

    这不是理论推演：实测 24 份真实谱面的 8408 个事件列表里，有 5485 个在
    第 16 拍内就结束了（最长 1136 拍），而 300 条带音符的判定线里有 41 条
    事件链比最后一个音符还早结束 —— 这些谱面在游戏里正常渲染，说明末值必然
    被持续使用。

    想要「处处有值的时间线」也可以直接用 :func:`resolve_timeline` 补齐后
    再调 :func:`value_at`；本函数是不复制序列的等价快捷方式。
    """
    if not events:
        return default
    t = Beat.of(time)
    ordered = sorted(events, key=lambda e: (e.startTime.frac, e.endTime.frac))
    hit = value_at(ordered, t, _MISSING)
    if hit is not _MISSING:
        return hit
    if t < ordered[0].startTime:
        # 首个事件开始之前：播放器用首值（与 resolve_timeline 的前导段一致）
        return ordered[0].start
    prev = None
    for event in ordered:
        if event.startTime <= t:
            prev = event
        else:
            break
    if prev is None:  # pragma: no cover - 排序后不可能发生
        return ordered[0].start
    return prev.end


def resolve_timeline(
    events: _t.Sequence,
    value: _t.Any = None,
    extend_to: _t.Any = None,
) -> list:
    """把事件序列补成一条**处处有值**的时间线。

    与 RPE 的内部行为一致：

    * 空隙用前一个事件的终值填成常量事件；
    * 开头之前的一段时间用第一个事件的**起点值**填（RPE 里 t<startTime 时取 start）；
    * 末尾追加哨兵事件，让最后一个值一直有效。
    """
    from .model import Event, SpeedEvent

    ordered = sorted(events, key=lambda e: (e.startTime.frac, e.endTime.frac))
    if not ordered:
        return []

    def _make(st: Beat, et: Beat, sv: _t.Any, ev: _t.Any) -> _t.Any:
        cls = SpeedEvent if isinstance(ordered[0], SpeedEvent) else Event
        if cls is SpeedEvent:
            return SpeedEvent(
                startTime=st, endTime=et, start=float(sv), end=float(ev), easingType=1
            )
        return Event(startTime=st, endTime=et, start=sv, end=ev, easingType=1)

    out: list = []
    first = ordered[0]
    if first.startTime > Beat(0):
        out.append(_make(Beat(0), first.startTime, first.start, first.start))
    for i, event in enumerate(ordered):
        out.append(event)
        nxt = ordered[i + 1] if i + 1 < len(ordered) else None
        end = Beat(extend_to) if (nxt is None and extend_to is not None) else None
        if nxt is not None and event.endTime < nxt.startTime:
            out.append(_make(event.endTime, nxt.startTime, event.end, event.end))
        elif nxt is None and (end is None or event.endTime < end):
            out.append(
                _make(
                    event.endTime,
                    end if end is not None else SENTINEL_TIME,
                    event.end,
                    event.end,
                )
            )
        elif nxt is not None and event.endTime > nxt.startTime:
            # 重叠是非法状态，交给校验器报告；这里不静默修改
            pass
    return out
