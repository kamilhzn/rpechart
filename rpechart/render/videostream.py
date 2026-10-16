"""视频叠底：把原片当背景垫在谱面下面。

渲染循环按时间**递增**取帧，所以这里开一个 ffmpeg 子进程从管道顺序读，
读过的帧立即丢弃 —— 内存里只留一帧。

早期实现（``_video_loader``）会把整片解成 raw 常驻内存：1080p60 的 93 秒是
5580 帧 × 6.2 MB ≈ **32 GB**，机器直接爆掉。那条路只保留给短片/小尺寸。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from ..audio import ffmpeg_path

__all__ = ["VideoStream", "load_video_frames", "open_video_stream"]


def load_video_frames(path, fps, w, h):
    """把一段视频解码成 ``(bytes, n, h, w, fps)``（本模块不依赖 numpy）。"""
    ff = ffmpeg_path("ffmpeg")
    if not ff:
        raise RuntimeError("找不到 ffmpeg，无法解码视频")
    with tempfile.TemporaryDirectory(prefix="rpechart_render_") as td:
        raw = Path(td) / "v.raw"
        cmd = [ff, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
               "-vf", f"fps={fps},scale={w}:{h}", "-pix_fmt", "rgb24",
               "-f", "rawvideo", str(raw)]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg 解码失败: {proc.stderr[:400]!r}")
        data = bytearray(raw.read_bytes())
    stride = w * h * 3
    n = len(data) // stride
    if n == 0:
        return bytearray(), 0, h, w, float(fps)
    return data[:n * stride], n, h, w, float(fps)


def _video_loader(path, fps, w, h):
    """**整片**解码后按时间取帧（只适合短片/小尺寸）。"""
    data, n, vh, vw, vfps = load_video_frames(path, fps, w, h)

    def get(t):
        i = int(round(t * vfps))
        if i < 0 or i >= n:
            return None
        off = i * vw * vh * 3
        return Image.frombuffer("RGB", (vw, vh), bytes(data[off:off + vw * vh * 3]),
                                "raw", "RGB", 0, 1)

    return get


class VideoStream:
    """顺序读取视频帧的流式解码器（内存里只留一帧）。

    只支持时间递增的访问（渲染天然如此）。要回退或大幅前跳就重启解码器，
    跳进用 ``-ss`` 定位（关键帧精确，代价可接受）。
    """

    def __init__(self, path, fps, w, h, *, seek_ok=True):
        self.path = str(path)
        self.fps = float(fps)
        self.w = int(w)
        self.h = int(h)
        self.stride = self.w * self.h * 3
        self._seek_ok = bool(seek_ok)
        self._proc = None
        self._next_t = 0.0

    # ---- 生命周期
    def _start(self, t0=0.0):
        self.close()
        ff = ffmpeg_path("ffmpeg")
        if not ff:
            raise RuntimeError("找不到 ffmpeg，无法解码视频")
        cmd = [ff, "-hide_banner", "-loglevel", "error"]
        if t0 > 1e-6 and self._seek_ok:
            cmd += ["-ss", f"{t0:.6f}"]
        cmd += ["-i", self.path,
                "-vf", f"fps={self.fps},scale={self.w}:{self.h}",
                "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL)
        self._next_t = t0

    def close(self):
        if self._proc is not None:
            try:
                if self._proc.stdout:
                    self._proc.stdout.close()
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __call__(self, t):
        """让它直接当 ``bg_at(t)`` 用，同时保留 ``.close()``。"""
        return self.get(t)

    # ---- 取帧
    def _read_raw(self):
        if self._proc is None or self._proc.stdout is None:
            return None
        buf = self._proc.stdout.read(self.stride)
        if not buf or len(buf) < self.stride:
            return None
        return buf

    def get(self, t):
        """取 ``t`` 秒处的帧；超出片子范围返回 ``None``。"""
        step = 1.0 / self.fps
        if t < self._next_t - step * 0.5:                 # 回退：重开并定位
            self._start(max(0.0, t))
        if self._proc is None:
            self._start(0.0)
        guard = 0
        while self._next_t + step * 0.5 < t:
            if self._read_raw() is None:
                return None
            self._next_t += step
            guard += 1
            if guard > 2_000_000:                          # 防御：异常输入别死循环
                return None
        raw = self._read_raw()
        if raw is None:
            return None
        self._next_t += step
        return Image.frombuffer("RGB", (self.w, self.h), raw, "raw", "RGB", 0, 1)


def open_video_stream(path, fps, w, h):
    """给 ``rpechart.video.render_chart_video(video=...)`` 用的流式取帧对象。

    返回的是 :class:`VideoStream` 本身（可调用、且能被 ``finally`` 关掉），
    不是裸的 bound method —— 否则调用方拿不到 ``close()``，ffmpeg 子进程会泄漏。
    """
    return VideoStream(path, fps, w, h)