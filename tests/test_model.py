"""model.py —— RPE JSON 模型的序列化 / 反序列化。

这里最重要的是**往返保真**：RPE 的文件里存在大量「不存在即等价于默认值」的字段，
以及我们没建模的未知字段。库必须做到「读进来再写出去不丢东西」，否则修改一张
别人做的谱面就会悄悄损坏它。
"""

from run_tests import approx, raises, test

from rpechart.easing import ease
from rpechart.model import (
    EVENT_ALPHA,
    EVENT_MOVE_X,
    EVENT_SPEED,
    NOTE_DRAG,
    NOTE_FLICK,
    NOTE_HOLD,
    NOTE_TAP,
    BPMItem,
    Chart,
    ControlKeyframe,
    Controls,
    Event,
    EventLayer,
    ExtendedEvents,
    JudgeLine,
    Meta,
    Note,
    SpeedEvent,
    TimeTag,
)
from rpechart.beat import Beat


# ------------------------------------------------------------------ Note
@test
def note_defaults_match_rpe():
    """Note 的字段默认值必须与 RPE 规范一致。"""
    n = Note()
    approx(n.positionX, 0.0)
    assert n.type == NOTE_TAP
    assert n.above == 1
    assert n.alpha == 255
    assert n.isFake == 0
    approx(n.size, 1.0)
    approx(n.speed, 1.0)
    approx(n.visibleTime, 999999.0)
    approx(n.yOffset, 0.0)
    approx(n.judgeArea, 1.0)
    assert n.hitsound is None
    assert n.tint is None
    assert n.tintHitEffects is None


@test
def note_type_ids_are_the_rpe_ids():
    """类型编号是硬编码进文件格式的，绝不能搞错（1/4/2/3 而不是 1/2/3/4）。"""
    assert NOTE_TAP == 1
    assert NOTE_HOLD == 2
    assert NOTE_FLICK == 3
    assert NOTE_DRAG == 4


@test
def note_optional_fields_are_omitted_when_default():
    """170 版新增字段（judgeArea/tint）只在非默认时写出。"""
    data = Note().to_json()
    assert "judgeArea" not in data
    assert "tint" not in data
    assert "tintHitEffects" not in data
    assert "hitsound" not in data

    n = Note(judgeArea=1.5, tint=[10, 20, 30], tintHitEffects=[1, 2, 3], hitsound="a.wav")
    data = n.to_json()
    assert data["judgeArea"] == 1.5
    assert data["tint"] == [10, 20, 30]
    assert data["tintHitEffects"] == [1, 2, 3]
    assert data["hitsound"] == "a.wav"


@test
def note_color_alias_is_accepted():
    """170 之前叫 ``color``，之后叫 ``tint``；两个键都要能读进来。"""
    d = {"type": 1, "startTime": [0, 0, 1], "endTime": [0, 0, 1], "color": [1, 2, 3]}
    assert Note.from_json(d).tint == [1, 2, 3]
    d2 = {"type": 1, "startTime": [0, 0, 1], "endTime": [0, 0, 1], "tint": [4, 5, 6]}
    assert Note.from_json(d2).tint == [4, 5, 6]
    # tint 优先于 color（新版字段优先）
    d3 = dict(d, tint=[7, 8, 9])
    assert Note.from_json(d3).tint == [7, 8, 9]
    # 写出时**保持原来的键名**：真实 170 谱面里 color 与 tint 互斥出现
    # （实测两者同时存在 0 次），替谱师改名等于篡改别人的文件。
    assert "color" in Note.from_json(d).to_json()
    assert "tint" not in Note.from_json(d).to_json()
    assert "tint" in Note.from_json(d2).to_json()
    assert "color" not in Note.from_json(d2).to_json()
    assert Note.from_json(d).to_json()["color"] == [1, 2, 3]
    # 程序化新建的音符用新名 tint
    assert Note(tint=[1, 2, 3]).to_json()["tint"] == [1, 2, 3]


@test
def note_non_hold_endtime_is_clamped_to_starttime():
    """非 Hold 的 endTime 必须等于 startTime；旧文件里偶有不一致，读入时归位。"""
    d = {
        "type": 1,
        "startTime": [4, 0, 1],
        "endTime": [8, 0, 1],  # 旧文件里的脏数据
    }
    n = Note.from_json(d)
    assert n.startTime == n.endTime == Beat(4)
    # Hold 则必须保留
    h = Note.from_json({"type": 2, "startTime": [4, 0, 1], "endTime": [8, 0, 1]})
    assert h.startTime == Beat(4) and h.endTime == Beat(8)


@test
def note_endtime_defaults_to_starttime_when_missing():
    n = Note.from_json({"type": 4, "startTime": [3, 1, 2]})
    assert n.startTime == n.endTime == Beat.of([3, 1, 2])
    approx(n.startTime.value, 3.5)


@test
def note_real_offset_is_yoffset_times_speed():
    """RPE 语义：真实偏移量 = yOffset * speed；speed=0 时偏移恒为 0。"""
    approx(Note(yOffset=10.0, speed=2.0).real_offset, 20.0)
    approx(Note(yOffset=10.0, speed=0.0).real_offset, 0.0)
    approx(Note(yOffset=-5.0, speed=1.5).real_offset, -7.5)


@test
def note_copy_and_shift_are_independent():
    n = Note(type=NOTE_HOLD, startTime=Beat(2), endTime=Beat(4), positionX=100.0)
    c = n.copy()
    c.positionX = 999.0
    c.startTime = Beat(50)
    approx(n.positionX, 100.0)  # 深拷贝，原对象不受影响
    assert n.startTime == Beat(2)

    s = n.shifted(Beat(3))
    assert s.startTime == Beat(5) and s.endTime == Beat(7)
    assert n.startTime == Beat(2)

    m = n.moved_to(Beat(10))
    assert m.startTime == Beat(10) and m.endTime == Beat(12)


@test
def note_roundtrip_preserves_unknown_fields():
    """未知字段必须原样带出去 —— 这是「不破坏别人的谱面」的底线。"""
    raw = {
        "type": 1,
        "startTime": [1, 0, 1],
        "endTime": [1, 0, 1],
        "positionX": 5.0,
        "someFutureField": {"a": [1, 2]},
        "另一未知": "值",
    }
    n = Note.from_json(raw)
    out = n.to_json()
    assert out["someFutureField"] == {"a": [1, 2]}
    assert out["另一未知"] == "值"
    # startTime 必须仍写成 [a,b,c] 三元组
    assert out["startTime"] == [1, 0, 1]


# ------------------------------------------------------------------ Event
@test
def event_roundtrip_keeps_easing_fields():
    e = Event(
        startTime=Beat(0),
        endTime=Beat(4),
        start=0.0,
        end=100.0,
        easingType=6,
        easingLeft=0.25,
        easingRight=0.75,
        linkgroup=3,
        bezier=1,
        bezierPoints=[0.1, 0.2, 0.3, 0.4],
    )
    out = e.to_json()
    assert out["easingType"] == 6
    approx(out["easingLeft"], 0.25)
    approx(out["easingRight"], 0.75)
    assert out["linkgroup"] == 3
    assert out["bezier"] == 1
    assert out["bezierPoints"] == [0.1, 0.2, 0.3, 0.4]
    back = Event.from_json(out)
    assert back.easingType == 6
    approx(back.easingLeft, 0.25)
    assert back.bezierPoints == [0.1, 0.2, 0.3, 0.4]


@test
def event_defaults_match_rpe():
    e = Event.from_json({"startTime": [0, 0, 1], "endTime": [1, 0, 1]})
    assert e.easingType == 1
    approx(e.easingLeft, 0.0)
    approx(e.easingRight, 1.0)
    assert e.linkgroup == 0
    assert e.bezier == 0
    assert e.bezierPoints == [0.0, 0.0, 0.0, 0.0]


@test
def event_color_and_text_are_detected():
    assert Event(start=[1, 2, 3], end=[4, 5, 6]).is_color
    assert not Event(start=1.0, end=2.0).is_color
    assert Event(start="hello", end="world").is_text
    assert not Event(start=1.0, end=2.0).is_text


@test
def event_color_roundtrip_keeps_int_triples():
    e = Event(startTime=Beat(0), endTime=Beat(2), start=[255, 0, 0], end=[0, 0, 255])
    out = e.to_json()
    assert out["start"] == [255, 0, 0]
    assert out["end"] == [0, 0, 255]
    back = Event.from_json(out)
    assert back.start == [255, 0, 0]


@test
def event_text_roundtrip_keeps_font_and_newlines():
    e = Event(
        startTime=Beat(0),
        endTime=Beat(2),
        start="第一行\\n第二行",
        end="结束 %P%",
        font="myfont.ttf",
    )
    out = e.to_json()
    assert out["font"] == "myfont.ttf"
    back = Event.from_json(out)
    assert back.font == "myfont.ttf"
    assert back.start == "第一行\\n第二行"
    # 没有 font 时不应写出该键（152+ 的默认字体不写）
    assert "font" not in Event(start="x", end="y").to_json()


@test
def event_easing_is_normalized_on_write():
    """整型 easingType 原样写回；只有字符串这类便捷输入才走规范化。

    真实谱面里存在 ``easingType = 0``（temmie 谱的 inclineEvents、若干 170 谱面），
    所以整型不能顺手改写 —— 规范化只发生在求值（``ease``）时。回退目标是
    **1（linear）而不是最近的边界**，对应 phira 的
    ``RPE_TWEEN_MAP.get(t.max(1)).unwrap_or(LINEAR)``。
    """
    # 整型一律原样保留
    assert Event(easingType=0).to_json()["easingType"] == 0
    assert Event(easingType=-5).to_json()["easingType"] == -5
    assert Event(easingType=999).to_json()["easingType"] == 999
    assert Event(easingType=6).to_json()["easingType"] == 6
    assert Event(easingType=29).to_json()["easingType"] == 29
    # 字符串名是便捷输入，写出合法 id
    assert Event(easingType="out_sine").to_json()["easingType"] == 2
    # 求值时才规范化：0 与 999 都按 linear 计算
    approx(ease(Event(easingType=0).easingType, 0.3), 0.3)
    approx(ease(Event(easingType=999).easingType, 0.3), 0.3)


@test
def event_unknown_fields_survive():
    e = Event.from_json(
        {"startTime": [0, 0, 1], "endTime": [1, 0, 1], "future": "keep", "n": 7}
    )
    out = e.to_json()
    assert out["future"] == "keep"
    assert out["n"] == 7


# ------------------------------------------------------------------ SpeedEvent
@test
def speed_event_writes_only_the_four_base_fields_by_default():
    """RPE 1.6.0 的流速事件只有 startTime/endTime/start/end 四个键。"""
    out = SpeedEvent(startTime=Beat(0), endTime=Beat(2), start=1.0, end=2.0).to_json()
    assert set(out) == {"startTime", "endTime", "start", "end"}


@test
def speed_event_has_no_bezier_fields_of_its_own():
    """流速事件从模型层面就不带 bezier/bezierPoints —— 不是「不写」而是「没有」。"""
    se = SpeedEvent()
    assert not hasattr(se, "bezier")
    assert not hasattr(se, "bezierPoints")
    # 本库新建的流速事件永远不产生这两个键
    out = SpeedEvent(startTime=Beat(0), endTime=Beat(2), start=2.0, end=1.0).to_json()
    assert "bezier" not in out and "bezierPoints" not in out


@test
def speed_event_preserves_foreign_unknown_fields():
    """别人文件里混进来的未知字段要原样带回去 —— 无损往返优先于「净化」。"""
    se = SpeedEvent.from_json(
        {"startTime": [0, 0, 1], "endTime": [1, 0, 1],
         "bezier": 1, "bezierPoints": [1, 2, 3, 4], "whatever": "x"}
    )
    out = se.to_json()
    assert out["bezier"] == 1
    assert out["bezierPoints"] == [1, 2, 3, 4]
    assert out["whatever"] == "x"


@test
def speed_event_writes_easing_only_when_non_default():
    out = SpeedEvent(easingType=6, easingLeft=0.2, easingRight=0.8).to_json()
    assert out["easingType"] == 6
    approx(out["easingLeft"], 0.2)
    approx(out["easingRight"], 0.8)
    # 默认值不写
    out2 = SpeedEvent().to_json()
    assert "easingType" not in out2
    assert "easingLeft" not in out2
    assert "easingRight" not in out2


@test
def speed_event_end_defaults_to_start():
    se = SpeedEvent.from_json({"startTime": [0, 0, 1], "endTime": [1, 0, 1], "start": 3.5})
    approx(se.start, 3.5)
    approx(se.end, 3.5)


# ------------------------------------------------------------------ EventLayer
@test
def empty_event_layer_is_dropped_from_json():
    """RPE 里「空层」等价于不存在；旧 Phira 解析器对空数组会崩，必须丢掉。"""
    assert EventLayer().to_json() is None
    assert EventLayer().is_empty()
    layer = EventLayer(moveXEvents=[Event(startTime=Beat(0), endTime=Beat(1))])
    assert not layer.is_empty()
    out = layer.to_json()
    assert "moveXEvents" in out
    assert "moveYEvents" not in out  # 无事件的种类不出键


@test
def event_layer_null_and_missing_are_both_fine():
    """旧版把空层写成 null，143+ 直接不写字段 —— 两种都要能读。"""
    assert EventLayer.from_json(None).is_empty()
    assert EventLayer.from_json({}).is_empty()
    assert EventLayer.from_json({"moveXEvents": []}).is_empty()


@test
def event_layer_roundtrip():
    layer = EventLayer(
        moveXEvents=[Event(startTime=Beat(0), endTime=Beat(4), start=0.0, end=100.0)],
        alphaEvents=[Event(startTime=Beat(0), endTime=Beat(4), start=255, end=0)],
        speedEvents=[SpeedEvent(startTime=Beat(0), endTime=Beat(4), start=1.0, end=2.0)],
    )
    out = layer.to_json()
    back = EventLayer.from_json(out)
    assert len(back.moveXEvents) == 1
    approx(back.moveXEvents[0].end, 100.0)
    assert len(back.alphaEvents) == 1
    assert back.alphaEvents[0].start == 255
    assert len(back.speedEvents) == 1
    approx(back.speedEvents[0].end, 2.0)


# ------------------------------------------------------------------ ExtendedEvents
@test
def extended_events_are_dropped_when_empty():
    assert ExtendedEvents().to_json() is None
    assert ExtendedEvents().is_empty()


@test
def extended_events_roundtrip_all_channels():
    ext = ExtendedEvents(
        scaleXEvents=[Event(startTime=Beat(0), endTime=Beat(1), start=1.0, end=2.0)],
        scaleYEvents=[Event(startTime=Beat(0), endTime=Beat(1), start=1.0, end=0.5)],
        colorEvents=[Event(startTime=Beat(0), endTime=Beat(1), start=[0, 0, 0], end=[255, 255, 255])],
        textEvents=[Event(startTime=Beat(0), endTime=Beat(1), start="a", end="b")],
        gifEvents=[Event(startTime=Beat(0), endTime=Beat(1), start=0.0, end=1.0)],
        inclineEvents=[Event(startTime=Beat(0), endTime=Beat(1), start=0.0, end=30.0)],
    )
    out = ext.to_json()
    for key in ("scaleXEvents", "scaleYEvents", "colorEvents", "textEvents", "gifEvents", "inclineEvents"):
        assert key in out, key
    back = ExtendedEvents.from_json(out)
    assert back.colorEvents[0].start == [0, 0, 0]
    assert back.textEvents[0].start == "a"


# ------------------------------------------------------------------ Controls
@test
def controls_use_per_type_value_keys():
    """五种 control 的关键帧数值键名各不相同（pos/size/alpha/y/skew）。"""
    assert Controls.VALUE_KEYS == {
        "alphaControl": "alpha",
        "sizeControl": "size",
        "posControl": "pos",
        "yControl": "y",
        "skewControl": "skew",
    }


@test
def controls_roundtrip_with_correct_key_names():
    ctrl = Controls(
        posControl=[ControlKeyframe(x=0.0, value=1.0, value_key="pos")],
        alphaControl=[ControlKeyframe(x=100.0, value=0.5, value_key="alpha", easing=2)],
        skewControl=[ControlKeyframe(x=0.0, value=1.0, value_key="skew")],
    )
    out = ctrl.to_json()
    assert out["posControl"][0]["pos"] == 1.0
    assert out["alphaControl"][0]["alpha"] == 0.5
    assert out["alphaControl"][0]["easing"] == 2
    assert out["skewControl"][0]["skew"] == 1.0
    assert "sizeControl" not in out
    back = Controls.from_json(out)
    approx(back.alphaControl[0].value, 0.5)
    assert back.alphaControl[0].value_key == "alpha"
    assert back.posControl[0].value_key == "pos"


@test
def controls_empty_is_none():
    assert Controls().to_json() is None
    assert Controls.from_json(None).alphaControl is None


@test
def control_keyframe_reads_the_type_specific_key():
    """关键帧里既可能叫 ``pos`` 也可能叫 ``value``，两种都要认。"""
    f = ControlKeyframe.from_json({"x": 10.0, "pos": 2.5, "easing": 3}, "pos")
    approx(f.value, 2.5)
    assert f.value_key == "pos"
    assert f.easing == 3
    # 回退到通用 ``value``
    f2 = ControlKeyframe.from_json({"x": 10.0, "value": 7.5}, "pos")
    approx(f2.value, 7.5)


# ------------------------------------------------------------------ JudgeLine
@test
def judge_line_defaults_match_rpe():
    ln = JudgeLine.from_json({})
    assert ln.Name == "Untitled"
    assert ln.Texture == "line.png"
    approx(ln.bpmfactor, 1.0)
    assert ln.father == -1
    assert ln.Group == 0
    assert ln.zOrder == 0
    assert ln.isCover == 1
    assert ln.anchor == [0.5, 0.5]
    assert ln.attachUI is None
    assert ln.isGif is False


@test
def judge_line_iscover_is_written_as_int_not_bool():
    """RPE 的 Bool 一律是 1/0，从不写 JSON 布尔字面量。"""
    out = JudgeLine(isCover=1).to_json()
    assert out["isCover"] == 1
    assert not isinstance(out["isCover"], bool)
    assert JudgeLine(isCover=0).to_json()["isCover"] == 0


@test
def judge_line_rotatewithfather_follows_presence_and_value():
    """163 之前缺该字段等于「不跟随父线旋转」，所以不能凭空补出默认键。

    读文件时缺字段按 **False** 处理（1.6.0 之前写出的谱面就是这个语义）；
    本库新建的判定线默认 True 并写出该键；载入过的只在「值为 True」或「原文件
    确实有该键」时写回 —— 否则 v113/v140 谱面会被塞进一个它没有的字段。
    """
    assert JudgeLine().to_json()["rotateWithFather"] is True
    assert JudgeLine(rotateWithFather=False).to_json()["rotateWithFather"] is False
    # 读到缺字段的旧文件：按 1.6.0 之前的行为解释为 False
    assert JudgeLine.from_json({}).rotateWithFather is False
    assert JudgeLine.from_json({"rotateWithFather": True}).rotateWithFather is True
    # 载入过的旧文件（原本没有该键）序列化时不能多出该键
    out = JudgeLine.from_json({"Group": 0}).to_json()
    assert "rotateWithFather" not in out
    # 原文件有这个键就原样保留
    out2 = JudgeLine.from_json({"Group": 0, "rotateWithFather": False}).to_json()
    assert out2["rotateWithFather"] is False


@test
def numofnotes_excludes_holds_and_includes_fakes():
    """RPE 口径：numOfNotes 含 FakeNote，但**不含 Hold**。"""
    ln = JudgeLine(
        notes=[
            Note(type=NOTE_TAP),          # 计入
            Note(type=NOTE_HOLD),         # 不计入
            Note(type=NOTE_DRAG, isFake=1),  # 计入（Fake 也算）
            Note(type=NOTE_FLICK),        # 计入
            Note(type=NOTE_HOLD, isFake=1),  # 不计入
        ]
    )
    assert ln.numOfNotes == 3
    # 真正的物量：排除 Fake 与 Hold，只剩 2
    assert ln.real_note_count == 2
    assert ln.to_json()["numOfNotes"] == 3


@test
def judge_line_note_sorting():
    """RPE 标准输出顺序：非 Hold 段（时间升序）在前，Hold 段（时间升序）在后。

    实测 v150+ 的真实谱面 190 行样本里 0 违例；纯时间升序则有 101 行违例。
    """
    ln = JudgeLine(
        notes=[
            Note(type=NOTE_HOLD, startTime=Beat(1)),
            Note(startTime=Beat(4)),
            Note(startTime=Beat(2)),
            Note(type=NOTE_HOLD, startTime=Beat(3)),
        ]
    )
    ln.sort()
    types = [n.type for n in ln.notes]
    assert types == [NOTE_TAP, NOTE_TAP, NOTE_HOLD, NOTE_HOLD]
    # 两段各自按时间升序
    assert [n.startTime.value for n in ln.notes] == [2.0, 4.0, 1.0, 3.0]


@test
def judge_line_new_layer_fills_gap():
    ln = JudgeLine()
    ln.new_layer(3)
    assert len(ln.eventLayers) == 4
    ln.new_layer(3)  # 已存在则不重复加
    assert len(ln.eventLayers) == 4
    ln.new_layer(6)
    assert len(ln.eventLayers) == 7


@test
def judge_line_omits_empty_layers_and_extended():
    out = JudgeLine().to_json()
    assert "eventLayers" not in out
    assert "extended" not in out
    # 程序化新建的线：空层被丢掉，只保留有内容的层
    ln = JudgeLine(eventLayers=[EventLayer(), EventLayer(moveXEvents=[Event()])])
    out = ln.to_json()
    assert len(out["eventLayers"]) == 1
    assert "moveXEvents" in out["eventLayers"][0]


@test
def judge_line_preserves_null_event_layer_slots():
    """真实谱面的 eventLayers 数组第 5 个元素常是 JSON null 占位（实测 88 处）。

    槽位本身是语义的一部分（RPE 用 5 元素数组表示「第 5 层为空」），
    载入过的必须原样还原，否则数组长度会从 5 缩成 4。
    """
    raw = {
        "Group": 0,
        "eventLayers": [
            {"moveXEvents": [{"start": 0.0, "end": 0.0}]},
            {"moveXEvents": [{"start": 1.0, "end": 1.0}]},
            None,
            {"moveXEvents": [{"start": 3.0, "end": 3.0}]},
            None,
        ],
    }
    ln = JudgeLine.from_json(raw)
    assert len(ln.eventLayers) == 5
    out = ln.to_json()
    assert len(out["eventLayers"]) == 5
    assert out["eventLayers"][2] is None
    assert out["eventLayers"][4] is None
    # 程序化新建的线不受影响：空层直接省略
    assert len(JudgeLine(eventLayers=[EventLayer(), EventLayer()]).to_json().get("eventLayers", [])) == 0


@test
def judge_line_anchor_only_written_when_non_default():
    assert "anchor" not in JudgeLine().to_json()
    assert JudgeLine(anchor=[0.0, 1.0]).to_json()["anchor"] == [0.0, 1.0]


@test
def judge_line_attachui_and_isgif_only_when_set():
    out = JudgeLine().to_json()
    assert "attachUI" not in out
    assert "isGif" not in out
    assert JudgeLine(attachUI="pause").to_json()["attachUI"] == "pause"
    assert JudgeLine(isGif=True).to_json()["isGif"] is True


@test
def judge_line_unknown_fields_survive():
    ln = JudgeLine.from_json({"Name": "x", "_id": 42, "weird": [1]})
    out = ln.to_json()
    assert out["_id"] == 42
    assert out["weird"] == [1]


# ------------------------------------------------------------------ Meta / Chart
@test
def meta_defaults_and_illustration():
    m = Meta()
    assert m.RPEVersion == 160
    assert m.id == "0"
    assert m.illustrator is None
    assert "illustration" not in m.to_json()
    assert Meta(illustrator="画师").to_json()["illustration"] == "画师"


@test
def meta_duration_is_kept_only_when_the_file_had_it():
    """``duration`` 是 RPE 1.5.0+ 的键；本库默认不写，载入过的原样保留。

    实测依据：24 份语料里 v113/v140 的 META 没有这个键、v150/160/170 都有；
    而 RPE 1.6.0 自己保存的 AutoSave 里刚新建的谱面写的是 ``"duration": 0.0``。
    """
    # 新建：不写
    assert Meta().duration is None
    assert "duration" not in Meta().to_json()
    assert "duration" not in Meta(duration=None).to_json()

    # 显式给值：写
    approx(Meta(duration=93.466).to_json()["duration"], 93.466)

    # 载入带 duration 的文件：保留（含 0.0 —— RPE 自己就会写 0.0）
    m = Meta.from_json({"RPEVersion": 160, "duration": 0.0})
    assert m.duration is not None
    approx(m.duration, 0.0)
    assert "duration" in m.to_json()

    m2 = Meta.from_json({"RPEVersion": 160, "duration": 130.703995})
    approx(m2.to_json()["duration"], 130.703995)

    # 载入没有 duration 的旧文件（v113/v140）：不凭空补出来
    m3 = Meta.from_json({"RPEVersion": 140, "name": "x"})
    assert m3.duration is None
    assert "duration" not in m3.to_json()


@test
def meta_foreign_keys_stay_in_extra():
    """``Malody2Phigros`` 之类的转换器键必须无损透传（不能进 known 被吞掉）。"""
    m = Meta.from_json({"RPEVersion": 140, "Malody2Phigros": 1, "name": "n"})
    out = m.to_json()
    assert out["Malody2Phigros"] == 1
    assert out["name"] == "n"


@test
def chart_top_level_keys_match_rpe():
    c = Chart()
    out = c.to_json()
    assert "BPMList" in out
    assert "META" in out
    assert "judgeLineList" in out
    assert out["META"]["RPEVersion"] == 160
    assert len(out["BPMList"]) == 1
    approx(out["BPMList"][0]["bpm"], 120.0)
    assert out["BPMList"][0]["startTime"] == [0, 0, 1]


@test
def chart_roundtrip_is_stable():
    """to_json -> from_json -> to_json 必须完全一致（幂等）。"""
    c = Chart(
        BPMList=[BPMItem(bpm=174.0, startTime=Beat(0)), BPMItem(bpm=87.0, startTime=Beat(32))],
        META=Meta(name="测试曲", charter="我", composer="他", level="IN 15", id="7"),
        judgeLineList=[
            JudgeLine(
                Name="L0",
                notes=[Note(type=NOTE_TAP, startTime=Beat(0), positionX=100.0),
                       Note(type=NOTE_HOLD, startTime=Beat(1), endTime=Beat(3))],
                eventLayers=[EventLayer(moveXEvents=[Event(startTime=Beat(0), endTime=Beat(4), start=0.0, end=200.0)])],
            )
        ],
        timeTags=[TimeTag(name="副歌", time=Beat(16))],
    )
    c.sort()
    once = c.to_json()
    twice = Chart.from_json(once).to_json()
    assert once == twice


@test
def chart_roundtrip_is_stable_for_a_plain_loadable_file():
    """连「RPE 自己写出的最小文件」也要能无损往返，包括缺 eventLayers 的情形。"""
    raw = {
        "BPMList": [{"bpm": 174.0, "startTime": [0, 0, 1]}],
        "META": {"RPEVersion": 160, "offset": 0, "name": "n", "id": "3",
                 "song": "song.mp3", "background": "bg.png",
                 "composer": "c", "charter": "ch", "level": "IN 14"},
        "chartTime": 90.0,
        "judgeLineGroup": ["Default"],
        "judgeLineList": [
            {"Group": 0, "Name": "Untitled", "Texture": "line.png",
             "father": -1, "isCover": 1,
             "notes": [{"above": 1, "alpha": 255, "endTime": [1, 0, 1], "isFake": 0,
                        "positionX": 0.0, "size": 1.0, "speed": 1.0,
                        "startTime": [1, 0, 1], "type": 1,
                        "visibleTime": 999999.0, "yOffset": 0.0}],
             "numOfNotes": 1, "zOrder": 0, "bpmfactor": 1.0},
        ],
        "multiLineString": "", "multiScale": 1.0, "xybind": True,
        "timeTags": [],
    }
    once = Chart.from_json(raw).to_json()
    twice = Chart.from_json(once).to_json()
    assert once == twice


@test
def chart_defaults_to_having_a_default_judge_line_group():
    """``judgeLineGroup`` 是「分组名数组」，每条线的 ``Group`` 是它的**下标**。

    实测依据：26 份真实谱面里 ``judgeLineGroup`` **没有一份是空数组**
    （长度分布 1→23、2→1、5→1），而且 RPE 1.6.0 自己改写谱面时会补上
    ``["Default"]``。若本库写成 ``[]``，那么每条线默认的 ``Group=0`` 就指向一个
    不存在的组名 —— 判定线管理里按分组筛选会一条线都列不出来。
    """
    assert Chart().judgeLineGroup == ["Default"]
    assert Chart().to_json()["judgeLineGroup"] == ["Default"]

    # 线的默认分组下标 0 必须落在默认组数组之内
    c = Chart()
    c.add_line()
    for line in c.judgeLineList:
        assert 0 <= line.Group < len(c.judgeLineGroup)


@test
def chart_preserves_an_explicitly_empty_group_list():
    """载入的文件本来就没有组名，不许替它凭空补 —— 保真优先于美观。"""
    c = Chart.from_json({"judgeLineGroup": [], "judgeLineList": []})
    assert c.judgeLineGroup == []
    assert c.to_json()["judgeLineGroup"] == []


@test
def chart_bpm_list_is_sorted_on_load():
    raw = {
        "BPMList": [
            {"bpm": 200.0, "startTime": [32, 0, 1]},
            {"bpm": 100.0, "startTime": [0, 0, 1]},
        ],
        "META": {"RPEVersion": 160},
        "judgeLineList": [],
    }
    c = Chart.from_json(raw)
    assert [b.bpm for b in c.BPMList] == [100.0, 200.0]


@test
def chart_counts_add_up():
    c = Chart(
        judgeLineList=[
            JudgeLine(notes=[Note(type=NOTE_TAP), Note(type=NOTE_HOLD), Note(isFake=1)]),
            JudgeLine(notes=[Note(type=NOTE_FLICK), Note(type=NOTE_HOLD)]),
        ]
    )
    assert c.note_count == 3   # 三条 Hold 之外的音符（含 1 个 Fake）
    assert c.hold_count == 2
    assert c.fake_count == 1


@test
def chart_duration_includes_notes_and_events():
    c = Chart(
        judgeLineList=[
            JudgeLine(
                notes=[Note(type=NOTE_HOLD, startTime=Beat(0), endTime=Beat(20))],
                eventLayers=[EventLayer(moveXEvents=[Event(startTime=Beat(0), endTime=Beat(30))])],
            )
        ]
    )
    assert c.duration_beats() == Beat(30)


@test
def chart_reads_unknown_top_level_keys():
    c = Chart.from_json({"judgeLineList": [], "_custom": 5, "multiScale": 2.5})
    out = c.to_json()
    assert out["_custom"] == 5
    approx(out["multiScale"], 2.5)


@test
def chart_empty_bpmlist_is_preserved_for_validation():
    """空 BPMList 不能被悄悄补成默认值 —— 校验器需要看见它并报错。"""
    c = Chart.from_json({"BPMList": [], "META": {}, "judgeLineList": []})
    assert c.BPMList == []


@test
def event_kind_constants_match_rpe_keys():
    assert EVENT_MOVE_X == "moveXEvents"
    assert EVENT_ALPHA == "alphaEvents"
    assert EVENT_SPEED == "speedEvents"
