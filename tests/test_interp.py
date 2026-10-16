"""interp.py —— 事件插值与求值。

RPE 播放器的求值语义：找**第一个**满足 ``startTime <= t <= endTime`` 的事件，
按缓动在起止值之间插值。这里的正确性直接决定「程序化生成的谱面播出来是不是
我想的样子」，所以每个边界都要钉死。
"""

import math

from run_tests import approx, test

from rpechart.beat import Beat
from rpechart.easing import ease
from rpechart.interp import (
    SENTINEL_TIME,
    interpolate_value,
    resolve_timeline,
    value_at,
)
from rpechart.model import Event, SpeedEvent


# ------------------------------------------------------------------ 单事件插值
@test
def interpolation_endpoints_are_exact():
    """起止点的取值必须精确等于 start/end（不能有浮点漂移）。"""
    e = Event(startTime=Beat(4), endTime=Beat(8), start=10.0, end=50.0)
    approx(interpolate_value(e, Beat(4)), 10.0)
    approx(interpolate_value(e, Beat(8)), 50.0)


@test
def linear_interpolation_is_arithmetic():
    e = Event(startTime=Beat(0), endTime=Beat(4), start=0.0, end=100.0)
    approx(interpolate_value(e, Beat(1)), 25.0)
    approx(interpolate_value(e, Beat(2)), 50.0)
    approx(interpolate_value(e, Beat(3)), 75.0)


@test
def interpolation_uses_the_easing_curve():
    """非线性缓动必须真的走曲线，不能被当成线性。"""
    e = Event(startTime=Beat(0), endTime=Beat(2), start=0.0, end=100.0, easingType=2)  # out_sine
    got = interpolate_value(e, Beat(1))
    approx(got, 100.0 * math.sin(0.5 * math.pi / 2), tol=1e-9)
    assert got > 50.0            # out_sine 前段更快
    assert not abs(got - 50.0) < 1e-6


@test
def zero_length_event_returns_its_end_value():
    """startTime == endTime 的退化事件返回终值，且不产生除零。"""
    e = Event(startTime=Beat(3), endTime=Beat(3), start=1.0, end=9.0)
    approx(interpolate_value(e, Beat(3)), 1.0)   # t == startTime 时返回 start
    approx(interpolate_value(e, Beat(5)), 9.0)   # 越过之后返回 end


@test
def decreasing_values_interpolate_correctly():
    """值可以递减（比如淡出、回收移动）。"""
    e = Event(startTime=Beat(0), endTime=Beat(4), start=255.0, end=0.0)
    approx(interpolate_value(e, Beat(2)), 127.5)


@test
def color_events_interpolate_per_channel():
    """颜色事件按 RGB 三通道各自线性插值。"""
    e = Event(startTime=Beat(0), endTime=Beat(2), start=[0, 0, 0], end=[100, 200, 255])
    got = interpolate_value(e, Beat(1))
    assert len(got) == 3
    approx(got[0], 50.0)
    approx(got[1], 100.0)
    approx(got[2], 127.5)


@test
def text_events_never_interpolate():
    """文字事件不做数值插值 —— 任意时刻都是起点文本。"""
    e = Event(startTime=Beat(0), endTime=Beat(4), start="begin", end="end")
    assert interpolate_value(e, Beat(2)) == "begin"
    assert interpolate_value(e, Beat(4)) == "begin"


@test
def easing_window_restricts_the_active_range():
    """easingLeft/easingRight 把曲线压缩到一个子区间内。"""
    e = Event(
        startTime=Beat(0),
        endTime=Beat(4),
        start=0.0,
        end=100.0,
        easingLeft=0.25,
        easingRight=0.75,
    )
    # 进度 0.25 之前已经走完 0，0.75 之后保持 100
    approx(interpolate_value(e, Beat(0)), 0.0)
    approx(interpolate_value(e, Beat(1)), 0.0)
    approx(interpolate_value(e, Beat(2)), 50.0)    # 窗口正中
    approx(interpolate_value(e, Beat(3)), 100.0)
    approx(interpolate_value(e, Beat(4)), 100.0)


@test
def bezier_flag_switches_the_curve():
    """bezier=1 时走贝塞尔而不是缓动表。"""
    e = Event(
        startTime=Beat(0),
        endTime=Beat(4),
        start=0.0,
        end=100.0,
        bezier=1,
        bezierPoints=[0.0, 0.0, 1.0, 1.0],
    )
    approx(interpolate_value(e, Beat(0)), 0.0)
    approx(interpolate_value(e, Beat(4)), 100.0)
    approx(interpolate_value(e, Beat(2)), 50.0)  # 这条贝塞尔恰好等价于线性


@test
def speed_event_interpolates_the_same_way():
    se = SpeedEvent(startTime=Beat(0), endTime=Beat(4), start=1.0, end=3.0)
    approx(interpolate_value(se, Beat(2)), 2.0)


# ------------------------------------------------------------------ 时间线求值
@test
def empty_timeline_returns_default():
    assert value_at([], Beat(0), default=7.0) == 7.0
    assert value_at([], Beat(0), default=0) == 0


@test
def value_at_picks_the_only_covering_event():
    e = Event(startTime=Beat(0), endTime=Beat(4), start=0.0, end=100.0)
    approx(value_at([e], Beat(2)), 50.0)
    approx(value_at([e], Beat(4)), 100.0)


@test
def value_at_returns_default_outside_every_event():
    """t 落在所有事件之外时返回 default（这正是 RPE 的 GetEventValue 行为）。"""
    e = Event(startTime=Beat(4), endTime=Beat(8), start=0.0, end=100.0)
    assert value_at([e], Beat(1), default=-1.0) == -1.0
    assert value_at([e], Beat(20), default=-1.0) == -1.0


@test
def value_at_handles_long_sorted_sequences():
    """事件很多时不能因为二分窗口太小而漏查（回归保护）。"""
    events = [
        Event(startTime=Beat(i * 10), endTime=Beat(i * 10 + 10), start=float(i), end=float(i + 1))
        for i in range(200)
    ]
    for i in range(0, 200, 7):
        got = value_at(events, Beat(i * 10 + 5))
        approx(got, i + 0.5, tol=1e-6)


@test
def value_at_picks_the_first_matching_event_on_overlap():
    """重叠时取第一个匹配的事件（RPE 的遍历顺序语义）。"""
    a = Event(startTime=Beat(0), endTime=Beat(4), start=0.0, end=100.0)
    b = Event(startTime=Beat(2), endTime=Beat(6), start=500.0, end=600.0)
    approx(value_at([a, b], Beat(3)), 75.0)


# ------------------------------------------------------------ 乱序输入（P0 回归）
@test
def value_at_is_independent_of_input_order():
    """乱序输入必须与排序后完全一致（旧实现承诺过却做不到，会静默取错值）。

    回归样本：短事件排在长事件前面时，旧实现的 ±8 窗口会漏掉真正的覆盖事件。
    """
    unsorted_events = [
        Event(startTime=Beat(30), endTime=Beat(50), start=3.0, end=4.0),
        Event(startTime=Beat(0), endTime=Beat(40), start=1.0, end=2.0),
    ]
    ordered = sorted(unsorted_events, key=lambda e: e.startTime.frac)
    approx(value_at(unsorted_events, Beat(35)), 1.875)
    approx(value_at(unsorted_events, Beat(35)), value_at(ordered, Beat(35)))


@test
def value_at_does_not_miss_events_outside_a_small_window():
    """已排序的重叠输入，覆盖事件排在很后面时也不能漏（旧实现只看 ±8 个）。

    长事件 [0,1000] 后面挂 k 个短事件；k>=9 时旧实现返回 0.0（漏查），
    正确值是长事件在 t=500 处的插值 8.0。
    """
    for k in (5, 8, 9, 12, 20):
        events = [Event(startTime=Beat(0), endTime=Beat(1000), start=7.0, end=9.0)]
        events += [
            Event(startTime=Beat(10 + i), endTime=Beat(10 + i + 0.5), start=float(i), end=float(i))
            for i in range(k)
        ]
        approx(value_at(events, Beat(500)), 8.0, tol=1e-9)


# ------------------------------------------------------------------ 时间线补齐
@test
def resolve_timeline_fills_gaps_with_the_previous_end_value():
    events = [
        Event(startTime=Beat(0), endTime=Beat(2), start=0.0, end=10.0),
        Event(startTime=Beat(5), endTime=Beat(7), start=100.0, end=110.0),
    ]
    out = resolve_timeline(events)
    # [0,2] 真事件、[2,5] 常量桥、[5,7] 真事件、[7,sentinel] 常量尾
    assert len(out) == 4
    bridge = out[1]
    assert bridge.startTime == Beat(2) and bridge.endTime == Beat(5)
    approx(bridge.start, 10.0)
    approx(bridge.end, 10.0)
    assert bridge.easingType == 1


@test
def resolve_timeline_appends_a_sentinel():
    out = resolve_timeline([Event(startTime=Beat(0), endTime=Beat(2), start=1.0, end=2.0)])
    tail = out[-1]
    assert tail.endTime == SENTINEL_TIME
    approx(tail.start, 2.0)
    approx(tail.end, 2.0)


@test
def resolve_timeline_prepends_preroll_from_zero():
    """第一个事件从第 4 拍才开始时，前面补一段常量，取值等于它的起点值。"""
    out = resolve_timeline([Event(startTime=Beat(4), endTime=Beat(6), start=33.0, end=44.0)])
    assert out[0].startTime == Beat(0)
    assert out[0].endTime == Beat(4)
    approx(out[0].start, 33.0)
    approx(out[0].end, 33.0)


@test
def resolve_timeline_does_not_prepend_when_starting_at_zero():
    out = resolve_timeline([Event(startTime=Beat(0), endTime=Beat(2), start=1.0, end=2.0)])
    assert out[0].startTime == Beat(0)
    assert len(out) == 2  # 真事件 + 哨兵，没有多余的前导


@test
def resolve_timeline_honours_extend_to():
    out = resolve_timeline(
        [Event(startTime=Beat(0), endTime=Beat(2), start=1.0, end=2.0)], extend_to=Beat(10)
    )
    assert out[-1].endTime == Beat(10)


@test
def resolve_timeline_keeps_speed_event_types():
    """流速事件补齐后仍必须是 SpeedEvent，否则写出 JSON 会多出 bezier 字段。"""
    out = resolve_timeline(
        [
            SpeedEvent(startTime=Beat(0), endTime=Beat(2), start=1.0, end=2.0),
            SpeedEvent(startTime=Beat(4), endTime=Beat(6), start=3.0, end=4.0),
        ]
    )
    assert all(isinstance(e, SpeedEvent) for e in out)
    assert "bezier" not in out[1].to_json()


@test
def resolve_timeline_of_empty_is_empty():
    assert resolve_timeline([]) == []


@test
def resolved_timeline_is_queryable_everywhere():
    """补齐后的时间线在任意时刻都有值 —— 这正是补齐的意义。"""
    events = [
        Event(startTime=Beat(0), endTime=Beat(2), start=0.0, end=10.0),
        Event(startTime=Beat(5), endTime=Beat(7), start=100.0, end=110.0),
    ]
    line = resolve_timeline(events)
    for beat in (0, 1, 2, 3, 4, 5, 6, 7, 50, 1000):
        assert value_at(line, Beat(beat), default=None) is not None
    approx(value_at(line, Beat(3)), 10.0)    # 空隙里保持前值
    approx(value_at(line, Beat(1000)), 110.0)  # 末尾一直有效


@test
def interpolation_matches_the_easing_table_directly():
    """随机抽查：interpolate_value 必须与 ease() 的结果一致。"""
    for easing_type in (1, 2, 4, 5, 8, 9, 16, 17, 20, 24, 26):
        e = Event(startTime=Beat(0), endTime=Beat(4), start=0.0, end=8.0, easingType=easing_type)
        for step in range(1, 4):
            t = step / 4.0
            expected = 8.0 * ease(easing_type, t)
            approx(interpolate_value(e, Beat(4 * t)), expected, tol=1e-9)
