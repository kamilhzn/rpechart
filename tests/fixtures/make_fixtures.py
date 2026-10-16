"""生成仓库自备的测试素材（不依赖任何外部文件）。

跑法::

    python tests/fixtures/make_fixtures.py

产出 ``tests/fixtures/`` 下的全部样本：

* ``charts/*.json`` —— 三份谱面，覆盖四种音符、长按、事件层、特殊事件、
  父子判定线、假音符、``-0.0``、整数 alpha 事件、17 位浮点；
* ``demo.pez`` —— 用库自己的 :func:`rpechart.export_pez` 产出；
* ``media/*`` —— 供音频探测回归用的合成容器（其中一份**故意**把 MP4 命名成 .mp3，
  另一份是**没有音轨**的 MP4）。

两个性质必须成立，它们各有一条守门用例：

* **不含任何第三方素材** —— 全部由本库自己合成，可以随仓库自由分发；
* **可重复生成** —— 重跑本脚本产出的字节必须完全一致（``demo.pez`` 的 zip 时间戳
  被显式冻结，否则每次都会产生无意义的 diff）。
"""

from __future__ import annotations

import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rpechart import ChartBuilder, export_pez, save_chart  # noqa: E402
from rpechart.rpedump import dumps  # noqa: E402

FIX = Path(__file__).resolve().parent
CHARTS = FIX / "charts"
MEDIA = FIX / "media"


def _modern_chart() -> None:
    """现代风格的谱面：两种判定线、四种音符、长按、速度事件、特殊事件。"""
    cb = ChartBuilder(
        bpm=174.0, name="Fixture Song", charter="rpechart", composer="nobody",
        level="IN Lv.12", song="song.mp3", background="background.png",
        chart_id="9000000001", rpe_version=160,
    )
    main = cb.line(name="main", x=0, y=-180)
    for i in range(16):
        main.tap(t=float(i), x=-405.0 + (i % 4) * 270.0)
    main.drag(t=4, x=0)
    main.flick(t=8, x=135)
    main.hold(t=12, x=-135, dur=2.0)
    main.line_speed(0, 4, 1.0, 3.0)
    main.line_speed(4, 8, 3.0, 1.5)
    main.at(0, 0)
    main.move_to(120, 60, t=2, dur=4, easing="in_out_sine")
    main.rotate_to(12.5, t=6, dur=2, easing="out_cubic")
    main.color_fade((255, 80, 80), (80, 160, 255), t=8, dur=4)
    main.scale_to(1.5, 1.5, t=10, dur=2)
    main.incline(18.0, t=12, dur=2)
    main.text("fixture", t=0, dur=8)

    side = cb.line(name="side", x=0, y=200)
    for i in range(8):
        side.tap(t=i * 2.0 + 0.5, x=0)
    side.line_speed(0, 20, 12.0, 12.0)
    save_chart(cb.chart, str(CHARTS / "modern.json"))


def _legacy_chart() -> None:
    """老版本（RPEVersion 113）风格 + 刁钻数值，手写 JSON 以便逐字节复现。"""
    cb = ChartBuilder(
        bpm=153.6, name="Legacy", charter="rpechart", composer="nobody",
        level="EZ Lv.1", chart_id="9000000002", rpe_version=113,
    )
    cb.line(name="legacy", x=0, y=0).tap(t=0, x=0)
    data = cb.chart.to_json()
    # alphaEvents 的 start/end 在 RPE 里恒为 int；末尾 4 个 null 层占位；
    # moveXEvents 带 -0.0（RPE 真的这么写）。
    data["judgeLineList"][0]["eventLayers"] = [
        {"alphaEvents": [{
            "startTime": [0, 0, 1], "endTime": [1, 0, 1], "start": 0, "end": 255,
            "easingType": 1, "easingLeft": 0.0, "easingRight": 1.0, "linkgroup": 0,
            "bezier": 0, "bezierPoints": [0.0, 0.0, 0.0, 0.0],
        }]},
        None, None, None,
        {"moveXEvents": [
            {"startTime": [1, 0, 1], "endTime": [2, 0, 2], "start": -0.0, "end": 0.0,
             "easingType": 6, "easingLeft": 0.0, "easingRight": 1.0, "linkgroup": 0},
            {"startTime": [2, 0, 2], "endTime": [3, 0, 2], "start": -135.0, "end": -0.0,
             "easingType": 1, "easingLeft": 0.0, "easingRight": 1.0, "linkgroup": 0},
        ]},
    ]
    data["multiScale"] = 1.0
    data["chartTime"] = 0.0
    data["multiLineString"] = ""
    data["xybind"] = True
    _write_text(CHARTS / "legacy.json", dumps(data) + "\n")


def _multi_chart() -> None:
    """多判定线 + 父子线 + 不遮罩的线 + 假音符 + 非默认贴图。"""
    cb = ChartBuilder(
        bpm=200.0, name="Multi", charter="rpechart", composer="nobody",
        level="AT Lv.15", chart_id="9000000003", rpe_version=170,
    )
    parent = cb.line(name="parent", x=0, y=-100)
    parent.tap(t=0, x=0)
    parent.tap(t=1, x=135)
    child = cb.line(name="child", x=0, y=100)
    child.father(0)
    child.rotate_with_father(True)
    child.tap(t=0.5, x=270)
    ghost = cb.line(name="ghost", x=0, y=300)
    ghost.cover(False)
    ghost.note("tap", t=2, x=-270, is_fake=True)
    ghost.texture("line5.png")
    save_chart(cb.chart, str(CHARTS / "multi.json"))


def _freeze_zip(path: Path, stamp=(2024, 1, 1, 0, 0, 0)) -> None:
    """把 zip 里每个成员的修改时间钉死，让样本可重复生成。

    ``export_pez`` 用的是当前时间（和 RPE 一样），所以同样的谱面重新导出一次，
    字节就变了 —— 样本一旦进版本库，每次重跑 `make_fixtures.py` 都会产生无意义的
    diff。这里把时间戳归零重打一遍，**成员名与内容原样保留**。
    """
    with zipfile.ZipFile(path) as src:
        members = [(i.filename, src.read(i.filename)) for i in src.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as dst:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            dst.writestr(info, data)


def _demo_pez() -> None:
    """打包链路样本：用库自己的导出器产出一个最小 .pez（时间戳冻结）。"""
    cb = ChartBuilder(
        bpm=174.0, name="Fixture Song", charter="rpechart", composer="nobody",
        level="IN Lv.12", song="song.mp3", background="background.png",
        chart_id="9000000001", rpe_version=160,
    )
    line = cb.line(name="main", x=0, y=0)
    for i in range(6):
        line.tap(t=float(i), x=-270.0 + i * 108.0)
    export_pez(cb.chart, str(FIX / "demo.pez"))
    _freeze_zip(FIX / "demo.pez")


# ------------------------------------------------------------------ 合成容器
def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload) + 8) + typ + payload


def _full_box(typ: bytes, version: int, payload: bytes) -> bytes:
    return _box(typ, bytes([version]) + b"\x00\x00\x00" + payload)


#: mvhd/mdhd 的矩阵字段（单位矩阵，3x3 定点）。
_MATRIX = struct.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)


def _mvhd(timescale: int, duration: int) -> bytes:
    """mvhd v0：timescale 在载荷 +12，duration 在 +16（探测器按此偏移读）。"""
    payload = (
        struct.pack(">IIII", 0, 0, timescale, duration)
        + struct.pack(">I", 0x00010000) + struct.pack(">H", 0x0100) + b"\x00" * 10
        + _MATRIX + b"\x00" * 24 + struct.pack(">I", 2)
    )
    return _full_box(b"mvhd", 0, payload)


def _mdhd(timescale: int, duration: int) -> bytes:
    """mdhd v0：timescale 在载荷 +8，duration 在 +12。"""
    payload = struct.pack(">IIII", 0, 0, timescale, duration) + b"\x55\xc4\x00\x00"
    return _full_box(b"mdhd", 0, payload)


def _mp4a(channels: int = 2, rate: int = 44100) -> bytes:
    body = b"\x00" * 6 + struct.pack(">H", 1) + b"\x00" * 8
    body += struct.pack(">HH", channels, 16) + b"\x00\x00\x00\x00"
    body += struct.pack(">I", rate << 16)
    return _box(b"mp4a", body)


def _trak(mdhd: bytes, entry: bytes = b"") -> bytes:
    stsd = _full_box(b"stsd", 0, struct.pack(">I", 1) + entry)
    return _box(b"trak", _box(b"mdia", mdhd + _box(b"stbl", stsd)))


def _mp4(*boxes: bytes) -> bytes:
    ftyp = _box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2mp41")
    # 末尾垫一个 mdat：MP4 探测器要求 mdhd 之后还有 32 字节可读。
    return ftyp + b"".join(boxes) + _box(b"mdat", b"\x00" * 8)


def _media() -> None:
    """两份合成容器 —— 都不含任何真实音频内容。"""
    # 1) 伪装成 .mp3 的 MP4：mdhd 时长为 0，必须回落到 mvhd（真实世界踩过的坑）。
    mvhd_only = _mvhd(1000, 93438)
    disguised = _mp4(_box(b"moov", mvhd_only + _trak(_mdhd(44100, 0), _mp4a(2, 44100))))
    (MEDIA / "disguised.mp3").write_bytes(disguised)
    # 2) 没有音轨的 MP4：采样率/声道都是 0。
    silent = _mp4(_box(b"moov", _mvhd(1000, 93434)))
    (MEDIA / "silent.mp4").write_bytes(silent)


def _write_text(path: Path, text: str) -> None:
    """按 RPE 的规矩写：UTF-8、无 BOM、LF（显式 ``newline=""``）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def main() -> int:
    CHARTS.mkdir(parents=True, exist_ok=True)
    MEDIA.mkdir(parents=True, exist_ok=True)
    _modern_chart()
    _legacy_chart()
    _multi_chart()
    _demo_pez()
    _media()
    for path in sorted(FIX.rglob("*")):
        if path.is_file() and path.name != Path(__file__).name:
            print(f"  {path.relative_to(FIX)}  {path.stat().st_size} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
