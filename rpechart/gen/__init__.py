"""`rpechart.gen` —— 「参数 → 谱面」生成层。

这一层的目标是**可复用**：调用方只提供参数（曲名 / BPM / 段落 / 风格），
就能得到一张完整的 :class:`rpechart.Chart`。它不依赖任何音频分析结果 ——
没有特征数据时会按段落角色**合成**一份确定性的逐拍特征。

**入口只有一个**：:class:`ChartSpec`。写一个 JSON，交给 :func:`assemble.generate`，
得到 chart.json + 校验报告。

一分钟上手
----------
::

    from rpechart.gen import ChartSpec, generate

    spec = ChartSpec.from_json("demo.spec.json")
    result = generate(spec)
    print(result["notes"], "个音符", result["report"].format())

或命令行：:

    python -m rpechart.gen demo.spec.json -o build/demo.json
    python -m rpechart.gen demo.spec.json --dry

模块分工
--------
=========================  ====================================================
:mod:`features`            逐拍特征（平滑 / 填充 / 限幅 / 角色判定 / beats.json IO）
:mod:`structure`           段落模型（角色枚举 + 自动切段 + 手动段表）
:mod:`lanes`               轨道网格 + 三道合法化（tame / free_holds / dedupe）
:mod:`notes`               音符编排（节奏型 / 角色分派 / Hold 生成）
:mod:`motion`              判定线运动（cos 半正弦 / 门限插值 / 线速 ramp）
:mod:`assemble`            组装成 ChartBuilder → Chart
:mod:`spec`                声明式参数对象 ChartSpec
:mod:`cli`                 命令行
=========================  ====================================================

四条**不能违反**的硬约束（都来自真实事故，模块注释里各有取证）
-------------------------------------------------------------
1. 半正弦用 `easing="in_out_sine"`，且端点必须用 `cos` 采样 —— 用 `sin` 会让
   `phase=0` 的轴恒等于 0，线「不动」（:mod:`motion`）；
2. 后处理顺序不可换：`plan_notes → tame → free_holds → dedupe`
   （:mod:`lanes`）；
3. Hold 与 Tap **叠加不替换**，中性段（`role == "N"`）**不能整段跳过**
   （:mod:`notes`）；
4. `line_speed` 不过 `_splice`，区间重叠由生成层自证
   （:func:`motion.assert_disjoint`）。
"""

from __future__ import annotations

from .assemble import (
    DEFAULT_PALETTE,
    build,
    build_chart,
    generate,
    load_features,
    synth_features,
    write,
)
from .features import (
    ROLE_B,
    ROLE_N,
    ROLE_R,
    ROLE_RB,
    ROLES,
    BeatFeatures,
    RoleThresholds,
    fill_none,
    limit_slew,
    load_beats,
    save_beats,
    smooth,
)
from .lanes import (
    DEFAULT_GRID,
    KINDS,
    GenNote,
    LaneGrid,
    dedupe_same_line,
    free_holds,
    tame_lanes,
)
from .motion import (
    MotionSpec,
    SwaySpec,
    apply_speed_ramp,
    assert_disjoint,
    build_line_motion,
    gate_keyframes,
    lerp_table,
    speed_ramp,
    sway,
    sway_waypoint,
)
from .notes import (
    DEFAULT_PLANS,
    OFFSETS,
    PATTERN_OFFSETS,
    NotePlanner,
    RolePlan,
    plan_notes,
    stable_jitter,
    stable_unit,
)
from .spec import ChartSpec, LineSpec, VideoSpec
from .structure import (
    DEFAULT_DENSITY,
    TONE_BY_ROLE,
    Section,
    auto_sections,
    normalize_sections,
    section_at,
    sections_tile,
)

__all__ = [
    # ---- 门面
    "ChartSpec",
    "LineSpec",
    "VideoSpec",
    # ---- 特征
    "BeatFeatures",
    "RoleThresholds",
    "ROLE_B",
    "ROLE_R",
    "ROLE_RB",
    "ROLE_N",
    "ROLES",
    "smooth",
    "fill_none",
    "limit_slew",
    "load_beats",
    "save_beats",
    # ---- 结构
    "Section",
    "TONE_BY_ROLE",
    "DEFAULT_DENSITY",
    "auto_sections",
    "normalize_sections",
    "section_at",
    "sections_tile",
    # ---- 轨道
    "LaneGrid",
    "GenNote",
    "KINDS",
    "DEFAULT_GRID",
    "tame_lanes",
    "free_holds",
    "dedupe_same_line",
    # ---- 音符
    "NotePlanner",
    "RolePlan",
    "DEFAULT_PLANS",
    "OFFSETS",
    "PATTERN_OFFSETS",
    "plan_notes",
    "stable_unit",
    "stable_jitter",
    # ---- 运动
    "MotionSpec",
    "SwaySpec",
    "sway",
    "sway_waypoint",
    "gate_keyframes",
    "lerp_table",
    "speed_ramp",
    "assert_disjoint",
    "build_line_motion",
    "apply_speed_ramp",
    # ---- 组装
    "DEFAULT_PALETTE",
    "synth_features",
    "load_features",
    "build",
    "build_chart",
    "generate",
    "write",
]

__version__ = "0.1.0"
