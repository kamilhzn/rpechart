"""端到端示例：从零生成一张能直接放进 RPE 打开的谱面。

跑法::

    python examples/make_demo_chart.py                    # 写出 build/chart.json
    python examples/make_demo_chart.py --song song.mp3    # 带音频（顺便测 BPM）
    python examples/make_demo_chart.py --install --rpe-root /path/to/RPE

脚本刻意用了**每一种**制谱要素各一次，所以它同时是可执行的 API 文档：
判定线的位置/移动/数量、四种音符、长按、速度、特殊事件（缩放/染色/文字）、
属性控制、时间与 BPM 换算、以及最后的校验。

生成的文件本身不是好谱面（音符是机械排列的），它的用途是证明
「这些要素都能被程序写出来」，以及给你一个可以照着改的骨架。

**不带任何音频/曲绘也能跑**：默认 ``--song`` 为空，谱面照样生成（只是没有
时长与 BPM 探测）。要音频就自己传 ``--song``；``--install`` 还会把它复制进
谱面文件夹。
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rpechart._bootstrap import ensure_importable  # noqa: E402

ROOT = ensure_importable(__file__)


from rpechart import (  # noqa: E402
    ChartBuilder,
    NoteSpec,
    duration_of,
    estimate_tempo,
    find_rpe_root,
    install_chart,
    load_chart,
)

#: 示例曲目。默认留空：这个仓库**不带任何音频**，给路径就用，不给就当没有。
#: RPE 接受 mp3 / wav / ogg —— 注意后缀不可信（很多 .mp3 其实是 MP4 封装）。
SONG = ""
#: 曲绘。同样默认留空；RPE 的 background 要的是静态图，不是视频。
BACKGROUND = ""
CHART_ID = "999999999"  # RPE 只扫描纯数字文件夹名


def build(chart_id=CHART_ID, song=SONG, background=BACKGROUND):
    """搭出示例谱面，返回 (ChartBuilder, 音频时长秒)。"""

    # ---------------------------------------------------------- 1. 基准信息
    # 先测时长与 BPM：BPMList 一旦错位，后面所有落点都会漂。
    song_duration = None
    bpm = 120.0
    if song and os.path.exists(song):
        try:
            song_duration = duration_of(song)
        except Exception as exc:  # 解码器缺失等
            print(f"  [警告] 读不出音频时长: {exc}")
        try:
            est = estimate_tempo(song)
            if est.bpm > 0:
                bpm = est.bpm
                print(f"  测得 BPM = {est.bpm:g} (置信度 {est.confidence:.2f})")
        except Exception as exc:
            print(f"  [警告] BPM 估计失败，退回 120: {exc}")

    cb = ChartBuilder(
        bpm=bpm,
        name="程序化示例",
        charter="rpechart",
        composer="未知",
        level="EZ Lv.1",
        song=os.path.basename(song) if song else "song.mp3",
        background=os.path.basename(background) if background else "background.png",
        chart_id=chart_id,
        rpe_version=160,
    )

    # ---------------------------------------------------------- 2. 判定线
    # 判定线是「容器」：位置、角度、不透明度、以及它上面所有音符。
    main = cb.line(name="主线", x=0, y=0)
    main.texture("line.png")  # 换贴图，例如 "line2.png"
    main.cover(True)  # 启用遮罩：背面的音符不渲染

    # 位置设计：at() 设初始位置，move_to() 在一段时间里滑过去。
    main.at(-400, 0)
    main.move_to(0, 200, t=0, dur=4, easing="out_sine")
    main.move_to(400, -100, t=8, dur=4.0, easing="in_out_quad")

    # 旋转与透明度
    main.rotate_to(15, t=4, dur=2, easing="out_back")
    main.spin(360, t=16, dur=8)  # 转一整圈
    main.fade_to(200, t=12, dur=1)

    # ---------------------------------------------------------- 3. 四种音符
    # Tap=蓝 Drag=黄 Flick=红 Hold=长条；t 单位是拍。
    for i in range(8):
        main.tap(t=i * 0.5, x=-400 + i * 100)

    # Drag / Flick 只标出类型，落点规则相同
    main.drag(t=5, x=-200)
    main.flick(t=5.5, x=0)
    main.drag(t=6, x=200)

    # 长按：dur 在 0.25 拍以上
    main.hold(t=8, x=-150, dur=2)
    main.hold(t=8, x=150, dur=2)

    # 批量：等差排布、之字形、以及自定义「音符配方」
    main.stream("tap", t=12, count=8, interval=0.25, x_from=-300, x_to=300)
    main.zigzag("drag", t=16, count=8, interval=0.25, x_left=-300, x_right=300)

    # NoteSpec 给整条线设默认值；显式传参可以逐个覆盖
    fast = cb.line(name="高速线", x=0, y=0)
    fast.note_spec = NoteSpec(speed=2.5, size=0.8)
    fast.tap(0, 0)
    fast.tap(1, 0, speed=1.0)  # 覆盖 NoteSpec 的 2.5
    fast.hold(t=2, x=0, dur=1)

    # 假音符：无判定、不计分，用来做视觉装饰
    fast.note("tap", t=3, x=-250, is_fake=True)

    # ---------------------------------------------------------- 4. 速度安排
    # 判定线的基准速度由速度事件控制；音符自己还有速度倍率。
    fast.line_speed(start_time=0, end_time=8, start=1.0, end=3.0, easing="linear")

    # ---------------------------------------------------------- 5. 特殊事件
    # 这些只写在第 5 层（layer=4）。缩放/染色/文字都不影响判定。
    decor = cb.line(name="装饰线", x=0, y=-300)
    decor.scale_to(1.5, 0.5, t=0, dur=4)
    decor.color_fade((255, 80, 80), (80, 160, 255), t=4, dur=4)
    decor.text("程序化制谱", t=0, dur=8)
    decor.incline(20, t=0, dur=4)  # Z 轴倾斜（RPE 界面里改不了，只能写文件）

    # ---------------------------------------------------------- 6. 曲线与函数
    # 曲线轨迹：起止拍 + 起止坐标，内部自动切成首尾相接的事件。
    curve_line = cb.line(name="曲线线", x=-300, y=-200)
    curve_line.curve(0, 8, -300, 300, -200, 200, beats=16)
    curve_line.curve(8, 16, 300, -300, 200, -200, beats=16, arc=180.0)

    # 任意函数驱动的单类事件（例如 alpha 走正弦）
    import math

    curve_line.event_curve("alphaEvents", t=0, dur=8, fn=lambda p: 255 * (0.5 + 0.5 * math.sin(p * math.pi * 4)))

    return cb, song_duration


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成 RPE 示例谱面")
    ap.add_argument("--install", action="store_true", help="装进 PhiEdit/Resources/ 让 RPE 直接看到")
    ap.add_argument("--out", default=os.path.join("build", "chart.json"), help="输出路径")
    ap.add_argument("--id", default=CHART_ID, help="谱面 ID（纯数字文件夹名）")
    ap.add_argument("--rpe-root", default=None, help="RPE 根目录，默认自动探测")
    ap.add_argument("--song", default=SONG, help="音频路径（换成自己的曲子）")
    ap.add_argument("--background", default=BACKGROUND, help="曲绘路径（空串表示不装曲绘）")
    ap.add_argument("--bpm", type=float, default=None, help="跳过 BPM 检测，直接指定")
    args = ap.parse_args(argv)

    song = args.song or ""
    background = args.background or ""
    cb, song_duration = build(chart_id=args.id, song=song, background=background)
    if args.bpm:
        cb.set_bpm(args.bpm)
        print(f"  使用指定 BPM = {args.bpm:g}")

    print()
    print(cb.summary())

    report = cb.validate(song_duration=song_duration)
    print()
    print(report.format())
    if not report.ok:
        print(f"!! 有 {len(report.errors)} 个 error，装进 RPE 前应该先修")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    cb.save(args.out)
    print(f"\n已写出 {os.path.abspath(args.out)}")

    if args.install:
        root = find_rpe_root(args.rpe_root or os.getcwd())
        if root is None:
            print("找不到 RPE 根目录（需要含 Resources/ 与 PhiEdit.exe），跳过安装")
            return 1
        folder = install_chart(
            root,
            args.id,
            cb.chart,
            song_src=song if song and os.path.exists(song) else None,
            background_src=background if background and os.path.exists(background) else None,
            overwrite=True,
        )
        print(f"已安装到 {folder.path}")
        print("在 RPE 里打开谱面列表即可看到（1.5.0+ 会自动扫描纯数字文件夹）")
        # 读回来确认真的落盘且能解析
        again = load_chart(os.path.join(str(folder.path), "chart.json"))
        print(f"读回校验: 线数 {len(again.judgeLineList)}  物量 {again.note_count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
