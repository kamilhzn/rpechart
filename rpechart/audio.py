"""音频时长探测 —— 不依赖 ffmpeg / mutagen 的纯 Python 实现。

制谱时最需要知道的只有一个数字：**音乐有多少秒**。它决定了音符与事件的时间上限
（超出去 RPE 会报 ``Time Out of Range``），也决定校验器能否工作。

支持 MP3 / WAV / OGG(Opus·Vorbis) / FLAC / M4A(AAC，粗略) 。

优先级：先试系统里的 ffprobe/ffmpeg，再退回纯 Python 解析。
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import typing as _t
from pathlib import Path

__all__ = ["AudioInfo", "probe_audio", "duration_of", "ffmpeg_path"]


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
        for off in range(len(buf) - 4):
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
    for off in range(len(buf) - 4):
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
