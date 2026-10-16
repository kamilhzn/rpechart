"""`python -m rpechart.gen` 的命令行。

用法::

    python -m rpechart.gen spec.json -o build/demo.json
    python -m rpechart.gen spec.json --dry              # 只统计，不写文件
    python -m rpechart.gen spec.json -o out.json --install
    python -m rpechart.gen spec.json -o out.json --video --video-out out.mp4

也可以作为子命令调用（`rpechart gen spec.json` 的写法）：首参若正好是
`gen`，会被自动吃掉。

**没有任何写死的路径、曲名、BPM、段落或线数** —— 全部来自 spec.json。
"""

from __future__ import annotations

import argparse
import json
import sys
import typing as _t
from collections import Counter
from pathlib import Path

from .assemble import build, generate
from .spec import ChartSpec, VideoSpec

__all__ = ["build_parser", "main", "stats_text", "render_video"]


def build_parser() -> argparse.ArgumentParser:
    """构造 argparse 解析器（单独暴露，方便测试直接检查参数表）。"""
    ap = argparse.ArgumentParser(
        prog="python -m rpechart.gen",
        description="从 spec.json 生成一张完整的 RPE 谱面",
    )
    ap.add_argument("spec", help="spec.json 路径（或内联 JSON 文本）")
    ap.add_argument("-o", "--out", default=None, help="输出 chart.json（默认用 spec.out）")
    ap.add_argument("--dry", action="store_true", help="只统计与校验，不写文件")
    ap.add_argument("--install", action="store_true", help="装进 RPE 的 Resources/ 目录")
    ap.add_argument("--rpe-root", default=None,
                    help="RPE 根目录（含 Resources/ 与 PhiEdit.exe，默认从输出路径往上找）")
    ap.add_argument("--id", default=None, help="安装时用的谱面 id（默认用 spec.chart_id）")
    ap.add_argument("--spec-out", default=None, help="把（补全后的）spec 也写一份出来")
    ap.add_argument("--indent", type=int, default=2,
                    help="spec.json 的缩进；chart.json 的格式由 rpedump 决定，与此无关")
    ap.add_argument("--quiet", action="store_true", help="只打印错误")
    # ---- 双输出：同一份 chart.json 也可以直接出 MP4
    ap.add_argument("--video", action="store_true",
                    help="写完谱面后再录一份 MP4（同一份 chart.json）")
    ap.add_argument("--video-out", default=None, help="MP4 输出路径")
    ap.add_argument("--video-fps", type=float, default=None)
    ap.add_argument("--video-size", default=None, help="形如 1920x1080")
    ap.add_argument("--video-from", type=float, default=None, help="起始秒")
    ap.add_argument("--video-to", type=float, default=None, help="结束秒")
    ap.add_argument("--video-audio", default=None, help="音轨路径")
    return ap


def stats_text(builder, report) -> str:
    """与 原生成脚本的 `stats(cb)` 等价的统计文本。"""
    ch = builder.chart
    kinds: Counter = Counter()
    per_line: Counter = Counter()
    for ln in ch.judgeLineList:
        for n in ln.notes:
            kinds[n.type_name] += 1
            per_line[ln.Name] += 1
    lines = [
        builder.summary(),
        f"音符类型: {dict(kinds)}",
        f"每条线物量: {dict(per_line)}",
        f"校验: 错误 {len(report.errors)} / 警告 {len(report.warnings)} / "
        f"提示 {len(report.cautions)}",
    ]
    if report.errors:
        lines.append(report.format(max_lines=12))
    return "\n".join(lines)


def render_video(chart_path, *, out=None, fps=60.0, size=(1920, 1080),
                 start=0.0, end=None, audio=None, cover=None, quiet=False):
    """把刚写出的谱面录成 MP4（同一份 chart.json ⇒ 双输出）。

    视频里看到的判定线运动 / 音符落点，与 RPE 里加载的是**同一份数据**，
    不存在两套美术资源对不上的问题。
    """
    from rpechart import render_chart_video

    out = Path(out or "render.mp4")
    state = {"last": -1}

    def progress(done, total):
        if not total or quiet:
            return
        pct = done * 100 // total
        if pct != state["last"] and pct % 5 == 0:
            state["last"] = pct
            print(f"  渲染 {pct}%  ({done}/{total} 帧)", flush=True)

    if not quiet:
        print(f"渲染视频 → {out}")
    info = render_chart_video(chart_path, out, fps=fps, size=size, start=start,
                              end=end, audio=audio, cover=cover, progress=progress)
    if not quiet:
        print(f"写出 {info.path}  {info.width}x{info.height}  {info.fps:g}fps  "
              f"{info.frames} 帧  {info.duration:.2f}s  "
              f"{info.bytesize / 1048576:.2f}MB  （含音轨）")
    return info


def main(argv: _t.Optional[_t.Sequence[str]] = None) -> int:
    """命令行入口。返回 0 表示成功，2 表示校验有 ERROR。"""
    args = list(sys.argv[1:] if argv is None else argv)
    # 允许「rpechart gen spec.json …」这种子命令写法
    if args and args[0] == "gen":
        args = args[1:]
    ns = build_parser().parse_args(args)

    spec = ChartSpec.from_json(ns.spec)
    if ns.out:
        spec.out = ns.out
    if ns.install:
        spec.install = True

    # ---- 生成（dry 也真跑一遍，否则统计没意义）
    cb = build(spec)
    report = cb.validate(song_duration=(spec.duration or None))

    if not ns.quiet:
        print(stats_text(cb, report))

    if ns.dry:
        # 干跑必须**不写文件、不编码**（守门测试 anime_dry_run_never_encodes_a_video）
        return 2 if report.errors else 0

    out = ns.out or spec.out
    if not out:
        print("错误：没有输出路径（spec.out 或 -o）", file=sys.stderr)
        return 1
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    # chart.json 的格式由 rpechart.rpedump 决定（RPE 的 3 空格风格），与 --indent 无关。
    cb.save(out)
    if not ns.quiet:
        print(f"写出 {out}")

    if ns.spec_out:
        spec.write(ns.spec_out, indent=ns.indent)
        if not ns.quiet:
            print(f"写出 spec {ns.spec_out}")

    # ---- 安装进 PhiEdit
    if ns.install:
        from rpechart import find_rpe_root, install_chart

        root = find_rpe_root(ns.rpe_root or Path(out).parent)
        if root is None:
            print("错误：找不到 PhiEdit 根目录（用 --rpe-root 指定）", file=sys.stderr)
            return 1
        cid = ns.id or spec.chart_id or "0"
        song = spec.song if Path(spec.song).is_file() else None
        background = spec.background if Path(spec.background).is_file() else None
        install_chart(root, cid, cb.chart,
                      song_src=song, song_name=Path(spec.song).name or "song.mp3",
                      background_src=background,
                      background_name=Path(spec.background).name or "background.png",
                      overwrite=True)
        if not ns.quiet:
            print(f"已安装到 {Path(root) / 'Resources' / str(cid)}")

    # ---- 双输出：同一份 chart.json
    if ns.video:
        vs: VideoSpec = spec.video_spec()
        if ns.video_out:
            vs.out = ns.video_out
        if ns.video_fps is not None:
            vs.fps = ns.video_fps
        if ns.video_size:
            vs.size = ns.video_size
        if ns.video_from is not None:
            vs.start = ns.video_from
        if ns.video_to is not None:
            vs.end = ns.video_to
        if ns.video_audio:
            vs.audio = ns.video_audio
        render_video(out, out=vs.out, fps=vs.fps, size=vs.width_height,
                     start=vs.start, end=vs.end, audio=vs.audio, cover=vs.cover,
                     quiet=ns.quiet)

    return 2 if report.errors else 0
