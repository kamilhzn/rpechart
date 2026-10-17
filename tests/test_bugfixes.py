"""库本体缺陷的回归测试（每条缺陷一个用例，全部自造数据）。

命名约定：``bugfix_<主题>``。这些用例逐个对应取证清单里的条目：

* ``bugfix_spec_*``          —— ``rpechart.gen.spec`` 的 spec 序列化 / 校验
* ``bugfix_normalize_*``     —— ``rpechart.gen.structure`` 的段表规范化
* ``bugfix_note_planner_*`` / ``bugfix_max_notes_*`` / ``bugfix_hold_every_*``
                             —— ``rpechart.gen.notes`` 的编排器
* ``bugfix_lane_grid_*``     —— ``rpechart.gen.lanes`` 的轨道吸附
* ``bugfix_chart_builder_*`` / ``bugfix_speed_event_*``
                             —— ``rpechart.builders`` 的 API 转发
* ``bugfix_out_of_screen_*`` / ``bugfix_bad_line_group_*`` / ``bugfix_father_*``
  ``bugfix_note_and_event_index_*`` / ``bugfix_hold_sticked_*``
  ``bugfix_short_readtime_*`` / ``bugfix_zero_bpm_*``
                             —— ``rpechart.validate`` 的误报 / 下标 / 边界
* ``bugfix_null_*`` / ``bugfix_loaded_*`` / ``bugfix_missing_group_*``
  ``bugfix_time_tag_*`` / ``bugfix_meta_*`` / ``bugfix_num_of_notes_*``
  ``bugfix_zero_bpmfactor_*`` / ``bugfix_zero_denominator_*``
                             —— ``rpechart.model`` / ``beat`` 的无损往返
* ``bugfix_save_chart_*``    —— ``rpechart.io`` 写出不改动调用方
* ``bugfix_value_at_*``      —— ``rpechart.interp`` 的乱序输入与查找
* ``bugfix_duration_beats_*``—— ``Chart.duration_beats`` 与渲染层口径一致
* ``bugfix_mp3_*``           —— ``rpechart.audio`` 的帧头边界

用例只使用公开 API，另有三个私有函数（``_parse_mp3_frame`` / ``_probe_mp3`` /
``normalize_sections``）被直接调用 —— 它们的边界行为就是缺陷本身，没有公开等价物。
"""

import json
import shutil
import tempfile
from fractions import Fraction
from pathlib import Path

from run_tests import approx, raises, test

from rpechart.audio import _parse_mp3_frame, _probe_mp3
from rpechart.beat import Beat
from rpechart.builders import ChartBuilder
from rpechart.gen.features import BeatFeatures
from rpechart.gen.lanes import LaneGrid
from rpechart.gen.notes import DEFAULT_PLANS, NotePlanner
from rpechart.gen.spec import ChartSpec, LineSpec
from rpechart.gen.structure import Section, normalize_sections, sections_tile
from rpechart.interp import value_at
from rpechart.io import save_chart
from rpechart.model import (
    EVENT_SPEED,
    BPMItem,
    Chart,
    Event,
    EventLayer,
    ExtendedEvents,
    JudgeLine,
    Meta,
    Note,
    NOTE_HOLD,
    NOTE_TAP,
    SpeedEvent,
    TimeTag,
)
from rpechart.validate import validate


class _Tmp:
    """给每个用例一个干净临时目录，退出时自动删。"""

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="rpechart_bugfix_")
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)
        return False


def _codes(report):
    return [i.code for i in report.issues]


def _chart(lines=None, bpm=120.0):
    return Chart(
        BPMList=[BPMItem(bpm=bpm, startTime=Beat(0))],
        judgeLineList=list(lines or []),
    )


def _line(notes=None, **kw):
    return JudgeLine(notes=list(notes or []), **kw)


# ============================================================== gen/spec
@test
def bugfix_spec_to_json_handles_section_objects():
    """spec 的 sections 里放 Section 对象时必须序列化成 dict（write 不再崩）。"""
    spec = ChartSpec(sections=[Section(0, 32, role="B")])
    out = spec.to_json()
    assert out["sections"][0]["start"] == 0.0
    assert out["sections"][0]["end"] == 32.0
    assert out["sections"][0]["role"] == "B"
    with _Tmp() as d:
        p = spec.write(Path(d) / "spec.json")  # 旧实现抛 TypeError
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert raw["sections"][0]["end"] == 32.0


@test
def bugfix_spec_converts_every_element_of_lines_and_sections():
    """lines / sections 混用对象与 dict 时，每个元素都要被转换（不是只看第 0 个）。"""
    spec = ChartSpec(
        lines=[LineSpec(name="a"), {"name": "b"}],
        sections=[Section(0, 8), {"start": 8, "end": 16}],
    )
    assert all(isinstance(x, LineSpec) for x in spec.lines)
    assert all(isinstance(x, Section) for x in spec.sections)
    assert spec.lines[1].name == "b"          # 旧实现这里是 dict，取 .name 会炸
    assert spec.sections[1].start == 8.0


@test
def bugfix_spec_problems_reports_a_table_that_misses_the_beginning():
    """只查段表尾部会漏掉「开头空一段」：0..16 拍会完全没有编排。"""
    spec = ChartSpec(sections=[{"start": 16, "end": 64, "role": "B"}], last_beat=64)
    probs = spec.problems()
    assert probs, probs
    assert any("才开始" in p for p in probs), probs
    # 铺满的段表不该报
    ok = ChartSpec(
        sections=[{"start": 0, "end": 32, "role": "B"},
                  {"start": 32, "end": 64, "role": "R"}],
        last_beat=64,
    )
    assert ok.problems() == [], ok.problems()


@test
def bugfix_spec_problems_reports_a_gap_between_sections():
    spec = ChartSpec(
        sections=[{"start": 0, "end": 16, "role": "B"},
                  {"start": 20, "end": 64, "role": "R"}],
        last_beat=64,
    )
    probs = spec.problems()
    assert any("不连续" in p for p in probs), probs


# ============================================================== structure
@test
def bugfix_normalize_sections_does_not_mutate_the_caller():
    """段表规范化必须作用在副本上：旧实现就地改 a.end，污染会跨调用累积。"""
    s1 = Section(0, 32, role="B")
    s2 = Section(48, 64, role="R")
    spec = ChartSpec(sections=[s1, s2], last_beat=64)
    spec.problems()
    assert spec.sections[0] is s1
    assert s1.end == 32.0, s1.end          # 旧实现变成 48.0
    assert s2.start == 48.0, s2.start

    out = normalize_sections([s1, s2])
    assert out[0].end == 48.0              # 规范化后的副本照旧填平
    assert s1.end == 32.0                  # 但调用方的对象没被动
    assert not sections_tile([s1, s2], 64.0)   # 16 拍的缝仍是缝


# ============================================================== gen/lanes
@test
def bugfix_lane_grid_snaps_with_real_lane_spacing():
    """lanes 间距不等于 step 时，吸附必须按真实轨道列表算。"""
    grid = LaneGrid(lanes=[-100, 0, 100], step=135.0)
    assert grid.index(100.0) == 2, grid.index(100.0)   # 旧实现吸到 0.0
    assert grid.lane(100.0) == 100.0
    assert grid.index(-100.0) == 0
    assert grid.index(0.0) == 1
    assert grid.lane(60.0) == 100.0
    assert grid.index(999.0) == 2        # 越界钳位
    assert grid.index(-999.0) == 0


# ============================================================== gen/notes
@test
def bugfix_note_planner_does_not_share_role_plans():
    """两个 planner 不能共享同一个 RolePlan 对象（浅拷贝 = 模块级默认被改）。"""
    p1, p2 = NotePlanner(), NotePlanner()
    assert p1.plans["B"] is not p2.plans["B"]
    assert p1.plans["B"] is not DEFAULT_PLANS["B"]
    p1.plans["B"].density_scale = 99.0
    assert DEFAULT_PLANS["B"].density_scale != 99.0
    assert p2.plans["B"].density_scale != 99.0
    s1, s2 = ChartSpec(), ChartSpec()
    assert s1.planner().plans["B"] is not s2.planner().plans["B"]


@test
def bugfix_max_notes_per_section_actually_caps_a_section():
    """max_notes_per_section 承诺的「单段上限保护」必须真的生效（此前无人读）。"""
    f = BeatFeatures(
        low=[0.9] * 32, rn=[0.0] * 32, bn=[6000.0] * 32,
        smooth_window=0, slew_limit=0.0, center_slew_limit=0.0,
    )
    sec = Section(0, 32, role="B", normal="burst", dense="burst", dense_every=1)
    full = NotePlanner(max_notes_per_section=0).plan(f, [sec])
    assert len(full) > 200, len(full)          # 不限制时确实是密集段
    capped = NotePlanner(max_notes_per_section=50).plan(f, [sec])
    assert 0 < len(capped) <= 50, len(capped)
    assert max(n.t for n in capped) >= 24.0    # 抽稀后仍覆盖整段


@test
def bugfix_hold_every_has_a_one_beat_floor():
    """holds 的 every 只挡住 <= 0 会让 0.001 拍生成约 1e6 个 Hold。"""
    f = BeatFeatures(low=[0.0] * 16, smooth_window=0, slew_limit=0.0,
                     center_slew_limit=0.0)
    sec = Section(0, 16, role="B",
                  holds=[{"from": 0.0, "to": 16.0, "every": 0.001, "dur": 1.0}])
    holds = [n for n in NotePlanner().plan(f, [sec]) if n.kind == "hold"]
    assert 0 < len(holds) <= 16, len(holds)


# ============================================================== builders
@test
def bugfix_chart_builder_line_forwards_identity_kwargs():
    """cb.line(**kwargs) 必须把身份参数转发给 builder，而不是静默吞掉。"""
    cb = ChartBuilder(bpm=120)
    cb.line(name="a", z_order=7, texture="t.png", group=3, cover=False, father=0)
    ln = cb.chart.judgeLineList[0]
    assert ln.zOrder == 7
    assert ln.Texture == "t.png"
    assert ln.Group == 3
    assert ln.isCover == 0
    assert ln.father == 0
    # 认不出的键必须报错，不能吞
    raises(TypeError, cb.line, name="b", nosuch_argument=1)


@test
def bugfix_speed_event_keeps_the_easing_window():
    """EVENT_SPEED 只转发 easing/linkgroup，会静默丢掉缓动窗口。"""
    cb = ChartBuilder(bpm=120)
    ln = cb.line()
    ev = ln.event(EVENT_SPEED, 0, 8, 1.0, 2.0,
                  easing=2, easing_left=0.25, easing_right=0.75)
    approx(ev.easingLeft, 0.25)
    approx(ev.easingRight, 0.75)
    assert ev.easingType == 2
    assert not hasattr(ev, "bezier")     # RPE 的流速事件没有 bezier 字段
    out = cb.chart.to_json()
    se = out["judgeLineList"][0]["eventLayers"][0]["speedEvents"][0]
    approx(se["easingLeft"], 0.25)
    approx(se["easingRight"], 0.75)
    assert se["easingType"] == 2


@test
def bugfix_line_speed_keeps_the_easing_window():
    cb = ChartBuilder(bpm=120)
    ev = cb.line().line_speed(0, 8, 1.0, 2.0, easing_left=0.2, easing_right=0.9)
    approx(ev.easingLeft, 0.2)
    approx(ev.easingRight, 0.9)


# ============================================================== validate
@test
def bugfix_out_of_screen_travel_uses_absolute_line_speed():
    """线速为负是合法写法（音符向上飞），不能算出负 travel 却报「永远」。"""
    cb = ChartBuilder(bpm=120)
    ln = cb.line(name="L")
    ln.line_speed(0, 64, -2.0, -2.0)
    ln.tap(4, x=0, visible_time=0.5)
    report = validate(cb.chart)
    issue = next(i for i in report.issues if i.code == "NoteJudgedOutScreen")
    assert "永远" not in issue.message, issue.message
    assert "3.750" in issue.message, issue.message
    # 线速 0（音符真的到不了判定线）仍然报「永远」
    ln2 = JudgeLine(
        notes=[Note(type=NOTE_TAP, startTime=Beat(0), visibleTime=90.0)],
        eventLayers=[EventLayer(speedEvents=[SpeedEvent(Beat(0), Beat(64), 0.0, 0.0)])],
    )
    report2 = validate(_chart([ln2]))
    never = next(i for i in report2.issues if i.code == "NoteJudgedOutScreen")
    assert "永远" in never.message


@test
def bugfix_bad_line_group_skips_when_the_group_table_is_empty():
    """RPE 能正常打开缺 judgeLineGroup 的谱面，不该逐线报越界。"""
    chart = Chart.from_json({
        "BPMList": [{"bpm": 120.0, "startTime": [0, 0, 1]}],
        "judgeLineList": [{"Group": 0, "Name": "L", "eventLayers": [], "notes": []}],
    })
    assert chart.judgeLineGroup == []
    assert "BadLineGroup" not in _codes(validate(chart))
    # 组表非空时越界仍要报
    chart.judgeLineGroup = ["Default"]
    chart.judgeLineList[0].Group = 3
    assert "BadLineGroup" in _codes(validate(chart))


@test
def bugfix_father_out_of_range_is_reported_once():
    report = validate(_chart([_line(father=99)]))
    assert _codes(report).count("BadFather") == 1, _codes(report)


@test
def bugfix_note_and_event_index_point_at_the_disk_position():
    """note_index / event_index 要能直接索引回磁盘数组，不是排序后的位置。"""
    line = _line([
        Note(type=NOTE_TAP, startTime=Beat(8), positionX=0.0),
        Note(type=NOTE_HOLD, startTime=Beat(0), endTime=Beat(16), positionX=0.0),
    ])
    issue = next(i for i in validate(_chart([line])).issues
                 if i.code == "TapHoldOverlapped")
    assert issue.note_index == 1, issue.note_index
    assert line.notes[issue.note_index].type == NOTE_HOLD

    line2 = _line()
    line2.eventLayers = [EventLayer(moveXEvents=[
        Event(Beat(8), Beat(12), 0.0, 12.0),
        Event(Beat(0), Beat(-1), 0.0, -1.0),      # 坏事件在磁盘下标 1
    ])]
    issue2 = next(i for i in validate(_chart([line2])).issues
                  if i.code == "IllegalEvent")
    assert issue2.event_index == 1, issue2.event_index
    assert line2.eventLayers[0].moveXEvents[issue2.event_index].endTime <= Beat(0)


@test
def bugfix_hold_sticked_catches_touching_and_center_lane():
    """gap == 0 正是 Hold Sticked?；X 为 0 的中心道也不能被符号积跳过。"""
    center = _line([
        Note(type=NOTE_HOLD, startTime=Beat(0), endTime=Beat(4), positionX=0.0),
        Note(type=NOTE_HOLD, startTime=Beat(4), endTime=Beat(8), positionX=0.0),
    ])
    assert "HoldSticked" in _codes(validate(_chart([center])))
    # 异号（跨过判定线中心）不算
    opposite = _line([
        Note(type=NOTE_HOLD, startTime=Beat(0), endTime=Beat(4), positionX=-100.0),
        Note(type=NOTE_HOLD, startTime=Beat(4), endTime=Beat(8), positionX=100.0),
    ])
    assert "HoldSticked" not in _codes(validate(_chart([opposite])))
    # 真有缝（500ms）不算
    gap = _line([
        Note(type=NOTE_HOLD, startTime=Beat(0), endTime=Beat(4), positionX=-100.0),
        Note(type=NOTE_HOLD, startTime=Beat(5), endTime=Beat(9), positionX=-100.0),
    ])
    assert "HoldSticked" not in _codes(validate(_chart([gap])))


@test
def bugfix_short_readtime_uses_the_epsilon():
    """40.0ms 恰好是下界；BPM=11 时浮点会算成 39.99999999999999，不能误报。"""
    exact = Beat(Fraction(11, 1500))          # 11 BPM 下正好 40ms
    chart = Chart(
        BPMList=[BPMItem(bpm=11.0, startTime=Beat(0))],
        judgeLineList=[_line([
            Note(type=NOTE_HOLD, startTime=Beat(0), endTime=exact),
        ])],
    )
    assert "ShortReadtime" not in _codes(validate(chart))
    shorter = Beat(Fraction(11, 1500) * Fraction(9, 10))   # 36ms
    chart2 = Chart(
        BPMList=[BPMItem(bpm=11.0, startTime=Beat(0))],
        judgeLineList=[_line([
            Note(type=NOTE_HOLD, startTime=Beat(0), endTime=shorter),
        ])],
    )
    assert "ShortReadtime" in _codes(validate(chart2))


@test
def bugfix_zero_bpm_does_not_crash_validation():
    """BPM == 0 时 Timing 会除零；校验器必须报 BadBPM 而不是抛异常。"""
    chart = Chart.from_json({
        "BPMList": [{"bpm": 0, "startTime": [0, 0, 1]}],
        "judgeLineList": [{
            "notes": [{"type": 2, "startTime": [4, 0, 1], "endTime": [8, 0, 1]}],
            "eventLayers": [{}],
        }],
    })
    report = validate(chart)
    assert "BadBPM" in _codes(report)
    assert report.ok is False


# ============================================================== model
@test
def bugfix_null_fields_are_treated_as_absent():
    """JSON 里的显式 null 等同「没写」：整键为 null 不能抛 TypeError。"""
    chart = Chart.from_json({
        "BPMList": None, "judgeLineList": None,
        "judgeLineGroup": None, "timeTags": None,
    })
    assert chart.judgeLineList == []
    assert chart.timeTags == []
    assert len(chart.BPMList) == 1            # 缺 BPM 时补默认

    line = JudgeLine.from_json({"notes": None, "anchor": None})
    assert line.notes == []
    assert line.anchor == [0.5, 0.5]

    n = Note.from_json({
        "type": None, "size": None, "alpha": None, "speed": None,
        "visibleTime": None, "positionX": None, "yOffset": None,
        "above": None, "isFake": None, "judgeArea": None,
    })
    approx(n.size, 1.0)
    assert n.alpha == 255
    assert n.type == NOTE_TAP
    approx(n.positionX, 0.0)

    ev = Event.from_json({
        "easingLeft": None, "easingRight": None, "linkgroup": None,
        "bezier": None, "bezierPoints": None, "easingType": None,
    })
    approx(ev.easingLeft, 0.0)
    approx(ev.easingRight, 1.0)
    assert ev.bezier == 0
    assert ev.bezierPoints == [0.0, 0.0, 0.0, 0.0]

    se = SpeedEvent.from_json({"start": None, "end": None})
    approx(se.start, 1.0)
    approx(se.end, 1.0)


@test
def bugfix_loaded_empty_layers_and_extended_survive_the_roundtrip():
    """文件里写过的 eventLayers / extended（哪怕是空的）必须原样写回。"""
    raw = {
        "BPMList": [{"bpm": 120.0, "startTime": [0, 0, 1]}],
        "judgeLineList": [{"Group": 0, "eventLayers": [{}], "extended": {}}],
    }
    out = Chart.from_json(raw).to_json()
    assert out["judgeLineList"][0]["eventLayers"] == [{}]
    assert out["judgeLineList"][0]["extended"] == {}
    # 空数组同样要保住
    raw2 = {
        "BPMList": [{"bpm": 120.0, "startTime": [0, 0, 1]}],
        "judgeLineList": [{"Group": 0, "eventLayers": []}],
    }
    assert Chart.from_json(raw2).to_json()["judgeLineList"][0]["eventLayers"] == []
    # 程序化新建仍然省略
    assert "eventLayers" not in JudgeLine().to_json()
    assert "extended" not in JudgeLine().to_json()


@test
def bugfix_a_missing_group_table_is_not_invented_on_write():
    """「缺 judgeLineGroup 键」与「显式空数组」要区分。"""
    base = {"BPMList": [{"bpm": 120.0, "startTime": [0, 0, 1]}], "judgeLineList": []}
    assert "judgeLineGroup" not in Chart.from_json(base).to_json()
    explicit = dict(base, judgeLineGroup=[])
    assert Chart.from_json(explicit).to_json()["judgeLineGroup"] == []
    assert Chart().to_json()["judgeLineGroup"] == ["Default"]


@test
def bugfix_time_tag_keeps_unknown_keys_and_start_time_alias():
    out = TimeTag.from_json({"name": "a", "time": [1, 0, 1], "color": [1, 2, 3]}).to_json()
    assert out["color"] == [1, 2, 3]
    assert out["time"] == [1, 0, 1]
    alias = TimeTag.from_json({"name": "b", "startTime": [2, 0, 1]}).to_json()
    assert alias["time"] == [2, 0, 1]
    assert TimeTag.from_json({}).to_json()["name"] == ""


@test
def bugfix_meta_keeps_explicit_null_keys():
    out = Meta.from_json(
        {"RPEVersion": 160, "illustration": None, "duration": None}
    ).to_json()
    assert "illustration" in out and out["illustration"] is None
    assert "duration" in out and out["duration"] is None
    assert "illustration" not in Meta().to_json()
    assert "duration" not in Meta().to_json()


@test
def bugfix_num_of_notes_string_survives_the_roundtrip():
    raw = {
        "BPMList": [{"bpm": 120.0, "startTime": [0, 0, 1]}],
        "judgeLineList": [{"numOfNotes": "12", "notes": [], "eventLayers": []}],
    }
    out = Chart.from_json(raw).to_json()
    assert out["judgeLineList"][0]["numOfNotes"] == 12


@test
def bugfix_zero_denominator_literal_survives_to_list():
    b = Beat([2, 0, 0])                # RPE 的合法写法：c=0 按分母 1 处理
    approx(b.value, 2.0)
    assert b.to_list() == [2, 0, 0], b.to_list()
    assert Beat.from_list([2, 0, 4]).to_list() == [2, 0, 4]


@test
def bugfix_zero_bpmfactor_is_not_silently_replaced():
    ln = JudgeLine.from_json({"bpmfactor": 0.0})
    approx(ln.bpmfactor, 0.0)
    assert "BadBpmFactor" in _codes(validate(_chart([ln])))
    approx(JudgeLine.from_json({"bpmfactor": 2.0}).bpmfactor, 2.0)


@test
def bugfix_duration_beats_counts_special_events():
    """Storyboard 事件（extended）也要计入时长，与渲染层口径一致。"""
    line = JudgeLine(
        notes=[Note(type=NOTE_TAP, startTime=Beat(0))],
        extended=ExtendedEvents(scaleXEvents=[Event(startTime=Beat(0), endTime=Beat(64))]),
    )
    assert Chart(judgeLineList=[line]).duration_beats() == Beat(64)


# ============================================================== io
def _unsorted_chart():
    line = JudgeLine(
        Name="L",
        notes=[
            Note(type=NOTE_TAP, startTime=Beat(8), positionX=0.0),
            Note(type=NOTE_TAP, startTime=Beat(1), positionX=0.0),
            Note(type=NOTE_TAP, startTime=Beat(4), positionX=0.0),
        ],
        eventLayers=[EventLayer(speedEvents=[
            SpeedEvent(Beat(0), Beat(8), 1.0, 1.0),
            SpeedEvent(Beat(16), Beat(24), 4.0, 4.0),
            SpeedEvent(Beat(8), Beat(16), 2.0, 2.0),
        ])],
    )
    return Chart(
        BPMList=[BPMItem(bpm=174.0, startTime=Beat(0))],
        META=Meta(name="排序", charter="我", id=""),
        judgeLineList=[line],
    )


@test
def bugfix_save_chart_does_not_reorder_the_callers_object():
    """写盘前排序只该作用在副本上，不能把调用方手里的 Chart 改掉顺序。"""
    chart = _unsorted_chart()
    notes_before = [n.startTime for n in chart.judgeLineList[0].notes]
    evs_before = [e.startTime for e in chart.judgeLineList[0].eventLayers[0].speedEvents]
    with _Tmp() as d:
        p = save_chart(chart, Path(d) / "c.json")
        raw = json.loads(p.read_text(encoding="utf-8"))
    assert [n.startTime for n in chart.judgeLineList[0].notes] == notes_before
    assert [e.startTime for e in chart.judgeLineList[0].eventLayers[0].speedEvents] == evs_before
    starts = [
        e["startTime"]
        for e in raw["judgeLineList"][0]["eventLayers"][0]["speedEvents"]
    ]
    assert starts == [[0, 0, 1], [8, 0, 1], [16, 0, 1]], starts


# ============================================================== interp
@test
def bugfix_value_at_matches_a_sorted_sequence_for_equal_starts():
    """乱序检测必须看 (startTime, endTime) 联合键，否则 docstring 的恒等式是假的。"""
    long_first = Event(Beat(0), Beat(8), 0.0, 1.0)
    short_second = Event(Beat(0), Beat(4), 3.0, 3.0)
    def key(e):
        return (e.startTime.frac, e.endTime.frac)
    sequences = [
        [long_first, short_second],
        [short_second, long_first],
        [Event(Beat(0), Beat(8), 0.0, 1.0), Event(Beat(4), Beat(4), 1.0, 1.0),
         Event(Beat(4), Beat(12), 2.0, 2.0), Event(Beat(0), Beat(4), 3.0, 3.0)],
    ]
    for seq in sequences:
        ordered = sorted(seq, key=key)
        for t in (0, 1, 3.999, 4, 6, 8, 12, 20):
            assert value_at(seq, t) == value_at(ordered, t), (
                t, value_at(seq, t), value_at(ordered, t))


@test
def bugfix_value_at_finds_an_early_long_event_among_many_short_ones():
    """前缀最大 endTime + 二分：早期长事件仍然覆盖 t，且 t == endTime 取最小下标。"""
    evs = [Event(Beat(0), Beat(200), 7.0, 7.0)]
    evs += [Event(Beat(i), Beat(i + 1), float(i), float(i)) for i in range(1, 150)]
    assert value_at(evs, 100) == 7.0
    assert value_at(evs, 200) == 7.0            # t == endTime 的兜底仍然命中
    assert value_at(evs, 10_000) == 0.0         # 全都不覆盖 → default

    tie = [Event(Beat(0), Beat(4), 1.0, 1.0), Event(Beat(4), Beat(4), 2.0, 2.0)]
    assert value_at(tie, 4) == 1.0              # 取最小下标的等值事件


# ============================================================== audio
@test
def bugfix_mp3_probe_sees_a_frame_in_the_last_four_bytes():
    """range(len - 4) 会漏掉正好压在缓冲区最后 4 字节的合法帧头。"""
    buf = b"\x00" * 8 + bytes([0xFF, 0xFB, 0x90, 0x00])
    assert _parse_mp3_frame(buf, 8) is not None
    with _Tmp() as d:
        p = Path(d) / "tail.mp3"
        p.write_bytes(buf)
        assert _probe_mp3(p) is not None
