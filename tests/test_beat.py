"""拍号 Beat 的精确性测试。"""

from run_tests import approx, raises, test

from rpechart.beat import Beat


@test
def beat_from_various_types():
    assert Beat(2).value == 2.0
    assert Beat(2.5).value == 2.5
    assert Beat("3:1/4").value == 3.25
    assert Beat([3, 1, 4]).value == 3.25
    assert Beat(Beat(1.5)).value == 1.5


@test
def beat_roundtrip_list():
    for text in ("0", "1", "2:1/4", "3:2/3", "10:0/1", "0:1/3"):
        b = Beat.parse(text)
        a, n, d = b.to_list()
        assert Beat([a, n, d]) == b, text


@test
def beat_zero_denominator_is_one():
    # c=0 且 b=0 时按分母 1 处理（phira-docs）
    assert Beat([3, 0, 0]).value == 3.0
    raises(ValueError, Beat, [3, 1, 0])


@test
def beat_is_exact():
    # 1/3 拍连加 3 次应该精确等于 1，不能有浮点误差
    third = Beat("0:1/3")
    total = third + third + third
    assert total == Beat(1), f"{total!r} != Beat(1)"
    assert total.to_list() == [1, 0, 1]
    # 三连音：1/3 * 3
    assert Beat("0:1/3") * 3 == Beat(1)


@test
def beat_arithmetic():
    assert Beat(2) + Beat("0:1/2") == Beat("2:1/2")
    assert Beat(3) - 1 == Beat(2)
    assert 1 - Beat("0:1/4") == Beat("0:3/4")
    assert Beat(4) / 2 == Beat(2)
    assert -Beat(1) == Beat(-1)
    assert Beat(1) < Beat(2) <= Beat(2)
    assert Beat(3) > Beat("2:1/2")


@test
def beat_string_form():
    assert str(Beat(2)) == "2"
    assert str(Beat("2:1/4")) == "2:1/4"
    assert "1/3" in repr(Beat("0:1/3"))
