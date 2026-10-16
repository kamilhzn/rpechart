"""BPM / 秒 / 拍 换算测试。"""

from run_tests import approx, test

from rpechart.beat import Beat
from rpechart.model import BPMItem
from rpechart.timing import Timing, beat_to_second, build_bpm_list, second_to_beat


@test
def single_bpm_basic():
    t = Timing([(120, 0)])
    # 120 BPM => 一拍 0.5 秒
    approx(t.beat_to_second(4), 2.0)
    approx(t.beat_to_second(1), 0.5)
    approx(t.beat_to_second(0), 0.0)
    assert t.second_to_beat(2.0) == Beat(4)
    assert t.second_to_beat(0.5) == Beat(1)
    approx(t.seconds_per_beat(0), 0.5)


@test
def multi_bpm_分段累加():
    # 0~4 拍 @120BPM (0.5s/拍)，4 拍之后 @240BPM (0.25s/拍)
    t = Timing([(120, 0), (240, 4)])
    approx(t.beat_to_second(4), 2.0)
    approx(t.beat_to_second(8), 2.0 + 1.0)
    approx(t.beat_to_second(2), 1.0)
    assert t.second_to_beat(2.0) == Beat(4)
    assert t.second_to_beat(3.0) == Beat(8)
    assert t.second_to_beat(2.5) == Beat(6)


@test
def roundtrip_is_stable():
    t = Timing([(174, 0), (87, 16), (200, 24)])
    for beat in (0, 1, "0:1/3", 7.25, 16, 23.999, 100, "33:2/5"):
        sec = t.beat_to_second(beat)
        back = t.second_to_beat(sec)
        approx(back.value, Beat.of(beat).value, 1e-9, f"beat={beat}")


@test
def bpmfactor_divides_bpm():
    # 判定线 bpmfactor=2 时该线实际 BPM = nowBpm / 2 => 一拍变两倍长
    base = Timing([(120, 0)])
    slow = Timing([(120, 0)], bpmfactor=2.0)
    approx(slow.beat_to_second(4), 4.0)
    approx(base.beat_to_second(4), 2.0)
    approx(slow.effective_bpm, 60.0)
    approx(base.effective_bpm, 120.0)


@test
def helper_functions_match_class():
    bpm_list = [BPMItem(bpm=150, startTime=Beat(0)), BPMItem(bpm=180, startTime=Beat(8))]
    t = Timing(bpm_list)
    for beat in (0, 3, 8, 15.5):
        approx(beat_to_second(beat, bpm_list), t.beat_to_second(beat))
        assert second_to_beat(beat_to_second(beat, bpm_list), bpm_list) == t.second_to_beat(
            t.beat_to_second(beat)
        )


@test
def build_bpm_list_normalises():
    out = build_bpm_list([(180, "8:0/1"), (120, 0)])
    assert out[0].startTime == Beat(0)
    assert out[0].bpm == 120
    assert out[1].startTime == Beat(8)
    # 首段不从 0 开始时补一段
    out2 = build_bpm_list([(150, 4)])
    assert out2[0].startTime == Beat(0)
    assert out2[0].bpm == 150
    # 空输入给默认 140
    assert build_bpm_list([])[0].bpm == 140


@test
def zero_and_negative_clamp():
    t = Timing([(120, 0)])
    approx(t.beat_to_second(-5), 0.0)
    assert t.second_to_beat(-1.0) == Beat(0)
