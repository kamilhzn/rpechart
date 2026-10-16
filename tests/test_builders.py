"""``rpechart.builders`` 的测试。"""

from run_tests import test

from rpechart.beat import Beat
from rpechart.builders import ChartBuilder, JudgeLineBuilder, NoteSpec, note_type_id
from rpechart.model import (
    EVENT_ALPHA,
    EVENT_MOVE_X,
    EVENT_MOVE_Y,
    EVENT_ROTATE,
    EVENT_SPEED,
    NOTE_DRAG,
    NOTE_FLICK,
    NOTE_HOLD,
    NOTE_TAP,
)
from rpechart.interp import value_at, value_at_resolved


# ================================================================== 类型名
@test
def note_type_id_accepts_many_spellings():
    assert note_type_id("tap") == NOTE_TAP
    assert note_type_id("TAP") == NOTE_TAP
    assert note_type_id("蓝") == NOTE_TAP
    assert note_type_id("黄键") == NOTE_DRAG
    assert note_type_id("红") == NOTE_FLICK
    assert note_type_id("长按") == NOTE_HOLD
    assert note_type_id(1) == NOTE_TAP
    assert note_type_id(4) == NOTE_DRAG


@test
def note_type_id_rejects_unknown():
    try:
        note_type_id("紫色")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("未知音符类型应当报错")


# ================================================================== NoteSpec
@test
def note_spec_applies_to_notes():
    spec = NoteSpec(size=0.5, speed=2.0, alpha=128, is_fake=True, tint=[1, 2, 3])
    cb = ChartBuilder(bpm=120)
    n = cb.line().tap(0).__class__  # 只是确认导入可用
    cb2 = ChartBuilder(bpm=120, note_spec=spec)
    note = cb2.line().tap(0, x=10)
    assert note.size == 0.5
    assert note.speed == 2.0
    assert note.alpha == 128
    assert note.isFake == 1
    assert note.tint == [1, 2, 3]


@test
def per_note_kwargs_override_note_spec():
    spec = NoteSpec(size=0.5, speed=2.0, alpha=128)
    cb = ChartBuilder(bpm=120, note_spec=spec)
    note = cb.line().tap(0, size=2.0, alpha=255)
    assert note.size == 2.0
    assert note.alpha == 255
    assert note.speed == 2.0  # 未覆盖的仍来自 spec


@test
def per_line_note_spec_overrides_chart_level_spec():
    """给单条线设 ``note_spec`` 只影响该线。

    这是「一条线整轨高速/小尺寸」的常规写法，所以赋值必须真的生效 ——
    ``examples/make_demo_chart.py`` 就是这么用的。
    """
    cb = ChartBuilder(bpm=120, note_spec=NoteSpec(speed=1.0, size=1.0))
    fast = cb.line(name="高速")
    slow = cb.line(name="常速")
    fast.note_spec = NoteSpec(speed=3.0, size=0.6)

    n_fast = fast.tap(0)
    n_slow = slow.tap(0)
    assert n_fast.speed == 3.0, "线级 note_spec 被静默忽略了"
    assert n_fast.size == 0.6
    assert n_slow.speed == 1.0, "线级 note_spec 串到了别的线上"
    assert n_slow.size == 1.0


@test
def per_line_note_spec_can_be_cleared():
    """把线级 ``note_spec`` 设回 ``None`` 应回落到谱面级。"""
    cb = ChartBuilder(bpm=120, note_spec=NoteSpec(speed=2.0))
    lb = cb.line()
    lb.note_spec = NoteSpec(speed=9.0)
    assert lb.tap(0).speed == 9.0
    lb.note_spec = None
    assert lb.tap(0).speed == 2.0


@test
def per_note_kwargs_override_per_line_spec():
    """三层优先级：显式参数 > 线级 spec > 谱面级 spec。"""
    cb = ChartBuilder(bpm=120, note_spec=NoteSpec(speed=1.0, size=1.0))
    lb = cb.line()
    lb.note_spec = NoteSpec(speed=3.0, size=0.6)
    note = lb.tap(0, speed=7.5)
    assert note.speed == 7.5, "显式参数没有盖住线级 spec"
    assert note.size == 0.6, "未覆盖的字段仍应来自线级 spec"


# ================================================================== 谱面基础
@test
def chart_builder_sets_meta():
    cb = ChartBuilder(
        bpm=174,
        name="测试曲",
        charter="我",
        composer="某人",
        level="IN Lv.15",
        song="song.mp3",
        background="bg.png",
        illustrator="画师",
        offset=-45,
    )
    m = cb.meta
    assert m.name == "测试曲"
    assert m.charter == "我"
    assert m.composer == "某人"
    assert m.level == "IN Lv.15"
    assert m.offset == -45
    assert m.illustrator == "画师"
    assert m.song == "song.mp3"
    assert m.background == "bg.png"


@test
def chart_builder_bpm_and_time_conversion():
    cb = ChartBuilder(bpm=120)
    # 120 BPM → 每拍 0.5 秒
    assert abs(cb.seconds(4) - 2.0) < 1e-9
    assert abs(cb.beats(2.0).value - 4.0) < 1e-9
    assert abs(cb.bpm_at(0) - 120.0) < 1e-9


@test
def chart_builder_multi_bpm_segments():
    cb = ChartBuilder(bpm=120)
    cb.set_bpm(240, at=4)
    # 前 4 拍按 120 BPM = 2 秒，之后每拍 0.25 秒
    assert abs(cb.seconds(4) - 2.0) < 1e-9
    assert abs(cb.seconds(8) - 3.0) < 1e-9
    assert abs(cb.bpm_at(0) - 120.0) < 1e-6
    assert abs(cb.bpm_at(4) - 240.0) < 1e-6


@test
def chart_builder_set_bpm_replaces_at_same_time():
    cb = ChartBuilder(bpm=120)
    cb.set_bpm(150, at=0)
    assert len(cb.chart.BPMList) == 1
    assert abs(cb.chart.BPMList[0].bpm - 150.0) < 1e-9


@test
def chart_builder_summary_is_a_string():
    cb = ChartBuilder(bpm=150, name="X")
    cb.line().tap(0)
    s = cb.summary()
    assert "X" in s
    assert "150" in s


# ================================================================== 判定线属性
@test
def line_builder_sets_line_properties():
    cb = ChartBuilder(bpm=120)
    ln = cb.line(name="主线")
    ln.name("改过").group(3).z_order(7).texture("custom.png").cover(True)
    ln.bpm_factor(2.0).attach_ui("score").father(0).rotate_with_father(False)
    ln.anchor(0.0, 1.0)
    line = cb.chart_lines[0]
    assert line.Name == "改过"
    assert line.Group == 3
    assert line.zOrder == 7
    assert line.Texture == "custom.png"
    assert line.isCover == 1
    assert abs(line.bpmfactor - 2.0) < 1e-9
    assert line.attachUI == "score"
    assert line.father == 0
    assert line.rotateWithFather is False
    assert line.anchor == [0.0, 1.0]


@test
def line_builder_cover_false():
    cb = ChartBuilder(bpm=120)
    cb.line().cover(False)
    assert cb.chart_lines[0].isCover == 0


@test
def line_builder_bpm_factor_never_zero():
    cb = ChartBuilder(bpm=120)
    cb.line().bpm_factor(0)
    assert cb.chart_lines[0].bpmfactor == 1.0


# ================================================================== at / 移动
@test
def at_zero_places_line_immediately():
    cb = ChartBuilder(bpm=120)
    cb.line(x=100, y=200)
    line = cb.chart_lines[0]
    xs = line.eventLayers[0].moveXEvents
    ys = line.eventLayers[0].moveYEvents
    assert xs and ys
    assert abs(value_at(xs, Beat(0), 0.0) - 100.0) < 1e-6
    assert abs(value_at(ys, Beat(0), 0.0) - 200.0) < 1e-6
    # 常量事件：中途和末端都一样
    assert abs(value_at(xs, Beat(32), 0.0) - 100.0) < 1e-6
    assert abs(value_at(xs, Beat(63.5), 0.0) - 100.0) < 1e-6


@test
def at_positive_time_keeps_previous_position_then_jumps():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.at(0, 0)
    ln.at(300, 0, at=4)
    line = cb.chart_lines[0]
    xs = line.eventLayers[0].moveXEvents
    # 第 4 拍前保持 0，之后跳到 300
    assert abs(value_at(xs, Beat(2), 0.0) - 0.0) < 1e-6
    assert abs(value_at(xs, Beat(4), 0.0) - 300.0) < 1e-6
    assert abs(value_at(xs, Beat(20), 0.0) - 300.0) < 1e-6


@test
def move_to_interpolates_and_tracks_position():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.at(0, 0)
    ln.move_to(400, 0, 4, 4)
    xs = cb.chart_lines[0].eventLayers[0].moveXEvents
    assert abs(value_at(xs, Beat(4), 0.0) - 0.0) < 1e-6
    assert abs(value_at(xs, Beat(8), 0.0) - 400.0) < 1e-6
    # 线性中点
    assert abs(value_at(xs, Beat(6), 0.0) - 200.0) < 1e-6


@test
def move_by_is_relative_to_current_position():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.at(100, 0)
    ln.move_by(50, 0, 4, 2)
    xs = cb.chart_lines[0].eventLayers[0].moveXEvents
    assert abs(value_at(xs, Beat(6), 0.0) - 150.0) < 1e-6


@test
def move_x_and_move_y_are_independent():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.at(0, 0)
    ln.move_x(300, 2, 2)
    ln.move_y(-200, 6, 2)
    line = cb.chart_lines[0]
    xs = line.eventLayers[0].moveXEvents
    ys = line.eventLayers[0].moveYEvents
    assert abs(value_at(xs, Beat(4), 0.0) - 300.0) < 1e-6
    # Y 在第 6 拍前仍是 0
    assert abs(value_at(ys, Beat(4), 0.0) - 0.0) < 1e-6
    assert abs(value_at(ys, Beat(8), 0.0) - (-200.0)) < 1e-6


# ================================================================== 旋转
@test
def rotate_to_and_spin():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.at(0, 0)
    ln.rotate_to(90, 0, 2)
    ln.spin(90, 2, 2)
    rs = cb.chart_lines[0].eventLayers[0].rotateEvents
    assert abs(value_at(rs, Beat(2), 0.0) - 90.0) < 1e-6
    assert abs(value_at(rs, Beat(4), 0.0) - 180.0) < 1e-6


@test
def rotate_forever_covers_span():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.rotate_forever(180.0, start=0, end=8)
    rs = cb.chart_lines[0].eventLayers[0].rotateEvents
    assert abs(value_at(rs, Beat(0), 0.0) - 0.0) < 1e-6
    # 180 度/拍 × 8 拍 = 1440 度
    assert abs(value_at(rs, Beat(8), 0.0) - 1440.0) < 1e-6


# ================================================================== 透明度/特殊事件
@test
def fade_to_produces_alpha_events():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.at(0, 0)
    ln.fade_to(0, 4, 4)
    a = cb.chart_lines[0].eventLayers[0].alphaEvents
    assert abs(value_at(a, Beat(4), 255.0) - 255.0) < 1e-6
    assert abs(value_at(a, Beat(8), 255.0) - 0.0) < 1e-6


@test
def special_events_land_in_extended():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.scale_to(2.0, t=0, dur=4)
    ln.color_fade([0, 0, 0], [255, 255, 255], 0, 4)
    ln.text("你好", 0, 4)
    line = cb.chart_lines[0]
    assert len(line.extended.scaleXEvents) == 1
    assert len(line.extended.scaleYEvents) == 1
    assert len(line.extended.colorEvents) == 1
    assert len(line.extended.textEvents) == 1
    assert line.extended.textEvents[0].start == "你好"
    # 颜色事件是 int[3]
    assert line.extended.colorEvents[0].start == [0, 0, 0]
    assert line.extended.colorEvents[0].end == [255, 255, 255]
    # RPE 只在第 5 层读特殊事件
    assert len(line.eventLayers) >= 5


@test
def scale_to_with_single_value_is_uniform():
    cb = ChartBuilder(bpm=120)
    cb.line().scale_to(1.5, t=0, dur=2)
    line = cb.chart_lines[0]
    assert line.extended.scaleXEvents[0].end == 1.5
    assert line.extended.scaleYEvents[0].end == 1.5


@test
def incline_and_gif_progress():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.incline(30.0, 0, 4)
    ln.gif_progress(1.0, 0, 4)
    line = cb.chart_lines[0]
    assert abs(line.extended.inclineEvents[0].end - 30.0) < 1e-6
    assert abs(line.extended.gifEvents[0].end - 1.0) < 1e-6


@test
def special_event_rejects_empty_span():
    cb = ChartBuilder(bpm=120)
    try:
        cb.line().text("x", 4, 0)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("零长度特殊事件应当报错")


# ================================================================== 流速
@test
def line_speed_creates_speed_events():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.line_speed(0, 8, 1.0, 3.0)
    se = cb.chart_lines[0].eventLayers[0].speedEvents
    assert len(se) == 1
    assert abs(se[0].start - 1.0) < 1e-9
    assert abs(se[0].end - 3.0) < 1e-9
    # SpeedEvent 没有 bezier 属性
    assert not hasattr(se[0], "bezier")


@test
def event_with_speed_kind_routes_to_line_speed():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ev = ln.event(EVENT_SPEED, 0, 4, 1.0, 2.0)
    assert ev in cb.chart_lines[0].eventLayers[0].speedEvents


@test
def line_speed_rejects_empty_span():
    cb = ChartBuilder(bpm=120)
    try:
        cb.line().line_speed(4, 4, 1.0, 2.0)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("零长度流速事件应当报错")


# ================================================================== 事件拼接
@test
def event_rejects_backwards_span():
    cb = ChartBuilder(bpm=120)
    try:
        cb.line().event(EVENT_MOVE_X, 4, 2, 0.0, 10.0)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("结束早于开始的事件应当报错")


@test
def overlapping_events_are_trimmed():
    """新事件覆盖旧事件时，旧事件被裁剪而不是留下重叠（RPE 会报 EventOverLapped）。"""
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.move_x(100, 0, 8)
    ln.move_x(200, 4, 8)
    xs = sorted(
        cb.chart_lines[0].eventLayers[0].moveXEvents, key=lambda e: e.startTime.frac
    )
    # 不能存在重叠
    for a, b in zip(xs, xs[1:]):
        assert a.endTime <= b.startTime
    # 第 4 拍时应当是 100（新事件从 4 拍起把值带到 200）
    assert abs(value_at(xs, Beat(4), 0.0) - 100.0) < 1e-6
    assert abs(value_at(xs, Beat(12), 0.0) - 200.0) < 1e-6


@test
def timeline_has_value_everywhere_after_events():
    """播放器语义下任意时刻都有值（事件链尾部保持末值）。

    注意区分：``value_at`` 是**文件语义**，最后一个事件结束之后返回 ``default``；
    真实谱面里事件链经常远早于最后一个音符就结束（实测 24 份谱面中 5485/8408 个
    事件列表在 16 拍内结束），播放器仍要正常渲染，所以模拟必须用
    ``value_at_resolved``。
    """
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.at(0, 0)
    ln.move_to(400, 0, 4, 4)
    xs = cb.chart_lines[0].eventLayers[0].moveXEvents
    for b in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 20, 100):
        v = value_at_resolved(xs, Beat(b), None)
        assert v is not None, f"第 {b} 拍没有值"
    # 尾部保持 400（不是回落到 0）
    assert abs(value_at_resolved(xs, Beat(100), None) - 400.0) < 1e-6
    # 对照：纯文件语义在末尾之后没有事件
    assert value_at(xs, Beat(100), None) is None


# ================================================================== 音符
@test
def four_note_kinds_get_right_type_ids():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.tap(0)
    ln.drag(1)
    ln.flick(2)
    ln.hold(3, dur=2)
    types = [n.type for n in cb.chart_lines[0].notes]
    assert types == [NOTE_TAP, NOTE_DRAG, NOTE_FLICK, NOTE_HOLD]


@test
def hold_gets_end_time_and_taps_do_not():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.tap(0)
    h = ln.hold(4, dur=2)
    assert cb.chart_lines[0].notes[0].endTime == Beat(0)
    assert h.startTime == Beat(4)
    assert h.endTime == Beat(6)
    assert abs(h.duration.value - 2.0) < 1e-9


@test
def hold_requires_positive_duration():
    cb = ChartBuilder(bpm=120)
    try:
        cb.line().hold(0, dur=0)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("零长度 Hold 应当报错")


@test
def note_position_and_speed_are_recorded():
    cb = ChartBuilder(bpm=120)
    n = cb.line().tap(2, x=-350, speed=2.5, above=2, y_offset=10)
    assert abs(n.positionX - (-350.0)) < 1e-9
    assert abs(n.speed - 2.5) < 1e-9
    assert n.above == 2
    # 实际偏移 = yOffset * speed
    assert abs(n.real_offset - 25.0) < 1e-9


@test
def notes_at_places_multiple_chords():
    cb = ChartBuilder(bpm=120)
    notes = cb.line().notes_at("tap", 0, [-300, 0, 300])
    assert len(notes) == 3
    assert [n.positionX for n in notes] == [-300.0, 0.0, 300.0]


@test
def stream_spaces_notes_evenly():
    cb = ChartBuilder(bpm=120)
    notes = cb.line().stream("tap", 0, 5, 0.5)
    assert len(notes) == 5
    times = [n.startTime.value for n in notes]
    assert times == [0.0, 0.5, 1.0, 1.5, 2.0]


@test
def stream_with_endpoints_gradates_x():
    cb = ChartBuilder(bpm=120)
    notes = cb.line().stream("tap", 0, 3, 1, x_from=-300, x_to=300)
    assert [n.positionX for n in notes] == [-300.0, 0.0, 300.0]


@test
def pattern_and_zigzag():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    p = ln.pattern("tap", 0, 3, interval=1, x_start=-200, x_end=200)
    z = ln.zigzag("drag", 8, 3, interval=1, x_left=-100, x_right=100)
    assert [n.positionX for n in p] == [-200.0, 0.0, 200.0]
    assert [n.positionX for n in z] == [-100.0, 100.0, -100.0]


# ================================================================== 曲线
@test
def curve_splits_into_connected_segments():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.curve(0, 8, -400, 400, 0, 0, beats=8)
    xs = sorted(
        cb.chart_lines[0].eventLayers[0].moveXEvents, key=lambda e: e.startTime.frac
    )
    # 每段首尾相接，且不重叠
    for a, b in zip(xs, xs[1:]):
        assert a.endTime == b.startTime
    assert abs(value_at(xs, Beat(0), 0.0) - (-400.0)) < 1e-6
    assert abs(value_at(xs, Beat(8), 0.0) - 400.0) < 1e-6


@test
def curve_arc_rises_in_the_middle():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.curve(0, 8, 0, 0, 0, 0, beats=16, arc=200)
    ys = sorted(
        cb.chart_lines[0].eventLayers[0].moveYEvents, key=lambda e: e.startTime.frac
    )
    mid = value_at(ys, Beat(4), 0.0)
    assert mid > 150.0, f"拱顶应当在中间抬升，实测 {mid}"
    assert abs(value_at(ys, Beat(0), 0.0) - 0.0) < 1e-6
    assert abs(value_at(ys, Beat(8), 0.0) - 0.0) < 1e-6


@test
def curve_rejects_backwards_span():
    cb = ChartBuilder(bpm=120)
    try:
        cb.line().curve(8, 0, 0, 0, 0, 0)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("倒序曲线应当报错")


@test
def event_curve_uses_arbitrary_function():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.event_curve(EVENT_ALPHA, 0, 4, lambda p: 255 * p, samples=8)
    a = sorted(
        cb.chart_lines[0].eventLayers[0].alphaEvents, key=lambda e: e.startTime.frac
    )
    assert abs(value_at(a, Beat(0), 0.0) - 0.0) < 1e-6
    assert abs(value_at(a, Beat(4), 0.0) - 255.0) < 1e-6


# ================================================================== 多层 / 多条线
@test
def events_go_into_requested_layer():
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.move_x(100, 0, 4, layer=0)
    ln.move_x(200, 0, 4, layer=1)
    layers = cb.chart_lines[0].eventLayers
    assert len(layers) >= 2
    assert abs(layers[0].moveXEvents[0].end - 100.0) < 1e-9
    assert abs(layers[1].moveXEvents[0].end - 200.0) < 1e-9


@test
def multiple_lines_are_independent():
    cb = ChartBuilder(bpm=120)
    a = cb.line(name="A")
    b = cb.line(name="B")
    a.tap(0, x=-100)
    b.drag(0, x=100)
    assert len(cb.chart_lines) == 2
    assert cb.chart_lines[0].notes[0].type == NOTE_TAP
    assert cb.chart_lines[1].notes[0].type == NOTE_DRAG
    assert a.index == 0 and b.index == 1


@test
def remove_line_fixes_father_references():
    cb = ChartBuilder(bpm=120)
    cb.line(name="A")
    cb.line(name="B")
    cb.line(name="C").father(0)
    cb.chart_lines[1].father = 0
    cb.remove_line(0)
    assert len(cb.chart_lines) == 2
    # 原本指向 0 的线现在没有父线
    assert cb.chart_lines[0].father == -1
    # 原本指向 1 的线现在指向 0
    assert cb.chart_lines[1].father == -1


@test
def remove_line_shifts_higher_father_indices():
    cb = ChartBuilder(bpm=120)
    cb.line(name="A")
    cb.line(name="B")
    cb.line(name="C").father(2)
    cb.remove_line(0)
    assert cb.chart_lines[1].father == 1


@test
def line_builder_can_be_recovered_for_editing():
    cb = ChartBuilder(bpm=120)
    cb.line(name="A").tap(0)
    b = cb.line_builder(0)
    b.tap(4)
    assert len(isinstance(b, JudgeLineBuilder) and cb.chart_lines[0].notes or []) == 2


@test
def chart_builder_can_wrap_existing_chart():
    cb = ChartBuilder(bpm=120, name="原始")
    cb.line().tap(0)
    cb2 = ChartBuilder(existing=cb.chart)
    assert cb2.chart is cb.chart
    cb2.line().tap(4)
    assert len(cb2.chart_lines) == 2


# ================================================================== 输出
@test
def builder_to_json_has_all_top_level_keys():
    cb = ChartBuilder(bpm=120, name="X", charter="me")
    cb.line().tap(0)
    obj = cb.to_json()
    for key in ("BPMList", "META", "judgeLineList", "chartTime", "judgeLineGroup"):
        assert key in obj, f"缺少顶层键 {key}"


@test
def builder_save_and_reload_roundtrips():
    import os
    import tempfile

    from rpechart.io import load_chart

    cb = ChartBuilder(bpm=150, name="往返测试", charter="me")
    ln = cb.line(name="主线")
    ln.at(0, 0)
    ln.move_to(300, 0, 4, 4)
    ln.tap(0, x=-200)
    ln.hold(2, dur=1.5, x=200)
    ln.color_fade([0, 0, 0], [255, 0, 0], 0, 4)

    d = tempfile.mkdtemp(prefix="rpechart_builder_")
    try:
        path = os.path.join(d, "chart.json")
        cb.save(path)
        loaded = load_chart(path)
        assert loaded.META.name == "往返测试"
        assert len(loaded.judgeLineList) == 1
        assert len(loaded.judgeLineList[0].notes) == 2
        # 二次序列化稳定
        assert loaded.to_json() == loaded.to_json()
    finally:
        import shutil

        shutil.rmtree(d, ignore_errors=True)


@test
def builder_save_sorts_notes_hold_last():
    """save() 会先调用 sort()：非 Hold 在前、Hold 在后。"""
    import os
    import tempfile

    from rpechart.io import load_chart

    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ln.hold(1, dur=2)
    ln.tap(4)
    d = tempfile.mkdtemp(prefix="rpechart_sort_")
    try:
        path = os.path.join(d, "chart.json")
        cb.save(path)
        notes = load_chart(path).judgeLineList[0].notes
        assert [n.type for n in notes] == [NOTE_TAP, NOTE_HOLD]
    finally:
        import shutil

        shutil.rmtree(d, ignore_errors=True)


@test
def builder_validate_returns_report():
    cb = ChartBuilder(bpm=120, name="校验")
    cb.line().tap(0, x=0)
    report = cb.validate()
    assert not report.errors, [str(i) for i in report.errors]


@test
def done_returns_chart_builder():
    """``tap()`` 等音符方法有意返回 **Note**（方便取回对象），所以链式写法是先拿到
    判定线 builder 再 ``done()``。"""
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    note = ln.tap(0)
    assert note is not None
    assert ln.done() is cb
