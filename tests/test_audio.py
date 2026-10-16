"""audio.py —— 音频时长探测。

两条路径分开测：

1. **纯 Python 解析器**（WAV / MP3 / FLAC / Ogg / MP4）——用逐字节手工构造的合成
   文件钉死格式布局，这样断言的是「规范」，而不是「本机某个样例文件碰巧的结果」。
2. **``probe_audio`` 的调度**：ffprobe → 魔数嗅探 → 按后缀 → 全部兜底。
   其中「魔数嗅探优先于后缀」是模块存在的理由（大量 .mp3 其实是 MP4 封装），
   所以既直接断言 ``_sniff`` 的返回值，也端到端断言解析出来的 ``method``。

所有合成文件都写在临时目录里。另有两条「真实世界踩过的坑」的回归用例，读的
是 ``tests/fixtures/media/`` 里**由本库自己合成**的两个容器（``make_fixtures.py``
生成，不含任何第三方素材）：一份是**伪装成 .mp3 的 MP4**（mdhd 时长为 0，必须
回落到 mvhd），一份是**没有音轨的 MP4**（采样率/声道都是 0）。

关于 ffprobe：本机 ``ffmpeg_path("ffprobe")`` 为 None（imageio-ffmpeg 只捆绑
ffmpeg），所以下面所有 ``method`` 断言用 :func:`_via` 写，它同时接受 ``"ffprobe"``
以便在有 ffprobe 的机器上不至于误报。
"""

import io
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from run_tests import approx, raises, test

from rpechart._bootstrap import find_repo_root

_ROOT = find_repo_root(__file__)

from rpechart.audio import (
    _MAGIC,
    _PROBES,
    AudioInfo,
    _mp3_xing_frames,
    _ogg_last_granule,
    _parse_mp3_frame,
    _probe_flac,
    _probe_mp3,
    _probe_mp4,
    _probe_ogg,
    _probe_wav,
    _probe_with_ffprobe,
    _skip_id3,
    _sniff,
    duration_of,
    ffmpeg_path,
    parse_lrc,
    probe_audio,
)

#: 合成容器样本（由 ``tests/fixtures/make_fixtures.py`` 生成，随仓库走）。
FIXTURES = _ROOT / "tests" / "fixtures" / "media"
REAL_AUDIO = FIXTURES / "disguised.mp3"   # 伪装成 mp3 的 MP4
REAL_VIDEO = FIXTURES / "silent.mp4"      # 没有音轨的 MP4


class _Tmp:
    """给每个用例一个干净临时目录，退出时自动删。"""

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="rpechart_audio_")
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)
        if hasattr(self, "path"):
            self.path = None
        return False


def _via(info, expected):
    """断言解析路径。有 ffprobe 的机器上它会先命中，那就允许 ``"ffprobe"``。"""
    assert info.method in (expected, "ffprobe"), f"method={info.method!r}, 期望 {expected!r}"


# ------------------------------------------------------------------ WAV 构造
def _chunk(cid: bytes, payload: bytes, pad: bool = True) -> bytes:
    out = cid + struct.pack("<I", len(payload)) + payload
    if pad and (len(payload) & 1):
        out += b"\x00"
    return out


def _wav(
    rate: int = 8000,
    channels: int = 1,
    bits: int = 16,
    data_size: int = 16000,
    fmt_tag: int = 1,
    fmt_extra: bytes = b"",
    between: tuple = (),
) -> bytes:
    block_align = channels * bits // 8
    byte_rate = rate * block_align
    fmt_body = struct.pack(
        "<HHIIHH", fmt_tag, channels, rate, byte_rate, block_align, bits
    ) + fmt_extra
    body = b"WAVE" + _chunk(b"fmt ", fmt_body)
    for c in between:
        body += c
    body += _chunk(b"data", b"\x00" * data_size)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _write(directory, name: str, raw: bytes) -> str:
    p = os.path.join(directory, name)
    with open(p, "wb") as fh:
        fh.write(raw)
    return p


# ------------------------------------------------------------------ MP3 构造
# MPEG1 / Layer III / 192 kbps / 48 kHz / 立体声
# 144 * 192000 // 48000 == 576 —— 整除，所以「按字节估算」与「按帧数估算」完全一致。
_MP3_HDR_192_48 = b"\xff\xfb\xb4\x00"
_MP3_FLEN_192_48 = 576
# MPEG1 / Layer III / 128 kbps / 44.1 kHz / 立体声 —— 144*128000//44100 == 417（截断）
_MP3_HDR_128_44 = b"\xff\xfb\x90\x00"
_MP3_FLEN_128_44 = 417
# MPEG1 / Layer III / 192 kbps / 44.1 kHz
_MP3_HDR_192_44 = b"\xff\xfb\xb0\x00"
_MP3_FLEN_192_44 = 626


def _mp3_frames(n: int, header: bytes = _MP3_HDR_192_48, frame_len: int = _MP3_FLEN_192_48,
                first_body: bytes = None) -> bytes:
    if first_body is not None:
        assert len(first_body) == frame_len - 4
    out = []
    for i in range(n):
        body = first_body if (i == 0 and first_body is not None) else b"\x00" * (frame_len - 4)
        out.append(header + body)
    return b"".join(out)


def _id3(size: int = 100, footer: bool = False) -> bytes:
    body_len = size + (10 if footer else 0)
    synchsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F,
                       (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3" + b"\x04\x00" + bytes([0x10 if footer else 0x00]) + synchsafe + b"\x00" * body_len


# ------------------------------------------------------------------ FLAC 构造
def _flac_streaminfo(rate: int = 44100, channels: int = 2, bits: int = 16,
                     total: int = 441000, last: bool = True, btype: int = 0) -> bytes:
    packed = (rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36) | total
    body = b"\x00" * 10 + packed.to_bytes(8, "big") + b"\x00" * 16
    assert len(body) == 34
    head = bytes([(0x80 if last else 0x00) | btype]) + len(body).to_bytes(3, "big")
    return head + body


def _flac(rate: int = 44100, channels: int = 2, bits: int = 16, total: int = 441000,
          blocks: tuple = ()) -> bytes:
    return b"fLaC" + b"".join(blocks) + _flac_streaminfo(rate, channels, bits, total)


# ------------------------------------------------------------------ Ogg 构造
def _ogg_page(packet: bytes, granule: int, header_type: int = 0, serial: int = 1,
              seq: int = 0) -> bytes:
    segs, n = [], len(packet)
    while n >= 255:
        segs.append(255)
        n -= 255
    segs.append(n)
    return (
        b"OggS" + b"\x00" + bytes([header_type]) + struct.pack("<q", granule)
        + struct.pack("<I", serial) + struct.pack("<I", seq) + b"\x00\x00\x00\x00"
        + bytes([len(segs)]) + bytes(segs) + packet
    )


def _vorbis_id(rate: int = 44100, channels: int = 2, nominal: int = 128000) -> bytes:
    pkt = (
        b"\x01vorbis" + struct.pack("<I", 0) + bytes([channels])
        + struct.pack("<I", rate) + struct.pack("<i", -1)
        + struct.pack("<i", nominal) + struct.pack("<i", -1)
        + bytes([0xB8]) + b"\x01"
    )
    assert len(pkt) == 30
    return pkt


def _opus_head(rate: int = 48000, channels: int = 2) -> bytes:
    pkt = (b"OpusHead" + bytes([1]) + bytes([channels]) + struct.pack("<H", 312)
           + struct.pack("<I", rate) + struct.pack("<h", 0) + bytes([0]))
    assert len(pkt) == 19
    return pkt


# ------------------------------------------------------------------ MP4 构造
_MATRIX = struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)


def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _full_box(typ: bytes, version: int, payload: bytes) -> bytes:
    return _box(typ, bytes([version]) + b"\x00\x00\x00" + payload)


def _mdhd_v0(timescale: int, duration: int) -> bytes:
    payload = struct.pack(">IIII", 0, 0, timescale, duration) + b"\x55\xc4\x00\x00"
    return _full_box(b"mdhd", 0, payload)


def _mdhd_v1(timescale: int, duration: int) -> bytes:
    payload = struct.pack(">QQIQ", 0, 0, timescale, duration) + b"\x55\xc4\x00\x00"
    return _full_box(b"mdhd", 1, payload)


def _mvhd_v0(timescale: int, duration: int) -> bytes:
    payload = (
        struct.pack(">IIII", 0, 0, timescale, duration)
        + struct.pack(">I", 0x00010000) + struct.pack(">H", 0x0100) + b"\x00" * 10
        + _MATRIX + b"\x00" * 24 + struct.pack(">I", 2)
    )
    assert len(payload) == 96
    return _full_box(b"mvhd", 0, payload)


def _mvhd_v1(timescale: int, duration: int) -> bytes:
    payload = (
        struct.pack(">QQIQ", 0, 0, timescale, duration)
        + struct.pack(">I", 0x00010000) + struct.pack(">H", 0x0100) + b"\x00" * 10
        + _MATRIX + b"\x00" * 24 + struct.pack(">I", 2)
    )
    assert len(payload) == 108
    return _full_box(b"mvhd", 1, payload)


def _mehd_v0(fragment_duration: int) -> bytes:
    return _full_box(b"mehd", 0, struct.pack(">I", fragment_duration))


def _mehd_v1(fragment_duration: int) -> bytes:
    return _full_box(b"mehd", 1, struct.pack(">Q", fragment_duration))


def _mp4a(channels: int = 2, sample_rate: int = 44100, samplesize: int = 16) -> bytes:
    """AudioSampleEntry：声道在 +20，采样率在 +28（16.16 定点）。"""
    body = (
        b"\x00" * 6 + struct.pack(">H", 1) + b"\x00" * 8
        + struct.pack(">H", channels) + struct.pack(">H", samplesize)
        + struct.pack(">H", 0) + struct.pack(">H", 0)
        + struct.pack(">I", sample_rate << 16)
    )
    assert len(body) == 28
    return _box(b"mp4a", body)


def _trak(mdhd_box: bytes, entry: bytes = None) -> bytes:
    stsd = _full_box(b"stsd", 0, struct.pack(">I", 1) + (entry or b""))
    return _box(b"trak", _box(b"mdia", mdhd_box + _box(b"stbl", stsd)))


def _mp4(*boxes: bytes) -> bytes:
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
    # 末尾垫一个 mdat：_probe_mp4 要求 mdhd 位置往后还有 32 字节可读
    return ftyp + b"".join(boxes) + _box(b"mdat", b"\x00" * 8)


def _tfdt(base: int) -> bytes:
    """tfdt v1：baseMediaDecodeTime 是 64 位，位于盒偏移 +12。"""
    return _full_box(b"tfdt", 1, struct.pack(">Q", base))


def _trun(sample_count: int, sample_delta: int) -> bytes:
    """trun：sample_count 在盒偏移 +12；这里带 data_offset 与逐样本 duration。"""
    payload = (
        struct.pack(">I", sample_count)
        + struct.pack(">i", 0)
        + struct.pack(">I", sample_delta) * sample_count
    )
    return _full_box(b"trun", 0, payload)


# ================================================================== WAV
@test
def wav_canonical_pcm_header_gives_exact_duration():
    """44 字节标准头：data 字节数 / (rate*channels*bits/8)，16 kHz 单声道 1 秒。"""
    with _Tmp() as d:
        p = _write(d, "a.wav", _wav(rate=8000, channels=1, bits=16, data_size=16000))
        info = probe_audio(p)
        approx(info.duration, 1.0, tol=1e-9)
        assert info.sample_rate == 8000
        assert info.channels == 1
        assert info.bitrate == 128000  # byte_rate * 8
        _via(info, "wav-header")
        assert float(info) == info.duration


@test
def wav_stereo_44100_duration_and_fields():
    with _Tmp() as d:
        # 44100 * 2ch * 2B = 176400 B/s；3 秒 = 529200 字节
        p = _write(d, "s.wav", _wav(rate=44100, channels=2, bits=16, data_size=529200))
        info = probe_audio(p)
        approx(info.duration, 3.0, tol=1e-9)
        assert (info.sample_rate, info.channels, info.bitrate) == (44100, 2, 1411200)


@test
def wav_duration_is_exact_to_within_one_sample():
    """data 字节数不是整数秒时，误差必须小于一个采样。"""
    with _Tmp() as d:
        p = _write(d, "odd.wav", _wav(rate=8000, channels=1, bits=16, data_size=16001))
        info = probe_audio(p)
        approx(info.duration, 16001 / 16000, tol=1e-12)
        assert abs(info.duration - 1.0) < 1.0 / 8000


@test
def wav_chunk_walking_skips_list_and_fact_chunks():
    """fmt 与 data 之间夹 LIST/fact：必须按 chunk 长度跳，不能假设固定偏移。"""
    with _Tmp() as d:
        info_chunk = _chunk(b"LIST", b"INFO" + b"ISFT" + struct.pack("<I", 8) + b"Lavf58.7")
        # fact 与 data 一致（16000 字节 / 2 = 8000 样本 = 1.0 s），只考察 chunk 游走
        fact_chunk = _chunk(b"fact", struct.pack("<I", 8000))
        raw = _wav(rate=8000, channels=1, bits=16, data_size=16000,
                   between=(info_chunk, fact_chunk))
        p = _write(d, "chunked.wav", raw)
        info = probe_audio(p)
        approx(info.duration, 1.0, tol=1e-9)
        _via(info, "wav-header")


@test
def wav_chunk_walking_skips_odd_sized_unknown_chunk():
    """奇数长度的未知 chunk 后面有一个补位字节，chunk 游走必须吃掉它。"""
    with _Tmp() as d:
        raw = _wav(rate=8000, channels=1, bits=16, data_size=16000,
                   between=(_chunk(b"junk", b"abcde"),))
        assert b"junk" in raw
        p = _write(d, "pad.wav", raw)
        approx(probe_audio(p).duration, 1.0, tol=1e-9)


@test
def wav_extensible_fmt_chunk_of_size_40():
    """WAVE_FORMAT_EXTENSIBLE：fmt 块 40 字节，只有前 16 字节是公共字段。"""
    sub = b"\x01\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
    ext = struct.pack("<H", 22) + struct.pack("<H", 16) + struct.pack("<I", 3) + sub
    assert len(ext) == 24
    with _Tmp() as d:
        raw = _wav(rate=48000, channels=2, bits=16, data_size=48000 * 4,
                   fmt_tag=0xFFFE, fmt_extra=ext)
        p = _write(d, "ext.wav", raw)
        info = probe_audio(p)
        approx(info.duration, 1.0, tol=1e-9)
        assert (info.sample_rate, info.channels) == (48000, 2)


@test
def wav_handles_data_chunk_before_fmt_chunk():
    """chunk 顺序不保证；data 在 fmt 前面也要能算出来。"""
    with _Tmp() as d:
        ba, rate, bits, n = 2, 8000, 16, 8000
        data = _chunk(b"data", b"\x00" * (ba * n))
        fmt = _chunk(b"fmt ", struct.pack("<HHIIHH", 1, 1, rate, rate * ba, ba, bits))
        body = b"WAVE" + data + fmt
        raw = b"RIFF" + struct.pack("<I", len(body)) + body
        p = _write(d, "reordered.wav", raw)
        approx(probe_audio(p).duration, 1.0, tol=1e-9)


@test
def wav_rejects_non_riff_and_truncated_headers():
    with _Tmp() as d:
        assert _probe_wav(Path(_write(d, "not.wav", b"RIFF\x00\x00\x00\x00AVI "))) is None
        assert _probe_wav(Path(_write(d, "short.wav", b"RIFF"))) is None
        assert _probe_wav(Path(_write(d, "empty.wav", b""))) is None


def _wav_custom(tag, channels, rate, avg_bytes, block_align, bits, data_size,
                fact=None) -> bytes:
    """按字段原样拼 WAV，用于构造非 PCM（wBitsPerSample 不代表字节率）的文件。"""
    fmt_body = struct.pack(
        "<HHIIHH", tag, channels, rate, avg_bytes, block_align, bits
    )
    body = b"WAVE" + _chunk(b"fmt ", fmt_body)
    if fact is not None:
        body += _chunk(b"fact", struct.pack("<I", fact))
    body += _chunk(b"data", b"\x00" * data_size)
    return b"RIFF" + struct.pack("<I", len(body)) + body


@test
def wav_non_pcm_uses_the_fact_sample_count():
    """ADPCM 的 wBitsPerSample=4 不代表字节率；时长只能取 fact 的 dwSampleLength。

    真实 ffmpeg 输出：adpcm_ms/adpcm_ima 的 fmt=(2,1,44100,16000,1024,4)、
    fact=220500、data=111616。按字节率折算得 111616/22050 = 5.0619 s，
    而 220500/44100 = 5.000000 s 才是文件自己声明的时长。
    """
    with _Tmp() as d:
        raw = _wav_custom(2, 1, 44100, 16000, 1024, 4, 111616, fact=220500)
        p = _write(d, "adpcm.wav", raw)
        info = probe_audio(p)
        approx(info.duration, 220500 / 44100, tol=1e-12)
        approx(info.duration, 5.0, tol=1e-9)
        assert (info.sample_rate, info.channels) == (44100, 1)
        _via(info, "wav-header")


@test
def wav_fact_agrees_with_byte_rate_for_pcm_style_payloads():
    """μ-law/alaw：fact 与按字节率折算的答案一致，两条路都不能互相打架。"""
    with _Tmp() as d:
        raw = _wav_custom(7, 1, 44100, 44100, 1, 8, 220500, fact=220500)
        p = _write(d, "mulaw.wav", raw)
        approx(probe_audio(p).duration, 5.0, tol=1e-9)


@test
def wav_zero_bits_falls_back_to_average_bytes_per_second():
    """GSM 这类格式 wBitsPerSample 写 0；此时唯一可用的字段是 nAvgBytesPerSec。"""
    with _Tmp() as d:
        raw = _wav_custom(0x31, 1, 8000, 1625, 65, 0, 8125, fact=40000)
        p = _write(d, "gsm.wav", raw)
        info = probe_audio(p)
        approx(info.duration, 5.0, tol=1e-9)
        assert info.bitrate == 1625 * 8


@test
def wav_without_bits_or_byte_rate_is_unreadable():
    """wBitsPerSample=0 且 nAvgBytesPerSec=0：没有任何可用的速率字段。"""
    with _Tmp() as d:
        raw = _wav_custom(0x31, 1, 8000, 0, 0, 0, 8125)
        p = _write(d, "nobps.wav", raw)
        assert _probe_wav(Path(p)) is None


@test
def wav_returns_none_when_fmt_is_missing():
    """只有 data、没有 fmt：字节率无从得知，必须返回 None 而不是瞎猜。"""
    with _Tmp() as d:
        body = b"WAVE" + _chunk(b"data", b"\x00" * 100)
        raw = b"RIFF" + struct.pack("<I", len(body)) + body
        p = _write(d, "nofmt.wav", raw)
        assert _probe_wav(Path(p)) is None
        raises(ValueError, probe_audio, p)


# ================================================================== MP3
@test
def mp3_frame_parser_decodes_mpeg1_layer3():
    got = _parse_mp3_frame(_MP3_HDR_192_48, 0)
    # (帧长, 采样率, 比特率, 声道, 每帧样本数)
    assert got == (576, 48000, 192000, 2, 1152), got
    # 单声道时声道数为 1（header 第 4 字节高 2 位 == 0b11）
    mono = _parse_mp3_frame(bytes([0xFF, 0xFB, 0xB4, 0xC0]), 0)
    assert mono[3] == 1


@test
def mp3_frame_parser_supports_layer1_and_mpeg25():
    # Layer I（bits 11）：帧长按 slot 算，每帧 384 样本
    layer1 = _parse_mp3_frame(bytes([0xFF, 0xFF, 0xB4, 0x00]), 0)
    assert layer1[0] == (12 * 192000 // 48000) * 4 == 192
    assert layer1[4] == 384
    # MPEG2.5（ver=0b00）Layer III：72*bitrate//rate，每帧 576 样本
    # rate_idx=1 → 12000 Hz（11025/12000/8000），bitrate_idx=11 → 112 kbps
    m25 = _parse_mp3_frame(bytes([0xFF, 0xE3, 0xB4, 0x00]), 0)
    assert m25 == (72 * 112000 // 12000, 12000, 112000, 2, 576), m25
    # MPEG2（ver=0b10）Layer III 也要走 72/576 分支，但采样率表不同：
    # 同一个 rate_idx=1 在 MPEG2 里是 24000 Hz
    m2 = _parse_mp3_frame(bytes([0xFF, 0xF3, 0xB4, 0x00]), 0)
    assert m2 == (72 * 112000 // 24000, 24000, 112000, 2, 576), m2


@test
def mp3_frame_parser_rejects_invalid_headers():
    bad = {
        "无同步": bytes([0xFE, 0xFB, 0xB4, 0x00]),
        "同步不全": bytes([0xFF, 0x1B, 0xB4, 0x00]),
        "保留版本": bytes([0xFF, 0xEB, 0xB4, 0x00]),   # ver == 0b01
        "保留层": bytes([0xFF, 0xF9, 0xB4, 0x00]),     # layer == 0b00
        "免费比特率": bytes([0xFF, 0xFB, 0x04, 0x00]),  # bitrate_idx == 0
        "非法比特率": bytes([0xFF, 0xFB, 0xF4, 0x00]),  # bitrate_idx == 15
        "保留采样率": bytes([0xFF, 0xFB, 0xBC, 0x00]),  # rate_idx == 3
    }
    for name, hdr in bad.items():
        assert _parse_mp3_frame(hdr, 0) is None, name
    assert _parse_mp3_frame(b"\xff\xfb\xb4", 0) is None  # 缓冲区不够 4 字节
    assert _parse_mp3_frame(b"\xff\xfb\xb4\x00", 1) is None


@test
def mp3_cbr_duration_matches_frame_count_formula():
    """192 kbps @ 48 kHz 的帧长正好 576 字节，所以两种算法给出同一个数。"""
    with _Tmp() as d:
        nframes = 100
        raw = _mp3_frames(nframes)
        assert len(raw) == nframes * 576
        p = _write(d, "cbr.mp3", raw)
        info = probe_audio(p)
        approx(info.duration, nframes * 1152 / 48000, tol=1e-9)
        approx(info.duration, len(raw) * 8 / 192000, tol=1e-9)
        assert (info.sample_rate, info.channels, info.bitrate) == (48000, 2, 192000)
        _via(info, "mp3-cbr")


@test
def mp3_cbr_byte_estimate_is_within_one_frame_of_the_sample_count():
    """128 kbps @ 44.1 kHz 帧长被截断成 417，按字节估的时长会略短，但只差不到一帧。"""
    with _Tmp() as d:
        nframes = 200
        raw = _mp3_frames(nframes, header=_MP3_HDR_128_44, frame_len=_MP3_FLEN_128_44)
        info = probe_audio(_write(d, "cbr44.mp3", raw))
        approx(info.duration, len(raw) * 8 / 128000, tol=1e-9)
        assert info.bitrate == 128000
        exact = nframes * 1152 / 44100
        assert abs(info.duration - exact) <= 1152 / 44100
        assert info.duration < exact


@test
def mp3_single_frame_at_end_of_file_is_accepted():
    """同步搜索允许「下一帧越界」——只有一个完整帧的文件也应当可读。"""
    with _Tmp() as d:
        raw = _mp3_frames(1)
        assert len(raw) == 576
        info = probe_audio(_write(d, "one.mp3", raw))
        approx(info.duration, 576 * 8 / 192000, tol=1e-9)


@test
def mp3_skips_id3v2_tag_before_finding_the_first_frame():
    with _Tmp() as d:
        nframes = 50
        frames = _mp3_frames(nframes)
        for name, tag in (("tag.mp3", _id3(100)), ("footer.mp3", _id3(100, footer=True))):
            raw = tag + frames
            info = probe_audio(_write(d, name, raw))
            # 时长只看音频字节（总长 - ID3 标签长度）
            approx(info.duration, len(frames) * 8 / 192000, tol=1e-9)
            approx(info.duration, nframes * 1152 / 48000, tol=1e-9)
            _via(info, "mp3-cbr")


@test
def mp3_skip_id3_handles_footer_flag_and_non_id3():
    n = 100
    fh = io.BytesIO(_id3(n))
    assert _skip_id3(fh) == 10 + n
    assert fh.tell() == 10 + n

    fh = io.BytesIO(_id3(n, footer=True))
    assert _skip_id3(fh) == 10 + n + 10

    fh = io.BytesIO(b"RANDOMBYTES....")
    assert _skip_id3(fh) == 0
    assert fh.tell() == 0

    fh = io.BytesIO(b"ID3")          # 头部不足 10 字节
    assert _skip_id3(fh) == 0
    assert fh.tell() == 0

    # 同步安全整数：每字节只用低 7 位，0xFF 的高位必须被屏蔽掉
    fh = io.BytesIO(b"ID3\x04\x00\x00\xff\xff\xff\xff")
    assert _skip_id3(fh) == 10 + (2 ** 28 - 1)


@test
def mp3_xing_frame_count_is_used_when_present():
    """Xing/Info 里的总帧数与物理帧数矛盾时，以标签为准——证明走的是帧数路径。"""
    # MPEG1 立体声的 side info 长度是 32，Xing 标签紧跟其后
    payload = struct.pack(">I", 1) + struct.pack(">I", 500) + b"\x00" * 132

    def first_frame(tag: bytes) -> bytes:
        head = b"\x00" * 32 + tag + payload
        return head + b"\x00" * (_MP3_FLEN_192_48 - 4 - len(head))

    with _Tmp() as d:
        raw = _mp3_frames(20, first_body=first_frame(b"Xing"))  # 物理上只有 20 帧
        p = _write(d, "xing.mp3", raw)
        assert _mp3_xing_frames(raw[:65536], 0) == 500
        info = probe_audio(p)
        approx(info.duration, 500 * 1152 / 48000, tol=1e-9)   # 12.0 s，而非 0.48 s
        _via(info, "mp3-xing")
        # Info 标签（CBR 文件常用）走同一条路
        raw2 = _mp3_frames(20, first_body=first_frame(b"Info"))
        info2 = probe_audio(_write(d, "info.mp3", raw2))
        approx(info2.duration, 500 * 1152 / 48000, tol=1e-9)
        _via(info2, "mp3-xing")


@test
def mp3_xing_helper_returns_none_without_the_tag():
    raw = _mp3_frames(4)
    assert _mp3_xing_frames(raw, 0) is None
    # 有 tag 但没有帧数字段（flags 第 0 位为 0）
    body = b"\x00" * 32 + b"Xing" + struct.pack(">I", 0) + b"\x00" * 132
    body += b"\x00" * (_MP3_FLEN_192_48 - 4 - len(body))
    raw2 = _mp3_frames(4, first_body=body)
    assert _mp3_xing_frames(raw2, 0) is None
    assert _mp3_xing_frames(b"", 0) is None


@test
def mp3_tail_bitrate_corrects_the_cbr_estimate():
    """头帧 128 kbps、尾部全是 192 kbps：用尾部真实帧率重算，并回写 bitrate。"""
    with _Tmp() as d:
        head = _mp3_frames(1, header=_MP3_HDR_128_44, frame_len=_MP3_FLEN_128_44)
        tail = _mp3_frames(40, header=_MP3_HDR_192_44, frame_len=_MP3_FLEN_192_44)
        raw = head + tail
        size = len(raw)
        assert size > 16384  # 尾部窗口必须完全落在 192k 区域里
        info = probe_audio(_write(d, "vbrish.mp3", raw))
        assert info.bitrate == 192000
        approx(info.duration, size * 8 / 192000, tol=1e-9)
        _via(info, "mp3-cbr")


@test
def mp3_returns_none_without_any_valid_frame():
    with _Tmp() as d:
        assert _probe_mp3(Path(_write(d, "zeros.mp3", b"\x00" * 4000))) is None
        tag_only = _id3(200) + b"\x00" * 500
        assert _probe_mp3(Path(_write(d, "tagonly.mp3", tag_only))) is None
        raises(ValueError, probe_audio, _write(d, "zeros.mp3", b"\x00" * 4000))


# ================================================================== FLAC
@test
def flac_streaminfo_duration_from_total_samples():
    with _Tmp() as d:
        p = _write(d, "a.flac", _flac(rate=44100, channels=2, bits=16, total=441000))
        info = probe_audio(p)
        approx(info.duration, 10.0, tol=1e-12)
        assert (info.sample_rate, info.channels) == (44100, 2)
        assert info.bitrate == 44100 * 2 * 16
        _via(info, "flac-streaminfo")


@test
def flac_streaminfo_field_layout_extremes():
    """36 位总样本数 / 20 位采样率 / 3 位声道-1 的位域边界。"""
    cases = [
        (96000, 1, 24, 96000, 1.0),
        (8000, 1, 8, 32000, 4.0),
        (192000, 8, 32, 192000 * 2, 2.0),
        (44100, 2, 16, 2 ** 36 - 1, (2 ** 36 - 1) / 44100),
    ]
    with _Tmp() as d:
        for i, (rate, ch, bits, total, expect) in enumerate(cases):
            p = _write(d, f"c{i}.flac", _flac(rate=rate, channels=ch, bits=bits, total=total))
            info = probe_audio(p)
            approx(info.duration, expect, tol=1e-9)
            assert (info.sample_rate, info.channels) == (rate, ch), (rate, ch)
            assert info.bitrate == rate * ch * bits


@test
def flac_walks_metadata_blocks_before_streaminfo():
    """STREAMINFO 不一定是第一个元数据块；前面的块按长度跳过。"""
    comment = bytes([0x04]) + (8).to_bytes(3, "big") + b"x" * 8   # VORBIS_COMMENT，非最后块
    with _Tmp() as d:
        raw = b"fLaC" + comment + _flac_streaminfo(48000, 2, 16, 48000)
        info = probe_audio(_write(d, "blocks.flac", raw + b"\x00" * 32))
        approx(info.duration, 1.0, tol=1e-12)
        _via(info, "flac-streaminfo")


@test
def flac_rejects_bad_magic_and_zero_fields():
    with _Tmp() as d:
        assert _probe_flac(Path(_write(d, "no.flac", b"NOPE" + _flac_streaminfo()))) is None
        assert _probe_flac(Path(_write(d, "zero.flac", _flac(total=0)))) is None
        assert _probe_flac(Path(_write(d, "rate0.flac", _flac(rate=0, total=1000)))) is None
        # 只有 fLaC 魔数、后面什么都没有
        assert _probe_flac(Path(_write(d, "bare.flac", b"fLaC"))) is None


# ================================================================== Ogg
@test
def ogg_vorbis_duration_from_last_granule():
    with _Tmp() as d:
        raw = (_ogg_page(_vorbis_id(44100, 2), granule=0, header_type=2, seq=0)
               + _ogg_page(b"\x03vorbis" + b"\x00" * 20, granule=441000, header_type=4, seq=1))
        info = probe_audio(_write(d, "a.ogg", raw))
        approx(info.duration, 10.0, tol=1e-12)
        assert (info.sample_rate, info.channels) == (44100, 2)
        _via(info, "ogg-granule")


@test
def ogg_opushead_duration_uses_the_48khz_granule():
    with _Tmp() as d:
        raw = (_ogg_page(_opus_head(48000, 2), granule=0, header_type=2, seq=0)
               + _ogg_page(b"\x00" * 40, granule=240000, header_type=4, seq=1))
        info = probe_audio(_write(d, "a.opus", raw))
        approx(info.duration, 5.0, tol=1e-12)
        assert (info.sample_rate, info.channels) == (48000, 2)
        _via(info, "opus-granule")


@test
def ogg_opus_with_zero_input_rate_reports_48000():
    """OpusHead 的输入采样率允许写 0；granule 永远是 48 kHz。"""
    with _Tmp() as d:
        raw = (_ogg_page(_opus_head(0, 1), granule=0, header_type=2, seq=0)
               + _ogg_page(b"\x00" * 40, granule=96000, header_type=4, seq=1))
        info = probe_audio(_write(d, "z.opus", raw))
        approx(info.duration, 2.0, tol=1e-12)
        assert (info.sample_rate, info.channels) == (48000, 1)


@test
def ogg_last_granule_reads_the_final_page():
    p1 = _ogg_page(_vorbis_id(), granule=0, seq=0)
    p2 = _ogg_page(b"\x00" * 4, granule=123456, seq=1)
    assert _ogg_last_granule(p1 + p2) == 123456
    assert _ogg_last_granule(p2 + p1) == 0
    assert _ogg_last_granule(b"not an ogg file") is None
    assert _ogg_last_granule(b"OggS") is None


@test
def ogg_with_zero_granule_is_unreadable():
    """最后一页 granule 为 0（还没写完/没有完整包）时不能凭空造时长。

    这里用 OpusHead 而不是 Vorbis 头：Vorbis 识别包里的 bitrate 字段按规范是
    ``-1``（0xFFFFFFFF），文件中会出现 ``ff ff b8 01`` 这样的字节串，恰好能被
    ``_parse_mp3_frame`` 当成 Layer I 同步字，于是 ``probe_audio`` 的穷举兜底会把
    它误判成 4 ms 的 MP3。那是穷举兜底的固有代价，不是本用例要证明的事情。
    """
    with _Tmp() as d:
        raw = (_ogg_page(_opus_head(), granule=0, header_type=2, seq=0)
               + _ogg_page(b"\x00" * 8, granule=0, header_type=4, seq=1))
        p = _write(d, "zero.opus", raw)
        assert _probe_ogg(Path(p)) is None
        raises(ValueError, probe_audio, p)


@test
def ogg_vorbis_minus_one_bitrates_can_look_like_an_mp3_frame():
    """记录穷举兜底的边界：Vorbis 规范里 bitrate=-1 会制造一个假的 MP3 同步字。

    这是 ``audio.py`` 先嗅探、再按扩展名、最后穷举的次序决定的固有行为：
    ``_parse_mp3_frame`` 的校验只要自洽就会接受，所以一个没有可用 granule 的
    Ogg 文件仍可能被报成一个极短的 mp3-cbr。这里把事实钉住，而不是要求修正。
    """
    raw = (_ogg_page(_vorbis_id(), granule=0, header_type=2, seq=0)
           + _ogg_page(b"\x00" * 8, granule=0, header_type=4, seq=1))
    # 文件偏移 54 处的 ff ff ff ff ... b8 01 里藏着 0xFF 0xFF 开头的 Layer I 同步字
    assert _parse_mp3_frame(raw, 54) is not None
    with _Tmp() as d:
        info = probe_audio(_write(d, "zero.ogg", raw))
        assert info.method == "mp3-cbr"
        assert info.duration < 0.05          # 4 ms 的伪时长，远小于真实音频
        assert info.sample_rate == 32000


# ================================================================== MP4
@test
def mp4_mvhd_fallback_when_mdhd_duration_is_zero():
    """分片/流式 MP4 的 mdhd 时长写 0，必须回落到 mvhd（audio(3).mp3 的真实结构）。"""
    with _Tmp() as d:
        raw = _mp4(_box(b"moov", _mvhd_v0(1000, 93438)
                        + _trak(_mdhd_v0(44100, 0), _mp4a(2, 44100))))
        info = probe_audio(_write(d, "f.mp4", raw))
        approx(info.duration, 93.438, tol=1e-9)
        assert (info.sample_rate, info.channels) == (44100, 2)
        _via(info, "mp4-mvhd")


@test
def mp4_longest_mdhd_wins_over_shorter_and_mvhd():
    """多条 mdhd（音轨/视频轨/文本轨）时取最长的那条，而不是第一条。"""
    with _Tmp() as d:
        moov = _box(b"moov", _mvhd_v0(1000, 3000)
                    + _trak(_mdhd_v0(600, 1200))      # 2.0 s
                    + _trak(_mdhd_v0(1000, 7000)))    # 7.0 s
        info = probe_audio(_write(d, "multi.mp4", _mp4(moov)))
        approx(info.duration, 7.0, tol=1e-9)
        _via(info, "mp4-mdhd")


@test
def mp4_mdhd_version_1_uses_64bit_duration():
    with _Tmp() as d:
        raw = _mp4(_box(b"moov", _mvhd_v0(1000, 99999) + _trak(_mdhd_v1(48000, 480000))))
        info = probe_audio(_write(d, "v1.mp4", raw))
        approx(info.duration, 10.0, tol=1e-9)
        _via(info, "mp4-mdhd")


@test
def mp4_mvhd_version_1_uses_64bit_duration():
    with _Tmp() as d:
        raw = _mp4(_box(b"moov", _mvhd_v1(600, 3600) + _trak(_mdhd_v0(48000, 0))))
        info = probe_audio(_write(d, "mv1.mp4", raw))
        approx(info.duration, 6.0, tol=1e-9)
        _via(info, "mp4-mvhd")


@test
def mp4_mehd_used_when_mvhd_duration_is_zero():
    """mvhd 存在但时长为 0（流式封装）时，用 mvex/mehd 里的分片总长。"""
    with _Tmp() as d:
        raw = _mp4(_box(b"moov", _mvhd_v0(1000, 0) + _trak(_mdhd_v0(44100, 0))
                        + _box(b"mvex", _mehd_v0(9000))))
        info = probe_audio(_write(d, "mehd.mp4", raw))
        approx(info.duration, 9.0, tol=1e-9)
        _via(info, "mp4-mehd")

        raw1 = _mp4(_box(b"moov", _mvhd_v0(1000, 0) + _trak(_mdhd_v0(44100, 0))
                         + _box(b"mvex", _mehd_v1(12000))))
        info1 = probe_audio(_write(d, "mehd1.mp4", raw1))
        approx(info1.duration, 12.0, tol=1e-9)
        _via(info1, "mp4-mehd")


@test
def mp4_audio_sample_entry_offsets_give_rate_and_channels():
    """AudioSampleEntry：+20 是 2 字节声道数，+28 是 16.16 定点采样率。"""
    entry = _mp4a(channels=1, sample_rate=22050)
    assert struct.unpack(">H", entry[20 + 4:20 + 6])[0] == 1
    assert struct.unpack(">I", entry[28 + 4:28 + 8])[0] >> 16 == 22050
    with _Tmp() as d:
        raw = _mp4(_box(b"moov", _mvhd_v0(1000, 2000) + _trak(_mdhd_v0(22050, 0), entry)))
        info = probe_audio(_write(d, "entry.mp4", raw))
        approx(info.duration, 2.0, tol=1e-9)
        assert (info.sample_rate, info.channels) == (22050, 1)


@test
def mp4_without_any_duration_box_returns_none():
    with _Tmp() as d:
        raw = _mp4(_box(b"moov", _box(b"free", b"\x00" * 16)))
        p = _write(d, "nolen.mp4", raw)
        assert _probe_mp4(Path(p)) is None


@test
def fragmented_mp4_with_zero_durations_is_a_documented_limitation():
    """已知局限（不是回归）：真实分片 MP4 既没有可用的 mdhd 也没有 mehd。

    用 ffmpeg 实测三种分片封装（``frag_keyframe+empty_moov``、``-movflags dash``、
    ``empty_moov+default_base_moof+frag_keyframe``）都是同一形状：
    ``mvhd`` timescale=1000 duration=0、``mdhd`` timescale=44100 duration=0、
    **没有 mehd**（dash 改用 sidx），总时长只能从 moof/traf/trun 的样本数累加得到。
    所以 audio.py:462 注释里“分片 MP4 的总长在 mehd 里”与真实封装器不符，
    mdhd 分支因 ``dur > 0`` 被跳过、mehd 分支因 mvhd duration=0 拿不到 movie_ts，
    两条路都不可达 —— 于是 probe_audio 抛 ValueError。

    这个用例只固定当前行为，不声称正确；若日后补上 trun/tfdt 累加实现，
    应当把断言改成 duration ≈ 5.0 并改用新 method。
    """
    with _Tmp() as d:
        traf = _box(b"traf", _box(b"tfhd", struct.pack(">I", 0x020000))
                    + _tfdt(0) + _trun(217, 1024))
        moof = _box(b"moof", _full_box(b"mfhd", 0, struct.pack(">I", 1)) + traf)
        raw = _mp4(
            _box(b"moov", _mvhd_v0(1000, 0) + _trak(_mdhd_v0(44100, 0))
                 + _box(b"mvex", _box(b"trex", struct.pack(">IIIII", 1, 1, 1024, 0, 0)))),
            moof,
        )
        p = _write(d, "frag.m4a", raw)
        # 前提：这正是真实分片文件的形状（mvhd/mdhd 时长都是 0、无 mehd）
        assert b"mehd" not in raw
        assert _probe_mp4(Path(p)) is None
        raises(ValueError, probe_audio, p)
        raises(ValueError, probe_audio, p)


# ================================================================== 调度 / 嗅探
@test
def magic_sniff_detects_each_container():
    with _Tmp() as d:
        cases = [
            ("a", b"RIFF\x00\x00\x00\x00WAVEfmt ", _probe_wav),
            ("b", b"fLaC" + b"\x00" * 12, _probe_flac),
            ("c", b"OggS" + b"\x00" * 12, _probe_ogg),
            ("d", b"ID3\x04\x00\x00" + b"\x00" * 6, _probe_mp3),
            ("e", b"\xff\xfb\x90\x00" + b"\x00" * 8, _probe_mp3),
            ("f", b"\xff\xf3\x90\x00" + b"\x00" * 8, _probe_mp3),
            ("g", b"\xff\xfa\x90\x00" + b"\x00" * 8, _probe_mp3),
            ("h", b"\x00\x00\x00\x20ftypisom", _probe_mp4),
            ("i", b"garbage bytes!!!", None),
        ]
        for name, head, expected in cases:
            got = _sniff(Path(_write(d, name, head)))
            assert got is expected, f"{name}: {got} != {expected}"


@test
def sniff_returns_mp4_for_every_box_prefix():
    """box 长度 + ftyp/styp/moov/free/skip 都算 MP4 家族。"""
    with _Tmp() as d:
        for i, typ in enumerate((b"ftyp", b"styp", b"moov", b"free", b"skip")):
            head = struct.pack(">I", 16) + typ + b"\x00" * 8
            assert _sniff(Path(_write(d, f"b{i}", head))) is _probe_mp4, typ


@test
def sniff_returns_none_for_missing_file():
    assert _sniff(Path(os.path.join(tempfile.gettempdir(), "definitely_missing_xyz.bin"))) is None


@test
def probes_map_extensions_to_parsers():
    assert _PROBES[".wav"] is _probe_wav
    assert _PROBES[".wave"] is _probe_wav
    assert _PROBES[".mp3"] is _probe_mp3
    assert _PROBES[".flac"] is _probe_flac
    for ext in (".ogg", ".oga", ".opus"):
        assert _PROBES[ext] is _probe_ogg, ext
    for ext in (".m4a", ".mp4", ".aac"):
        assert _PROBES[ext] is _probe_mp4, ext
    assert ".txt" not in _PROBES
    # _MAGIC 里每一项都是 (bytes, 可调用)
    for magic, fn in _MAGIC:
        assert isinstance(magic, bytes) and callable(fn)


@test
def disguised_mp4_named_mp3_is_parsed_as_mp4():
    """核心回归：后缀说 mp3、内容是 MP4 —— 魔数嗅探必须赢。"""
    with _Tmp() as d:
        raw = _mp4(_box(b"moov", _mvhd_v0(1000, 93438)
                        + _trak(_mdhd_v0(44100, 0), _mp4a(2, 44100))))
        p = _write(d, "pretend.mp3", raw)
        assert _sniff(Path(p)) is _probe_mp4           # 嗅探直接不看后缀
        info = probe_audio(p)
        approx(info.duration, 93.438, tol=1e-9)
        assert (info.sample_rate, info.channels) == (44100, 2)
        _via(info, "mp4-mvhd")
        assert _PROBES[".mp3"] is _probe_mp3           # 后缀表本身仍指向 mp3
        # 换个更离谱的后缀也一样
        p2 = _write(d, "pretend.wav", raw)
        _via(probe_audio(p2), "mp4-mvhd")


@test
def disguised_mp4_regression_on_a_synthetic_file():
    """把 MP4 命名成 .mp3 必须靠魔数认出来（mdhd 为 0，回落到 mvhd）。

    样本是 ``tests/fixtures/media/disguised.mp3`` —— 由本库自己合成的容器，
    不含任何真实音频。真实世界里网易云 / Bilibili 的下载经常长这样。
    """
    if not REAL_AUDIO.is_file():
        return  # 样本缺失 → 跳过而不是失败
    info = probe_audio(REAL_AUDIO)
    approx(info.duration, 93.438, tol=0.02)
    assert info.sample_rate == 44100
    assert info.channels == 2
    _via(info, "mp4-mvhd")
    assert float(info) == info.duration
    assert "1:33.438" in repr(info)
    assert info.path.name == "disguised.mp3"
    # 同一个文件，后缀表会给出 mp3 解析器，但那不是实际走的路径
    assert _PROBES[".mp3"] is _probe_mp3
    headers = REAL_AUDIO.open("rb").read(16)
    assert headers[4:8] == b"ftyp"          # 没有 ID3，也不是 0xFF 同步字
    assert not headers.startswith(b"ID3")


@test
def disguised_container_regression_on_a_synthetic_file():
    """没有音轨的 MP4：时长能读出来，但采样率/声道都是 0。文件缺失时静默跳过。"""
    if not REAL_VIDEO.is_file():
        return
    info = probe_audio(REAL_VIDEO)
    approx(info.duration, 93.434, tol=0.02)
    assert (info.sample_rate, info.channels) == (0, 0)
    _via(info, "mp4-mvhd")


@test
def wav_named_mp3_uses_the_wav_parser():
    with _Tmp() as d:
        p = _write(d, "wavpretend.mp3", _wav(rate=8000, channels=1, bits=16, data_size=16000))
        info = probe_audio(p)
        approx(info.duration, 1.0, tol=1e-9)
        assert info.sample_rate == 8000
        _via(info, "wav-header")


@test
def flac_named_ogg_uses_the_flac_parser():
    with _Tmp() as d:
        p = _write(d, "fpretend.ogg", _flac(rate=44100, channels=2, bits=16, total=88200))
        info = probe_audio(p)
        approx(info.duration, 2.0, tol=1e-12)
        _via(info, "flac-streaminfo")


@test
def container_with_unknown_extension_falls_back_to_magic():
    with _Tmp() as d:
        for name, raw, method in (
            ("a.bin", _wav(rate=8000, channels=1, bits=16, data_size=8000), "wav-header"),
            ("b.dat", _flac(rate=8000, channels=1, bits=16, total=16000), "flac-streaminfo"),
            ("c.xyz", _mp3_frames(30), "mp3-cbr"),
            ("noext", _mp3_frames(30), "mp3-cbr"),
        ):
            _via(probe_audio(_write(d, name, raw)), method)


@test
def mp3_without_a_known_sync_word_falls_back_to_the_exhaustive_probe():
    """MPEG2.5 的同步字是 0xFF 0xE3，不在 _MAGIC 里——靠「全部兜底」那一步救回来。"""
    raw = _mp3_frames(30, header=b"\xff\xe3\xb4\x00", frame_len=72 * 112000 // 12000)
    with _Tmp() as d:
        p = _write(d, "m25.mp3", raw)
        assert _sniff(Path(p)) is None
        assert _PROBES[".mp3"] is _probe_mp3
        info = probe_audio(p)
        assert info.sample_rate == 12000
        _via(info, "mp3-cbr")
        # 换成不知道的后缀，仍然靠兜底找到
        p2 = _write(d, "m25.bin", raw)
        assert _sniff(Path(p2)) is None
        assert probe_audio(p2).sample_rate == 12000


# ================================================================== 失败模式
@test
def probe_audio_reports_missing_file():
    missing = os.path.join(tempfile.gettempdir(), "no_such_audio_file_zzz.mp3")
    assert not os.path.exists(missing)
    e = raises(FileNotFoundError, probe_audio, missing)
    assert "音频不存在" in str(e)
    raises(FileNotFoundError, duration_of, missing)


@test
def probe_audio_reports_a_directory_as_missing():
    with _Tmp() as d:
        sub = os.path.join(d, "adir.mp3")
        os.makedirs(sub)
        raises(FileNotFoundError, probe_audio, sub)


@test
def probe_audio_reports_unrecognisable_content():
    with _Tmp() as d:
        for name, raw in (("empty.wav", b""), ("tiny.mp3", b"\x00\x01\x02"),
                          ("junk.flac", b"not audio at all, truly"), ("z.m4a", b"\x00" * 64)):
            e = raises(ValueError, probe_audio, _write(d, name, raw))
            assert "无法识别" in str(e), (name, str(e))


@test
def probe_audio_accepts_str_and_pathlike():
    with _Tmp() as d:
        p = _write(d, "a.wav", _wav(rate=8000, channels=1, bits=16, data_size=8000))
        assert probe_audio(p).duration == probe_audio(Path(p)).duration
        approx(duration_of(Path(p)), 0.5, tol=1e-9)


@test
def duration_of_matches_probe_audio():
    with _Tmp() as d:
        p = _write(d, "a.mp3", _mp3_frames(50))
        approx(duration_of(p), probe_audio(p).duration, tol=1e-12)
        approx(duration_of(p), 50 * 1152 / 48000, tol=1e-9)


# ================================================================== ffmpeg_path
@test
def ffmpeg_path_resolves_to_an_existing_file():
    exe = ffmpeg_path("ffmpeg")
    assert exe is not None, "imageio-ffmpeg 应当提供捆绑版 ffmpeg"
    assert os.path.isfile(exe), exe
    assert ffmpeg_path() == exe          # 默认参数就是 "ffmpeg"


@test
def ffprobe_path_is_recorded_without_requiring_it():
    """本机没有捆绑 ffprobe（imageio-ffmpeg 不带），所以只断言「要么 None 要么真实文件」。"""
    p = ffmpeg_path("ffprobe")
    assert p is None or os.path.isfile(p)
    if p is None:
        assert ffmpeg_path("ffprobe") is None      # 真的没有，而不是随机一个路径
        assert _probe_with_ffprobe(REAL_AUDIO) is None if REAL_AUDIO.is_file() else True


@test
def probe_with_ffprobe_returns_none_or_an_ffprobe_result():
    with _Tmp() as d:
        p = _write(d, "a.wav", _wav(rate=8000, channels=1, bits=16, data_size=16000))
        got = _probe_with_ffprobe(Path(p))
        assert got is None or got.method == "ffprobe"


@test
def bundled_ffmpeg_actually_runs():
    exe = ffmpeg_path("ffmpeg")
    if exe is None:
        return          # 本机没有可执行的 ffmpeg，静默跳过
    out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-500:]
    assert "ffmpeg" in (out.stdout + out.stderr).lower()


# ================================================================== AudioInfo
@test
def audioinfo_float_returns_duration():
    info = AudioInfo(Path("x.mp3"), 12.5)
    assert float(info) == 12.5
    approx(float(info), info.duration, tol=1e-12)


@test
def audioinfo_repr_contains_name_time_and_method():
    info = AudioInfo(Path("audio(3).mp3"), 93.438, sample_rate=44100, channels=2,
                     method="mp4-mvhd")
    r = repr(info)
    assert "audio(3).mp3" in r
    assert "1:33.438" in r
    assert "44100Hz 2ch" in r
    assert "via mp4-mvhd" in r
    assert r.startswith("<AudioInfo ") and r.endswith(">")


@test
def audioinfo_repr_formats_seconds_with_milliseconds():
    cases = {
        0.0: "0:00.000",
        1.5: "0:01.500",
        59.999: "0:59.999",
        60.0: "1:00.000",
        3661.5: "61:01.500",     # 不折算成小时，分钟一直往上加
    }
    for seconds, expect in cases.items():
        r = repr(AudioInfo(Path("t.mp3"), seconds))
        assert f" {expect} " in r, (seconds, r)


@test
def audioinfo_keeps_optional_fields_and_defaults():
    d = AudioInfo(Path("x"), 1.0)
    assert (d.sample_rate, d.channels, d.bitrate, d.method, d.title) == (0, 0, 0, "", None)
    e = AudioInfo(Path("x"), 1.0, sample_rate=8000, channels=1, bitrate=64000,
                  method="wav-header", title="曲名")
    assert (e.sample_rate, e.channels, e.bitrate, e.method, e.title) == \
        (8000, 1, 64000, "wav-header", "曲名")
    assert e.path == Path("x")


# ================================================================== parse_lrc
@test
def parse_lrc_mm_ss_form():
    assert parse_lrc("[00:12]hello") == [(12.0, "hello")]
    assert parse_lrc("[01:02]a") == [(62.0, "a")]
    assert parse_lrc("[00:00]") == [(0.0, "")]
    assert parse_lrc("[1:2]x") == [(62.0, "x")]


@test
def parse_lrc_decimal_and_colon_forms():
    assert parse_lrc("[01:02.50]a") == [(62.5, "a")]
    assert parse_lrc("[01:02:50]a") == [(62.5, "a")]      # 冒号当小数点的老写法
    assert parse_lrc("[00:12.5]a") == [(12.5, "a")]
    assert parse_lrc("[00:00.001]a") == [(0.001, "a")]
    assert parse_lrc("[10:00.0]a") == [(600.0, "a")]


@test
def parse_lrc_multiple_timestamps_share_one_text():
    assert parse_lrc("[00:01]hello[00:02]") == [(1.0, "hello"), (2.0, "hello")]
    assert parse_lrc("[00:01][00:02]dup") == [(1.0, "dup"), (2.0, "dup")]


@test
def parse_lrc_skips_lines_without_a_timestamp():
    text = "[ar:Artist]\n[ti:Title]\nhello world\n\noffset: 100\n[00:05]real\n"
    assert parse_lrc(text) == [(5.0, "real")]


@test
def parse_lrc_orders_by_time():
    text = "[00:05]b\n[00:01]a\n[00:03]c\n"
    assert parse_lrc(text) == [(1.0, "a"), (3.0, "c"), (5.0, "b")]


@test
def parse_lrc_empty_and_blank_input():
    assert parse_lrc("") == []
    assert parse_lrc("\n\n   \n") == []
    assert parse_lrc("just text") == []


@test
def parse_lrc_strips_whitespace_and_keeps_full_text():
    assert parse_lrc("  [00:01]  spaced out  ") == [(1.0, "spaced out")]
    assert parse_lrc("prefix[00:09]suffix") == [(9.0, "prefixsuffix")]
