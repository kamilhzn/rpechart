"""音频时长探测 —— 不依赖 mutagen 的纯 Python 实现。

制谱时最需要知道的只有一个数字：**音乐有多少秒**。它决定了音符与事件的时间上限
（超出去 RPE 会报 ``Time Out of Range``），也决定校验器能否工作。

支持 MP3 / WAV / OGG(Opus·Vorbis) / FLAC / M4A(AAC，粗略) 。

优先级：先试系统里的 ffprobe/ffmpeg，再退回纯 Python 解析。
"""

from __future__ import annotations

import operator
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import typing as _t
import wave
from array import array
from pathlib import Path

__all__ = [
    "AudioInfo",
    "probe_audio",
    "duration_of",
    "ffmpeg_path",
    "AudioMixError",
    "hitsound_schedule",
    "resolve_se_sources",
    "find_se_dir",
    "build_hitsound_track",
    "SE_VOLUME",
    "SE_OFFSET_TAP",
    "SE_OFFSET_DRAG",
    "SE_BY_NOTE_TYPE",
    "SE_SAMPLE_RATE",
    "SE_CHANNELS",
    "SE_KINDS",
    "SE_TRACK_PREFIX",
    "SE_DIR_ENV",
    "SE_DEDUP_MS",
    "SE_MAX_EVENTS",
    "find_se_dir",
]


class AudioInfo:
    """音频文件的元信息。"""

    __slots__ = ("path", "duration", "sample_rate", "channels", "bitrate", "method", "title")

    def __init__(
        self,
        path: Path,
        duration: float,
        *,
        sample_rate: int = 0,
        channels: int = 0,
        bitrate: int = 0,
        method: str = "",
        title: _t.Optional[str] = None,
    ) -> None:
        self.path = path
        self.duration = duration
        self.sample_rate = sample_rate
        self.channels = channels
        self.bitrate = bitrate
        self.method = method
        self.title = title

    def __repr__(self) -> str:
        m, s = divmod(self.duration, 60)
        return (
            f"<AudioInfo {self.path.name} {int(m)}:{s:06.3f} "
            f"({self.sample_rate}Hz {self.channels}ch, via {self.method})>"
        )

    def __float__(self) -> float:
        return self.duration


# ------------------------------------------------------------------ ffmpeg
def ffmpeg_path(what: str = "ffmpeg") -> _t.Optional[str]:
    """找一个可用的 ffmpeg/ffprobe：先看 PATH，再看 imageio-ffmpeg 的捆绑版。"""
    found = shutil.which(what)
    if found:
        return found
    if what == "ffprobe":
        # imageio-ffmpeg 只捆绑 ffmpeg，ffprobe 一般没有
        return None
    try:  # pragma: no cover - 依赖可选包
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _probe_with_ffprobe(path: Path) -> _t.Optional[AudioInfo]:
    exe = ffmpeg_path("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [
                exe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=sample_rate,channels,bit_rate",
                "-of",
                "default=noprint_wrappers=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    values: dict[str, str] = {}
    for line in out.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            values.setdefault(k.strip(), v.strip())
    try:
        duration = float(values.get("duration", "0"))
    except ValueError:
        return None
    if duration <= 0:
        return None
    return AudioInfo(
        path,
        duration,
        sample_rate=int(values.get("sample_rate", 0) or 0),
        channels=int(values.get("channels", 0) or 0),
        bitrate=int(values.get("bit_rate", 0) or 0),
        method="ffprobe",
    )


# ------------------------------------------------------------------ WAV
def _probe_wav(path: Path) -> _t.Optional[AudioInfo]:
    with path.open("rb") as fh:
        header = fh.read(12)
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            return None
        fmt: tuple[int, int, int, int] | None = None
        data_size = 0
        fact_samples = 0
        while True:
            chunk = fh.read(8)
            if len(chunk) < 8:
                break
            cid, size = chunk[:4], struct.unpack("<I", chunk[4:8])[0]
            if cid == b"fmt ":
                body = fh.read(size)
                if len(body) >= 16:
                    _tag, channels, rate, avg_bytes, _ba, bits = struct.unpack("<HHIIHH", body[:16])
                    fmt = (channels, rate, bits, avg_bytes)
                else:
                    fh.seek(size, 1)
            elif cid == b"fact":
                # 非 PCM 格式按规范必须带 fact，dwSampleLength 就是每声道样本数
                body = fh.read(size)
                if len(body) >= 4:
                    fact_samples = struct.unpack("<I", body[:4])[0]
                if size & 1:
                    fh.seek(1, 1)
            elif cid == b"data":
                data_size = size
                fh.seek(size, 1)
            else:
                fh.seek(size + (size & 1), 1)
        if fmt is None:
            return None
        channels, rate, bits, avg_bytes = fmt
        if rate <= 0 or channels <= 0:
            return None
        byte_rate = rate * channels * bits // 8
        if byte_rate <= 0:
            # wBitsPerSample 对 GSM 之类的格式没有意义（写 0），只能用 nAvgBytesPerSec
            byte_rate = avg_bytes
        if byte_rate <= 0:
            return None
        # 压缩格式的样本数只能来自 fact；PCM 没有 fact，按字节率折算
        duration = fact_samples / rate if fact_samples else data_size / byte_rate
        return AudioInfo(
            path,
            duration,
            sample_rate=rate,
            channels=channels,
            bitrate=byte_rate * 8,
            method="wav-header",
        )


# ------------------------------------------------------------------ MP3
def _skip_id3(fh) -> int:
    head = fh.read(10)
    if len(head) < 10 or head[:3] != b"ID3":
        fh.seek(0)
        return 0
    size = (
        (head[6] & 0x7F) << 21
        | (head[7] & 0x7F) << 14
        | (head[8] & 0x7F) << 7
        | (head[9] & 0x7F)
    )
    total = 10 + size
    if head[5] & 0x10:  # footer
        total += 10
    fh.seek(total)
    return total


_MP3_RATES = {
    0b11: (44100, 48000, 32000),  # MPEG1
    0b10: (22050, 24000, 16000),  # MPEG2
    0b00: (11025, 12000, 8000),  # MPEG2.5
}
_MP3_BITRATES = {
    0b11: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0],
    0b10: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
    0b00: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0],
}


def _parse_mp3_frame(buf: bytes, offset: int):
    """解析位于 ``offset`` 的 MP3 帧头，返回 (帧长, 采样率, 比特率, 声道, 每帧样本数)。"""
    if offset + 4 > len(buf):
        return None
    h = buf[offset : offset + 4]
    if h[0] != 0xFF or (h[1] & 0xE0) != 0xE0:
        return None
    ver = (h[1] >> 3) & 0b11  # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
    layer = (h[1] >> 1) & 0b11
    bitrate_idx = (h[2] >> 4) & 0b1111
    rate_idx = (h[2] >> 2) & 0b11
    padding = (h[2] >> 1) & 1
    channel_mode = (h[3] >> 6) & 0b11
    if ver == 1 or layer == 0 or bitrate_idx in (0, 15) or rate_idx == 3:
        return None
    rate = _MP3_RATES[ver][rate_idx]
    bitrate = _MP3_BITRATES[ver][bitrate_idx] * 1000
    if bitrate <= 0 or rate <= 0:
        return None
    if layer == 3:  # Layer I
        frame_len = (12 * bitrate // rate + padding) * 4
        samples = 384
    elif layer == 2:  # Layer II
        frame_len = 144 * bitrate // rate + padding
        samples = 1152
    else:  # Layer III
        if ver == 0b11:
            frame_len = 144 * bitrate // rate + padding
            samples = 1152
        else:
            frame_len = 72 * bitrate // rate + padding
            samples = 576
    channels = 1 if channel_mode == 3 else 2
    return frame_len, rate, bitrate, channels, samples


def _probe_mp3(path: Path) -> _t.Optional[AudioInfo]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        start = _skip_id3(fh)
        # 找第一帧同步
        buf = fh.read(min(65536, size - start))
        first: tuple | None = None
        first_off = -1
        # 上界是 len-3 而不是 len-4：帧头正好压在缓冲区最后 4 字节时
        # _parse_mp3_frame 仍能解出完整帧，漏掉它就是漏掉一个合法起点。
        for off in range(len(buf) - 3):
            parsed = _parse_mp3_frame(buf, off)
            if parsed:
                nxt = _parse_mp3_frame(buf, off + parsed[0])
                if nxt is not None or off + parsed[0] >= len(buf):
                    first = parsed
                    first_off = off
                    break
        if first is None:
            return None
        frame_len, rate, bitrate, channels, samples = first

        # Xbox/Info 标签里带总帧数时可直接算
        frames = _mp3_xing_frames(buf, first_off)
        if frames:
            return AudioInfo(
                path,
                frames * samples / rate,
                sample_rate=rate,
                channels=channels,
                bitrate=bitrate,
                method="mp3-xing",
            )

        # 否则按 CBR 估算，并用尾部若干帧校正
        audio_bytes = size - start
        if bitrate > 0:
            duration = audio_bytes * 8 / bitrate
        else:
            duration = 0.0
        # 用文件尾部真实帧率做一次校验
        tail_rate = _mp3_tail_bitrate(path, size)
        if tail_rate and tail_rate != bitrate:
            duration = audio_bytes * 8 / tail_rate
            bitrate = tail_rate
        if duration <= 0:
            return None
        return AudioInfo(
            path,
            duration,
            sample_rate=rate,
            channels=channels,
            bitrate=bitrate,
            method="mp3-cbr",
        )


def _mp3_xing_frames(buf: bytes, first_off: int) -> _t.Optional[int]:
    if first_off + 4 > len(buf):
        return None
    h = buf[first_off : first_off + 4]
    ver = (h[1] >> 3) & 0b11
    channel_mode = (h[3] >> 6) & 0b11
    # 侧信息长度
    if ver == 0b11:
        side = 17 if channel_mode == 3 else 32
    else:
        side = 9 if channel_mode == 3 else 17
    tag_off = first_off + 4 + side
    if tag_off + 16 > len(buf):
        return None
    tag = buf[tag_off : tag_off + 4]
    if tag not in (b"Xing", b"Info"):
        return None
    flags = struct.unpack(">I", buf[tag_off + 4 : tag_off + 8])[0]
    if not (flags & 1):
        return None
    return struct.unpack(">I", buf[tag_off + 8 : tag_off + 12])[0]


def _mp3_tail_bitrate(path: Path, size: int) -> _t.Optional[int]:
    chunk = min(16384, size)
    with path.open("rb") as fh:
        fh.seek(size - chunk)
        buf = fh.read(chunk)
    for off in range(len(buf) - 3):
        parsed = _parse_mp3_frame(buf, off)
        if parsed:
            return parsed[2]
    return None


# ------------------------------------------------------------------ FLAC
def _probe_flac(path: Path) -> _t.Optional[AudioInfo]:
    with path.open("rb") as fh:
        if fh.read(4) != b"fLaC":
            return None
        while True:
            head = fh.read(4)
            if len(head) < 4:
                return None
            last = head[0] & 0x80
            btype = head[0] & 0x7F
            length = int.from_bytes(head[1:4], "big")
            body = fh.read(length)
            if btype == 0 and len(body) >= 18:  # STREAMINFO
                # 采样率 20bit | 声道-1 3bit | 位深-1 5bit | 总样本 36bit
                packed = int.from_bytes(body[10:18], "big")
                rate = (packed >> 44) & 0xFFFFF
                channels = ((packed >> 41) & 0x7) + 1
                bits = ((packed >> 36) & 0x1F) + 1
                total = packed & 0xFFFFFFFFF
                if rate > 0 and total > 0:
                    return AudioInfo(
                        path,
                        total / rate,
                        sample_rate=rate,
                        channels=channels,
                        bitrate=rate * channels * bits,
                        method="flac-streaminfo",
                    )
                return None
            if last:
                return None


# ------------------------------------------------------------------ OGG
def _probe_ogg(path: Path) -> _t.Optional[AudioInfo]:
    data = path.read_bytes()
    idx = data.find(b"\x01vorbis")
    if idx != -1 and idx + 30 <= len(data):
        rate = struct.unpack("<I", data[idx + 12 : idx + 16])[0]
        channels = data[idx + 11]
        # 用文件大小 / 平均码率估算（Vorbis 无总长字段，靠 granule 才准）
        granule = _ogg_last_granule(data)
        if granule and rate > 0:
            return AudioInfo(
                path,
                granule / rate,
                sample_rate=rate,
                channels=channels,
                method="ogg-granule",
            )
    idx = data.find(b"OpusHead")
    if idx != -1 and idx + 19 <= len(data):
        channels = data[idx + 9]
        rate = struct.unpack("<I", data[idx + 12 : idx + 16])[0]
        granule = _ogg_last_granule(data)
        if granule:
            # Opus 的 granule 固定是 48kHz
            return AudioInfo(
                path,
                granule / 48000.0,
                sample_rate=rate or 48000,
                channels=channels,
                method="opus-granule",
            )
    return None


def _ogg_last_granule(data: bytes) -> _t.Optional[int]:
    pos = data.rfind(b"OggS")
    if pos < 0 or pos + 14 > len(data):
        return None
    return struct.unpack("<q", data[pos + 6 : pos + 14])[0]


# ------------------------------------------------------------------ M4A/MP4
def _probe_mp4(path: Path) -> _t.Optional[AudioInfo]:
    """解析 MP4/M4A/MOV 容器。

    注意：**很多"mp3"其实是 MP4 封装**（网易云/Bilibili 下载、格式转换器的常见产物），
    所以这个函数会参与兜底嗅探，不能只看后缀。
    优先用音轨自身 ``mdhd``（精度最高），其次 ``mvhd``。
    """
    data = path.read_bytes()

    rate = 0
    channels = 0
    # stsd 内的 AudioSampleEntry：找到采样描述子（'mp4a'/'alac'/…）后往里读
    # 布局（相对描述子起始位置）：4B 四字码 → 6B 保留 → 2B data_reference_index
    #   → 8B 保留 → 2B channelcount → 2B samplesize → 2B pre_defined → 2B 保留
    #   → 4B samplerate(16.16 定点)
    for tag in (b"mp4a", b"alac", b"Opus", b"ac-3", b"ec-3"):
        pos = data.find(tag)
        if pos == -1 or pos + 32 > len(data):
            continue
        ch = struct.unpack(">H", data[pos + 20 : pos + 22])[0]
        sr = struct.unpack(">I", data[pos + 28 : pos + 32])[0] >> 16
        if 0 < ch <= 8 and 4000 < sr <= 384000:
            channels, rate = ch, sr
            break
        # 少数封装会多一层，宽松兜底：在附近扫描合理的 (声道, 采样率) 组合
        for off in range(pos + 16, min(pos + 64, len(data) - 4)):
            ch2 = struct.unpack(">H", data[off : off + 2])[0]
            sr2 = struct.unpack(">I", data[off + 8 : off + 12])[0] >> 16
            if 0 < ch2 <= 8 and 4000 < sr2 <= 384000:
                channels, rate = ch2, sr2
                break
        if rate:
            break

    # 音轨 mdhd：最长的那条（音频通常比视频/文本长）
    best: tuple[float, int, int] | None = None  # (duration, timescale, at)
    start = 0
    while True:
        pos = data.find(b"mdhd", start)
        if pos == -1:
            break
        start = pos + 4
        if pos + 32 > len(data):
            continue
        version = data[pos + 4]
        if version == 0:
            ts = struct.unpack(">I", data[pos + 16 : pos + 20])[0]
            dur = struct.unpack(">I", data[pos + 20 : pos + 24])[0]
        else:
            ts = struct.unpack(">I", data[pos + 24 : pos + 28])[0]
            dur = struct.unpack(">Q", data[pos + 28 : pos + 36])[0]
        # 分片 MP4（moof/mvex）的 mdhd 时长写 0，总长在 mehd 里
        if ts > 0 and dur > 0:
            secs = dur / ts
            if best is None or secs > best[0]:
                best = (secs, ts, pos)

    if best is not None:
        return AudioInfo(path, best[0], sample_rate=rate, channels=channels, method="mp4-mdhd")

    mvhd = data.find(b"mvhd")
    movie_ts = 0
    if mvhd != -1 and mvhd + 36 <= len(data):
        if data[mvhd + 4] == 0:
            movie_ts = struct.unpack(">I", data[mvhd + 16 : mvhd + 20])[0]
            duration = struct.unpack(">I", data[mvhd + 20 : mvhd + 24])[0]
        else:
            movie_ts = struct.unpack(">I", data[mvhd + 24 : mvhd + 28])[0]
            duration = struct.unpack(">Q", data[mvhd + 28 : mvhd + 36])[0]
        if movie_ts and duration:
            return AudioInfo(
                path,
                duration / movie_ts,
                sample_rate=rate,
                channels=channels,
                method="mp4-mvhd",
            )

    # 最后再看 mehd（分片总时长，单位是 mvhd 的 timescale）
    mehd = data.find(b"mehd")
    if mehd != -1 and movie_ts:
        if data[mehd + 4] == 0:
            frag = struct.unpack(">I", data[mehd + 8 : mehd + 12])[0]
        else:
            frag = struct.unpack(">Q", data[mehd + 8 : mehd + 16])[0]
        if frag:
            return AudioInfo(
                path,
                frag / movie_ts,
                sample_rate=rate,
                channels=channels,
                method="mp4-mehd",
            )
    return None


# ------------------------------------------------------------------ 魔数嗅探
_MAGIC = (
    (b"RIFF", _probe_wav),
    (b"fLaC", _probe_flac),
    (b"OggS", _probe_ogg),
    (b"ID3", _probe_mp3),
    (b"\xff\xfb", _probe_mp3),
    (b"\xff\xf3", _probe_mp3),
    (b"\xff\xfa", _probe_mp3),
)


def _sniff(path: Path) -> _t.Optional[_t.Callable[[Path], _t.Optional[AudioInfo]]]:
    """按文件头判断真实格式，忽略扩展名与编码。"""
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError:
        return None
    for magic, fn in _MAGIC:
        if head.startswith(magic):
            return fn
    # MP4 家族：前 4 字节是 box 长度，5-8 字节是 ftyp
    if len(head) >= 12 and head[4:8] in (b"ftyp", b"styp", b"moov", b"free", b"skip"):
        return _probe_mp4
    return None


# ------------------------------------------------------------------ 统一入口
_PROBES = {
    ".wav": _probe_wav,
    ".wave": _probe_wav,
    ".mp3": _probe_mp3,
    ".flac": _probe_flac,
    ".ogg": _probe_ogg,
    ".oga": _probe_ogg,
    ".opus": _probe_ogg,
    ".m4a": _probe_mp4,
    ".mp4": _probe_mp4,
    ".aac": _probe_mp4,
}


def probe_audio(path: _t.Union[str, os.PathLike]) -> AudioInfo:
    """探测音频时长。识别不了时抛 :class:`ValueError`。

    顺序：ffprobe → 魔数嗅探（忽略扩展名）→ 按后缀 → 全部兜底。
    之所以要嗅探，是因为大量"mp3"其实是 MP4/AAC 封装。
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"音频不存在: {p}")
    info = _probe_with_ffprobe(p)
    if info is not None:
        return info

    order: list[_t.Callable[[Path], _t.Optional[AudioInfo]]] = []
    sniffed = _sniff(p)
    if sniffed is not None:
        order.append(sniffed)
    by_suffix = _PROBES.get(p.suffix.lower())
    if by_suffix is not None and by_suffix not in order:
        order.append(by_suffix)
    for fn in (_probe_mp3, _probe_ogg, _probe_wav, _probe_flac, _probe_mp4):
        if fn not in order:
            order.append(fn)

    for fn in order:
        try:
            info = fn(p)
        except Exception:
            continue
        if info is not None and info.duration > 0:
            return info
    raise ValueError(f"无法识别音频格式或读取时长: {p}")


def duration_of(path: _t.Union[str, os.PathLike]) -> float:
    """只要秒数。"""
    return probe_audio(path).duration


# ------------------------------------------------------------------ 歌词/标题（可选）
_LRC_TIME = re.compile(r"\[(\d+):(\d+(?:[.:]\d+)?)\]")


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """解析 ``.lrc`` 歌词，返回 ``[(秒, 文本), ...]``。

    制谱时很有用：歌词时间轴可以直接拿来卡判定线动画和长条的起止。
    """
    out: list[tuple[float, str]] = []
    for line in text.splitlines():
        stamps = list(_LRC_TIME.finditer(line))
        if not stamps:
            continue
        content = _LRC_TIME.sub("", line).strip()
        for m in stamps:
            minutes = int(m.group(1))
            sec = float(m.group(2).replace(":", "."))
            out.append((minutes * 60 + sec, content))
    out.sort(key=lambda x: x[0])
    return out


class AudioMixError(RuntimeError):
    """打击音混音失败。调用方（``render_chart_video``）据此退化为「不混 SE」。"""


#: 默认音量（PhiEdit Settings.json 的 SEVolume）。
SE_VOLUME = 0.532
#: Tap/Flick 默认偏移（秒）。
SE_OFFSET_TAP = 0.0
#: Drag 默认偏移（秒）；-30ms = 音效比音符开始时间早 30ms。
SE_OFFSET_DRAG = -0.030
#: 音符类型 -> 打击音名（Hold 的打击音在头部，所以也是 tap）。
SE_BY_NOTE_TYPE = {1: "tap", 2: "tap", 3: "flick", 4: "drag"}
#: 三类打击音，也是混音时的固定顺序。
SE_KINDS = ("tap", "flick", "drag")
#: 中间 WAV 的临时文件前缀（测试按它做「有没有残留」的断言）。
SE_TRACK_PREFIX = "rpechart-se-"
#: SE 目录的环境变量。
SE_DIR_ENV = "RPECHART_SE_DIR"
#: 中间 WAV 的采样格式：48kHz 立体声 16bit，之后交给 ffmpeg 编成 AAC。
SE_SAMPLE_RATE = 48000
SE_CHANNELS = 2
#: 去重粒度（毫秒）。默认 0 = **不去重**：游戏里每个音符各响一次，双押就是两声，
#: 叠加到同一个样本上（超限时饱和夹紧）。给个正数才会把同一毫秒的同类合并。
SE_DEDUP_MS = 0.0
#: 事件表上限：超过就按时间均匀抽稀（正常谱面远远碰不到）。
SE_MAX_EVENTS = 60000

_SE_EXTS = (".mp3", ".ogg", ".wav", ".m4a", ".flac", ".opus", ".oga")
#: 解码缓存：同一进程里同一个文件只解一次（键含音量）。
_PCM_CACHE: dict = {}
_PCM_CACHE_MAX = 8
#: 叠加时的分批样本数。array 支持缓冲协议，按批做 map(operator.add) 比
#: 逐元素生成器快一倍以上（见 tests/test_se_audio.py 的混音用例）。
_MIX_CHUNK = 1 << 15


def _fmt_num(value: float) -> str:
    """把数字写成 ffmpeg 认的短形式：``0.532`` 保持，``2.0`` 写成 ``2``。"""
    text = "%.6f" % float(value)
    text = text.rstrip("0").rstrip(".")
    return text or "0"


def find_se_dir(start=None) -> _t.Optional[str]:
    """找官方 SE 目录（含 tap/flick/drag 的那一级）。找不到返回 ``None``。

    顺序：环境变量 ``RPECHART_SE_DIR`` → 与贴图同一套向上查找（``PhiEdit/Resources/SE``）
    → 从 ``start``（通常是谱面目录）逐级向上碰 ``PhiEdit/Resources/SE``。
    """
    env = os.environ.get(SE_DIR_ENV)
    if env and os.path.isdir(env):
        return env
    try:
        from .render.assets import find_resources

        res = find_resources(start)
        if res is not None:
            cand = os.path.join(str(res), "SE")
            if os.path.isdir(cand):
                return cand
    except Exception:
        pass  # 没装 Pillow 时 render.assets 不可用，继续按路径找
    base = Path(start).resolve() if start else Path(__file__).resolve()
    for parent in [base] + list(base.parents):
        for cand in (
            parent / "PhiEdit" / "Resources" / "SE",
            # 常见布局：仓库与 PhiEdit 同级放在一个父目录下
            # （例如 F:/rpechart 与 F:/Phi/PhiEdit/Resources/SE）。
            parent / "Phi" / "PhiEdit" / "Resources" / "SE",
            parent / "SE",
        ):
            try:
                if cand.is_dir():
                    return str(cand)
            except OSError:
                continue
    return None


def resolve_se_sources(se_dir, mapping=None) -> dict:
    """把「SE 目录」或「{类型: 路径}」解析成实际存在的 ``{类型: 路径}``。

    只认 tap / flick / drag；文件缺失的那类直接缺席（调用方跳过对应音符），
    所以 SE 目录不全时不会让导出失败。``mapping`` 用于覆盖单个类型。
    """
    out: dict = {}
    if isinstance(se_dir, dict):
        for kind, path in se_dir.items():
            if kind in SE_KINDS and path and os.path.isfile(str(path)):
                out[kind] = str(path)
    elif se_dir:
        root = str(se_dir)
        if os.path.isdir(root):
            for kind in SE_KINDS:
                for ext in _SE_EXTS:
                    cand = os.path.join(root, kind + ext)
                    if os.path.isfile(cand):
                        out[kind] = cand
                        break
    if mapping:
        for kind, path in mapping.items():
            if kind in SE_KINDS and path and os.path.isfile(str(path)):
                out[kind] = str(path)
    return out


def _as_chart(chart):
    """``chart`` 可以是 :class:`~rpechart.model.Chart` 或谱面文件路径。"""
    if hasattr(chart, "lines"):
        return chart
    from .io import load_chart

    return load_chart(chart)


def hitsound_schedule(chart, *, start=None, end=None, offset_tap=SE_OFFSET_TAP,
                      offset_drag=SE_OFFSET_DRAG, timing=None,
                      dedup_ms=SE_DEDUP_MS, max_events=SE_MAX_EVENTS) -> list:
    """谱面 -> ``[(秒, 打击音名), ...]``（按时间升序）。

    遍历**所有判定线**的所有音符：跳过 ``isFake``；Hold 取 ``startTime``（头部）；
    音符自带 ``hitsound`` 且是 tap/flick/drag 之一时以它为准。时间换算用谱面的
    ``BPMList``（判定线 ``bpmfactor`` 在真实谱面里都是 1.0，这里不缩放）。

    ``start`` / ``end`` 是**含头不含尾**的秒窗口，``None`` 表示不裁剪。
    ``dedup_ms`` 是去重粒度：落在同一个 ``dedup_ms`` 毫秒格里的同类只留一个
    （``0``（默认）表示不去重）；``max_events`` 是事件表上限（超出按时间均匀抽稀）。
    """
    chart = _as_chart(chart)
    if timing is None:
        from .timing import Timing

        timing = Timing(chart.BPMList)
    out: list = []
    seen = set()
    for line in chart.lines:
        for note in getattr(line, "notes", ()) or ():
            if note.is_fake:
                continue
            kind = note.hitsound if note.hitsound in SE_KINDS else SE_BY_NOTE_TYPE.get(note.type)
            if kind is None:
                continue
            t = timing.beat_to_second(note.startTime)
            t += offset_drag if kind == "drag" else offset_tap
            if start is not None and t < start:
                continue
            if end is not None and t >= end:
                continue
            if dedup_ms:
                slot = int(round(t * 1000.0 / dedup_ms))
                key = (kind, slot)
                if key in seen:
                    continue
                seen.add(key)
            out.append((t, kind))
    out.sort(key=lambda item: (item[0], SE_KINDS.index(item[1])))
    if max_events and len(out) > max_events:
        stride = len(out) / float(max_events)
        out = [out[int(i * stride)] for i in range(max_events)]
    return out


def _source_duration(path) -> float:
    """素材时长（秒）；探测不出来就当 0（只影响自动推断的轨长）。"""
    if not path:
        return 0.0
    try:
        return max(0.0, float(duration_of(path)))
    except Exception:
        return 0.0


def _decode_pcm(path, volume=1.0, ffmpeg=None, rate=SE_SAMPLE_RATE):
    """把任意音频解成 ``array("h")`` 的交错立体声 PCM（``volume`` 在 ffmpeg 里缩放）。

    只走 **stdout**：``-f s16le -`` 输出原始样本，Python 侧拿到就能直接叠加。
    同一文件 + 同一音量在一个进程里只解一次（密集音符会引用同一份样本）。
    """
    key = (os.path.abspath(str(path)), float(volume), int(rate))
    hit = _PCM_CACHE.get(key)
    if hit is not None:
        return hit
    exe = ffmpeg or ffmpeg_path("ffmpeg")
    if not exe:
        raise AudioMixError(
            "找不到 ffmpeg，无法把打击音混进音轨（装 imageio-ffmpeg 或把 ffmpeg 放进 PATH）")
    cmd = [str(exe), "-hide_banner", "-loglevel", "error"]
    if float(volume) != 1.0:
        cmd += ["-i", str(path), "-af", "volume=%s" % _fmt_num(volume)]
    else:
        cmd += ["-i", str(path)]
    cmd += ["-vn", "-ac", str(SE_CHANNELS), "-ar", str(int(rate)),
            "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True)
    except OSError as exc:
        raise AudioMixError("调用 ffmpeg 失败：%s" % exc) from exc
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-4:]
        raise AudioMixError("打击音解码失败（%s，ffmpeg exit %d）：%s"
                            % (os.path.basename(str(path)), proc.returncode,
                               " / ".join(tail)))
    pcm = array("h")
    pcm.frombytes(proc.stdout[:len(proc.stdout) - (len(proc.stdout) % 2)])
    if sys.byteorder == "big":
        pcm.byteswap()
    if len(_PCM_CACHE) >= _PCM_CACHE_MAX:
        _PCM_CACHE.clear()
    _PCM_CACHE[key] = pcm
    return pcm


#: 已经把 buffer 写到第几帧了（完全落在后面的素材直接整段拷贝，跳过逐样本加法）。
_TAIL = [0]


def _remove(path):
    """删掉可能存在的文件（清理中间产物时不在乎失败）。"""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _overlap_add(buf, extra, frame):
    """把 ``extra``（交织样本）饱和叠加到 ``buf`` 的第 ``frame`` 帧上（原地修改）。

    返回这一段的**结束帧号**；``_TAIL`` 记录 buffer 已经连续写到第几帧，
    因此调用方必须按时间升序喂事件。**饱和**加法：正负都要夹在 int16 里，
    直接 ``operator.add`` 会抛 OverflowError（Python 的 array 不做回绕）。
    """
    if frame < 0:                                       # 越界到轨头之前：裁掉多余部分
        extra = extra[frame * SE_CHANNELS:]
        frame = 0
    pos = frame * SE_CHANNELS if frame >= 0 else 0
    room = len(buf) - pos
    if room <= 0:
        return frame
    if len(extra) > room:
        extra = extra[:room]
    if not extra:
        return frame
    tail = _TAIL[0]
    end = frame + len(extra) // SE_CHANNELS
    if frame >= tail:                                   # 完全落在已写区之外：直接拷贝
        buf[pos:pos + len(extra)] = extra
        _TAIL[0] = end
        return end
    overlap = min(len(extra), (tail - frame) * SE_CHANNELS)
    seg = buf[pos:pos + overlap]
    add = extra[:overlap]
    step = _MIX_CHUNK - (_MIX_CHUNK % SE_CHANNELS)
    i = 0
    while i < overlap:
        j = min(overlap, i + step)
        x, y = seg[i:j], add[i:j]
        try:
            seg[i:j] = array("h", map(operator.add, x, y))
        except OverflowError:                           # 罕见的叠加溢出：这一批逐样本夹紧
            seg[i:j] = array("h", [
                -32768 if v < -32768 else (32767 if v > 32767 else v)
                for v in map(operator.add, x, y)])
        i = j
    buf[pos:pos + overlap] = seg
    rest = extra[overlap:]
    if rest:                                            # 超出已写区的尾巴：接在后面
        start = pos + overlap
        buf[start:start + len(rest)] = rest
    if end > tail:
        _TAIL[0] = end
    return end


def _write_wav(path, buf, frames, rate=SE_SAMPLE_RATE):
    """把交错 PCM 写成 48kHz 立体声 16bit WAV（原子替换，不留半截文件）。"""
    tmp = "%s.part" % path
    data = buf.tobytes() if sys.byteorder == "little" else _swapped_bytes(buf)
    need = int(frames) * SE_CHANNELS * 2
    if len(data) < need:
        data += b"\x00" * (need - len(data))
    else:
        data = data[:need]
    try:
        with wave.open(tmp, "wb") as fh:
            fh.setnchannels(SE_CHANNELS)
            fh.setsampwidth(2)
            fh.setframerate(int(rate))
            fh.writeframes(data)
        os.replace(tmp, path)
    except Exception:
        _remove(tmp)
        raise


def _swapped_bytes(buf):
    copy = array("h", buf)
    copy.byteswap()
    return copy.tobytes()


def build_hitsound_track(chart, *, se_dir=None, out_path=None, volume=SE_VOLUME,
                         offset_tap=SE_OFFSET_TAP, offset_drag=SE_OFFSET_DRAG,
                         song=None, duration=None, start=0.0, end=None,
                         ffmpeg=None, sources=None, sample_rate=SE_SAMPLE_RATE) -> str:
    """把谱面打击音（可选叠上原曲）混成一个 48kHz 立体声 WAV，返回它的路径。

    实现方式：每个素材用 ffmpeg **一次**解成 ``pcm_s16le`` 原始样本（走 stdout），
    然后在 Python 里按下标做**饱和叠加**，最后用标准库 ``wave`` 落盘。之所以不是
    一条 ``filter_complex``：ade​​lay+amix 的耗时随音符数是超线性的（实测 400 音符
    20s、2000 音符 166s），而逐样本叠加只跟「音符数 × 素材长度」成正比（2000 音符
    0.06s），且不依赖 adelay 的行为细节。

    参数
    ----
    chart
        谱面对象或 ``chart.json`` 路径。
    se_dir
        官方 SE 目录（``tap.mp3`` / ``flick.mp3`` / ``drag.mp3``），或
        ``{类型: 路径}`` 映射。``None`` 表示不解析出任何 SE。
    out_path
        中间 WAV 的落点。``None`` 时在系统临时目录新建（**调用方负责删除**）。
    volume
        打击音音量，默认 :data:`SE_VOLUME`（0.532，来自 PhiEdit 设置）。
    offset_tap / offset_drag
        秒。Tap/Flick 与 Drag 分开设置（默认 0 与 -30ms）。
    song
        原曲；给了就一起混（这样调用方可以把结果直接当音轨用）。
    duration
        目标时长（**视频**时长，秒）。``None`` 时按「最后一个打击音 + 它自己的
        时长」与原曲长度里较长者推断。比目标短就补静音，长就截断。
    start / end
        秒。视频窗口的起点/终点；``start`` 非 0 时原曲从该点开始截取，返回的轨
        第 0 帧就是 ``start`` 秒。
    ffmpeg
        指定 ffmpeg 可执行文件（默认 :func:`ffmpeg_path`）。
    sources
        已经解析好的 ``{类型: 路径}``；给了就忽略 ``se_dir`` 的解析。
    sample_rate
        输出采样率，默认 :data:`SE_SAMPLE_RATE`（48000）。

    失败一律抛 :class:`AudioMixError`（半截文件会被删掉），由调用方决定退化。
    没有任何音符、也没有原曲时**不调 ffmpeg**，直接写静音 WAV。
    """
    start = float(start)
    rate = int(sample_rate)
    if duration is not None:
        duration = float(duration)
        if duration <= 0:
            duration = None
    if end is not None:
        end = float(end)
    src = dict(sources) if sources is not None else resolve_se_sources(se_dir)

    window_end = end if end is not None else (start + duration if duration is not None else None)
    sched = hitsound_schedule(chart, start=start, end=window_end,
                              offset_tap=offset_tap, offset_drag=offset_drag)

    if out_path is None:
        fd, out_path = tempfile.mkstemp(prefix=SE_TRACK_PREFIX, suffix=".wav")
        os.close(fd)
    out_path = str(out_path)
    try:
        parent = os.path.dirname(os.path.abspath(out_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
    except OSError:
        pass

    events = [(t, kind) for t, kind in sched if kind in src]
    if duration is None:
        total = (end - start) if end is not None else 0.0
        for t, kind in events:
            total = max(total, (t - start) + _source_duration(src[kind]))
        if song is not None:
            total = max(total, _source_duration(song) - start)
        duration = max(0.0, total)

    frames = int(round(duration * rate))
    if frames <= 0 and not events and song is None:
        frames = 1

    buf = array("h", bytes(SE_CHANNELS * 2 * frames)) if frames > 0 else array("h")
    _TAIL[0] = 0
    try:
        if song is not None:
            song_pcm = _decode_pcm(song, 1.0, ffmpeg, rate)
            offset = int(round(max(0.0, start) * rate)) * SE_CHANNELS
            piece = song_pcm[offset:] if offset < len(song_pcm) else array("h")
            n = min(len(piece), len(buf))
            if n > 0:
                buf[:n] = piece[:n]
                _TAIL[0] = n // SE_CHANNELS
        cache: dict = {}
        for kind in sorted({k for _, k in events}, key=SE_KINDS.index):
            cache[kind] = _decode_pcm(src[kind], volume, ffmpeg, rate)
        for t, kind in sorted(events, key=lambda item: (item[0], SE_KINDS.index(item[1]))):
            frame = int(round((t - start) * rate))
            if frame >= frames:
                continue                                    # 落在视频之外（-t 会截掉）
            _overlap_add(buf, cache[kind], frame)
        _write_wav(out_path, buf, frames, rate)
    except Exception:
        _remove(out_path)
        _remove("%s.part" % out_path)
        raise
    return out_path

