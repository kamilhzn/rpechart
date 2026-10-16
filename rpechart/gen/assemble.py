"""组装 —— 把 features / structure / lanes / notes / motion 拼成 ChartBuilder。

这是 原生成脚本的 `build(f, chart_id)` 的泛化。原函数的签名是
`build(f: Feats, chart_id: str)`，曲名 / BPM / 段落 / 线定义全部读模块常量；
这里的签名是 :func:`build(spec: ChartSpec) -> ChartBuilder`，**所有输入都来自
spec**。

固定流水线（顺序有意义）
------------------------
::

    1. 建 ChartBuilder（BPM / META / offset / rpe_version）
    2. 建判定线（LineSpec → cb.line）
    3. META.duration = spec.duration          ← 不写会让 RPE 报 Time Out of Range
    4. 判定线运动（半正弦 + 门限 + 线速 ramp）  ← 与音符**完全解耦**
    5. 音符：plan_notes → tame → free_holds → dedupe   ← 顺序不可换
    6. 写音符到 builder（Hold 与 Tap 叠加，不替换）
    7. 特殊事件演出（色调 / 缩放 / 倾斜 / 文字卡点）
    8. 每条线内排序（非 Hold 在前，再按拍、按 x）

第 3 步的取证
-------------
这是一次真实事故：音频容器与后缀不符（很多 ``.mp3`` 其实是 MP4/AAC），RPE 读不出
时长 ⇒ ``META.duration = 0`` ⇒ 全谱 131 个 ``Time Out of Range`` Error。
所以 `META.duration` 必须显式写，且必须来自参数（`spec.duration`），不能猜。

没有 beats.json 时怎么办
-----------------------
:func:`synth_features` 按段落声明的角色**合成**一份逐拍特征：每个拍的角色由它
落在哪一段决定，`bx` 与 `rx` 走确定性的多正弦曲线（因此音符会横跨轨道而不是
全挤在中间），`low` 给一个固定的重拍图案（因此密集型有地方落）。合成过程
完全确定性，且不依赖任何外部文件 —— 这让「AI 只写一个 spec.json 就能出谱」
在没有音频分析结果时也成立。
"""

from __future__ import annotations

import math
import typing as _t

from .features import ROLE_B, ROLE_N, ROLE_R, ROLE_RB, BeatFeatures
from .motion import MotionSpec, apply_speed_ramp, build_line_motion
from .notes import NotePlanner
from .spec import ChartSpec
from .structure import Section, section_at

__all__ = [
    "DEFAULT_PALETTE",
    "synth_features",
    "load_features",
    "build",
    "build_chart",
    "generate",
    "write",
]


#: 色调 → `[起始 RGB, 结束 RGB]`。原本写死在脚本里的 `PALETTE`。
DEFAULT_PALETTE: dict = {
    "dark": [[40, 40, 60], [70, 70, 110]],
    "blue": [[70, 150, 255], [150, 210, 255]],
    "red": [[255, 70, 70], [255, 150, 110]],
    "neutral": [[150, 150, 170], [220, 220, 240]],
}


# ---------------------------------------------------------------- 特征
#: 没有段表也没有 beats.json 时，用来**合成角色**的默认块表。
#: 每 16 拍一块，循环使用 —— 于是自动切段能切出 B / R / RB / N 交替的结构，
#: 而不是「整首歌都是中性段」（那样只会出稀疏音符，等于没写）。
DEFAULT_ROLE_BLOCKS: tuple = (
    ROLE_N, ROLE_N, ROLE_B, ROLE_B, ROLE_R, ROLE_RB, ROLE_B, ROLE_R,
)

#: 默认块表的块长（拍）。
DEFAULT_ROLE_BLOCK = 16


def synth_features(spec: ChartSpec,
                   sections: _t.Optional[_t.Sequence[Section]] = None) -> BeatFeatures:
    """**合成**逐拍特征（没有 beats.json 时的兜底）。

    两种模式：

    * 给了 `sections`：角色由「这一拍落在哪一段」直接决定，于是
      :meth:`BeatFeatures.role` 的返回值与段表声明**逐拍一致**；
    * 没给 `sections`（自动切段模式）：角色来自 :data:`DEFAULT_ROLE_BLOCKS`
      的确定性块表 —— 这样自动切段才有结构可切，否则整首歌都是中性段。

    合成时关闭平滑与限幅，避免边界处角色被邻段抹掉。

    `bx` 与 `rx` 走三条互质周期的正弦叠加：质心会在网格上来回扫，于是音符
    自然横跨多条轨道，而不是全挤在中央。曲线只与拍号有关，与音符无关
    （判定线运动解耦的同一条原则）。
    """
    n = max(1, int(math.ceil(spec.last_beat or 1.0)))
    secs = list(sections) if sections else []
    rn: list = []
    bn: list = []
    warmth: list = []
    bx: list = []
    rx: list = []
    cx: list = []
    low: list = []
    motion: list = []
    for i in range(n):
        if secs:
            sec = section_at(secs, i)
            role = sec.role if sec is not None else ROLE_N
        else:
            role = DEFAULT_ROLE_BLOCKS[(i // DEFAULT_ROLE_BLOCK) % len(DEFAULT_ROLE_BLOCKS)]
        if role == ROLE_RB:
            rn.append(4000.0)
            bn.append(4000.0)
            warmth.append(0.0)
        elif role == ROLE_R:
            rn.append(5000.0)
            bn.append(100.0)
            warmth.append(3.0)
        elif role == ROLE_B:
            rn.append(100.0)
            bn.append(5000.0)
            warmth.append(-6.0)
        else:
            rn.append(0.0)
            bn.append(0.0)
            warmth.append(0.0)
        # 质心曲线：三条互质周期，跨度覆盖整个网格（±405）
        bx.append(405.0 * math.sin(2.0 * math.pi * (i / 17.0) + 0.30))
        rx.append(405.0 * math.sin(2.0 * math.pi * (i / 23.0) + 1.10))
        cx.append(405.0 * math.sin(2.0 * math.pi * (i / 29.0) + 2.00))
        # 重拍图案：偶数拍是局部极大，因此重拍落在偶数拍上
        low.append(1.0 if i % 2 == 0 else 0.2)
        motion.append(0.5 if i % 4 == 0 else 0.1)
    return BeatFeatures(
        bpm=spec.bpm,
        phase=spec.phase,
        duration=spec.duration,
        sec=[spec.phase + i * (60.0 / spec.bpm if spec.bpm else 0.0) for i in range(n)],
        low=low,
        mid=[0.0] * n,
        high=[0.0] * n,
        full=[0.0] * n,
        motion=motion,
        warmth=warmth,
        rn=rn,
        bn=bn,
        rx=rx,
        bx=bx,
        cx=cx,
        thresholds=spec.thresholds(),
        # 合成特征必须**关闭**平滑与限幅，否则段边界处的角色会被邻段抹掉
        smooth_window=0,
        slew_limit=0.0,
        center_slew_limit=0.0,
    )


def load_features(spec: ChartSpec,
                  sections: _t.Optional[_t.Sequence[Section]] = None) -> BeatFeatures:
    """取特征：`spec.features`（内联）> `spec.beats`（路径）> 合成。

    `spec.bpm` 与 `spec.phase` 会**覆盖** beats.json 里的同名值 —— spec 是
    唯一真值来源，避免「文件里的 BPM 和 spec 里写的不是一回事」。

    `sections` 为 `None` 表示「自动切段模式」：合成特征改用确定性角色块表，
    而不是从（此时还不存在的）段表里读角色。
    """
    f: _t.Optional[BeatFeatures] = None
    if spec.features:
        f = BeatFeatures.from_json(spec.features)
    elif spec.beats:
        from .features import load_beats
        f = load_beats(spec.beats)
    if f is None:
        return synth_features(spec, sections)
    f.bpm = spec.bpm
    f.phase = spec.phase
    f.thresholds = spec.thresholds()
    if spec.duration:
        f.duration = spec.duration
    return f


# ---------------------------------------------------------------- 线
def _make_lines(cb, spec: ChartSpec) -> list:
    """建判定线并返回 JudgeLineBuilder 列表（索引即列表下标）。"""
    builders = []
    for ln in spec.lines:
        kwargs = {"name": ln.name, "x": ln.x, "y": ln.y}
        if ln.group:
            kwargs["group"] = ln.group
        if ln.z_order:
            kwargs["z_order"] = ln.z_order
        if ln.father is not None:
            kwargs["father"] = ln.father
        lb = cb.line(**kwargs)
        if ln.texture:
            lb.texture(ln.texture)
        lb.cover(bool(ln.cover))
        if ln.attach_ui:
            lb.attach_ui(ln.attach_ui)
        builders.append(lb)
    return builders


def _motion_with_defaults(spec: ChartSpec, motion: MotionSpec) -> MotionSpec:
    """补全 `y_tracks` 与 `angle_tracks` 的缺省值。

    **这一步是必需的**：`cb.line(y=…)` 把线的初始 y 放在 `LineSpec.y`，而
    `moveY` 事件在 `t=0` 就会覆盖它。若 `y_tracks` 里没有这条线，事件会从
    `0 + 摆动` 开始，线在第一个事件处「跳」一下。所以缺省值必须等于
    `LineSpec.y`（角度同理，默认 0）。
    """
    for idx, ln in enumerate(spec.lines):
        motion.y_tracks.setdefault(idx, {0.0: float(ln.y)})
        motion.angle_tracks.setdefault(idx, {0.0: 0.0})
    return motion


# ---------------------------------------------------------------- 特殊事件
def _apply_special(cb, spec: ChartSpec, builders: list,
                   sections: _t.Sequence[Section]) -> None:
    """色调渐变 / 缩放 / 倾斜 / 文字卡点 / 闪烁。

    全部来自 `spec.special` 与 `spec.palette` 以及 `spec.captions`，没有
    硬编码的段名或秒数（原脚本的 `CAPTIONS` 是 9 条写死的秒 → 文字）。
    """
    if not builders:
        return
    sp = dict(spec.special or {})
    palette = {**DEFAULT_PALETTE, **(spec.palette or {})}
    color_idx = int(sp.get("color_line", len(builders) - 1))
    color_idx = max(0, min(len(builders) - 1, color_idx))
    color_lb = builders[color_idx]

    # ---- 色调渐变：每段开头若干拍（或段长）从起始色过渡到结束色
    fade_len = float(sp.get("color_fade_len", 4.0))
    for sec in sections:
        if sec.end <= sec.start:
            continue
        pair = palette.get(sec.tone) or palette.get("neutral")
        if not pair:
            continue
        c0, c1 = pair[0], pair[1]
        color_lb.color_fade(list(c0), list(c1), float(sec.start),
                            float(min(fade_len, sec.end - sec.start)))

    # ---- 缩放 / 倾斜：密度够高的段做一次「爆发」
    scale_at = float(sp.get("scale_threshold", 1.5))
    pulse_in = float(sp.get("pulse_in", 2.0))
    pulse_hold = float(sp.get("pulse_hold", 6.0))
    scale_to = float(sp.get("scale_to", 1.7))
    incline_angle = float(sp.get("incline_angle", 18.0))
    do_pulse = bool(sp.get("pulse", True))
    do_incline = bool(sp.get("incline", True))
    color_lb.scale_to(1.0, 1.0, 0.0, 1.0)
    for sec in sections:
        if sec.end <= sec.start or sec.density < scale_at:
            continue
        a, b = float(sec.start), float(sec.end)
        if do_pulse:
            color_lb.scale_to(scale_to, scale_to, a, pulse_in, easing="out_quad")
            color_lb.scale_to(1.0, 1.0, float(min(b - 2, a + pulse_hold)), pulse_in,
                              easing="in_quad")
        if do_incline:
            color_lb.incline(incline_angle, a, pulse_in, from_angle=0.0)
            color_lb.incline(0.0, float(min(b - 2, a + pulse_hold)), pulse_in,
                             from_angle=incline_angle)

    # ---- 文字卡点：秒 → 拍（用 spec 自己的 BPM 与 phase 换算）
    captions = spec.captions or {}
    if captions and spec.bpm:
        for sec_str, txt in captions.items():
            try:
                sec_v = float(sec_str)
            except (TypeError, ValueError):
                continue
            beat = (sec_v - spec.phase) / (60.0 / spec.bpm)
            if 0.0 <= beat < (spec.last_beat or 0.0):
                color_lb.text(str(txt), round(beat, 3), float(sp.get("caption_dur", 3.0)))

    # ---- 闪烁：让某条线间歇露面（原脚本是线 2「旁观者」每 32 拍闪一次）
    blink_idx = sp.get("blink_line", None)
    blink_every = int(sp.get("blink_every", 0) or 0)
    if blink_idx is not None and blink_every > 0:
        bi = max(0, min(len(builders) - 1, int(blink_idx)))
        blink_lb = builders[bi]
        dim = float(sp.get("blink_alpha", 60.0))
        dur = float(sp.get("blink_dur", 0.5))
        last = int(spec.last_beat or 0)
        for i in range(16, last - 4, blink_every):
            blink_lb.fade_to(dim, float(i), dur)
            blink_lb.fade_to(255.0, float(i + 2), dur)


# ---------------------------------------------------------------- 主流程
def build(spec: ChartSpec):
    """`ChartSpec → ChartBuilder`（原 `build(f, chart_id)` 的泛化）。"""
    from rpechart import ChartBuilder, NOTE_HOLD

    problems = spec.problems()
    if problems:
        raise ValueError("spec 参数不合法：\n  - " + "\n  - ".join(problems))

    cb = ChartBuilder(
        bpm=spec.bpm,
        name=spec.name,
        charter=spec.charter,
        composer=spec.composer,
        level=spec.level,
        song=spec.song,
        background=spec.background,
        illustrator=spec.illustrator,
        offset=spec.offset,
        rpe_version=spec.rpe_version,
        chart_id=spec.chart_id,
    )

    # ---- 1. 判定线
    builders = _make_lines(cb, spec)

    # ---- 2. META.duration：不写会让 RPE 报 Time Out of Range（真实事故）
    cb.chart.META.duration = float(spec.duration)

    # ---- 3. 特征与段表
    auto = bool(spec.auto_sections and not spec.sections)
    sections = spec.section_list(None)
    # 自动切段模式下，合成特征的角色来自确定性块表（此时还没有段表可读）
    features = load_features(spec, None if auto else sections)
    if auto:
        sections = spec.section_list(features)
    last_beat = float(spec.last_beat or features.n or 1.0)

    # ---- 4. 判定线运动（与音符解耦）
    motion = _motion_with_defaults(spec, spec.motion_spec())
    if builders:
        build_line_motion(builders, motion, sections=sections, last_beat=last_beat)
        apply_speed_ramp(builders, motion, sections=sections, last_beat=last_beat)

    # ---- 5. 音符：plan → tame → free_holds → dedupe（顺序不可换）
    grid = spec.lane_grid()
    planner = spec.planner()
    notes = planner.plan(features, sections)
    notes = grid.legalize(notes)

    # ---- 6. 写音符（Hold 与 Tap **叠加**，不替换）
    playable = {i for i, ln in enumerate(spec.lines) if ln.playable}
    nlines = len(builders)
    for n in notes:
        line = int(n.line)
        if line < 0 or line >= nlines or line not in playable:
            continue
        lb = builders[line]
        if n.kind == "tap":
            lb.tap(n.t, n.x)
        elif n.kind == "flick":
            lb.flick(n.t, n.x)
        elif n.kind == "drag":
            lb.drag(n.t, n.x)
        elif n.kind == "hold":
            if n.dur <= 0:
                continue
            lb.hold(n.t, n.x, dur=n.dur)

    # ---- 7. 特殊事件
    _apply_special(cb, spec, builders, sections)

    # ---- 8. 线内排序：非 Hold 段在前（时间升序），Hold 段在后（时间升序）
    #         与真实 RPE v150+ 一致（原脚本 build() 末尾那一段）。
    for ln in cb.chart.judgeLineList:
        ln.notes.sort(key=lambda x: (x.type == NOTE_HOLD, x.startTime.frac, x.positionX))

    return cb


def build_chart(spec: ChartSpec):
    """`ChartSpec → Chart`（只要谱面对象时用）。"""
    return build(spec).chart


def write(spec: ChartSpec, path: _t.Optional[str] = None, *, indent: int = 2):
    """生成并写出 `chart.json`，返回写出路径。"""
    cb = build(spec)
    out = path or spec.out
    if not out:
        raise ValueError("没有输出路径：请给 spec.out 或显式传 path")
    cb.save(out, indent=indent)
    return out


def generate(spec: ChartSpec, path: _t.Optional[str] = None, *,
             validate_song_duration: bool = True, indent: int = 2) -> dict:
    """一步到位：生成 → 校验 → 写出。返回统计 dict。

    返回值：`{"path", "chart", "builder", "report", "notes", "kinds", "lines"}`。
    `report` 是 :class:`rpechart.ValidationReport`，CLI 用它决定退出码。
    """
    from collections import Counter

    cb = build(spec)
    report = cb.validate(song_duration=(spec.duration if validate_song_duration else None))
    out = path or spec.out
    if not out:
        raise ValueError("没有输出路径：请给 spec.out 或显式传 path")
    cb.save(out, indent=indent)

    kinds: Counter = Counter()
    for ln in cb.chart.judgeLineList:
        for n in ln.notes:
            kinds[n.type_name] += 1
    return {
        "path": out,
        "chart": cb.chart,
        "builder": cb,
        "report": report,
        "notes": sum(kinds.values()),
        "kinds": dict(kinds),
        "lines": len(cb.chart.judgeLineList),
    }
