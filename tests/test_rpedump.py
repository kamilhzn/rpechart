"""rpedump.py —— RPE 自己的 JSON 写出风格。

核心断言是**逐字节相同**：把样本谱面用 ``json.loads`` 读进来，再用
:func:`rpechart.rpedump.dumps` 写回去，必须一个字都不差。

样本在 ``tests/fixtures/``（``make_fixtures.py`` 用本库自己生成，不含第三方素材）：
``demo.pez`` 覆盖常规写出，``charts/legacy.json`` 专门覆盖 ``-0.0`` 与整数
``alphaEvents`` 这两个最容易回归的坑。样本不在时跳过。
"""

import json
import os
import zipfile

from run_tests import test

from rpechart.model import Chart
from rpechart.rpedump import dumps, dumps_chart

_FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "tests", "fixtures")
_SAMPLE_PEZ = os.path.join(_FIXTURES, "demo.pez")
_LEGACY_JSON = os.path.join(_FIXTURES, "charts", "legacy.json")


def _json_of_pez(path):
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        names.sort(key=lambda n: (n.count("/"), len(n)))
        return zf.read(names[0]).decode("utf-8")


# ------------------------------------------------------------------ 基本规则
@test
def indent_is_three_spaces_and_colon_has_spaces_around_it():
    """RPE 是 3 空格缩进 + ``"key" : value``，不是 json.dumps(indent=2)。"""
    text = dumps({"a": 1, "b": {"c": 2}})
    assert text == '{\n   "a" : 1,\n   "b" : {\n      "c" : 2\n   }\n}'


@test
def object_keys_are_sorted_by_byte_order():
    """RapidJSON 用 std::map 存对象，键按字节序，与插入顺序无关。"""
    assert dumps({"b": 1, "a": 2, "A": 3}) == '{\n   "A" : 3,\n   "a" : 2,\n   "b" : 1\n}'


@test
def scalar_arrays_are_inlined_but_nested_ones_are_not():
    """kFormatSingleLineArray：纯标量数组压成一行；含对象的数组逐行展开。"""
    assert dumps([0, 0, 1]) == "[ 0, 0, 1 ]"
    assert dumps(["Default"]) == '[ "Default" ]'
    assert dumps([]) == "[]"
    assert dumps({}) == "{}"
    nested = dumps([{"a": 1}])
    assert nested == '[\n   {\n      "a" : 1\n   }\n]'


@test
def floats_use_seventeen_significant_digits():
    """RPE 的浮点是 ``%.17g``；整数值的浮点要补 ``.0``。"""
    assert dumps(0.8) == "0.80000000000000004"
    assert dumps(148.119995) == "148.11999499999999"
    assert dumps(153.6) == "153.59999999999999"
    assert dumps(1.0) == "1.0"
    assert dumps(0.0) == "0.0"
    assert dumps(3) == "3"
    assert dumps(True) == "true"
    assert dumps(None) == "null"


@test
def negative_zero_keeps_its_sign():
    """``-0.0`` 在真实谱面里很常见，不能写成 ``0.0``。

    ``'%.17g' % -0.0`` 得到的是 ``'-0'``，直接补 ``.0`` 会变成 ``-0.0`` ——
    看起来对，但如果实现里先做了 ``float(raw or 0.0)`` 就已经丢了符号。
    """
    assert dumps(-0.0) == "-0.0"
    assert dumps([-0.0, 0.0]) == "[ -0.0, 0.0 ]"


@test
def dumps_chart_appends_exactly_one_trailing_newline():
    """RPE 的 chart.json 末尾恰好一个 LF。"""
    text = dumps_chart(Chart())
    assert text.endswith("}\n")
    assert not text.endswith("\n\n")
    assert "\r" not in text


# ------------------------------------------------------------------ 字节级保真
def _byte_equal_case(label, loader):
    def run():
        raw = loader()
        if raw is None:
            return  # 样本缺失 → 跳过
        obj = json.loads(raw)
        assert dumps(obj) + "\n" == raw, f"{label}: 序列化后与原文不逐字节相同"

    return run


def _load_demo_pez():
    if not os.path.isfile(_SAMPLE_PEZ):
        return None
    return _json_of_pez(_SAMPLE_PEZ)


def _load_legacy_json():
    """老版本（RPEVersion 113）样本：整数 alphaEvents + ``-0.0`` + null 层占位。"""
    if not os.path.isfile(_LEGACY_JSON):
        return None
    with open(_LEGACY_JSON, "r", encoding="utf-8") as fh:
        return fh.read()


_demo = _byte_equal_case("demo.pez", _load_demo_pez)
_demo.__name__ = "demo_pez_chart_json_is_byte_identical"
test(_demo)

_legacy = _byte_equal_case("legacy.json", _load_legacy_json)
_legacy.__name__ = "legacy_chart_json_is_byte_identical"
test(_legacy)


@test
def model_roundtrip_of_a_fixture_pez_is_byte_identical():
    """load → to_json → dumps 必须与 .pez 里的原文逐字节相同。

    这条把「模型侧类型保真」与「写出器」一起钉住：只要 alphaEvents 的整数化或
    ``-0.0`` 的保留任一处回归，字节就会不同。
    """
    raw = _load_demo_pez()
    if raw is None:
        return
    chart = Chart.from_json(json.loads(raw))
    assert dumps(chart.to_json()) + "\n" == raw


@test
def model_roundtrip_of_the_legacy_fixture_is_byte_identical():
    """老版本样本（整数 alphaEvents + ``-0.0`` + null 层占位）也要逐字节复刻。"""
    raw = _load_legacy_json()
    if raw is None:
        return
    chart = Chart.from_json(json.loads(raw))
    assert dumps(chart.to_json()) + "\n" == raw


@test
def alpha_event_values_are_written_as_integers():
    """RPE 的 alphaEvents/颜色事件的 start/end 恒为 int（实测 21603/21603）。"""
    from rpechart.beat import Beat
    from rpechart.model import EVENT_ALPHA, Event, EventLayer

    layer = EventLayer(
        alphaEvents=[Event(Beat(0), Beat(1), 0.0, 255.0), Event(Beat(1), Beat(2), 255.0, 0.0)]
    )
    data = layer.to_json()
    for event in data[EVENT_ALPHA]:
        assert isinstance(event["start"], int), event
        assert isinstance(event["end"], int), event


@test
def negative_zero_survives_the_model():
    """``-0.0`` 经过 Event.from_json 不能变成 ``0.0``。"""
    from rpechart.model import Event

    event = Event.from_json({
        "startTime": [0, 0, 1],
        "endTime": [1, 0, 1],
        "start": -0.0,
        "end": -0.0,
    })
    assert str(event.start) == "-0.0", event.start
    assert str(event.end) == "-0.0", event.end
