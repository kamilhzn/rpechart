"""SE 打击音混音测试 —— 把 PhiEdit 官方打击音叠到导出的音轨上。

分三层，尽量不依赖外部素材：

* 纯函数：hitsound_schedule 的音符筛选 / 类型映射 / 偏移 / 去重；
* 假 ffmpeg：把 argv 记下来、把固定 PCM 打到 stdout —— 于是可以**逐样本**断言
  叠加结果、饱和夹紧、窗口裁剪与素材缺失时的退化；
* 真 ffmpeg smoke：只有 ffmpeg_available() 为真、且本机真的能找到官方 SE
  目录时才跑（否则 return 跳过，不算失败）。

素材路径一律从环境变量 RPECHART_SE_DIR 或用户目录推出来，不把绝对路径写死在
代码里（tests/test_packaging.py 会用 AST 扫这类字面量）。
"""

import fnmatch
import json
import os
import struct
import subprocess
import sys
import tempfile
import wave

from run_tests import test, approx, raises

from rpechart import BPMItem, Beat, Chart, JudgeLine, Note
from rpechart import audio as A
from rpechart import video as V

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ---------------------------------------------------------------- 测试替身
#: 假 ffmpeg：
#:   * 每次调用都把 argv 记进 argv.json；
#:   * 看到 "-f s16le ... -" 就当解码请求，把 <here>/pcm/<素材名>.raw 打到 stdout；
#:   * 其它情况当编码请求，写一个假文件；
#:   * mode.txt 里的 fail / partial 让它按指定方式失败。
FAKE_FFMPEG_SRC = """\
import json, os, sys

here = os.path.dirname(os.path.abspath(__file__))
argv = sys.argv[1:]
with open(os.path.join(here, "argv.json"), "w", encoding="utf-8") as fh:
    json.dump(argv, fh)
with open(os.path.join(here, "argvs.jsonl"), "a", encoding="utf-8") as fh:
    print(json.dumps(argv), file=fh)

mode = "ok"
mode_path = os.path.join(here, "mode.txt")
if os.path.exists(mode_path):
    with open(mode_path) as fh:
        mode = fh.read().strip()

def boom(what):
    print("fake-ffmpeg: " + what, file=sys.stderr)
    sys.exit(1)

if mode == "fail":
    boom("Unknown encoder")

if "s16le" in argv and argv[-1] == "-":
    src = argv[argv.index("-i") + 1]
    raw = os.path.join(here, "pcm", os.path.basename(src) + ".raw")
    if not os.path.isfile(raw):
        boom("no pcm fixture for " + src)
    with open(raw, "rb") as fh:
        data = fh.read()
    if mode == "partial":
        sys.stdout.buffer.write(data[:max(2, len(data) // 2)])
        sys.stdout.buffer.flush()
        boom("half decoded then fail")
    sys.stdout.buffer.write(data)
    sys.exit(0)

with open(argv[-1], "wb") as fh:
    fh.write(b"FAKEWAV")
"""

def _fake_ffmpeg(td, mode="ok"):
    """在 td 里造一个可以当 ffmpeg 用的可执行文件，返回它的路径。"""
    script = os.path.join(td, "fake_ffmpeg.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(FAKE_FFMPEG_SRC)
    with open(os.path.join(td, "mode.txt"), "w", encoding="utf-8") as fh:
        fh.write(mode)
    if os.name == "nt":
        exe = os.path.join(td, "ffmpeg.cmd")
        with open(exe, "w", encoding="utf-8") as fh:
            fh.write('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, script))
    else:
        exe = os.path.join(td, "ffmpeg")
        with open(exe, "w", encoding="utf-8") as fh:
            fh.write("#!%s\n" % sys.executable)
            fh.write(FAKE_FFMPEG_SRC)
        os.chmod(exe, 0o755)
    return exe

def _argv(td):
    """假 ffmpeg **最后一次**收到的 argv（没有调用过则 None）。"""
    path = os.path.join(td, "argv.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _argvs(td):
    """假 ffmpeg 收到过的**全部** argv（按调用顺序）。"""
    path = os.path.join(td, "argvs.jsonl")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _pcm(td, name, samples):
    """给假 ffmpeg 造素材：samples 是交织立体声的采样列表。"""
    d = os.path.join(td, "pcm")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name + ".raw"), "wb") as fh:
        fh.write(struct.pack("<%dh" % len(samples), *samples))

def _pcm_const(td, name, frames, value):
    _pcm(td, name, [value] * (2 * frames))

def _write_wav(path, seconds, rate=24000, value=0):
    n = int(seconds * rate)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<%dh" % (2 * n), *([value] * (2 * n))))

def _fake_se(td, names=("tap", "flick", "drag")):
    """假 SE 目录：文件名是真名，内容是静音 WAV（探测走魔数嗅探）。"""
    d = os.path.join(td, "SE")
    os.makedirs(d, exist_ok=True)
    for n in names:
        _write_wav(os.path.join(d, n + ".mp3"), 0.1)
    return d

def _note(t, type=1, isFake=0, hitsound=None):
    return Note(type=type, startTime=Beat(t), endTime=Beat(t),
                isFake=isFake, hitsound=hitsound)

def _chart(notes, bpm=120.0):
    return Chart(BPMList=[BPMItem(bpm=bpm, startTime=Beat(0))],
                 judgeLineList=[JudgeLine(Name="L", notes=list(notes))])

def _write_chart(td, chart, name="chart.json"):
    path = os.path.join(td, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(chart.to_json(), fh, ensure_ascii=False)
    return path

def _read_wav(path):
    with wave.open(path, "rb") as w:
        frames = w.getnframes()
        data = w.readframes(frames)
        return frames, struct.unpack("<%dh" % (len(data) // 2), data)

def _real_se_dir():
    """本机官方 SE 目录；找不到返回 None（调用方跳过而不是失败）。"""
    cands = []
    env = os.environ.get("RPECHART_SE_DIR")
    if env:
        cands.append(env)
    try:
        from rpechart.render.assets import find_resources
        res = find_resources()
        if res is not None:
            cands.append(os.path.join(str(res), "SE"))
    except Exception:
        pass
    cands.append(os.path.join(os.path.expanduser("~"), "Phi", "PhiEdit", "Resources", "SE"))
    for c in cands:
        if c and os.path.isdir(c):
            return c
    return None

def _tmpdir(name):
    return tempfile.mkdtemp(prefix="se_audio_" + name + "_")

# ---------------------------------------------------------------- 纯函数：时间表


@test
def hitsound_schedule_maps_note_types_and_skips_fake():
    """Tap/Hold/Flick/Drag 各自映射到对应 SE；Hold 用头部；Fake 完全不出声。"""
    ch = _chart([_note(0, 1), _note(1, 2), _note(2, 3), _note(3, 4), _note(4, 1, isFake=1)])
    sched = A.hitsound_schedule(ch)
    assert [k for _, k in sched] == ["tap", "tap", "flick", "drag"], sched
    approx(sched[0][0], 0.0)
    approx(sched[1][0], 0.5)
    approx(sched[2][0], 1.0)
    approx(sched[3][0], 1.47)          # 3 拍 = 1.5s，Drag 默认 -30ms


@test
def hitsound_schedule_defaults_match_the_phiedit_settings():
    """默认值来自 PhiEdit 的 Settings.json：SEVolume 0.532、SEOffset [0, -30]。"""
    approx(A.SE_VOLUME, 0.532, 1e-9)
    approx(A.SE_OFFSET_TAP, 0.0)
    approx(A.SE_OFFSET_DRAG, -0.030)
    assert A.SE_BY_NOTE_TYPE[1] == "tap"
    assert A.SE_BY_NOTE_TYPE[2] == "tap"     # Hold 的打击音在头部
    assert A.SE_BY_NOTE_TYPE[3] == "flick"
    assert A.SE_BY_NOTE_TYPE[4] == "drag"
    assert A.SE_KINDS == ("tap", "flick", "drag")
    assert A.SE_SAMPLE_RATE == 48000 and A.SE_CHANNELS == 2


@test
def hitsound_schedule_accepts_overrides_and_a_note_level_hitsound():
    """offset 可以覆盖；音符自带 hitsound 时以它为准。"""
    ch = _chart([_note(1, 2), _note(2, 4), _note(3, 1, hitsound="flick")])
    sched = A.hitsound_schedule(ch, offset_tap=0.1, offset_drag=-0.2)
    assert [k for _, k in sched] == ["tap", "drag", "flick"], sched
    approx(sched[0][0], 0.5 + 0.1)     # Hold 头部 + Tap 偏移
    approx(sched[1][0], 1.0 - 0.2)     # Drag 偏移
    approx(sched[2][0], 1.5 + 0.1)     # 命中 hitsound="flick"：走 Tap/Flick 的 offset1


@test
def hitsound_schedule_takes_a_chart_path_and_sorts_by_time():
    """可以接谱面路径；结果按时间升序。"""
    td = _tmpdir("sched")
    path = _write_chart(td, _chart([_note(4, 1), _note(0, 1), _note(2, 3)]))
    sched = A.hitsound_schedule(path)
    ts = [t for t, _ in sched]
    assert ts == sorted(ts), ts
    approx(ts[0], 0.0)
    approx(ts[-1], 2.0)


@test
def hitsound_schedule_window_is_half_open():
    """窗口是 [start, end)：起点含、终点不含。"""
    ch = _chart([_note(0, 1), _note(2, 1), _note(4, 1), _note(6, 1)])   # 0/1/2/3 秒
    sched = A.hitsound_schedule(ch, start=1.0, end=3.0)
    assert [round(t, 6) for t, _ in sched] == [1.0, 2.0], sched


@test
def hitsound_schedule_dedups_the_same_kind_in_the_same_millisecond():
    """去重是**可选的**：默认每个音符各响一次，显式给 dedup_ms 才合并同类。"""
    ch = _chart([_note(0, 1), _note(0, 1), _note(0, 3), _note(2, 1)])
    raw = A.hitsound_schedule(ch)                     # 默认 dedup_ms=0
    assert [(round(t, 6), k) for t, k in raw] == [(0.0, "tap"), (0.0, "tap"),
                                                  (0.0, "flick"), (1.0, "tap")], raw
    merged = A.hitsound_schedule(ch, dedup_ms=1.0)
    assert [(round(t, 6), k) for t, k in merged] == [(0.0, "tap"), (0.0, "flick"),
                                                     (1.0, "tap")], merged


@test
def hitsound_schedule_caps_the_event_table():
    """事件表有硬上限：超出时按时间均匀抽稀，绝不会无限膨胀。"""
    ch = _chart([_note(i, 1) for i in range(400)])
    sched = A.hitsound_schedule(ch, max_events=10)
    assert len(sched) == 10, len(sched)
    ts = [t for t, _ in sched]
    assert ts == sorted(ts), ts


@test
def resolve_se_sources_finds_a_directory_and_honours_a_mapping():
    """目录里按名字找 SE；显式映射优先，缺文件的那类直接缺席。"""
    td = _tmpdir("resolve")
    se = _fake_se(td, names=("tap", "flick"))
    got = A.resolve_se_sources(se)
    assert set(got) == {"tap", "flick"}, got
    assert str(got["tap"]).endswith("tap.mp3")
    custom = os.path.join(td, "mine.wav")
    _write_wav(custom, 0.1)
    got2 = A.resolve_se_sources(se, {"drag": custom})
    assert set(got2) == {"tap", "flick", "drag"}, got2
    assert str(got2["drag"]) == custom
    assert A.resolve_se_sources(None) == {}
    assert A.resolve_se_sources(os.path.join(td, "nope")) == {}

# ---------------------------------------------------------------- 假 ffmpeg：逐样本


@test
def build_hitsound_track_writes_a_silent_track_when_there_are_no_notes():
    """没有音符也不给原曲时不调 ffmpeg，直接写静音 WAV（长 = 目标时长）。"""
    td = _tmpdir("silent")
    exe = _fake_ffmpeg(td)
    path = _write_chart(td, _chart([]))
    out = os.path.join(td, "silent.wav")
    got = A.build_hitsound_track(path, se_dir=_fake_se(td), out_path=out,
                                 duration=0.25, ffmpeg=exe)
    assert os.path.isfile(got), got
    assert not os.path.isfile(os.path.join(td, "argv.json")), "无音符时不该调 ffmpeg"
    frames, data = _read_wav(got)
    with wave.open(got, "rb") as w:
        assert w.getnchannels() == 2
        approx(w.getframerate(), 48000.0, 0.5)
    assert frames >= int(0.25 * 48000)
    assert not any(data), "静音轨必须全零"


@test
def build_hitsound_track_decodes_each_se_once_and_mixes_at_the_right_offset():
    """三类 SE 各解一次（走 stdout 的 s16le），叠加位置 = 音符时间 - start。"""
    td = _tmpdir("mix")
    exe = _fake_ffmpeg(td)
    se = _fake_se(td)
    _pcm_const(td, "tap.mp3", 10, 100)      # 每素材固定 10 帧
    _pcm_const(td, "flick.mp3", 10, 200)
    _pcm_const(td, "drag.mp3", 10, 400)
    ch = _chart([_note(0, 1), _note(1, 3), _note(2, 4)])   # 0 / 0.5s / 1.0s(-30ms)
    path = _write_chart(td, ch)
    out = os.path.join(td, "mix.wav")
    A.build_hitsound_track(path, se_dir=se, out_path=out, duration=1.0, ffmpeg=exe)
    frames, data = _read_wav(out)
    approx(frames, 48000, 1)
    assert data[0] == 100, data[:4]                       # tap 从第 0 帧铺 10 帧
    assert data[2 * 24000] == 200, data[2 * 24000]         # flick 0.5s -> 第 24000 帧
    drag_frame = int(round((1.0 - 0.03) * 48000))
    assert data[2 * drag_frame] == 400, data[2 * drag_frame]
    assert data[2 * (drag_frame - 1)] == 0, "SE 之前必须是静音"
    # 每个素材恰好解一次，全部走 stdout 的原始 PCM
    argvs = _argvs(td)
    assert len(argvs) == 3, argvs
    decoded = sorted(os.path.basename(a[a.index("-i") + 1]) for a in argvs)
    assert decoded == ["drag.mp3", "flick.mp3", "tap.mp3"], decoded
    for a in argvs:
        assert a[-1] == "-", a
        assert a[a.index("-f") + 1] == "s16le", a
        assert a[a.index("-ar") + 1] == "48000", a
        assert a[a.index("-ac") + 1] == "2", a
        assert a[a.index("-af") + 1] == "volume=0.532", a


@test
def build_hitsound_track_saturates_instead_of_wrapping():
    """同帧叠加必须饱和夹紧：正负都不能回绕（array 直接加会 OverflowError）。"""
    td = _tmpdir("sat")
    exe = _fake_ffmpeg(td)
    se = _fake_se(td)
    _pcm_const(td, "tap.mp3", 8, 30000)
    # 同一拍两个 Tap（两条判定线 -> 都保留），叠加后必须夹到 32767
    ch = Chart(BPMList=[BPMItem(bpm=120.0, startTime=Beat(0))],
               judgeLineList=[JudgeLine(Name="A", notes=[_note(0, 1)]),
                              JudgeLine(Name="B", notes=[_note(0, 1)])])
    path = _write_chart(td, ch)
    out = os.path.join(td, "sat.wav")
    A.build_hitsound_track(path, se_dir=se, out_path=out, duration=0.1, ffmpeg=exe)
    _, data = _read_wav(out)
    assert data[0] == 32767, data[:4]                      # 30000 + 30000 -> 夹紧
    assert data[7] == 32767, data[:8]                      # 整段都夹紧
    # 负向同样夹紧
    td2 = _tmpdir("satneg")
    exe2 = _fake_ffmpeg(td2)
    se2 = _fake_se(td2)
    _pcm_const(td2, "tap.mp3", 8, -30000)
    path2 = _write_chart(td2, ch)
    out2 = os.path.join(td2, "sat.wav")
    A.build_hitsound_track(path2, se_dir=se2, out_path=out2, duration=0.1, ffmpeg=exe2)
    _, data2 = _read_wav(out2)
    assert data2[0] == -32768, data2[:4]


@test
def build_hitsound_track_subtracts_start_and_keeps_drag_offset():
    """给了 start 之后样本按窗口内相对时间落位（第 0 帧 = start 秒）。"""
    td = _tmpdir("window")
    exe = _fake_ffmpeg(td)
    se = _fake_se(td)
    _pcm_const(td, "tap.mp3", 4, 1000)
    _pcm_const(td, "drag.mp3", 4, 4000)
    ch = _chart([_note(2, 1), _note(4, 4)])      # 1.0s / 2.0s(-30ms -> 1.97s)
    path = _write_chart(td, ch)
    out = os.path.join(td, "o.wav")
    A.build_hitsound_track(path, se_dir=se, out_path=out, start=0.5, duration=1.5,
                           ffmpeg=exe)
    frames, data = _read_wav(out)
    approx(frames, 72000, 2)                     # 1.5s
    tap = int(round((1.0 - 0.5) * 48000)) * 2    # (1.0 - 0.5)s
    assert data[tap] == 1000, (tap, data[tap])
    assert data[tap - 2] == 0, "第一个样本之前必须是静音"
    drag = int(round((1.97 - 0.5) * 48000)) * 2
    assert data[drag] == 4000, (drag, data[drag])
    assert data[drag - 2] == 0


@test
def build_hitsound_track_lays_the_song_down_first_and_trims_from_start():
    """原曲从 start 秒开始截，打击音叠在它上面。"""
    td = _tmpdir("song")
    exe = _fake_ffmpeg(td)
    se = _fake_se(td)
    _pcm_const(td, "tap.mp3", 4, 500)
    song = os.path.join(td, "song.wav")
    _write_wav(song, 2.0, value=1000)
    _pcm(td, "song.wav", [1000] * (2 * 48000 * 2))   # 假解码：整首 2 秒恒定 1000
    ch = _chart([_note(3, 1)])                       # 1.5s，落在原曲区间内
    path = _write_chart(td, ch)
    out = os.path.join(td, "o.wav")
    A.build_hitsound_track(path, se_dir=se, song=song, out_path=out,
                           start=0.5, duration=2.0, ffmpeg=exe)
    frames, data = _read_wav(out)
    approx(frames, 96000, 2)
    assert data[0] == 1000, data[:2]              # 原曲从 0.5s 起
    hit = int(round((1.5 - 0.5) * 48000)) * 2     # 音符 1.5s（无偏移）
    assert data[hit] == 1500, (hit, data[hit])    # 1000(原曲) + 500(SE)
    assert data[hit - 2] == 1000
    assert data[hit + 8] == 1000, "SE 之外仍是原曲"


@test
def build_hitsound_track_skips_notes_whose_se_is_missing():
    """目录里没有 drag.mp3 时，Drag 音符静默跳过而不是报错。"""
    td = _tmpdir("missing")
    exe = _fake_ffmpeg(td)
    se = _fake_se(td, names=("tap",))
    _pcm_const(td, "tap.mp3", 4, 100)
    ch = _chart([_note(0, 1), _note(1, 4)])
    path = _write_chart(td, ch)
    out = os.path.join(td, "o.wav")
    A.build_hitsound_track(path, se_dir=se, out_path=out, duration=0.5, ffmpeg=exe)
    frames, data = _read_wav(out)
    assert frames == 24000, frames
    assert list(data[:8]) == [100] * 8, data[:8]
    assert not any(data[8:]), "缺素材的那类不该有任何声音"


@test
def build_hitsound_track_scales_the_volume_inside_ffmpeg():
    """音量交给 ffmpeg 的 volume 滤镜（不做 Python 侧浮点乘，省一遍全量循环）。"""
    td = _tmpdir("vol")
    exe = _fake_ffmpeg(td)
    se = _fake_se(td)
    _pcm_const(td, "tap.mp3", 4, 1000)
    path = _write_chart(td, _chart([_note(0, 1)]))
    A.build_hitsound_track(path, se_dir=se, out_path=os.path.join(td, "o.wav"),
                           volume=0.25, duration=0.1, ffmpeg=exe)
    argv = _argv(td)
    assert "-af" in argv, argv
    assert argv[argv.index("-af") + 1] == "volume=0.25", argv


@test
def build_hitsound_track_caches_identical_decodes():
    """同一素材只解一次：100 个 Tap 仍然只有 1 次解码调用。"""
    td = _tmpdir("cache")
    exe = _fake_ffmpeg(td)
    se = _fake_se(td)
    _pcm_const(td, "tap.mp3", 4, 100)
    notes = [_note(i, 1) for i in range(100)]        # 每拍一个，50 秒
    path = _write_chart(td, _chart(notes))
    A.build_hitsound_track(path, se_dir=se, out_path=os.path.join(td, "o.wav"),
                           duration=50.0, ffmpeg=exe)
    argv = _argv(td)
    assert argv.count("-i") == 1, argv


@test
def build_hitsound_track_removes_partial_files_when_ffmpeg_fails():
    """解码失败/半截输出时删掉半截 WAV 并抛 AudioMixError（调用方据此退化）。"""
    td = _tmpdir("fail")
    exe = _fake_ffmpeg(td, mode="partial")
    se = _fake_se(td)
    _pcm_const(td, "tap.mp3", 40, 100)
    path = _write_chart(td, _chart([_note(0, 1)]))
    out = os.path.join(td, "o.wav")
    err = raises(A.AudioMixError, A.build_hitsound_track, path,
                 se_dir=se, out_path=out, duration=1.0, ffmpeg=exe)
    assert not os.path.exists(out), "半截文件必须删掉"
    assert not os.path.exists(out + ".part"), "临时文件也要删掉"
    assert isinstance(err, RuntimeError)
    assert "ffmpeg" in str(err) or "SE" in str(err) or "解码" in str(err)


@test
def build_hitsound_track_reports_a_missing_ffmpeg_instead_of_crashing():
    """找不到 ffmpeg 时抛 AudioMixError（带说明），由调用方决定是否退化。"""
    td = _tmpdir("noff")
    se = _fake_se(td)
    path = _write_chart(td, _chart([_note(0, 1)]))
    old = A.ffmpeg_path
    A.ffmpeg_path = lambda what="ffmpeg": None
    try:
        err = raises(A.AudioMixError, A.build_hitsound_track, path,
                     se_dir=se, out_path=os.path.join(td, "o.wav"), duration=1.0)
    finally:
        A.ffmpeg_path = old
    assert "ffmpeg" in str(err).lower()

# ---------------------------------------------------------------- 真 ffmpeg smoke


@test
def build_hitsound_track_mixes_real_hitsounds_when_ffmpeg_is_available():
    """真 ffmpeg + 真官方 SE：出 48kHz 立体声 WAV，真的有波形，Drag 比音符早 30ms。"""
    if not V.ffmpeg_available():
        return
    se = _real_se_dir()
    if se is None:
        return
    td = _tmpdir("real")
    ch = _chart([_note(0, 1), _note(1, 3), _note(2, 4)])   # 0 / 0.5 / 1.0s
    path = _write_chart(td, ch)
    out = os.path.join(td, "se.wav")
    got = A.build_hitsound_track(path, se_dir=se, out_path=out, duration=0.3)
    assert os.path.isfile(got), got
    with wave.open(got, "rb") as w:
        assert w.getnchannels() == 2
        approx(w.getframerate(), 48000.0, 0.5)
        frames = w.getnframes()
        data = w.readframes(frames)
    assert frames / 48000.0 >= 0.3
    assert any(b != 0 for b in data), "混出来的轨必须是真声音"


@test
def real_hitsounds_land_on_the_note_time_with_the_drag_offset():
    """真素材的**第一个非零样本**要落在音符时间（Drag 提前 30ms）。"""
    if not V.ffmpeg_available():
        return
    se = _real_se_dir()
    if se is None:
        return
    td = _tmpdir("onset")
    for kind, note_type, want in (("tap", 1, 1.000), ("drag", 4, 0.970)):
        ch = _chart([_note(2, note_type)])          # 2 拍 @120BPM = 1.0s
        path = _write_chart(td, ch, name=kind + ".json")
        out = os.path.join(td, kind + ".wav")
        A.build_hitsound_track(path, se_dir=se, out_path=out, duration=1.2,
                               sources={kind: os.path.join(se, kind + ".mp3")})
        frames, data = _read_wav(out)
        first = next((i for i, v in enumerate(data) if v), None)
        assert first is not None, "%s 一点声音都没有" % kind
        onset = (first // 2) / 48000.0
        approx(onset, want, 0.005)                  # 素材本身的开头就是起振
        assert data[0] == 0, "音符之前必须是静音"


@test
def render_chart_video_accepts_hitsounds_and_cleans_the_temp_track():
    """端到端：render_chart_video(hitsounds=...) 出带音轨的 mp4，临时轨回收。"""
    if not V.ffmpeg_available():
        return
    se = _real_se_dir()
    if se is None:
        return
    try:
        import PIL  # noqa: F401
    except Exception:
        return
    td = _tmpdir("e2e")
    ch = _chart([_note(0, 1), _note(1, 3), _note(2, 4)])
    path = _write_chart(td, ch)
    out = os.path.join(td, "out.mp4")

    def _leftovers():
        return sorted(fnmatch.filter(os.listdir(tempfile.gettempdir()),
                                     V.SE_TRACK_PREFIX + "*"))

    before = _leftovers()
    info = V.render_chart_video(path, out, fps=10, size=(200, 120),
                               start=0.0, end=0.4, hitsounds=se)
    assert os.path.isfile(str(info.path))
    assert os.path.getsize(str(info.path)) > 0
    assert _leftovers() == before, "临时混音轨没回收"
    probe = subprocess.run([V._resolve_ffmpeg(None), "-hide_banner", "-i", out],
                           capture_output=True)
    text = probe.stderr.decode(errors="replace")
    assert "Audio:" in text, text[-400:]


@test
def render_chart_video_degrades_gracefully_when_se_is_unusable():
    """SE 目录不存在 / 混音抛错：导出照常完成，只是没有 SE。"""
    td = _tmpdir("degrade")
    ch = _chart([_note(0, 1)])
    path = _write_chart(td, ch)
    out = os.path.join(td, "out.mp4")
    missing = os.path.join(td, "no-such-se-dir")
    old = V.build_hitsound_track

    def boom(*a, **kw):
        raise A.AudioMixError("故意失败")

    V.build_hitsound_track = boom
    try:
        try:
            import PIL  # noqa: F401
        except Exception:
            return
        if not V.ffmpeg_available():
            return
        info = V.render_chart_video(path, out, fps=5, size=(64, 48),
                                    start=0.0, end=0.2, hitsounds=missing)
    finally:
        V.build_hitsound_track = old
    assert os.path.isfile(str(info.path))


@test
def render_chart_video_without_hitsounds_keeps_the_old_behaviour():
    """不传 hitsounds 时完全不碰 SE（不建临时轨、不给 ffmpeg 加输入）。"""
    td = _tmpdir("noflat")
    called = []
    old = V.build_hitsound_track
    V.build_hitsound_track = lambda *a, **kw: called.append(1)
    try:
        try:
            import PIL  # noqa: F401
        except Exception:
            return
        if not V.ffmpeg_available():
            return
        ch = _chart([_note(0, 1)])
        path = _write_chart(td, ch)
        out = os.path.join(td, "out.mp4")
        before = sorted(fnmatch.filter(os.listdir(tempfile.gettempdir()),
                                       V.SE_TRACK_PREFIX + "*"))
        info = V.render_chart_video(path, out, fps=5, size=(64, 48),
                                    start=0.0, end=0.2)
        after = sorted(fnmatch.filter(os.listdir(tempfile.gettempdir()),
                                      V.SE_TRACK_PREFIX + "*"))
    finally:
        V.build_hitsound_track = old
    assert not called, "没给 hitsounds 就不该混音"
    assert before == after
    assert os.path.isfile(str(info.path))

# ---------------------------------------------------------------- CLI 接线


@test
def video_cli_exposes_se_options():
    """video 子命令要有 --se-dir / --se-volume，默认音量 0.532。"""
    import contextlib
    import io

    from rpechart.render import cli as C
    ns = C.build_parser().parse_args(["video", "chart.json", "-o", "out.mp4",
                                      "--se-dir", "SE"])
    assert ns.se_dir == "SE"
    approx(ns.se_volume, A.SE_VOLUME, 1e-9)

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            C.main(["video", "--help"])
    except SystemExit:
        pass
    help_text = buf.getvalue()
    assert "--se-dir" in help_text, help_text[-600:]
    assert "--se-volume" in help_text, help_text[-600:]


@test
def video_cli_passes_se_options_to_the_renderer():
    """cmd_video 要把 se_dir / se_volume 透传给 render_chart_video。"""
    from rpechart.render import cli as C
    td = _tmpdir("cli")
    se = _fake_se(td)
    path = _write_chart(td, _chart([_note(0, 1)]))
    out = os.path.join(td, "out.mp4")
    seen = {}

    def fake_render(chart, out_path, **kw):
        seen.update(kw)
        return V.VideoInfo(path=out_path, frames=3, fps=10.0, width=200,
                           height=120, duration=0.3, bytesize=1024)

    old_render, old_avail = C.render_chart_video, C.ffmpeg_available
    C.render_chart_video, C.ffmpeg_available = fake_render, lambda: True
    try:
        rc = C.main(["video", path, "-o", out, "--se-dir", se, "--se-volume", "0.25"])
    finally:
        C.render_chart_video, C.ffmpeg_available = old_render, old_avail
    assert rc == 0
    assert seen.get("hitsounds") == se, seen
    approx(seen.get("se_volume"), 0.25)


@test
def audio_public_names_are_exported():
    """audio 的 __all__ 与实际属性一致，混音入口可以从 rpechart.audio 直接拿。"""
    for name in ("hitsound_schedule", "build_hitsound_track", "resolve_se_sources",
                 "find_se_dir", "AudioMixError", "SE_DEDUP_MS", "SE_MAX_EVENTS"):
        assert name in A.__all__, f"audio.__all__ 少了 {name}"
        assert hasattr(A, name), name
    for name in A.__all__:
        assert hasattr(A, name), f"audio.__all__ 列了不存在的 {name}"
    for name in ("build_hitsound_track", "SE_TRACK_PREFIX"):
        assert hasattr(V, name), f"video 应当转出 {name}"
