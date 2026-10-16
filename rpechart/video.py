"""谱面 → MP4：把渲染出来的帧流式喂给 ffmpeg 编码成视频。

为什么单独一个模块，而不是塞进渲染器里：

* ``rpechart`` 是**纯 Python 库**（除 ``audio`` 里可选的 imageio-ffmpeg 外没有第三方依赖），
  离线渲染器却需要 numpy + Pillow。把渲染器 import 进包里会污染这层边界，
  所以这里只做「**帧的迭代器 → 视频文件**」这一件事，用什么画帧由调用方决定；
* 帧可以来自 :meth:`rpechart.render.renderer.Renderer.frame`，也可以是任何
  ``PIL.Image`` / ``(bytes, w, h)`` 的迭代器 —— 于是它天然可测（本模块的测试
  用假的帧迭代器就能覆盖全部逻辑，不必真的调 ffmpeg）。

用法::

    from rpechart import encode_frames, render_chart_video

    # 1) 已经有一串帧
    encode_frames((img for img in my_frames), "out.mp4", fps=60, audio="song.mp3")

    # 2) 直接给一张谱面（内部用离线渲染器逐帧画）
    info = render_chart_video("build/anime.json", "out.mp4", fps=60,
                              size=(1920, 1080), audio="song.mp3")
    print(info)

为什么不落盘 PNG 再合成：一首 4 分钟的曲子按 60fps 是 14400 帧，按 1080p 每张
PNG 约 1–2 MB，光磁盘就要几十 GB，而且多出来一轮编解码。这里直接
``-f rawvideo … -i -`` 从 stdin 流式喂 ``rgb24`` 原始字节给 ffmpeg。
"""

from __future__ import annotations

import os
import subprocess
import threading
import typing as _t
from dataclasses import dataclass, field
from pathlib import Path

from .audio import ffmpeg_path

__all__ = [
    "VideoInfo",
    "VideoEncodeError",
    "encode_frames",
    "render_chart_video",
    "ffmpeg_available",
    "builtin_renderer",
]

#: 一帧默认编码多少路 rgb24 字节之外的余量（留空即可）
DEFAULT_FPS = 60.0
DEFAULT_SIZE = (1920, 1080)
DEFAULT_CRF = 18
DEFAULT_PRESET = "medium"
DEFAULT_AUDIO_BITRATE = "192k"

#: 出错时保留多少行 ffmpeg stderr（末尾若干行足以定位问题）
STDERR_TAIL_LINES = 30

#: 渲染器定位：环境变量优先，然后按「包旁边」推断
RENDERER_ENV = "RPECHART_RENDERER"


class VideoEncodeError(RuntimeError):
    """编码失败。``str(e)`` 里带 ffmpeg stderr 的末尾若干行。

    刻意派生自 :class:`RuntimeError`，这样 ``except RuntimeError`` 也能接住。
    """

    def __init__(self, message: str, *, stderr_tail: str = "", returncode=None):
        self.stderr_tail = stderr_tail
        self.returncode = returncode
        text = message
        if stderr_tail:
            text = f"{message}\n--- ffmpeg stderr（末尾）---\n{stderr_tail}"
        super().__init__(text)


class FFmpegNotFound(VideoEncodeError):
    """找不到可用的 ffmpeg 可执行文件。"""


# ---------------------------------------------------------------- 返回值
@dataclass
class VideoInfo:
    """一次成功编码的结果。"""

    path: Path
    frames: int
    fps: float
    width: int
    height: int
    duration: float
    bytesize: int

    @property
    def size(self) -> tuple[int, int]:
        """``(width, height)``。"""
        return (self.width, self.height)

    def __repr__(self) -> str:
        mb = self.bytesize / (1024.0 * 1024.0)
        return (f"<VideoInfo {Path(self.path).name} {self.width}x{self.height} "
                f"{self.fps:g}fps {self.frames}帧 {self.duration:.2f}s {mb:.2f}MB>")

    def __str__(self) -> str:
        return (f"{self.path}  {self.width}x{self.height}  {self.fps:g}fps  "
                f"{self.frames} 帧  {self.duration:.2f}s  "
                f"{self.bytesize / (1024.0 * 1024.0):.2f}MB")


# ---------------------------------------------------------------- ffmpeg
def ffmpeg_available() -> bool:
    """本机有没有能用的 ffmpeg（PATH 或 imageio-ffmpeg 的捆绑版）。"""
    try:
        return bool(ffmpeg_path("ffmpeg"))
    except Exception:                                    # pragma: no cover - 防御
        return False


def _resolve_ffmpeg(ffmpeg) -> str:
    if ffmpeg:
        return str(ffmpeg)
    exe = ffmpeg_path("ffmpeg")
    if not exe:
        raise FFmpegNotFound(
            "找不到 ffmpeg：既不在 PATH 里，也没有安装 imageio-ffmpeg。"
            "装一个即可：pip install imageio-ffmpeg"
        )
    return exe


def _fmt_fps(fps) -> str:
    f = float(fps)
    return str(int(f)) if f == int(f) else f"{f:g}"


# ---------------------------------------------------------------- 帧归一化
def _is_pil_like(obj) -> bool:
    """鸭子类型判断是不是 PIL.Image —— 这样模块本身不依赖 Pillow。"""
    return all(hasattr(obj, a) for a in ("convert", "tobytes", "size", "mode"))


def _read_frame(frame) -> tuple[bytes, int, int]:
    """把一帧统一成 ``(rgb24 字节, w, h)``。"""
    if _is_pil_like(frame):
        img = frame if frame.mode == "RGB" else frame.convert("RGB")
        w, h = img.size
        return img.tobytes(), int(w), int(h)
    if isinstance(frame, (tuple, list)) and len(frame) == 3:
        data, w, h = frame
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(
                f"帧的元组形式要求 (bytes, w, h)，实际第一项是 {type(data).__name__}")
        w, h = int(w), int(h)
        need = w * h * 3
        if len(data) != need:
            raise ValueError(
                f"帧字节数 {len(data)} 与 {w}x{h} 的 rgb24 需求 {need} 不符")
        return bytes(data), w, h
    raise TypeError(
        "不认识的帧类型：需要 PIL.Image 或 (bytes, w, h) 元组，"
        f"实际是 {type(frame).__name__}")


def _size_tuple(size) -> tuple[int, int]:
    if isinstance(size, (tuple, list)) and len(size) == 2:
        return int(size[0]), int(size[1])
    if isinstance(size, str) and "x" in size.lower():
        a, _, b = size.lower().partition("x")
        return int(a), int(b)
    raise ValueError(f"无法解析尺寸 {size!r}：要 (宽, 高) 或 'WxH'")


# ---------------------------------------------------------------- 主编码器
def encode_frames(frames, out_path, *, fps=DEFAULT_FPS, size=None, audio=None,
                  extra_args=None, progress=None, ffmpeg=None) -> VideoInfo:
    """把一串帧编码成 mp4。

    参数
    ----
    frames
        可迭代对象，元素是 ``PIL.Image`` 或 ``(bytes, w, h)``（rgb24 原始字节）。
        **只会被遍历一次**，且是惰性的 —— 生成器可以直接给，不会把整片谱面堆进内存。
    out_path
        输出文件路径（应当以 ``.mp4`` 结尾）。
    fps
        帧率；输入按 rawvideo 的 ``-r`` 解释。
    size
        ``(宽, 高)`` 或 ``"1920x1080"``。**不给就取第一帧的尺寸**；给了就校验
        后续每一帧，尺寸不一致立刻报错（否则 ffmpeg 会静默把画面撕裂）。
    audio
        音频文件路径；给了就作为第二个输入 mux 进去（``aac 192k`` + ``-shortest``）。
    extra_args
        追加到 ffmpeg 命令末尾的额外参数（例如 ``["-t", "10"]``）。
    progress
        回调 ``progress(done, total)``；``total`` 在拿不到长度时是 ``None``。
    ffmpeg
        指定 ffmpeg 可执行文件（测试用）；默认走 :func:`ffmpeg_available`。

    返回
    ----
    :class:`VideoInfo`
    """
    exe = _resolve_ffmpeg(ffmpeg)
    out_path = Path(out_path)
    if out_path.parent and not out_path.parent.exists():
        out_path.parent.mkdir(parents=True, exist_ok=True)

    it = iter(frames)
    try:
        first = next(it)
    except StopIteration:
        raise ValueError("没有可编码的帧：frames 是空的") from None

    first_bytes, fw, fh = _read_frame(first)
    if size is not None:
        w, h = _size_tuple(size)
        if (w, h) != (fw, fh):
            raise ValueError(
                f"第一帧尺寸 {fw}x{fh} 与指定的 {w}x{h} 不一致")
    else:
        w, h = fw, fh

    try:
        total = len(frames)                                # type: ignore[arg-type]
    except TypeError:
        total = None

    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{w}x{h}", "-r", _fmt_fps(fps), "-i", "-"]
    if audio:
        cmd += ["-i", str(audio)]
    cmd += ["-c:v", "libx264", "-preset", DEFAULT_PRESET, "-crf", str(DEFAULT_CRF),
            "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-map", "0:v:0", "-map", "1:a:0",
                "-c:a", "aac", "-b:a", DEFAULT_AUDIO_BITRATE, "-shortest"]
    cmd += ["-movflags", "+faststart"]
    if extra_args:
        cmd += [str(a) for a in extra_args]
    cmd.append(str(out_path))

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            bufsize=0)

    # ffmpeg 的 stderr 要在写 stdin 的同时排空，否则管道缓冲写满会死锁。
    tail: list[str] = []
    tail_lock = threading.Lock()

    def _drain() -> None:
        stream = proc.stderr
        if stream is None:                                 # pragma: no cover
            return
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                with tail_lock:
                    tail.append(line)
                    del tail[:-STDERR_TAIL_LINES]
        except Exception:                                  # pragma: no cover
            pass

    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()

    done = 0
    write_error: BaseException | None = None
    try:
        chunk = first_bytes
        while True:
            if len(chunk) != w * h * 3:
                raise ValueError(
                    f"第 {done} 帧字节数 {len(chunk)} 与 {w}x{h} 的 rgb24 需求 "
                    f"{w * h * 3} 不符")
            proc.stdin.write(chunk)                        # type: ignore[union-attr]
            done += 1
            if progress is not None:
                progress(done, total)
            try:
                nxt = next(it)
            except StopIteration:
                break
            data, nw, nh = _read_frame(nxt)
            if (nw, nh) != (w, h):
                raise ValueError(
                    f"第 {done} 帧尺寸 {nw}x{nh} 与第一帧 {w}x{h} 不一致"
                    "（尺寸一致的帧才能编成一条视频）")
            chunk = data
    except BrokenPipeError as exc:                          # ffmpeg 提前退出
        write_error = exc
    except OSError as exc:
        write_error = exc
    finally:
        try:
            proc.stdin.close()                             # type: ignore[union-attr]
        except Exception:
            pass

    returncode = proc.wait()
    drainer.join(timeout=5.0)
    stderr_tail = "\n".join(tail[-STDERR_TAIL_LINES:])

    if write_error is not None and returncode == 0:
        raise VideoEncodeError(
            f"写入 ffmpeg 失败（已写 {done} 帧）：{write_error!r}",
            stderr_tail=stderr_tail, returncode=returncode) from write_error
    if returncode != 0:
        raise VideoEncodeError(
            f"ffmpeg 退出码 {returncode}，编码 {out_path} 失败（已写 {done} 帧）",
            stderr_tail=stderr_tail, returncode=returncode)
    if not out_path.is_file() or out_path.stat().st_size <= 0:
        raise VideoEncodeError(
            f"ffmpeg 返回 0 但没有产出有效文件：{out_path}",
            stderr_tail=stderr_tail, returncode=returncode)
    if done == 0:                                          # pragma: no cover - 前面已拦
        raise ValueError("没有可编码的帧：frames 是空的")

    n_frames = done
    return VideoInfo(path=out_path, frames=n_frames, fps=float(fps),
                     width=w, height=h, duration=n_frames / float(fps),
                     bytesize=out_path.stat().st_size)


# ---------------------------------------------------------------- 渲染器定位
def builtin_renderer():
    """库自带的渲染器模块（:mod:`rpechart.render.renderer`）。

    **这是默认路径**：渲染器已经收进库里，不再依赖仓库里某个脚本文件。
    返回 ``None`` 表示渲染子系统不可用（通常是没装 Pillow）。
    """
    try:
        from .render import renderer as mod
    except Exception:                                       # pragma: no cover
        return None
    return mod


def _locate_renderer() -> Path | None:
    """找**外部**渲染器文件（默认路径用不上，留给旧脚本与自定义实现）。

    顺序：

    1. 环境变量 ``RPECHART_RENDERER``（显式指定，最高优先级）；
    2. ``<库的上级目录>/.dsh/skills/rpe-chart/scripts/rperender.py`` —— skill 的入口。

    找不到返回 ``None``（由调用方给出可读的报错）。
    """
    cands: list[Path] = []
    env = os.environ.get(RENDERER_ENV)
    if env:
        cands.append(Path(env))
    pkg_parent = Path(__file__).resolve().parents[1]
    cands += [
        pkg_parent / ".dsh" / "skills" / "rpe-chart" / "scripts" / "rperender.py",
    ]
    for c in cands:
        try:
            if c.is_file():
                return c
        except OSError:                                     # pragma: no cover
            continue
    return None


def _load_renderer_module(path: Path):
    """用 importlib 按**路径**加载渲染器，不要求它在 sys.path 上。"""
    import importlib.util

    name = f"_rpechart_renderer_{abs(hash(str(path.resolve()))) & 0xFFFFFF:x}"
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:                 # pragma: no cover
        raise VideoEncodeError(f"无法加载渲染器：{path}")
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 便利函数
def render_chart_video(chart_path, out_path, *, fps=DEFAULT_FPS, size=DEFAULT_SIZE,
                       start=0.0, end=None, audio=None, cover=None, hud=None,
                       progress=None, renderer=None, grid=False,
                       extra_args=None, video=None, blur=None, dim=None) -> VideoInfo:
    """把一张 ``chart.json`` 直接渲染成 mp4。

    参数
    ----
    chart_path
        谱面文件。
    out_path
        输出 mp4。
    fps / size
        帧率与分辨率（``(宽, 高)`` 或 ``"1920x1080"``）。
    start / end
        **秒**。``end=None`` 时取谱面内容真正结束处（``Renderer.content_beats``），
        不会拖出「最后 8 拍只有背景」的尾巴。
    audio
        音频文件；不给就不混音。
    cover
        曲绘/背景图（路径或 ``PIL.Image``）。会给它做铺底 + 模糊 + 压暗；
        不给就用深色渐变。
    video
        **动图底版**：一段视频文件（通常是原片）。给了就按时间逐帧取原片做背景，
        谱面叠在上面 —— 于是「看着画面写谱」和「把谱面录回画面」用的是同一条
        时间轴，拍点与镜头天然对齐。原片会被压暗，音符仍是主体。
        内部走**流式**解码（顺序读、用完即弃），所以整片叠底不会爆内存。
    blur / dim
        底版的模糊半径与压暗强度（``0~1``）。``dim=0`` 保留原片亮度，
        想看清画面里的人时用；``blur=0`` 完全不模糊。都不给就用默认值
        （``blur≈min(w,h)/240``、``dim=0.45``）。
    hud
        是否画 HUD（combo / 分数 / 曲名 / 难度 / 判定线编号 / 暂停图标）。
        **``None`` = 跟随渲染器的主题开关**（``Theme.hud.enabled``，默认开）——
        这是默认值，因为真实游戏画面里 HUD 一直在；要出干净画面显式传 ``False``。
    progress
        回调 ``progress(done, total)``。
    renderer
        指定渲染器文件（高级用法；默认按 :func:`_locate_renderer` 找）。
    grid
        是否画参考网格。默认 ``False``（视频里网格很碍眼）。

    返回
    ----
    :class:`VideoInfo`
    """
    from PIL import Image                                        # 只在真要画帧时才需要

    w, h = _size_tuple(size)
    start = float(start)
    fps = float(fps)

    if renderer is not None:
        rpath = Path(renderer)
        if not rpath.is_file():
            raise VideoEncodeError(
                f"指定的渲染器不存在：{rpath}；给 renderer= 一个存在的 .py 文件，"
                f"或设置环境变量 {RENDERER_ENV}")
        mod = _load_renderer_module(rpath)
    else:
        # 默认走**库自带**的渲染器；只有它不可用时才去磁盘上找外部实现。
        mod = builtin_renderer()
        if mod is None:                                     # pragma: no cover
            rpath = _locate_renderer()
            if rpath is None:
                raise VideoEncodeError(
                    "找不到渲染器：rpechart.render 不可用（通常是没装 Pillow），"
                    "磁盘上也没有备用的渲染器脚本")
            mod = _load_renderer_module(rpath)
    if not hasattr(mod, "Renderer"):                        # pragma: no cover
        raise VideoEncodeError("渲染器里没有 Renderer 类")
    renderer_obj = mod.Renderer(str(chart_path))

    if end is None:
        end = float(renderer_obj.timing.beat_to_second(renderer_obj.content_beats))
    end = float(end)
    if end <= start:
        raise ValueError(f"end({end}) 必须大于 start({start})")

    n_frames = int(round((end - start) * fps))
    if n_frames <= 0:
        raise ValueError(f"区间 {start}~{end}s 在 {fps}fps 下不足一帧")

    bg_img = None
    if cover is not None:
        bg_img = cover if hasattr(cover, "convert") else Image.open(str(cover))
        bg_img = bg_img.convert("RGB")

    # 动图底版：**流式**逐帧解码。早期实现用 _video_loader() 把整片解成 raw
    # 常驻内存 —— 1080p60 的 93 秒 = 5580 帧 × 6.2 MB ≈ 32 GB，机器会爆掉。
    # 渲染是按时间递增取帧的，所以顺序读 + 用完即弃就够了。
    bg_at = None
    stream = None
    if video is not None:
        opener = getattr(mod, "open_video_stream", None)
        if opener is None:                                  # 兼容旧渲染器
            loader = getattr(mod, "_video_loader", None)
            if loader is None:                              # pragma: no cover
                raise VideoEncodeError(
                    f"{rpath} 里没有 open_video_stream()，无法用 video= 做动图底版")
            bg_at = loader(str(video), fps, w, h)
        else:
            bg_at = opener(str(video), fps, w, h)

    def _frames():
        try:
            for i in range(n_frames):
                t = start + i / fps
                bg = bg_img
                if bg_at is not None:
                    frame = bg_at(t)
                    if frame is not None:
                        bg = frame
                yield renderer_obj.frame(t, size=(w, h), bg=bg, hud=hud, grid=grid,
                                         blur=blur, dim=dim)
        finally:
            closer = getattr(bg_at, "close", None) if bg_at is not None else None
            if callable(closer):
                closer()
            if stream is not None:                          # pragma: no cover
                stream.close()

    info = encode_frames(_frames(), out_path, fps=fps, size=(w, h), audio=audio,
                         progress=progress, extra_args=extra_args)
    return info
