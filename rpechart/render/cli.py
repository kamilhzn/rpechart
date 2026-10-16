"""命令行入口：``python -m rpechart.render`` / skill 的 ``rperender.py``。

子命令：

* ``stats``  —— 每 N 拍统计物量 / 屏上音符 / 分线分布，标出空段（**收尾必跑**）；
* ``frame``  —— 渲染某一秒的一帧 PNG；
* ``range``  —— 按固定帧率导出整段每一帧；
* ``sheet``  —— 间隔抽帧拼成 contact sheet（可叠真实视频画面比对）；
* ``video``  —— 把整张谱面渲染成 MP4（双输出：同一份 chart.json）。
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from PIL import Image

from ..io import load_chart
from ..video import ffmpeg_available, render_chart_video
from .renderer import Renderer
from .timeline import RenderModel
from .videostream import VideoStream

__all__ = ["main", "build_parser", "parse_size"]

DEFAULT_OUT_DIR = Path("work")


def parse_size(text):
    """``"1920x1080"`` / ``"640*360"`` → ``(1920, 1080)``。"""
    parts = str(text).lower().replace("*", "x").split("x")
    if len(parts) != 2:
        raise ValueError(f"--size 要写成 1920x1080，收到 {text!r}")
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"--size 要写成 1920x1080，收到 {text!r}") from None
    if w <= 0 or h <= 0:
        raise ValueError(f"--size 必须为正，收到 {text!r}")
    return (w, h)


# 旧名字（测试与文档历史上用过）
_parse_size = parse_size


def _bg_from_video(path, t, w, h, fps):
    """取原片 ``t`` 秒处的一帧做底图（单帧，用完即关）。"""
    if not path:
        return None
    vs = VideoStream(path, fps, w, h)
    try:
        return vs.get(t)
    finally:
        vs.close()


def _auto_cover(chart_path):
    """谱面同目录里按 META.background 找曲绘；找不到返回 None。

    RPE 的约定是 background 就是谱面文件夹里的文件名（真机截图那种纯黑底也来自它）。
    找不到不报错 —— 没曲绘就用纯黑/渐变底。
    """
    try:
        from ..io import load_chart
        meta = load_chart(chart_path).META
        name = getattr(meta, 'background', None)
        if not name:
            return None
        p = Path(chart_path).resolve().parent / str(name)
        if p.is_file():
            return str(p)
        alt = p.with_suffix('.png')
        if alt.is_file():
            return str(alt)
    except Exception:
        return None
    return None


def cmd_stats(args):
    model = RenderModel(load_chart(args.chart))
    rows = model.stats(args.step)
    print(f"{'时刻':>8} {'拍':>8} {'物量':>6} {'屏上':>5}   分线")
    empty = 0
    for sec, b, tot, shown, cnt in rows:
        flag = ""
        if tot == 0:
            empty += 1
            flag = "   <== 空"
        print(f"{sec:8.2f} {b:8.1f} {tot:6d} {shown:5d}   "
              f"{' '.join(f'{k}:{v}' for k, v in cnt.items())}{flag}")
    sec_per_beat = float(model.timing.seconds_per_beat(0.0))
    print(f"\n共 {len(rows)} 个区间（统计到内容结束 {model.content_beats:.1f} 拍），"
          f"其中 {empty} 个完全没有音符"
          f"（区间长度 {args.step} 拍 = {args.step * sec_per_beat:.2f}s）")
    return 0


def cmd_frame(args):
    w, h = args.width, args.height
    bg = Image.open(args.cover).convert("RGB") if args.cover else \
        _bg_from_video(args.with_video, args.at, w, h, 20.0)
    r = Renderer(args.chart)
    img = r.frame(args.at, (w, h), bg=bg, hud=not args.no_hud)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"写出 {out}  ({img.width}x{img.height})")
    return 0


def cmd_range(args):
    r = Renderer(args.chart)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    step = 1.0 / args.fps
    n = 0
    t = args.start
    stream = VideoStream(args.with_video, args.fps, args.width, args.height) \
        if args.with_video else None
    try:
        while t <= args.end + 1e-9:
            bg = stream.get(t) if stream is not None else None
            if bg is None and args.cover:
                bg = Image.open(args.cover).convert("RGB")
            img = r.frame(t, (args.width, args.height), bg=bg, hud=not args.no_hud)
            img.save(outdir / f"f_{t:07.3f}.png")
            n += 1
            t += step
    finally:
        if stream is not None:
            stream.close()
    print(f"写出 {n} 帧到 {outdir}（{args.fps}fps, {args.start}~{args.end}s）")
    return 0


def cmd_sheet(args):
    r = Renderer(args.chart)
    times = []
    t = args.start
    while t <= args.end + 1e-9:
        times.append(round(t, 3))
        t += args.every
    fps = max(1, int(round(1.0 / max(args.every / 4, 0.05))))
    stream = VideoStream(args.with_video, fps, args.width, args.height) \
        if args.with_video else None
    cover = Image.open(args.cover).convert("RGB") if args.cover else None
    cols = args.cols
    rows = math.ceil(len(times) / cols)
    fw, fh = args.width, args.height
    pad = 4
    sheet = Image.new("RGB", (cols * (fw + pad) + pad, rows * (fh + pad) + pad),
                      (25, 25, 32))
    try:
        for i, tt in enumerate(times):
            bg = stream.get(tt) if stream is not None else None
            if bg is None:
                bg = cover
            cell = r.frame(tt, (fw, fh), bg=bg, hud=not args.no_hud)
            cx = pad + (i % cols) * (fw + pad)
            cy = pad + (i // cols) * (fh + pad)
            sheet.paste(cell, (cx, cy))
    finally:
        if stream is not None:
            stream.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(f"写出 {out}  {sheet.width}x{sheet.height}  {len(times)} 帧"
          f"（每 {args.every}s 一帧，{cols} 列，{args.start}~{args.end}s）")
    return 0


def cmd_video(args):
    """谱面 → MP4。音频默认取谱面同目录的 song.mp3（没有就不混音）。"""
    out_path = Path(args.out)
    audio = args.audio
    if audio is None and not args.no_audio:
        guess = Path(args.chart).resolve().parent / "song.mp3"
        if guess.is_file():
            audio = str(guess)
            print(f"自动混音：{audio}")
    if audio is not None and not Path(audio).is_file():
        print(f"警告：音频不存在，忽略：{audio}")
        audio = None

    if not ffmpeg_available():
        print("错误：找不到 ffmpeg（PATH 与 imageio-ffmpeg 都没有）。"
              "装一个即可：pip install imageio-ffmpeg", file=sys.stderr)
        return 3

    w, h = args.size
    state = {"last": -1}

    def progress(done, total):
        if total:
            pct = done * 100 // total
            if pct == state["last"]:
                return
            state["last"] = pct
            sys.stdout.write(f"\r渲染中 {done}/{total} 帧  {pct}%")
        elif done % 30 == 0:
            sys.stdout.write(f"\r渲染中 {done} 帧")
        sys.stdout.flush()

    cover = args.cover
    if cover is None and args.video is None:
        cover = _auto_cover(args.chart)
        if cover:
            print(f"自动铺底：{cover}")

    info = render_chart_video(
        args.chart, out_path, fps=args.fps, size=(w, h),
        start=args.start, end=args.end, audio=audio, cover=cover,
        hud=(args.hud and not args.no_hud), progress=progress,
        extra_args=args.extra,
        video=args.video, blur=args.bg_blur, dim=args.bg_dim)
    sys.stdout.write("\r" + " " * 48 + "\r")
    print(f"写出 {info.path}  {info.width}x{info.height}  {info.fps:g}fps  "
          f"{info.frames} 帧  {info.duration:.2f}s  "
          f"{info.bytesize / (1024.0 * 1024.0):.2f}MB"
          + ("  （含音轨）" if audio else ""))
    return 0


def build_parser():
    ap = argparse.ArgumentParser(
        prog="rperender",
        description="离线谱面渲染器（不打开 PhiEdit 就能抽帧检查 / 出视频）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("chart")
        p.add_argument("--width", type=int, default=640)
        p.add_argument("--height", type=int, default=428)
        p.add_argument("--with-video", default=None,
                       help="把真实视频帧作为底图叠加（需要 ffmpeg）")
        p.add_argument("--cover", default=None,
                       help="曲绘/背景图，铺底并压暗压模糊")
        p.add_argument("--no-hud", action="store_true",
                       help="不画 HUD（combo/分数/曲名/难度）")

    p = sub.add_parser("stats", help="按区间统计物量，找空段")
    common(p)
    p.add_argument("--step", type=float, default=8.0, help="区间长度（拍），默认 8")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("frame", help="渲染单个时刻")
    common(p)
    p.add_argument("--at", type=float, required=True, help="时刻（秒）")
    p.add_argument("-o", "--out", default=str(DEFAULT_OUT_DIR / "frame.png"))
    p.set_defaults(func=cmd_frame)

    p = sub.add_parser("range", help="按固定帧率导出整段每一帧")
    common(p)
    p.add_argument("--from", dest="start", type=float, default=0.0)
    p.add_argument("--to", dest="end", type=float, default=93.0)
    p.add_argument("--fps", type=float, default=4.0)
    p.add_argument("-o", "--out", default=str(DEFAULT_OUT_DIR / "frames"))
    p.set_defaults(func=cmd_range)

    p = sub.add_parser("sheet", help="间隔抽帧拼 contact sheet")
    common(p)
    p.add_argument("--from", dest="start", type=float, default=0.0)
    p.add_argument("--to", dest="end", type=float, default=93.0)
    p.add_argument("--every", type=float, default=2.0)
    p.add_argument("--cols", type=int, default=6)
    p.add_argument("-o", "--out", default=str(DEFAULT_OUT_DIR / "sheet.png"))
    p.set_defaults(func=cmd_sheet)

    p = sub.add_parser("video", help="把谱面渲染成 MP4（双输出：同一份 chart.json）")
    p.add_argument("chart")
    p.add_argument("-o", "--out", required=True, help="输出 mp4")
    p.add_argument("--fps", type=float, default=60.0)
    p.add_argument("--size", default="1920x1080", help="分辨率，形如 1920x1080")
    p.add_argument("--from", dest="start", type=float, default=0.0, help="起始秒")
    p.add_argument("--to", dest="end", type=float, default=None,
                   help="结束秒；默认到谱面内容结束处")
    p.add_argument("--audio", default=None,
                   help="音轨；默认取谱面同目录的 song.mp3（没有则不出声）")
    p.add_argument("--no-audio", action="store_true", help="强制不混音")
    p.add_argument("--cover", default=None, help="曲绘图，铺底并压暗压模糊")
    p.add_argument("--video", default=None,
                   help="动图底版：用原片逐帧做背景（谱面叠在上面）")
    p.add_argument("--bg-blur", type=float, default=None,
                   help="底版模糊半径；0 表示不模糊")
    p.add_argument("--bg-dim", type=float, default=None,
                   help="底版压暗强度 0~1；0 表示保留原片亮度")
    # HUD 默认开：真实游戏画面里 combo/分数/曲名/难度一直在（用户要求「始终渲染」）。
    p.add_argument("--hud", action="store_true", default=True,
                   help="画 HUD（默认就画；保留此开关只为兼容旧命令）")
    p.add_argument("--no-hud", action="store_true", help="不画 HUD")
    p.add_argument("--extra", nargs="*", default=None,
                   help="追加给 ffmpeg 的编码参数")
    p.set_defaults(func=cmd_video)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd == "video":
        try:
            args.size = parse_size(args.size)
        except ValueError as exc:
            ap.error(str(exc))
    return args.func(args)