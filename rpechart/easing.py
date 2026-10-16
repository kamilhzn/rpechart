"""RPE 的 29 种缓动（easingType 1~29）。

数值与函数实现逐字取自 phira-docs 的官方 Python 参考实现
(<https://teamflos.github.io/phira-docs/> 的 ``easingType`` 一节)，与 RPE 编辑器、
Phira 播放器完全一致。

约定：``easing_type`` 为 1 基索引，``ease(1, t) == t``。
"""

from __future__ import annotations

import math
import typing as _t

__all__ = [
    "EASE_FUNCS",
    "EASE_NAMES",
    "EASE_NAME_TO_ID",
    "MAX_EASING",
    "clamp_easing",
    "ease",
    "easing_name",
    "easing_id",
    "bezier",
    "OVERSHOOT",
    "overshoot_bounds",
]

#: 各缓动的 (min, max) 实测过冲包络，用于校验与 clamp 判定。
#: back 族过冲 ±10%，elastic 族最大到 1.3731（easings.net 的 c4 = 2π/3 标准构造）。
#: 数值由 ``tests/test_easing.py`` 以 2001 点采样实测得出。
OVERSHOOT: list[tuple[float, float]] = [
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.100004),
    (-0.100004, 1.0),
    (0.0, 1.0),
    (-0.100151, 1.100151),
    (0.0, 1.373093),
    (-0.373093, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (0.0, 1.0),
    (-0.118348, 1.118348),
]


def overshoot_bounds(easing_type: _t.Any, tol: float = 1e-6) -> tuple[float, float]:
    """返回该缓动在 ``t∈[0,1]`` 上的实测值域（含容差）。

    ``ease()`` 的结果对移动/旋转/缩放事件是插值权重，过冲意味着视觉上会冲过终点
    再回弹；写谱时若不想过冲，请避免 20/21/23/24/25/29 这几个 id。
    """
    lo, hi = OVERSHOOT[clamp_easing(easing_type) - 1]
    return (lo - tol, hi + tol)

# ---------------------------------------------------------------- 函数表
EASE_FUNCS: list[_t.Callable[[float], float]] = [
    lambda t: t,  # 1 linear
    lambda t: math.sin((t * math.pi) / 2),  # 2 out sine
    lambda t: 1 - math.cos((t * math.pi) / 2),  # 3 in sine
    lambda t: 1 - (1 - t) * (1 - t),  # 4 out quad
    lambda t: t**2,  # 5 in quad
    lambda t: -(math.cos(math.pi * t) - 1) / 2,  # 6 in out sine
    lambda t: 2 * (t**2) if t < 0.5 else 1 - (-2 * t + 2) ** 2 / 2,  # 7 in out quad
    lambda t: 1 - (1 - t) ** 3,  # 8 out cubic
    lambda t: t**3,  # 9 in cubic
    lambda t: 1 - (1 - t) ** 4,  # 10 out quart
    lambda t: t**4,  # 11 in quart
    lambda t: 4 * (t**3) if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2,  # 12 in out cubic
    lambda t: 8 * (t**4) if t < 0.5 else 1 - (-2 * t + 2) ** 4 / 2,  # 13 in out quart
    lambda t: 1 - (1 - t) ** 5,  # 14 out quint
    lambda t: t**5,  # 15 in quint
    lambda t: 1 if t == 1 else 1 - 2 ** (-10 * t),  # 16 out expo
    lambda t: 0 if t == 0 else 2 ** (10 * t - 10),  # 17 in expo
    lambda t: (1 - (t - 1) ** 2) ** 0.5,  # 18 out circ
    lambda t: 1 - (1 - t**2) ** 0.5,  # 19 in circ
    lambda t: 1 + 2.70158 * ((t - 1) ** 3) + 1.70158 * ((t - 1) ** 2),  # 20 out back
    lambda t: 2.70158 * (t**3) - 1.70158 * (t**2),  # 21 in back
    lambda t: (1 - (1 - (2 * t) ** 2) ** 0.5) / 2
    if t < 0.5
    else (((1 - (-2 * t + 2) ** 2) ** 0.5) + 1) / 2,  # 22 in out circ
    lambda t: ((2 * t) ** 2 * ((2.5949095 + 1) * 2 * t - 2.5949095)) / 2
    if t < 0.5
    else ((2 * t - 2) ** 2 * ((2.5949095 + 1) * (t * 2 - 2) + 2.5949095) + 2) / 2,  # 23 in out back
    lambda t: 0
    if t == 0
    else (
        1
        if t == 1
        else 2 ** (-10 * t) * math.sin((t * 10 - 0.75) * (2 * math.pi / 3)) + 1
    ),  # 24 out elastic
    lambda t: 0
    if t == 0
    else (
        1
        if t == 1
        else -(2 ** (10 * t - 10)) * math.sin((t * 10 - 10.75) * (2 * math.pi / 3))
    ),  # 25 in elastic
    lambda t: 7.5625 * (t**2)
    if (t < 1 / 2.75)
    else (
        7.5625 * (t - (1.5 / 2.75)) * (t - (1.5 / 2.75)) + 0.75
        if (t < 2 / 2.75)
        else (
            7.5625 * (t - (2.25 / 2.75)) * (t - (2.25 / 2.75)) + 0.9375
            if (t < 2.5 / 2.75)
            else 7.5625 * (t - (2.625 / 2.75)) * (t - (2.625 / 2.75)) + 0.984375
        )
    ),  # 26 out bounce
    lambda t: 1
    - (
        7.5625 * ((1 - t) ** 2)
        if ((1 - t) < 1 / 2.75)
        else (
            7.5625 * ((1 - t) - (1.5 / 2.75)) * ((1 - t) - (1.5 / 2.75)) + 0.75
            if ((1 - t) < 2 / 2.75)
            else (
                7.5625 * ((1 - t) - (2.25 / 2.75)) * ((1 - t) - (2.25 / 2.75)) + 0.9375
                if ((1 - t) < 2.5 / 2.75)
                else 7.5625 * ((1 - t) - (2.625 / 2.75)) * ((1 - t) - (2.625 / 2.75))
                + 0.984375
            )
        )
    ),  # 27 in bounce
    lambda t: (
        (
            1
            - (
                7.5625 * ((1 - 2 * t) ** 2)
                if ((1 - 2 * t) < 1 / 2.75)
                else (
                    7.5625 * ((1 - 2 * t) - (1.5 / 2.75)) * ((1 - 2 * t) - (1.5 / 2.75)) + 0.75
                    if ((1 - 2 * t) < 2 / 2.75)
                    else (
                        7.5625 * ((1 - 2 * t) - (2.25 / 2.75)) * ((1 - 2 * t) - (2.25 / 2.75))
                        + 0.9375
                        if ((1 - 2 * t) < 2.5 / 2.75)
                        else 7.5625
                        * ((1 - 2 * t) - (2.625 / 2.75))
                        * ((1 - 2 * t) - (2.625 / 2.75))
                        + 0.984375
                    )
                )
            )
        )
        / 2
        if t < 0.5
        else (
            1
            + (
                7.5625 * ((2 * t - 1) ** 2)
                if ((2 * t - 1) < 1 / 2.75)
                else (
                    7.5625 * ((2 * t - 1) - (1.5 / 2.75)) * ((2 * t - 1) - (1.5 / 2.75)) + 0.75
                    if ((2 * t - 1) < 2 / 2.75)
                    else (
                        7.5625 * ((2 * t - 1) - (2.25 / 2.75)) * ((2 * t - 1) - (2.25 / 2.75))
                        + 0.9375
                        if ((2 * t - 1) < 2.5 / 2.75)
                        else 7.5625
                        * ((2 * t - 1) - (2.625 / 2.75))
                        * ((2 * t - 1) - (2.625 / 2.75))
                        + 0.984375
                    )
                )
            )
        )
        / 2
    ),  # 28 in out bounce
    # 29 in out elastic
    # 采用 easings.net 的解析式（亦即 phira-docs「Python 缓动示例」给出的那一支，
    # 常量 c5 = 2π/4.5、相位 11.125）。
    #
    # ⚠ 已知实现分歧：phira 的 ``prpr/src/core/tween.rs`` 用的是通用 ``f3!`` 宏
    #   （「把 In 函数压缩到两半」f_in(2t)/2），PhiStudio ``Easing.java`` 的
    #   ``case 29`` 同样走 ``inElastic(2t)/2``。这两支在 t=0.1 处相差约 0.168，
    #   但都满足端点、中点与点对称——即二者只在「中段形状」上不同。
    #   本库选择 easings.net 解析式，理由是它才是 RPE 文档直接给出的公式；
    #   若日后确有实机证据表明 RPE 走的是 f3! 构造，只需替换本项实现
    #   （tests/test_easing.py::in_out_back_uses_easings_net_constant 用的是
    #   同一套「锁死选择」做法，可照此再加一条 #29 的锁定测试）。
    #
    # 另注：phira-docs 的那段 Python 片段在内层写了 ``1 if t == 0``（应为 ``t == 1``），
    # 直接用会在 t=1 处得到 0.99991 而不是 1，这里已修正。
    lambda t: 0
    if t == 0
    else (
        1
        if t == 1
        else (
            (-(2 ** (20 * t - 10)) * math.sin((20 * t - 11.125) * ((2 * math.pi) / 4.5))) / 2
            if t < 0.5
            else 1
            - (-(2 ** (20 * (1 - t) - 10)) * math.sin((20 * (1 - t) - 11.125) * ((2 * math.pi) / 4.5)))
            / 2
        )
    ),  # in out elastic
]

MAX_EASING = len(EASE_FUNCS)  # 29

#: 中文/英文名 -> 1 基 id
EASE_NAMES: list[str] = [
    "linear",
    "out_sine",
    "in_sine",
    "out_quad",
    "in_quad",
    "in_out_sine",
    "in_out_quad",
    "out_cubic",
    "in_cubic",
    "out_quart",
    "in_quart",
    "in_out_cubic",
    "in_out_quart",
    "out_quint",
    "in_quint",
    "out_expo",
    "in_expo",
    "out_circ",
    "in_circ",
    "out_back",
    "in_back",
    "in_out_circ",
    "in_out_back",
    "out_elastic",
    "in_elastic",
    "out_bounce",
    "in_bounce",
    "in_out_bounce",
    "in_out_elastic",
]

EASE_NAME_TO_ID: dict[str, int] = {name: i + 1 for i, name in enumerate(EASE_NAMES)}

#: 允许的中文别名（写谱时更顺手）
_CN_ALIASES: dict[str, int] = {
    "线性": 1,
    "匀速": 1,
    "正弦出": 2,
    "正弦入": 3,
    "二次出": 4,
    "二次入": 5,
    "正弦出入": 6,
    "二次出入": 7,
    "三次出": 8,
    "三次入": 9,
    "四次出": 10,
    "四次入": 11,
    "三次出入": 12,
    "四次出入": 13,
    "五次出": 14,
    "五次入": 15,
    "指数出": 16,
    "指数入": 17,
    "圆形出": 18,
    "圆形入": 19,
    "回弹出": 20,
    "回弹入": 21,
    "圆形出入": 22,
    "回弹出入": 23,
    "弹性出": 24,
    "弹性入": 25,
    "弹跳出": 26,
    "弹跳入": 27,
    "弹跳出入": 28,
    "弹性出入": 29,
}


def clamp_easing(easing_type: _t.Any) -> int:
    """把任意输入规范化成 1~29 的合法缓动 id。

    越界与非法值**一律回退到 1（linear）**，与两个参考渲染器一致：

    * phira ``prpr/src/parse.rs``：``RPE_TWEEN_MAP`` 是长度 30 的表
      （索引 0 与 1 都是 linear），取值为
      ``RPE_TWEEN_MAP.get(t.max(1) as usize).copied().unwrap_or(RPE_TWEEN_MAP[0])``
      —— 即 ``easingType = 999`` 实际按 linear 渲染。
    * PhiStudio ``model/Easing.java``：``MIN_TYPE = 1, MAX_TYPE = 29``，未知标识回退 Linear。

    注意这里**不是**夹到最近的边界：把 999 夹成 29 会让本地模拟与真实渲染器
    产生不同的动画曲线。
    """
    if isinstance(easing_type, str):
        key = easing_type.strip().lower().replace(" ", "_").replace("-", "_")
        if key in EASE_NAME_TO_ID:
            return EASE_NAME_TO_ID[key]
        if easing_type.strip() in _CN_ALIASES:
            return _CN_ALIASES[easing_type.strip()]
        if key.isdigit():
            return clamp_easing(int(key))
        return 1
    try:
        value = int(easing_type)
    except (TypeError, ValueError):
        return 1
    if value < 1 or value > MAX_EASING:
        return 1
    return value


#: 语义化别名：``clamp_easing`` 做的是「规范化 + 渲染器同款回退」，不是数值夹取。
normalize_easing = clamp_easing


def ease(easing_type: _t.Any, t: float) -> float:
    """对归一化进度 ``t`` 施加缓动，返回归一化结果。"""
    return EASE_FUNCS[clamp_easing(easing_type) - 1](t)


def easing_name(easing_type: _t.Any) -> str:
    return EASE_NAMES[clamp_easing(easing_type) - 1]


def easing_id(name: str) -> int:
    return clamp_easing(name)


def bezier(t: float, p: tuple[float, float, float, float]) -> float:
    """RPE 的贝塞尔缓动（``bezier == 1`` 时生效）。

    ``p`` 是 ``bezierPoints``，四次控制点中的 (x1, y1, x2, y2)，
    以 x(t) 求解参数再取 y —— 与 CSS ``cubic-bezier`` 同构。
    """
    x1, y1, x2, y2 = p

    def _curve(u: float, a: float, b: float) -> float:
        return 3 * a * u * (1 - u) ** 2 + 3 * b * u * u * (1 - u) + u**3

    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if _curve(mid, x1, x2) < t:
            lo = mid
        else:
            hi = mid
    return _curve((lo + hi) / 2, y1, y2)
