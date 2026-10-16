"""谱面 → MP4 的输出链路测试。

``rpechart/video.py`` 只做一件事：**把一串帧流式喂给 ffmpeg**。帧从哪来它不关心，
于是绝大部分逻辑都能用「假的帧迭代器 + 假的 ffmpeg 可执行文件」覆盖，不必真的
编码 —— 这样 CI（或者一台没装 imageio-ffmpeg 的机器）也能跑完整套。

真正需要真 ffmpeg 的只有最后一条 smoke：它编 ~1 秒纯色帧，再**用 ffmpeg 自己
探测**产物里有没有 h264 视频流。ffmpeg 不可用就 ``return``（跳过）而不是失败。

渲染器本体就在库里（``rpechart.render``），不再依赖仓库里的任何脚本文件，
所以「默认能不能出图」不取决于某个路径在不在（这个坑踩过一次：渲染器曾经是一个
靠 importlib 按路径加载的独立脚本，一旦被搬走或复制成两份就会悄悄分叉）。
"""

import json
import os
import subprocess
import sys
import tempfile

from run_tests import test

import rpechart
from rpechart import video as V

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


# ---------------------------------------------------------------- 测试替身
#: 假的 ffmpeg：把 argv 记下来、数一数 stdin 收了多少字节、按 mode.txt 决定成败。
FAKE_FFMPEG_SRC = '''\
import json, os, sys

here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "argv.json"), "w", encoding="utf-8") as fh:
    json.dump(sys.argv[1:], fh)

n = 0
while True:
    chunk = sys.stdin.buffer.read(1 << 16)
    if not chunk:
        break
    n += len(chunk)
with open(os.path.join(here, "stdin_bytes.txt"), "w") as fh:
    fh.write(str(n))

mode_path = os.path.join(here, "mode.txt")
mode = "ok"
if os.path.exists(mode_path):
    with open(mode_path) as fh:
        mode = fh.read().strip()

if mode == "fail":
    sys.stderr.write("fake-ffmpeg: 第一行错误\\n")
    sys.stderr.write("fake-ffmpeg: Unknown encoder 'libx264'\\n")
    sys.stderr.write("fake-ffmpeg: 最后一行错误\\n")
    sys.exit(2)
if mode == "silent-fail":
    sys.exit(1)

with open(sys.argv[-1], "wb") as fh:
    fh.write(b"FAKEVIDEO")
sys.stderr.write("fake-ffmpeg: 正常结束\\n")
'''


def _fake_ffmpeg(td, mode="ok"):
    """在 td 里造一个可以当 ffmpeg 用的可执行文件，返回它的路径。"""
    script = os.path.join(td, "fake_ffmpeg.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(FAKE_FFMPEG_SRC)
    with open(os.path.join(td, "mode.txt"), "w", encoding="utf-8") as fh:
        fh.write(mode)

    if os.name == "nt":
        # .cmd 包装器能把 stdin/argv 原样透传，且不需要 .exe 后缀
        exe = os.path.join(td, "ffmpeg.cmd")
        with open(exe, "w", encoding="utf-8") as fh:
            fh.write(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n')
    else:
        exe = os.path.join(td, "ffmpeg")
        with open(exe, "w", encoding="utf-8") as fh:
            fh.write(f'#!{sys.executable}\n')
            fh.write(open(script, encoding="utf-8").read())
        os.chmod(exe, 0o755)
    return exe


def _solid(w, h, rgb):
    """造一帧 ``(bytes, w, h)``，不依赖 Pillow。"""
    return (bytes(rgb) * (w * h), w, h)


def _read(td, name, default=None):
    path = os.path.join(td, name)
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _make_chart(path, *, beats=8, bpm=120.0):
    """造一张最小可渲染谱面：一条线 + 若干 Tap。"""
    from rpechart import BPMItem, Beat, Chart, JudgeLine, Note

    notes = [Note(type=1, startTime=Beat(t), endTime=Beat(t))
             for t in range(0, beats)]
    ch = Chart(BPMList=[BPMItem(bpm=bpm, startTime=Beat(0))],
               judgeLineList=[JudgeLine(Name="L", notes=notes)])
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ch.to_json(), fh, ensure_ascii=False)
    return path


# ---------------------------------------------------------------- 纯接口
@test
def video_module_exports_are_declared_and_resolvable():
    """``__all__`` 里列的名字必须真的存在，且要出现在包的导出里。"""
    for name in V.__all__:
        assert hasattr(V, name), f"video.__all__ 列了不存在的 {name}"
    for name in ("VideoInfo", "VideoEncodeError", "encode_frames",
                 "render_chart_video", "ffmpeg_available"):
        assert name in V.__all__, f"video.__all__ 少了 {name}"
        assert name in rpechart.__all__, f"rpechart.__all__ 少了 {name}"
        assert hasattr(rpechart, name), f"rpechart 上没有 {name}"


@test
def video_info_reports_size_and_units():
    """``VideoInfo`` 的 duration 是「帧数 / fps」，size 是 (w, h)。"""
    info = V.VideoInfo(path="x.mp4", frames=300, fps=60.0,
                       width=1920, height=1080, duration=5.0, bytesize=1024 * 1024)
    assert info.size == (1920, 1080)
    assert "1920x1080" in repr(info)
    assert "5.00s" in str(info)


@test
def ffmpeg_available_returns_a_bool():
    """在缺 ffmpeg 的机器上也要能安全地问一句。"""
    assert isinstance(V.ffmpeg_available(), bool)


@test
def video_encode_error_carries_stderr_tail_in_message():
    """异常里必须能看到 ffmpeg 的 stderr 尾巴，否则等于静默失败。"""
    err = V.VideoEncodeError("编码失败", stderr_tail="line1\nline2", returncode=2)
    assert isinstance(err, RuntimeError), "应当能被 except RuntimeError 接住"
    assert "编码失败" in str(err)
    assert "line1" in str(err) and "line2" in str(err)
    assert err.returncode == 2
    assert err.stderr_tail == "line1\nline2"


# ---------------------------------------------------------------- 假 ffmpeg
@test
def encode_frames_streams_raw_frames_and_writes_the_file():
    """帧应当以 rgb24 rawvideo 从 stdin 流式喂进去，字节数正好是 w*h*3*帧数。"""
    with tempfile.TemporaryDirectory(prefix="rpechart_video_test_") as td:
        exe = _fake_ffmpeg(td)
        out = os.path.join(td, "out.mp4")
        frames = [_solid(4, 2, (10, 20, 30)) for _ in range(5)]
        seen = []
        info = V.encode_frames(frames, out, fps=25, ffmpeg=exe,
                               progress=lambda d, t: seen.append((d, t)))

        argv = json.loads(_read(td, "argv.json"))
        n_bytes = int(_read(td, "stdin_bytes.txt"))
        on_disk = os.path.isfile(info.path)
        stored_bytes = os.path.getsize(info.path) if on_disk else -1

    assert n_bytes == 4 * 2 * 3 * 5, f"喂进去的字节数不对: {n_bytes}"
    assert info.frames == 5
    assert info.width == 4 and info.height == 2
    assert info.fps == 25.0
    assert abs(info.duration - 5 / 25.0) < 1e-9
    assert info.bytesize > 0
    assert on_disk, f"编码器应当真的写出了 {info.path}"
    assert info.bytesize == stored_bytes
    assert seen == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)], seen

    # 输入侧：rawvideo / rgb24 / -s WxH / -r fps / -i -
    assert "-f" in argv and argv[argv.index("-f") + 1] == "rawvideo"
    assert argv[argv.index("-pix_fmt") + 1] == "rgb24"
    assert argv[argv.index("-s") + 1] == "4x2"
    assert argv[argv.index("-r") + 1] == "25"
    assert argv[argv.index("-i") + 1] == "-"
    # 输出侧：libx264 / crf 18 / yuv420p / +faststart
    assert argv[argv.index("-c:v") + 1] == "libx264"
    assert argv[argv.index("-crf") + 1] == "18"
    assert argv[argv.index("-preset") + 1] == "medium"
    assert argv[argv.index("-pix_fmt", argv.index("-c:v")) + 1] == "yuv420p"
    assert argv[argv.index("-movflags") + 1] == "+faststart"


@test
def encode_frames_accepts_a_bare_generator():
    """生成器没有 ``__len__``，total 就报 ``None`` —— 不能因此炸掉。"""
    def gen():
        for _ in range(4):
            yield _solid(3, 3, (9, 9, 9))

    with tempfile.TemporaryDirectory(prefix="rpechart_video_test_") as td:
        exe = _fake_ffmpeg(td)
        seen = []
        info = V.encode_frames(gen(), os.path.join(td, "o.mp4"), fps=4,
                               ffmpeg=exe, progress=lambda d, t: seen.append((d, t)))

    assert info.frames == 4
    assert seen and all(t is None for _, t in seen), seen


@test
def encode_frames_accepts_pil_images():
    """``PIL.Image`` 帧也要吃 —— 渲染器给的就是这个。"""
    try:
        from PIL import Image
    except Exception:                                        # pragma: no cover
        return
    with tempfile.TemporaryDirectory(prefix="rpechart_video_test_") as td:
        exe = _fake_ffmpeg(td)
        frames = [Image.new("RGB", (6, 3), (i * 10, 0, 0)) for i in range(3)]
        info = V.encode_frames(frames, os.path.join(td, "o.mp4"), fps=30,
                               ffmpeg=exe)
        n_bytes = int(_read(td, "stdin_bytes.txt"))

    assert n_bytes == 6 * 3 * 3 * 3
    assert info.size == (6, 3)
    assert info.frames == 3


@test
def encode_frames_takes_the_size_from_the_first_frame():
    """不给 size 就用第一帧的尺寸，并把「字符串尺寸」也解析掉。"""
    with tempfile.TemporaryDirectory(prefix="rpechart_video_test_") as td:
        exe = _fake_ffmpeg(td)
        info = V.encode_frames([_solid(8, 5, (1, 2, 3))],
                               os.path.join(td, "a.mp4"), fps=1, ffmpeg=exe)
        assert info.size == (8, 5)

        info2 = V.encode_frames([_solid(8, 5, (1, 2, 3))],
                                os.path.join(td, "b.mp4"), fps=1, size="8x5",
                                ffmpeg=exe)
        assert info2.size == (8, 5)


@test
def encode_frames_muxes_audio_as_a_second_input():
    """给了 audio 就要 -i 两路，并用 aac 192k + -shortest 收尾。"""
    with tempfile.TemporaryDirectory(prefix="rpechart_video_test_") as td:
        exe = _fake_ffmpeg(td)
        wav = os.path.join(td, "song.mp3")
        with open(wav, "wb") as fh:
            fh.write(b"\x00" * 16)
        V.encode_frames([_solid(2, 2, (0, 0, 0))],
                        os.path.join(td, "o.mp4"), fps=1, audio=wav, ffmpeg=exe)
        argv = json.loads(_read(td, "argv.json"))

    assert argv.count("-i") == 2, f"应当有两个输入: {argv}"
    assert argv[argv.index("-i", argv.index("-i") + 1) + 1] == wav
    assert argv[argv.index("-c:a") + 1] == "aac"
    assert argv[argv.index("-b:a") + 1] == "192k"
    assert "-shortest" in argv
    assert argv[argv.index("-map") + 1] == "0:v:0"
    assert argv[argv.index("-map", argv.index("-map") + 1) + 1] == "1:a:0"


@test
def encode_frames_appends_extra_args():
    """``extra_args`` 是逃生舱，必须原样追加在输出文件之前。"""
    with tempfile.TemporaryDirectory(prefix="rpechart_video_test_") as td:
        exe = _fake_ffmpeg(td)
        out = os.path.join(td, "o.mp4")
        V.encode_frames([_solid(2, 2, (0, 0, 0))], out, fps=1, ffmpeg=exe,
                        extra_args=["-t", "3"])
        argv = json.loads(_read(td, "argv.json"))

    assert argv[-3:] == ["-t", "3", out], f"extra_args 应当紧贴输出文件: {argv}"


@test
def encode_frames_rejects_an_empty_iterator():
    """没有帧就报错，别产出 0 字节的假文件。"""
    with tempfile.TemporaryDirectory(prefix="rpechart_video_test_") as td:
        exe = _fake_ffmpeg(td)
        try:
            V.encode_frames([], os.path.join(td, "o.mp4"), fps=1, ffmpeg=exe)
        except ValueError as exc:
            assert "空" in str(exc)
        else:
            raise AssertionError("空的 frames 应当抛 ValueError")


@test
def encode_frames_rejects_a_mismatched_frame_size():
    """尺寸不一致要立刻报错 —— 否则 ffmpeg 会把画面静默撕裂。"""
    with tempfile.TemporaryDirectory(prefix="rpechart_video_test_") as td:
        exe = _fake_ffmpeg(td)
        frames = [_solid(4, 4, (0, 0, 0)), _solid(4, 5, (0, 0, 0))]
        try:
            V.encode_frames(frames, os.path.join(td, "o.mp4"), fps=1, ffmpeg=exe)
        except ValueError as exc:
            assert "4x5" in str(exc) and "4x4" in str(exc), str(exc)
        else:
            raise AssertionError("后续帧尺寸变了应当抛 ValueError")


@test
def encode_frames_rejects_a_size_that_contradicts_the_first_frame():
    """显式 size 与第一帧不符时报错，而不是默默缩放。"""
    try:
        V.encode_frames([_solid(4, 4, (0, 0, 0))], "x.mp4", fps=1, size=(8, 8))
    except ValueError as exc:
        assert "8x8" in str(exc) and "4x4" in str(exc), str(exc)
    else:
        raise AssertionError("显式 size 与首帧不符应当抛 ValueError")


@test
def encode_frames_rejects_unknown_frame_objects():
    """喂错类型要给出能读懂的话，而不是 AttributeError。"""
    try:
        V.encode_frames([object()], "x.mp4", fps=1)
    except TypeError as exc:
        assert "object" in str(exc), str(exc)
    else:
        raise AssertionError("不认识的帧类型应当抛 TypeError")


@test
def encode_frames_rejects_a_short_byte_buffer():
    """``(bytes, w, h)`` 里的字节数必须正好是 w*h*3。"""
    try:
        V.encode_frames([(b"\x00" * 7, 4, 4)], "x.mp4", fps=1)
    except ValueError as exc:
        assert "48" in str(exc) or "7" in str(exc), str(exc)
    else:
        raise AssertionError("字节数不足应当抛 ValueError")


@test
def encode_frames_surfaces_ffmpeg_stderr_on_failure():
    """ffmpeg 非 0 退出时，异常里要带上 stderr 的末尾若干行。"""
    with tempfile.TemporaryDirectory(prefix="rpechart_video_test_") as td:
        exe = _fake_ffmpeg(td, mode="fail")
        try:
            V.encode_frames([_solid(4, 4, (0, 0, 0))],
                            os.path.join(td, "o.mp4"), fps=1, ffmpeg=exe)
        except V.VideoEncodeError as exc:
            text = str(exc)
            assert "Unknown encoder 'libx264'" in text, text
            assert "最后一行错误" in text, text
            assert exc.returncode == 2
        else:
            raise AssertionError("ffmpeg 失败时不该静默返回")


@test
def encode_frames_reports_when_ffmpeg_exits_zero_without_a_file():
    """退出码 0 但没产出文件，也必须报错。"""
    with tempfile.TemporaryDirectory(prefix="rpechart_video_test_") as td:
        exe = _fake_ffmpeg(td, mode="silent-fail")
        try:
            V.encode_frames([_solid(4, 4, (0, 0, 0))],
                            os.path.join(td, "o.mp4"), fps=1, ffmpeg=exe)
        except V.VideoEncodeError:
            pass
        else:
            raise AssertionError("退出码 0 无产物应当抛 VideoEncodeError")


@test
def encode_frames_creates_missing_parent_directories():
    """输出到还不存在的子目录里要自己建目录。"""
    with tempfile.TemporaryDirectory(prefix="rpechart_video_test_") as td:
        exe = _fake_ffmpeg(td)
        out = os.path.join(td, "deep", "er", "o.mp4")
        info = V.encode_frames([_solid(2, 2, (0, 0, 0))], out, fps=1, ffmpeg=exe)
        assert os.path.isfile(out)
        assert os.path.samefile(str(info.path), out)


# ---------------------------------------------------------------- 渲染器联动
@test
def render_chart_video_needs_a_renderer_and_says_so():
    """找不到渲染器时要给出「怎么指定」的可执行提示。"""
    try:
        V.render_chart_video("nope.json", "o.mp4", renderer="根本没有这个文件.py")
    except V.VideoEncodeError as exc:
        assert "渲染器" in str(exc), str(exc)
    else:
        raise AssertionError("渲染器不存在应当抛 VideoEncodeError")


@test
def render_chart_video_rejects_an_empty_time_range():
    """end <= start 时直接报错，而不是编出 0 帧。"""
    with tempfile.TemporaryDirectory(prefix="rpechart_video_test_") as td:
        chart = _make_chart(os.path.join(td, "chart.json"))
        try:
            V.render_chart_video(chart, os.path.join(td, "o.mp4"),
                                 start=5.0, end=5.0)
        except (ValueError, V.VideoEncodeError) as exc:
            assert "5.0" in str(exc) or "5" in str(exc), str(exc)
        else:
            raise AssertionError("空区间应当报错")


@test
def the_builtin_renderer_is_the_default():
    """``render_chart_video`` 默认就用库自带的渲染器，不依赖磁盘上的脚本。

    渲染器以前是独立的脚本，``video.py`` 靠路径去找它；现在它已经收进
    ``rpechart.render``，所以「默认能不能出图」不再取决于某个文件在不在。
    """
    mod = V.builtin_renderer()
    assert mod is not None, "库自带渲染器不可用（是不是没装 Pillow？）"
    for attr in ("Renderer", "open_video_stream", "VideoStream"):
        assert hasattr(mod, attr), f"内置渲染器缺少 {attr}"


# ---------------------------------------------------------------- 收尾守门
@test
def the_renderer_lives_only_inside_the_package():
    """渲染实现只能在 ``rpechart/render/`` 里有一份，不允许再出现仓库级脚本副本。

    这个坑踩过一次：实现曾经在仓库里有一份、外面又有一份副本，
    两份 883 行必须逐字节同步，一改就分叉。
    """
    import rpechart.render.renderer as R

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.isfile(R.__file__), R.__file__
    assert os.path.normcase(R.__file__).startswith(os.path.normcase(root)), (
        f"渲染器被搬到了包外：{R.__file__}")
    # 仓库根下不应再有独立的渲染器脚本
    for stray in ("_render.py", "rperender.py"):
        assert not os.path.isfile(os.path.join(root, stray)), (
            f"仓库根又出现了渲染器脚本 {stray} —— 实现应当只在 rpechart/render/ 里")


@test
def renderer_cli_registers_the_video_subcommand():
    """``video`` 子命令及其参数必须真的注册进渲染器 CLI（文档在演示它）。"""
    from rpechart.render import cli

    import contextlib
    import io

    def _help(argv):
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                cli.main(argv)
        except SystemExit:
            pass
        return buf.getvalue()

    top = _help(["--help"])
    assert "video" in top, f"顶层 help 里没有 video 子命令:\n{top}"

    sub = _help(["video", "--help"])
    for flag in ("-o", "--out", "--fps", "--size", "--from", "--to",
                 "--audio", "--cover", "--hud"):
        assert flag in sub, f"`video --help` 里缺少 {flag}:\n{sub}"


@test
def renderer_cli_video_parses_seconds_and_size():
    """``--from/--to`` 是**秒**，``--size`` 收 1920x1080 这种写法。"""
    from rpechart.render import cli

    assert cli.parse_size("1920x1080") == (1920, 1080)
    assert cli.parse_size("640*360") == (640, 360)
    for bad in ("1920", "abc", "0x100", "-2x10"):
        try:
            cli.parse_size(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"--size 应当拒绝 {bad!r}")


# ---------------------------------------------------------------- 真 smoke
@test
def encodes_a_real_mp4_with_a_probed_video_stream():
    """端到端：真编 1 秒谱面视频，再用 ffmpeg 探测到 h264 视频流。

    ffmpeg 不可用就直接跳过（这台机器上它是 imageio-ffmpeg 捆绑的那份）。
    """
    if not V.ffmpeg_available():
        return
    try:
        from PIL import Image  # noqa: F401  —— 渲染器需要
    except Exception:                                        # pragma: no cover
        return

    exe = rpechart.ffmpeg_path("ffmpeg")
    with tempfile.TemporaryDirectory(prefix="rpechart_video_smoke_") as td:
        chart = _make_chart(os.path.join(td, "chart.json"), beats=8, bpm=120.0)
        out = os.path.join(td, "out.mp4")
        info = V.render_chart_video(chart, out, fps=10, size=(320, 240),
                                    start=0.0, end=1.0, hud=False)

        assert os.path.isfile(out)
        assert info.frames == 10, f"1s @10fps 应当是 10 帧，实际 {info.frames}"
        assert abs(info.duration - 1.0) < 1e-9
        assert info.size == (320, 240)
        assert info.bytesize > 0, "产出了 0 字节的文件"
        assert info.bytesize == os.path.getsize(out)

        # 用 ffmpeg 自己探测：有 Video: h264 才算真的编出了视频流
        probe = subprocess.run([exe, "-hide_banner", "-i", out],
                               capture_output=True, text=True, timeout=60)
        text = probe.stdout + probe.stderr
        assert "Video:" in text, f"探测不到视频流:\n{text}"
        assert "h264" in text.lower(), f"视频流不是 h264:\n{text}"
        assert "320x240" in text, f"分辨率不对:\n{text}"

    assert not os.path.exists(out), "临时目录应当被清掉了"


# ---------------------------------------------------------------- 视频叠底的显存/内存
@test
def renderer_offers_a_streaming_video_reader_not_only_a_ram_hog():
    """叠原片必须走**流式**解码，不能把整片解进内存。

    这是一次真实缺陷：早期 `--video` 复用了 `_video_loader()`，它把全部帧解成
    raw 字节常驻内存 —— 1080p60 的 93 秒是 5580 帧 × 6.2MB ≈ **32GB**。
    测试机没爆只是因为当时只渲了 3 秒的短视频。这条测试要求渲染器提供
    `open_video_stream`（顺序读、用完即弃），并确认它**不是**先把所有帧读进来。
    """
    from rpechart.render import videostream

    assert hasattr(videostream, "open_video_stream"), (
        "渲染器缺少 open_video_stream()，视频叠底会退回整片入内存的实现")
    assert hasattr(videostream, "VideoStream"), "缺少 VideoStream 类"


@test
def video_stream_does_not_preload_every_frame():
    """`VideoStream` 在**没开始读**时不能持有任何帧数据。

    判据：构造对象本身（不调 get）应当是零成本的 —— 这里用「构造后立刻检查
    没有任何已解码缓冲」来实现。一旦有人把它改成「init 里先解码全片」，
    这条会失败（那个实现会需要真实视频文件且耗时可观测）。
    """
    from rpechart.render.videostream import VideoStream

    # 构造一个不存在的路径：流式实现只记参数、不碰磁盘 ⇒ 不该抛错也不该解码
    vs = VideoStream(os.path.join(os.path.dirname(HERE), "no_such_video.mp4"),
                     30.0, 64, 48)
    try:
        assert vs._proc is None, "构造时就开了 ffmpeg 子进程（应当懒启动）"
        assert vs.stride == 64 * 48 * 3
        assert vs._next_t == 0.0
    finally:
        vs.close()
