"""`rpechart.validate` 的测试 —— 逐条对齐 RPE 说明书第 037 页的纠错规则。

规则来源（逐条对照，别凭印象改）：

    Caution:XToo Large                       音符 X 绝对值过大
    Caution:Short Readtime([?]ms)            读谱时间过短
    Caution:Hold Sticked?                    两个 X 同号的 Hold 头尾相连
    Warning:Note Judged OutScreen            判定位置在屏幕外
    Warning:Tap AfterDrag([?]ms)             40 <= 间隔 <= 160
    Error:TapAfterFlick([?]ms)               40 <= 间隔 <= 160
    Error:TimeOut ofRange                    <0 或超过音乐时长
    Error:Tap&Hold/Tap&Tap/Hold&Hold Overlapped
    Caution:Too ManyLinearMoveX/MoveY/Rotate 线性占比 > 25%
    Warning:Alpha EventOverRange             起点或终点不在 [0,255]
    Error:EventTimeOutofRange
    Error:illegal[?] Event                   结束 <= 开始
    Error:[?] EventOverLapped                同层同类重叠
    Caution:CustomizedNote Size              宽度 != 1.0
    Caution:Using FakeNote
    Caution:CustomizedVisibleTime

测试里刻意分开「该报的必须报」与「不该报的绝不能报」两类，后者是靠真实谱面
语料反推的（比如 Drag 垫在 Hold 下面 1283 处，属于合法写法）。
"""

from run_tests import test

from rpechart.beat import Beat
from rpechart.model import (
    EVENT_ALPHA,
    EVENT_MOVE_X,
    EVENT_MOVE_Y,
    EVENT_ROTATE,
    NOTE_DRAG,
    NOTE_FLICK,
    NOTE_HOLD,
    NOTE_TAP,
    BPMItem,
    Chart,
    Event,
    EventLayer,
    ExtendedEvents,
    JudgeLine,
    Meta,
    Note,
    SpeedEvent,
)
from rpechart.validate import (
    CAUTION,
    ERROR,
    WARNING,
    MAX_LINES,
    ValidationReport,
    validate,
)

_BPM = 120.0  # 120 BPM ⇒ 1 拍 = 0.5 秒，1ms = 1/500 拍


def _chart(lines=None, bpm=_BPM):
    return Chart(
        BPMList=[BPMItem(bpm=bpm, startTime=Beat(0))],
        META=Meta(name="T", charter="me", id="1"),
        judgeLineList=list(lines or []),
    )


def _line(notes=None):
    return JudgeLine(Name="L", notes=list(notes or []))


def _codes(report):
    return [i.code for i in report.issues]


def _ms(ms):
    """把毫秒换算成当前的拍数（120 BPM 下 1ms = 1/500 拍）。"""
    return Beat(ms * _BPM / 60000.0)


# --------------------------------------------------------------- 基础结构
@test
def empty_chart_is_clean():
    report = validate(_chart())
    assert report.ok, report.format()
    assert len(report) == 0


@test
def missing_bpm_is_an_error():
    chart = _chart()
    chart.BPMList = []
    report = validate(chart)
    assert "NoBPM" in _codes(report)
    assert not report.ok


@test
def non_positive_bpm_is_an_error():
    chart = _chart(bpm=0)
    assert "BadBPM" in _codes(validate(chart))
    chart = _chart(bpm=-5)
    assert "BadBPM" in _codes(validate(chart))


@test
def bad_rpe_version_is_a_caution():
    chart = _chart()
    chart.META.RPEVersion = 0
    report = validate(chart)
    assert "BadRPEVersion" in _codes(report)
    assert report.ok  # caution 不影响可导入性


@test
def too_many_lines_is_an_error():
    chart = _chart([_line() for _ in range(3)])
    assert "TooManyLines" not in _codes(validate(chart))
    chart.judgeLineList = [_line() for _ in range(MAX_LINES)]
    assert "TooManyLines" not in _codes(validate(chart))
    chart.judgeLineList = [_line() for _ in range(MAX_LINES + 1)]
    assert "TooManyLines" in _codes(validate(chart))


@test
def too_many_event_layers_is_an_error():
    line = _line()
    line.eventLayers = [EventLayer() for _ in range(5)]
    assert "TooManyEventLayers" not in _codes(validate(_chart([line])))
    line.eventLayers = [EventLayer() for _ in range(6)]
    assert "TooManyEventLayers" in _codes(validate(_chart([line])))


@test
def z_order_out_of_range_is_a_caution():
    line = _line()
    line.zOrder = 100
    assert "ZOrderOutOfRange" not in _codes(validate(_chart([line])))
    line.zOrder = 101
    assert "ZOrderOutOfRange" in _codes(validate(_chart([line])))
    line.zOrder = -101
    assert "ZOrderOutOfRange" in _codes(validate(_chart([line])))


@test
def bad_attach_ui_is_an_error():
    line = _line()
    for good in ("pause", "combonumber", "combo", "score", "bar", "name", "level"):
        line.attachUI = good
        assert "BadAttachUI" not in _codes(validate(_chart([line]))), good
    line.attachUI = "scoer"
    assert "BadAttachUI" in _codes(validate(_chart([line])))


@test
def non_positive_bpmfactor_is_an_error():
    line = _line()
    line.bpmfactor = 1.0
    assert "BadBpmFactor" not in _codes(validate(_chart([line])))
    line.bpmfactor = 0.0
    assert "BadBpmFactor" in _codes(validate(_chart([line])))


@test
def bad_father_index_is_an_error():
    chart = _chart([_line()])
    chart.judgeLineList[0].father = 5
    assert "BadFather" in _codes(validate(chart))


@test
def father_loop_is_an_error():
    a, b, c = _line(), _line(), _line()
    a.father, b.father, c.father = 1, 2, 0
    report = validate(_chart([a, b, c]))
    assert "FatherLoop" in _codes(report)


@test
def self_father_is_a_loop():
    a = _line()
    a.father = 0
    assert "FatherLoop" in _codes(validate(_chart([a])))


@test
def line_group_index_out_of_range_is_reported():
    """线的 ``Group`` 是 ``judgeLineGroup`` 的下标；越界就是引用了不存在的组。

    实测：26 份真实谱面的 601 条判定线里，越界的**一条都没有**；而本库早期
    默认写 ``judgeLineGroup: []``，于是每条线默认的 ``Group=0`` 全部悬空，
    判定线管理里按分组筛选会一条线都列不出来。
    """
    chart = _chart([_line()])
    assert "BadLineGroup" not in _codes(validate(chart))

    chart.judgeLineGroup = ["Default"]
    assert "BadLineGroup" not in _codes(validate(chart))

    chart.judgeLineList[0].Group = 3
    report = validate(chart)
    assert "BadLineGroup" in _codes(report)
    assert not report.ok, "悬空的分组下标不该只是提示"


@test
def extended_events_do_not_require_layer_five():
    """实测 577 条真实判定线里 535 条有 extended 但没有第 5 层 —— 那是正常写法。

    RPE 1.5+ 把特殊事件挂在 judgeLine 的 ``extended`` 键上，不再要求第 5 层事件层。
    """
    line = _line()
    line.eventLayers = [EventLayer()]
    line.extended = ExtendedEvents(
        inclineEvents=[Event(Beat(0), Beat(1), start=0.0, end=0.0)]
    )
    codes = _codes(validate(_chart([line])))
    assert not any("Layer5" in c for c in codes), codes


# --------------------------------------------------------------- 音符：范围与自定义
@test
def x_too_large_is_a_caution():
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0), positionX=675.0)])
    assert "XTooLarge" not in _codes(validate(_chart([line])))
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0), positionX=676.0)])
    assert "XTooLarge" in _codes(validate(_chart([line])))


@test
def negative_note_time_is_an_error():
    line = _line([Note(type=NOTE_TAP, startTime=Beat(-1))])
    report = validate(_chart([line]))
    assert "TimeOutOfRange" in _codes(report)
    assert not report.ok


@test
def note_beyond_song_duration_is_an_error():
    line = _line([Note(type=NOTE_TAP, startTime=Beat(200))])
    chart = _chart([line])
    assert "TimeOutOfRange" not in _codes(validate(chart))  # 没给时长就不查
    assert "TimeOutOfRange" in _codes(validate(chart, song_duration=10.0))


@test
def hold_ending_at_or_before_start_is_an_error():
    line = _line([Note(type=NOTE_HOLD, startTime=Beat(4), endTime=Beat(4))])
    assert "IllegalHold" in _codes(validate(_chart([line])))
    line = _line([Note(type=NOTE_HOLD, startTime=Beat(4), endTime=Beat(3))])
    assert "IllegalHold" in _codes(validate(_chart([line])))


@test
def short_hold_is_a_caution():
    # 10ms 的 Hold
    line = _line([Note(type=NOTE_HOLD, startTime=Beat(4), endTime=Beat(4) + _ms(10))])
    assert "ShortReadtime" in _codes(validate(_chart([line])))
    # 100ms 的 Hold 不再提示
    line = _line([Note(type=NOTE_HOLD, startTime=Beat(4), endTime=Beat(4) + _ms(100))])
    assert "ShortReadtime" not in _codes(validate(_chart([line])))


@test
def fake_note_is_a_caution():
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0), isFake=1)])
    assert "UsingFakeNote" in _codes(validate(_chart([line])))
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0), isFake=0)])
    assert "UsingFakeNote" not in _codes(validate(_chart([line])))


@test
def customized_note_size_is_a_caution():
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0), size=0.5)])
    assert "CustomizedNoteSize" in _codes(validate(_chart([line])))
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0), size=1.0)])
    assert "CustomizedNoteSize" not in _codes(validate(_chart([line])))


@test
def customized_visible_time_is_a_caution():
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0), visibleTime=2.0)])
    assert "CustomizedVisibleTime" in _codes(validate(_chart([line])))
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0))])
    assert "CustomizedVisibleTime" not in _codes(validate(_chart([line])))


@test
def note_alpha_out_of_range_is_a_warning():
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0), alpha=300)])
    assert "NoteAlphaOutOfRange" in _codes(validate(_chart([line])))


@test
def judged_out_of_screen_needs_a_short_visible_time():
    """可见时间短于「从屏外落到判定线」所需时间 → 玩家看不到音符。

    默认线速 1.0（判定线没有 speedEvents 时）+ 默认 120 单位/秒，
    走完 900 单位需要 7.5 秒。
    """
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0), visibleTime=1.0, speed=1.0)])
    assert "NoteJudgedOutScreen" in _codes(validate(_chart([line])))
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0), visibleTime=10.0, speed=1.0)])
    assert "NoteJudgedOutScreen" not in _codes(validate(_chart([line])))
    # 流速快 10 倍，只需 0.75 秒
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0), visibleTime=1.0, speed=10.0)])
    assert "NoteJudgedOutScreen" not in _codes(validate(_chart([line])))


@test
def judged_out_of_screen_accounts_for_the_line_speed_events():
    """真实落下速度 = 120 Y/s × 线流速事件 × note.speed（P0 回归）。

    旧实现只看 note.speed，完全忽略线流速；而真实谱面的线流速
    几乎从不等于 1（实测 24 份 pezc 语料 8116 个流速事件里 8115 个非 1）。
    """
    # 线流速 0.5 → 60 Y/s → 走完 900 单位需 15s，可见 10s 不够
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0), visibleTime=10.0, speed=1.0)])
    line.eventLayers = [EventLayer(speedEvents=[SpeedEvent(Beat(0), Beat(64), 0.5, 0.5)])]
    assert "NoteJudgedOutScreen" in _codes(validate(_chart([line])))

    # 线流速 12 → 1440 Y/s → 只需 0.625s，可见 1s 足够
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0), visibleTime=1.0, speed=1.0)])
    line.eventLayers = [EventLayer(speedEvents=[SpeedEvent(Beat(0), Beat(64), 12.0, 12.0)])]
    assert "NoteJudgedOutScreen" not in _codes(validate(_chart([line])))


@test
def judged_out_of_screen_reports_notes_that_never_fall():
    """note.speed == 0 或线流速为 0 时音符永远到不了判定线。

    真实语料里存在这种写法（pezc-80113.json line7 note8：
    visible=90s、note.speed=0、线流速=0）。
    """
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0), visibleTime=90.0, speed=0.0)])
    line.eventLayers = [EventLayer(speedEvents=[SpeedEvent(Beat(0), Beat(64), 0.0, 0.0)])]
    report = validate(_chart([line]))
    assert "NoteJudgedOutScreen" in _codes(report)
    issue = next(i for i in report.issues if i.code == "NoteJudgedOutScreen")
    assert "永远" in issue.message


@test
def default_visible_time_is_never_out_of_screen():
    line = _line([Note(type=NOTE_TAP, startTime=Beat(0))])
    assert "NoteJudgedOutScreen" not in _codes(validate(_chart([line])))


# --------------------------------------------------------------- 音符：相撞
@test
def two_taps_at_the_same_place_collide():
    line = _line(
        [
            Note(type=NOTE_TAP, startTime=Beat(4), positionX=100.0),
            Note(type=NOTE_TAP, startTime=Beat(4), positionX=100.0),
        ]
    )
    assert "TapTapOverlapped" in _codes(validate(_chart([line])))


@test
def two_taps_at_different_x_do_not_collide():
    line = _line(
        [
            Note(type=NOTE_TAP, startTime=Beat(4), positionX=100.0),
            Note(type=NOTE_TAP, startTime=Beat(4), positionX=200.0),
        ]
    )
    assert "TapTapOverlapped" not in _codes(validate(_chart([line])))


@test
def two_taps_halving_a_beat_apart_do_not_collide():
    line = _line(
        [
            Note(type=NOTE_TAP, startTime=Beat(4), positionX=0.0),
            Note(type=NOTE_TAP, startTime=Beat(4) + _ms(250), positionX=0.0),
        ]
    )
    assert "TapTapOverlapped" not in _codes(validate(_chart([line])))


@test
def tap_inside_hold_collides():
    line = _line(
        [
            Note(type=NOTE_HOLD, startTime=Beat(4), endTime=Beat(8), positionX=0.0),
            Note(type=NOTE_TAP, startTime=Beat(6), positionX=0.0),
        ]
    )
    assert "TapHoldOverlapped" in _codes(validate(_chart([line])))


@test
def tap_at_hold_start_collides_but_at_end_does_not():
    line = _line(
        [
            Note(type=NOTE_HOLD, startTime=Beat(4), endTime=Beat(8), positionX=0.0),
            Note(type=NOTE_TAP, startTime=Beat(4), positionX=0.0),
        ]
    )
    assert "TapHoldOverlapped" in _codes(validate(_chart([line])))

    line = _line(
        [
            Note(type=NOTE_HOLD, startTime=Beat(4), endTime=Beat(8), positionX=0.0),
            Note(type=NOTE_TAP, startTime=Beat(8), positionX=0.0),
        ]
    )
    assert "TapHoldOverlapped" not in _codes(validate(_chart([line])))


@test
def two_holds_really_overlapping_collide():
    line = _line(
        [
            Note(type=NOTE_HOLD, startTime=Beat(4), endTime=Beat(8), positionX=0.0),
            Note(type=NOTE_HOLD, startTime=Beat(6), endTime=Beat(10), positionX=0.0),
        ]
    )
    assert "HoldHoldOverlapped" in _codes(validate(_chart([line])))


@test
def two_holds_that_only_touch_do_not_collide():
    """首尾相接（前者终点 == 后者起点）不是重叠，只算 Hold Sticked?。"""
    line = _line(
        [
            Note(type=NOTE_HOLD, startTime=Beat(4), endTime=Beat(8), positionX=0.0),
            Note(type=NOTE_HOLD, startTime=Beat(8), endTime=Beat(12), positionX=0.0),
        ]
    )
    codes = _codes(validate(_chart([line])))
    assert "HoldHoldOverlapped" not in codes, codes


@test
def drag_under_a_hold_is_legal():
    """实测 24 份真实谱面里 Drag 垫在 Hold 下面同刻同 X 共 1283 处，是合法写法。

    RPE 说明书第 037 页的相撞清单只有 Tap&Hold / Tap&Tap / Hold&Hold，不含 Drag。
    """
    line = _line(
        [
            Note(type=NOTE_HOLD, startTime=Beat(4), endTime=Beat(8), positionX=0.0),
            Note(type=NOTE_DRAG, startTime=Beat(6), positionX=0.0),
        ]
    )
    codes = _codes(validate(_chart([line])))
    assert not any("Overlapped" in c for c in codes), codes


@test
def drag_and_flick_at_the_same_spot_are_legal():
    line = _line(
        [
            Note(type=NOTE_DRAG, startTime=Beat(4), positionX=0.0),
            Note(type=NOTE_DRAG, startTime=Beat(4), positionX=0.0),
            Note(type=NOTE_FLICK, startTime=Beat(4), positionX=0.0),
        ]
    )
    codes = _codes(validate(_chart([line])))
    assert not any("Overlapped" in c for c in codes), codes


@test
def overlap_check_can_be_disabled():
    line = _line(
        [
            Note(type=NOTE_TAP, startTime=Beat(4), positionX=0.0),
            Note(type=NOTE_TAP, startTime=Beat(4), positionX=0.0),
        ]
    )
    codes = _codes(validate(_chart([line]), check_overlap_types=False))
    assert "TapTapOverlapped" not in codes


@test
def fake_notes_never_collide():
    line = _line(
        [
            Note(type=NOTE_TAP, startTime=Beat(4), positionX=0.0, isFake=1),
            Note(type=NOTE_TAP, startTime=Beat(4), positionX=0.0),
        ]
    )
    assert "TapTapOverlapped" not in _codes(validate(_chart([line])))


# --------------------------------------------------------------- 音符：先后手顺序
@test
def tap_after_drag_in_the_window_is_a_warning():
    # 120 BPM 下 100ms = 0.2 拍
    line = _line(
        [
            Note(type=NOTE_DRAG, startTime=Beat(4), positionX=0.0),
            Note(type=NOTE_TAP, startTime=Beat(4) + _ms(100), positionX=0.0),
        ]
    )
    report = validate(_chart([line]))
    assert "TapAfterDrag" in _codes(report)
    assert not any(i.severity == ERROR for i in report.issues), report.format()


@test
def tap_after_flick_in_the_window_is_an_error():
    line = _line(
        [
            Note(type=NOTE_FLICK, startTime=Beat(4), positionX=0.0),
            Note(type=NOTE_TAP, startTime=Beat(4) + _ms(100), positionX=0.0),
        ]
    )
    report = validate(_chart([line]))
    assert "TapAfterFlick" in _codes(report)
    assert not report.ok


@test
def tap_outside_the_40_to_160ms_window_is_fine():
    for gap in (10.0, 30.0, 200.0, 400.0):
        line = _line(
            [
                Note(type=NOTE_FLICK, startTime=Beat(4), positionX=0.0),
                Note(type=NOTE_TAP, startTime=Beat(4) + _ms(gap), positionX=0.0),
            ]
        )
        codes = _codes(validate(_chart([line])))
        assert "TapAfterFlick" not in codes, f"gap={gap}ms 不该报"
        assert "TapAfterDrag" not in codes, f"gap={gap}ms 不该报"


@test
def window_boundaries_are_inclusive():
    for gap in (40.0, 160.0):
        line = _line(
            [
                Note(type=NOTE_FLICK, startTime=Beat(4), positionX=0.0),
                Note(type=NOTE_TAP, startTime=Beat(4) + _ms(gap), positionX=0.0),
            ]
        )
        assert "TapAfterFlick" in _codes(validate(_chart([line]))), f"gap={gap}ms 应在窗口内"


@test
def tap_after_drag_at_a_different_x_is_fine():
    line = _line(
        [
            Note(type=NOTE_DRAG, startTime=Beat(4), positionX=0.0),
            Note(type=NOTE_TAP, startTime=Beat(4) + _ms(100), positionX=200.0),
        ]
    )
    assert "TapAfterDrag" not in _codes(validate(_chart([line])))


@test
def tap_before_drag_is_fine():
    """顺序反了（Tap 在前）不属于 Tap After Drag。"""
    line = _line(
        [
            Note(type=NOTE_TAP, startTime=Beat(4), positionX=0.0),
            Note(type=NOTE_DRAG, startTime=Beat(4) + _ms(100), positionX=0.0),
        ]
    )
    assert "TapAfterDrag" not in _codes(validate(_chart([line])))


@test
def drag_after_drag_is_fine():
    line = _line(
        [
            Note(type=NOTE_DRAG, startTime=Beat(4), positionX=0.0),
            Note(type=NOTE_DRAG, startTime=Beat(4) + _ms(100), positionX=0.0),
        ]
    )
    assert "TapAfterDrag" not in _codes(validate(_chart([line])))


@test
def holds_with_opposite_x_are_not_sticked():
    """``Hold Sticked?`` 只看 X 同号的情况。"""
    line = _line(
        [
            Note(type=NOTE_HOLD, startTime=Beat(4), endTime=Beat(8), positionX=-100.0),
            Note(type=NOTE_HOLD, startTime=Beat(8), endTime=Beat(12), positionX=100.0),
        ]
    )
    assert "HoldSticked" not in _codes(validate(_chart([line])))


@test
def holds_with_same_sign_x_and_touching_are_sticked():
    line = _line(
        [
            Note(type=NOTE_HOLD, startTime=Beat(4), endTime=Beat(8), positionX=-100.0),
            Note(type=NOTE_HOLD, startTime=Beat(8) + _ms(10), endTime=Beat(12), positionX=-200.0),
        ]
    )
    assert "HoldSticked" in _codes(validate(_chart([line])))


@test
def holds_with_a_real_gap_are_not_sticked():
    line = _line(
        [
            Note(type=NOTE_HOLD, startTime=Beat(4), endTime=Beat(8), positionX=-100.0),
            Note(type=NOTE_HOLD, startTime=Beat(8) + _ms(500), endTime=Beat(12), positionX=-100.0),
        ]
    )
    assert "HoldSticked" not in _codes(validate(_chart([line])))


@test
def note_index_points_at_the_original_array_position():
    """报错的下标要能直接索引回 line.notes，不能是排序后的位置。"""
    notes = [
        Note(type=NOTE_TAP, startTime=Beat(20), positionX=0.0),
        Note(type=NOTE_FLICK, startTime=Beat(4), positionX=0.0),
        Note(type=NOTE_TAP, startTime=Beat(4) + _ms(100), positionX=0.0),
    ]
    line = _line(notes)
    report = validate(_chart([line]))
    issue = next(i for i in report.issues if i.code == "TapAfterFlick")
    assert issue.note_index == 2, issue.note_index
    assert line.notes[issue.note_index].type == NOTE_TAP


# --------------------------------------------------------------- 事件
@test
def illegal_event_is_an_error():
    line = _line()
    line.eventLayers = [EventLayer(moveXEvents=[Event(Beat(4), Beat(4), 0.0, 0.0)])]
    assert "IllegalEvent" in _codes(validate(_chart([line])))


@test
def overlapped_same_kind_events_in_one_layer_are_errors():
    line = _line()
    line.eventLayers = [
        EventLayer(
            moveXEvents=[
                Event(Beat(0), Beat(4), 0.0, 100.0),
                Event(Beat(2), Beat(6), 0.0, 100.0),
            ]
        )
    ]
    assert "EventOverLapped" in _codes(validate(_chart([line])))


@test
def events_in_different_layers_may_overlap():
    line = _line()
    line.eventLayers = [
        EventLayer(moveXEvents=[Event(Beat(0), Beat(4), 0.0, 100.0)]),
        EventLayer(moveXEvents=[Event(Beat(2), Beat(6), 0.0, 100.0)]),
    ]
    assert "EventOverLapped" not in _codes(validate(_chart([line])))


@test
def different_kinds_in_one_layer_may_overlap():
    line = _line()
    line.eventLayers = [
        EventLayer(
            moveXEvents=[Event(Beat(0), Beat(4), 0.0, 100.0)],
            moveYEvents=[Event(Beat(2), Beat(6), 0.0, 100.0)],
        )
    ]
    assert "EventOverLapped" not in _codes(validate(_chart([line])))


@test
def alpha_event_out_of_range_is_a_warning():
    line = _line()
    line.eventLayers = [EventLayer(alphaEvents=[Event(Beat(0), Beat(4), 0.0, 300.0)])]
    assert "AlphaEventOverRange" in _codes(validate(_chart([line])))
    line = _line()
    line.eventLayers = [EventLayer(alphaEvents=[Event(Beat(0), Beat(4), -1.0, 255.0)])]
    assert "AlphaEventOverRange" in _codes(validate(_chart([line])))
    line = _line()
    line.eventLayers = [EventLayer(alphaEvents=[Event(Beat(0), Beat(4), 0.0, 255.0)])]
    assert "AlphaEventOverRange" not in _codes(validate(_chart([line])))


@test
def event_beyond_song_duration_is_an_error():
    line = _line()
    line.eventLayers = [EventLayer(moveXEvents=[Event(Beat(0), Beat(40), 0.0, 100.0)])]
    chart = _chart([line])
    assert "EventTimeOutOfRange" not in _codes(validate(chart))
    assert "EventTimeOutOfRange" in _codes(validate(chart, song_duration=10.0))


@test
def constant_events_do_not_trigger_too_many_linear():
    """实测 641/1803 个 (线,事件类) 的非常量事件数为 0。

    垫底的 ``start == end`` 常量事件如果算进分母，这条提示会变成纯噪音。
    """
    line = _line()
    # 一条真实位移 + 九条常量：线性占比 1/1 = 100% 应报警
    events = [Event(Beat(0), Beat(1), 0.0, 100.0)]
    events += [Event(Beat(i), Beat(i + 1), 50.0, 50.0) for i in range(1, 10)]
    line.eventLayers = [EventLayer(moveXEvents=events)]
    assert "TooManyLinearEvent" in _codes(validate(_chart([line])))

    # 十条全是常量 ⇒ 分母为 0，不该报警
    line = _line()
    line.eventLayers = [
        EventLayer(moveXEvents=[Event(Beat(i), Beat(i + 1), 50.0, 50.0) for i in range(10)])
    ]
    codes = _codes(validate(_chart([line])))
    assert "TooManyLinearEvent" not in codes, codes


@test
def mostly_linear_events_are_cautioned():
    line = _line()
    # 8 条线性 + 2 条缓动位移 ⇒ 80% 线性
    events = [Event(Beat(i), Beat(i + 1), float(i), float(i + 1), easingType=1) for i in range(8)]
    events += [
        Event(Beat(8), Beat(9), 0.0, 100.0, easingType=2),
        Event(Beat(9), Beat(10), 100.0, 0.0, easingType=2),
    ]
    line.eventLayers = [EventLayer(moveYEvents=events)]
    assert "TooManyLinearEvent" in _codes(validate(_chart([line])))


@test
def mostly_eased_events_are_not_cautioned():
    line = _line()
    events = [Event(Beat(i), Beat(i + 1), float(i), float(i + 1), easingType=2) for i in range(8)]
    events += [
        Event(Beat(8), Beat(9), 0.0, 100.0, easingType=1),
        Event(Beat(9), Beat(10), 100.0, 0.0, easingType=1),
    ]
    line.eventLayers = [EventLayer(moveYEvents=events)]
    assert "TooManyLinearEvent" not in _codes(validate(_chart([line])))


@test
def speed_events_do_not_count_towards_linear_ratio():
    """RPE 的提示只针对 MoveX / MoveY / Rotate 三类。"""
    line = _line()
    events = [SpeedEvent(Beat(i), Beat(i + 1), 1.0, 2.0) for i in range(20)]
    line.eventLayers = [EventLayer(speedEvents=events)]
    assert "TooManyLinearEvent" not in _codes(validate(_chart([line])))


@test
def special_events_are_validated_too():
    line = _line()
    line.extended = ExtendedEvents(
        scaleXEvents=[Event(Beat(4), Beat(4), 1.0, 2.0)]  # 非法：结束 == 开始
    )
    assert "IllegalEvent" in _codes(validate(_chart([line])))


# --------------------------------------------------------------- Issue / Report
@test
def issue_location_is_readable():
    line = _line()
    line.eventLayers = [EventLayer(alphaEvents=[Event(Beat(4), Beat(4), 0.0, 0.0)])]
    report = validate(_chart([line]))
    issue = next(i for i in report.issues if i.code == "IllegalEvent")
    text = str(issue)
    assert "错误" in text
    assert "线0" in issue.location
    assert f"{EVENT_ALPHA}[0]" in issue.location


@test
def report_filters_by_severity():
    line = _line(
        [
            Note(type=NOTE_TAP, startTime=Beat(4), positionX=800.0, size=0.5, isFake=1),
        ]
    )
    report = validate(_chart([line]))
    assert report.cautions
    assert not report.errors
    assert report.ok
    assert set(i.severity for i in report.cautions) == {CAUTION}
    assert report.of(WARNING) == report.warnings


@test
def report_is_sorted_by_severity():
    line = _line(
        [
            Note(type=NOTE_TAP, startTime=Beat(-1), positionX=800.0),  # error + caution
        ]
    )
    report = validate(_chart([line]))
    severities = [i.severity for i in report.issues]
    assert severities == sorted(severities, key=lambda s: {ERROR: 0, WARNING: 1, CAUTION: 2}[s])


@test
def report_is_iterable_and_countable():
    report = validate(_chart())
    assert isinstance(report, ValidationReport)
    assert list(report) == report.issues
    assert len(report) == len(report.issues)
    assert not report
    assert report.summary().startswith("错误 0")


@test
def report_format_can_be_truncated():
    line = _line(
        [
            Note(type=NOTE_TAP, startTime=Beat(4 + i), positionX=800.0 + i)
            for i in range(20)
        ]
    )
    report = validate(_chart([line]))
    text = report.format(max_lines=5)
    assert "另有" in text
    assert len(text.splitlines()) == 7  # 汇总行 + 5 条 + 省略行


@test
def song_duration_boundary_is_tolerant():
    """正好落在音乐结尾的音符不该被误报。"""
    line = _line([Note(type=NOTE_TAP, startTime=Beat(20))])  # 120BPM ⇒ 10s
    chart = _chart([line])
    assert "TimeOutOfRange" not in _codes(validate(chart, song_duration=10.0))
    assert "TimeOutOfRange" in _codes(validate(chart, song_duration=9.0))
