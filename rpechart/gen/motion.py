"""判定线运动 —— 位置 / 旋转摆动 + 线速爬升。

这是 原生成脚本里 `build_line_motion` 与 `_sway` 与
`_sway_waypoint` 与 `_gate_keyframes` 与 `_lerp_table` 以及线速 ramp
那一段的泛化。它是整个生成层**踩坑最多**的一块，所以每条约束都在这里再记一遍。

半正弦 = 每半个周期一个 `in_out_sine` 事件
-------------------------------------------
三种移动事件（`moveX`、`moveY`、`rotate`）都用「每半个周期写一个
`in_out_sine` 事件」的方式逼近正弦波：数学上 `in_out_sine` 事件的正是一个
精确的半正弦（端点导数为 0，中点取极值），所以曲线是**解析正确**的，而不是
折线采样。

**不要**把正弦采样成几十段线性事件 —— 那会触发校验器的
`TooManyLinearEvent` 警告（它的分母只算非常量事件 `start != end`，而折线
采样出来的每一段都非常量）。周期取 17~51 拍时，每轴只有 10~28 个事件，
与真实谱面（50~2000 个移动事件）量级相符。

`_sway_waypoint` 必须用 `cos`
-------------------------------
端点间隔正好是半个周期。若用 `sin` 采样 `sin(πk)` 会**永远落在零点**上 ——
`phase=0` 的轴会整条恒等于 0，判定线就「不动」了（这正是修 bug 时踩到的第二个
坑）。用 `cos` 采样则是 `cos(πk) = ±1` 交替，端点正好是波峰 / 波谷，
半正弦事件才真的动起来。所以本模块的 :func:`sway_waypoint` **只能用 cos**，
且守门测试 `anime_sway_waypoints_do_not_collapse_to_zero` 就是钉这一条的。

判定线运动与音符轨道**解耦**
-----------------------------
摆动只由 :class:`SwaySpec`（振幅 / 周期 / 相位）与段门限决定，**绝不**读音符的
`x`。原脚本早期版本用音符 x 驱动线 x，结果是线跟着键抖，观感是画面在抽搐。

`line_speed` 是 builder 里唯一不过 `_splice` 的方法
---------------------------------------------------
`JudgeLineBuilder.event()` 会自动裁剪重叠并填隙，但 `line_speed()` 是
**直接 append**（见 `rpechart/builders.py` 的注释）。所以线速区间必须由调用方
自证互不重叠 —— 本模块把所有区间集中生成，并在 :func:`assert_disjoint` 里硬
校验：一旦重叠立刻 `ValueError`，而不是等 RPE 报 `EventOverLapped`。
"""

from __future__ import annotations

import math
import typing as _t
from dataclasses import dataclass, field

from .structure import Section

__all__ = [
    "SwaySpec",
    "MotionSpec",
    "sway",
    "sway_waypoint",
    "gate_keyframes",
    "lerp_table",
    "speed_ramp",
    "assert_disjoint",
    "build_line_motion",
    "apply_speed_ramp",
]


# ---------------------------------------------------------------- 摆动
def sway(amp: float, period: float, phase: float, t: float) -> float:
    """单轴摆动：一个正弦。period 与其它轴互质 ⇒ 叠加后不显重复。

    与 原生成脚本的 `_sway` 等价（这是**连续**形式，仅供校验与
    离线采样；写进谱面的是 :func:`sway_waypoint` 的端点）。
    """
    if period <= 0:
        return 0.0
    return float(amp) * math.sin(2.0 * math.pi * (float(t) / float(period) + float(phase)))


def sway_waypoint(amp: float, period: float, phase: float, k: int, gate: float = 1.0) -> float:
    """正弦波第 `k` 个半周期端点上的取值（**必须用 cos**）。

    端点间隔正好是半个周期，若用 `sin` 采样 `sin(πk)` 会永远落在零点上 ——
    `phase=0` 的轴（例如线 0 / 线 2）会整条恒等于 0，线就「不动」了。
    用 `cos` 采样则是 `cos(πk) = ±1` 交替，端点正好是波峰 / 波谷。

    `gate` 是段落门限（`0..1`）：门限为 0 的段落该轴完全不摆。
    """
    if period <= 0:
        return 0.0
    t = k * float(period) / 2.0
    return float(amp) * math.cos(2.0 * math.pi * (t / float(period) + float(phase))) * float(gate)


#: 兼容原脚本的私有名（守门测试用的是 `_sway_waypoint`）。
_sway_waypoint = sway_waypoint
#: 兼容原脚本的 `_sway`。
_sway = sway


# ---------------------------------------------------------------- 门限插值
def _pick(table: _t.Mapping[str, float], sec: Section, default: float) -> float:
    """段 → 值：先按段名匹配，再按角色匹配，都没有就用默认值。"""
    if sec.name and sec.name in table:
        return float(table[sec.name])
    if sec.role in table:
        return float(table[sec.role])
    return float(default)


def gate_keyframes(sections: _t.Sequence[Section], gates: _t.Mapping[str, float],
                   last_beat: float, default: float = 0.6) -> list:
    """把「段落 → 门限」变成「拍 → 门限」的关键帧表。

    直接按乐段取值会让门限在边界上跳变，判定线会在那一刻瞬移。所以做成
    线性插值的关键帧，让摆动幅度连续地涨落。

    `gates` 的键优先按 :attr:`Section.name` 匹配，其次按 `role` —— 这样
    spec 里既能写中文段名（沿用原脚本），也能只按角色给。
    """
    ks: list = []
    for s in sections:
        ks.append((float(s.start), _pick(gates, s, default)))
    if sections:
        ks.append((float(last_beat), _pick(gates, sections[-1], default)))
    if not ks:
        ks = [(0.0, float(default)), (float(last_beat), float(default))]
    return ks


def lerp_table(ks: _t.Sequence, t: float) -> float:
    """在 `[(拍, 值)]` 关键帧上线性插值（端点外取端点值）。"""
    if not ks:
        return 0.0
    if t <= ks[0][0]:
        return float(ks[0][1])
    for (t0, v0), (t1, v1) in zip(ks, ks[1:]):
        if t <= t1:
            if t1 - t0 <= 1e-9:
                return float(v1)
            f = (t - t0) / (t1 - t0)
            return float(v0) + (float(v1) - float(v0)) * f
    return float(ks[-1][1])


# ---------------------------------------------------------------- 线速
def speed_ramp(sections: _t.Sequence[Section], speeds: _t.Mapping[str, float],
               last_beat: float, default: float = 11.0) -> list:
    """段落基准速度 → **互不重叠**的 `[(t0, t1, v0, v1)]` 区间列表。

    取证：真实谱面判定线速度 p50 约 10~13.5（24 份谱的第一条线实测
    8/10/12/12.8/13.2/13.4/13.5）。旧实现用 8→16→18 的硬阶跃，音符下落速度在
    乐段边界突然翻倍，观感是「键抽搐」。改成锚点之间线性爬升，全程锁在真实
    区间内。

    `speeds` 的键同 :func:`gate_keyframes`（先段名后角色）。返回的区间按拍
    严格递增且首尾相接，绝不会重叠 —— 这是 `line_speed` 不做 `_splice` 的
    前提。
    """
    ks: list = []
    for s in sections:
        ks.append((float(s.start), _pick(speeds, s, default)))
    if sections:
        ks.append((float(last_beat), _pick(speeds, sections[-1], default)))
    if not ks:
        return []
    ramp: list = []
    for (t0, v0), (t1, v1) in zip(ks, ks[1:]):
        if t1 > t0:
            ramp.append((float(t0), float(t1), float(v0), float(v1)))
    return ramp


def assert_disjoint(intervals: _t.Sequence, tol: float = 1e-9) -> None:
    """校验 `[(t0, t1, ...)]` 互不重叠。

    `line_speed` 不过 `_splice`，重叠会直接变成 RPE 的 `EventOverLapped`。
    本函数把这条约束从「调用方自律」变成「生成层硬保证」：一旦重叠就抛
    `ValueError`，并且报出具体是哪两段。
    """
    ivs = sorted((float(iv[0]), float(iv[1])) for iv in intervals)
    for (a0, a1), (b0, b1) in zip(ivs, ivs[1:]):
        if b0 < a1 - tol:
            raise ValueError(f"line_speed 区间重叠: [{a0:g},{a1:g}) 与 [{b0:g},{b1:g})")


# ---------------------------------------------------------------- 运动规格
@dataclass
class SwaySpec:
    """一条判定线的摆动规格（对应 `LINE_SWAY` 的一个八元组）。

    原来的八元组 `(x振幅,x周期,x相位, y振幅,y周期,y相位, rot振幅,rot周期)`
    在这里有了字段名，且旋转相位不再写死 `phx + 0.17`。
    """

    x_amp: float = 0.0
    x_period: float = 0.0
    x_phase: float = 0.0
    y_amp: float = 0.0
    y_period: float = 0.0
    y_phase: float = 0.0
    rot_amp: float = 0.0
    rot_period: float = 0.0
    rot_phase: float = 0.0
    #: 未显式给 `rot_phase` 时的相位偏移（原脚本写死 0.17）。
    rot_phase_offset: float = 0.17

    def __post_init__(self) -> None:
        for name in ("x_amp", "x_period", "x_phase", "y_amp", "y_period", "y_phase",
                     "rot_amp", "rot_period", "rot_phase", "rot_phase_offset"):
            setattr(self, name, float(getattr(self, name)))

    @property
    def phase_r(self) -> float:
        """旋转轴实际使用的相位（显式值为 0 时回落到 `x_phase + offset`）。"""
        if self.rot_phase:
            return self.rot_phase
        return self.x_phase + self.rot_phase_offset

    def to_json(self) -> dict:
        return {
            "x_amp": self.x_amp, "x_period": self.x_period, "x_phase": self.x_phase,
            "y_amp": self.y_amp, "y_period": self.y_period, "y_phase": self.y_phase,
            "rot_amp": self.rot_amp, "rot_period": self.rot_period,
            "rot_phase": self.rot_phase, "rot_phase_offset": self.rot_phase_offset,
        }

    @classmethod
    def from_json(cls, data: _t.Mapping[str, _t.Any]) -> "SwaySpec":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class MotionSpec:
    """整个「判定线运动」子系统的参数包。

    * `sways`：线索引 → :class:`SwaySpec`；
    * `gates`：段名 / 角色 → 摆动门限（原 `SWAY_GATE`）；
    * `speeds`：段名 / 角色 → 基准线速（原 `SECTION_SPEED`）；
    * `y_tracks` 与 `angle_tracks`：线索引 → `{拍: 基准值}`（原 `LINE_Y`
      与 `LINE_ANGLE`）；
    * `speed_boosts`：线索引 → `[(t0, t1, v0, v1)]` 的额外加速段。它们**必须**
      落在 ramp 的某个区间内部，本模块会把那个区间切成三段以保证不重叠。
    """

    sways: dict = field(default_factory=dict)
    gates: dict = field(default_factory=dict)
    speeds: dict = field(default_factory=dict)
    y_tracks: dict = field(default_factory=dict)
    angle_tracks: dict = field(default_factory=dict)
    speed_boosts: dict = field(default_factory=dict)
    default_gate: float = 0.6
    default_speed: float = 11.0
    #: 线速的合法区间（守门测试钉的是 [8, 15]）。生成层会拒绝越界的输入。
    speed_range: tuple = (8.0, 15.0)

    def sway_of(self, idx: int) -> SwaySpec:
        """取线 `idx` 的摆动规格（缺省为「不动」）。"""
        spec = self.sways.get(idx)
        if spec is None:
            return SwaySpec()
        if isinstance(spec, SwaySpec):
            return spec
        return SwaySpec.from_json(spec)


# ---------------------------------------------------------------- 写事件
def build_line_motion(builders: _t.Sequence, motion: MotionSpec, *,
                      sections: _t.Sequence[Section], last_beat: float) -> None:
    """给每条判定线写一段**连续的**位置 / 旋转动画。

    见 :class:`SwaySpec` 上方取证：旧实现 moveX 恒为 0 且只在乐段边界放锚点，
    所以线看上去「没有动作」。这里改成正弦摆动，**每半个周期写一个
    `in_out_sine` 事件**（数学上就是一个精确的半正弦波）。

    每轴一个周期 T ⇒ 事件数 = 2·T_total/T。取 17~51 拍的周期，全程 237 拍，
    于是每轴 10~28 个事件、三条轴合计 30~80 个事件每线 —— 与真实谱面
    （50~2000 个移动事件）量级相符。

    本函数**只读** :class:`MotionSpec` 与段表，不碰任何音符 —— 判定线运动与
    音符轨道是解耦的。
    """
    end_beat = float(last_beat)
    gate_ks = gate_keyframes(sections, motion.gates, end_beat, motion.default_gate)

    for idx, lb in enumerate(builders):
        spec = motion.sway_of(idx)
        y_of = sorted((motion.y_tracks.get(idx) or {0.0: 0.0}).items())
        a_of = sorted((motion.angle_tracks.get(idx) or {0.0: 0.0}).items())

        axes = (
            ("moveXEvents", spec.x_amp, spec.x_period, spec.x_phase, False),
            ("moveYEvents", spec.y_amp, spec.y_period, spec.y_phase, True),
            ("rotateEvents", spec.rot_amp, spec.rot_period, spec.phase_r, True),
        )
        for kind, amp, period, phase, has_base in axes:
            if amp <= 0 or period <= 0:
                continue
            half = period / 2.0
            base = a_of if kind == "rotateEvents" else y_of
            t = 0.0
            k = 0
            while t < end_beat - 1e-9:
                t2 = min(t + half, end_beat)
                g0 = lerp_table(gate_ks, t)
                g1 = lerp_table(gate_ks, t2)
                v0 = sway_waypoint(amp, period, phase, k, g0)
                v1 = sway_waypoint(amp, period, phase, k + 1, g1)
                if has_base:
                    v0 += lerp_table(base, t)
                    v1 += lerp_table(base, t2)
                lb.event(kind, t, t2, round(v0, 3), round(v1, 3), easing="in_out_sine")
                t, k = t2, k + 1


def apply_speed_ramp(builders: _t.Sequence, motion: MotionSpec, *,
                     sections: _t.Sequence[Section], last_beat: float) -> None:
    """给每条判定线写线速区间（**自证互不重叠**）。

    基础区间来自 :func:`speed_ramp`；若某条线在
    :attr:`MotionSpec.speed_boosts` 里声明了加速段，就把包含它的基础区间切成
    `[t0, b0)`、`[b0, b1)`、`[b1, t1)` 三段，中间那段用加速值 —— 这样
    加速段永远嵌在合法区间内，不会和邻居重叠。

    写之前会跑 :func:`assert_disjoint`；越界
    :attr:`MotionSpec.speed_range` 的速度直接 `ValueError`。
    """
    lo, hi = motion.speed_range
    base = speed_ramp(sections, motion.speeds, last_beat, motion.default_speed)

    for idx, lb in enumerate(builders):
        boosts = [tuple(float(v) for v in b)
                  for b in (motion.speed_boosts.get(idx) or [])]
        ivs: list = []
        for t0, t1, v0, v1 in base:
            cuts = sorted((b[0], b[1]) for b in boosts if t0 < b[0] and b[1] <= t1)
            if not cuts:
                ivs.append((t0, t1, v0, v1))
                continue
            cursor = t0
            for b0, b1 in cuts:
                if b0 > cursor:
                    f0 = (cursor - t0) / (t1 - t0)
                    f1 = (b0 - t0) / (t1 - t0)
                    ivs.append((cursor, b0,
                                v0 + (v1 - v0) * f0, v0 + (v1 - v0) * f1))
                boost = next(b for b in boosts if b[0] == b0 and b[1] == b1)
                ivs.append((b0, b1, boost[2], boost[3]))
                cursor = b1
            if cursor < t1:
                f0 = (cursor - t0) / (t1 - t0)
                ivs.append((cursor, t1, v0 + (v1 - v0) * f0, v1))

        assert_disjoint(ivs)
        for t0, t1, v0, v1 in ivs:
            for label, v in (("start", v0), ("end", v1)):
                if v < lo - 1e-9 or v > hi + 1e-9:
                    raise ValueError(
                        f"线 {idx} 的 line_speed {label}={v:g} 越界 "
                        f"[{lo:g}, {hi:g}]（真实谱面实测 8~13.5）")
            lb.line_speed(t0, t1, v0, v1, easing=1)
