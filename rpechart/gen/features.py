"""逐拍特征 —— 「参数 → 谱面」生成层的输入数据。

本模块把 ``beats.json``（逐拍特征表，由音频/视频分析产出；本仓库不附分析器）
读成一组 **逐拍数组**，并提供平滑 / 前向填充 / 限幅三种清洗，以及
:meth:`BeatFeatures.role` 这个「画面主体是谁」的判定。

为什么要有这一层
----------------
要把「按某一首歌写死的脚本」变成库，第一件事就是把「跟着音乐走的数据」与
「这一首歌的编排」拆开。本模块只负责前者，且**不含任何曲子相关的常量**。

特征字段（与 `beats.json` 的 `beats[i]` 一一对应）
------------------------------------------------------
===========  ====================================================
`t`        该拍在音频内的秒数（`phase + beat * period`）
`low`      低频 30–150Hz 起音强度（底鼓）—— 用来找重拍
`mid`      中频 150–1200Hz 起音强度
`high`     高频 4k–10kHz 起音强度
`motion`   画面运动量（相邻帧亮度差）
`warmth`   暖度 `mean(r - b)`：> 0 偏红、< 0 偏蓝
`rn`       红色能量团像素数
`bn`       蓝色能量团像素数
`rx`/`bx` 红 / 蓝能量团的质心 x（映射到 RPE 的 ±675 视界；无目标时为 `None`）
`cx`       整幅画面的亮度重心 x
===========  ====================================================

清洗的三道工序（顺序有意义）
----------------------------
1. **平滑**（:func:`smooth`，滑动平均）：单拍的像素抖动不该变成音符落点的抖动；
2. **前向填充**（:func:`fill_none`）：`rx`/`bx` 在画面里没有红/蓝团时是 `None`，
   直接当 0 处理会让音符瞬移到屏幕中央，所以用「上一次已知值」顶住；
3. **限幅**（:func:`limit_slew`）：即使平滑过，镜头切换仍会让质心一拍跳几百单位，
   限幅把相邻拍的跳变压到 `max_step` 以内。

这三道工序是 原生成脚本里 `Feats.__init__` 的原样泛化，只是
窗口大小与限幅量都改成了参数（原脚本写死 3 / 3 / 5 与 90 / 90 / 60）。
"""

from __future__ import annotations

import json
import typing as _t
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
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
]


# ---------------------------------------------------------------- 角色枚举
#: 蓝主导（原生成脚本里映射到「蓝键 Tap」）
ROLE_B = "B"
#: 红主导（映射到「红键 Flick」）
ROLE_R = "R"
#: 红蓝同场（两线交错）
ROLE_RB = "RB"
#: 中性：两个角色都不是主体。**不能整段跳过**，否则转场镜头会变成整段空白
#: （这是一次真实事故：见 原生成脚本的 `plan_notes` 注释）。
ROLE_N = "N"
ROLES = (ROLE_B, ROLE_R, ROLE_RB, ROLE_N)


# ---------------------------------------------------------------- 清洗原语
def smooth(values: _t.Sequence[float], window: int) -> list[float]:
    """居中滑动平均。`window <= 1` 时原样返回（拷贝）。

    与 原生成脚本的 `_smooth` 逐字等价：窗口按 `half = window // 2`
    向两侧各取 half 个邻居，边界处自动收窄，因此**不引入相位偏移**。
    """
    vals = list(values)
    if window <= 1:
        return vals
    half = window // 2
    out: list[float] = []
    for i in range(len(vals)):
        a = max(0, i - half)
        b = min(len(vals), i + half + 1)
        out.append(sum(vals[a:b]) / (b - a))
    return out


def fill_none(values: _t.Sequence[_t.Optional[float]], default: float = 0.0) -> list[float]:
    """前向填充 `None`：用上一次已知值顶住，开头没有已知值时用 `default`。"""
    out: list[float] = []
    last = float(default)
    for v in values:
        if v is None:
            out.append(last)
        else:
            last = float(v)
            out.append(last)
    return out


def limit_slew(values: _t.Sequence[float], max_step: float) -> list[float]:
    """限制相邻值的跳变幅度（前向传播）。

    `max_step <= 0` 表示不限幅，原样返回。这不是「钳到区间」，而是**逐拍
    跟着前一个值走**：镜头从左边切到右边时，质心不会一拍跳 600 单位，而是
    以每拍 `max_step` 的速度爬过去 —— 观感上就是「键不乱飞」。
    """
    out = [float(v) for v in values]
    if max_step <= 0:
        return out
    for i in range(1, len(out)):
        d = out[i] - out[i - 1]
        if d > max_step:
            out[i] = out[i - 1] + max_step
        elif d < -max_step:
            out[i] = out[i - 1] - max_step
    return out


# ---------------------------------------------------------------- 角色阈值
@dataclass
class RoleThresholds:
    """「画面主体是谁」的判定阈值。

    默认值就是 原生成脚本里的那一组（3000 / 1200 / 2.0 / 0.55 / -4.0 /
    2400），由真实曲子的 `beats.json` 分布反推（红团中位 8、蓝团中位 5577 ——
    红是间歇性爆发而蓝是底色）。换曲子时这些阈值**应当**重新标定，所以它们是
    参数而不是常量。

    判定顺序（与 `Feats.role` 完全一致，先命中先返回）：

    1. `rn > both_px 且 bn > both_px` → `RB`（同场）
    2. `rn > strong_px 且 warmth > warm_hi 且 rn > bn * red_share` → `R`
    3. `bn > strong_px 且 warmth < warm_lo` → `B`
    4. `bn > solo_px` → `B`
    5. `rn > solo_px` → `R`
    6. 否则 → `N`
    """

    both_px: float = 3000.0
    strong_px: float = 1200.0
    solo_px: float = 2400.0
    warm_hi: float = 2.0
    warm_lo: float = -4.0
    red_share: float = 0.55

    def to_json(self) -> dict:
        return {
            "both_px": self.both_px,
            "strong_px": self.strong_px,
            "solo_px": self.solo_px,
            "warm_hi": self.warm_hi,
            "warm_lo": self.warm_lo,
            "red_share": self.red_share,
        }

    @classmethod
    def from_json(cls, data: _t.Optional[_t.Mapping[str, _t.Any]]) -> "RoleThresholds":
        if not data:
            return cls()
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------- 主对象
@dataclass
class BeatFeatures:
    """一张谱面的逐拍特征。

    构造后立刻完成清洗，因此 `rx`/`bx`/`cx` 都是「有限浮点数组」，
    :meth:`role` 可以直接用。数组长度都等于 :attr:`n`。
    """

    bpm: float = 120.0
    phase: float = 0.0
    duration: float = 0.0
    #: 每拍在音频内的秒数；缺省时按 `phase + i * 60 / bpm` 推算
    sec: list[float] = field(default_factory=list)
    low: list[float] = field(default_factory=list)
    mid: list[float] = field(default_factory=list)
    high: list[float] = field(default_factory=list)
    full: list[float] = field(default_factory=list)
    motion: list[float] = field(default_factory=list)
    warmth: list[float] = field(default_factory=list)
    rn: list[float] = field(default_factory=list)
    bn: list[float] = field(default_factory=list)
    rx: list[float] = field(default_factory=list)
    bx: list[float] = field(default_factory=list)
    cx: list[float] = field(default_factory=list)
    thresholds: RoleThresholds = field(default_factory=RoleThresholds)
    #: 平滑窗口（拍）。`0` 表示不平滑。
    smooth_window: int = 3
    #: 角色质心的限幅（RPE x 单位 / 拍）。`0` 表示不限幅。
    slew_limit: float = 90.0
    #: 画面亮度重心的限幅；它比角色质心稳，所以默认更小。
    center_slew_limit: float = 60.0

    def __post_init__(self) -> None:
        self._normalize_lengths()
        # ---- 1. 平滑：low/motion/warmth 共用同一个窗口（原脚本是 3/3/5，
        #         这里统一成 smooth_window 以免参数爆炸；要区分就分别构造对象）
        self.low = smooth(self.low, self.smooth_window)
        self.motion = smooth(self.motion, self.smooth_window)
        self.warmth = smooth(self.warmth, self.smooth_window)
        # ---- 2. 前向填充 + 3. 限幅：质心
        self.rx = limit_slew(fill_none(self.rx), self.slew_limit)
        self.bx = limit_slew(fill_none(self.bx), self.slew_limit)
        self.cx = limit_slew(fill_none(self.cx), self.center_slew_limit)
        # 其余通道统一转 float，避免后续出现 int/float 混算的意外
        for name in ("mid", "high", "full", "rn", "bn"):
            setattr(self, name, [float(v or 0.0) for v in getattr(self, name)])
        self.sec = [float(v) for v in self.sec]

    #: 参与对齐的逐拍通道名。手写 :class:`BeatFeatures` 时不必给全 ——
    #: 缺失的通道会在 :meth:`_normalize_lengths` 里补 0.0 到共同长度，
    #: 这样测试可以直接 `BeatFeatures(low=[...], rn=[...], bn=[...])`。
    CHANNELS = ("sec", "low", "mid", "high", "full", "motion", "warmth", "rn", "bn", "rx", "bx", "cx")

    def _normalize_lengths(self) -> None:
        """把各通道对齐到「最长通道」的长度，缺的补 0.0。

        真实 `beats.json` 里所有数组等长，这一步是空操作；但手写特征（测试、
        或只有 `low` 的极简 spec）必须能跑，所以统一补齐。
        `rx`/`bx`/`cx` 是 `None` 敏感通道，补齐用 `None` 而不是 0.0 —— 交给
        :func:`fill_none` 用前一个已知值顶住，而不是凭空插一个「屏幕中央」。
        """
        n = max([len(getattr(self, k)) for k in self.CHANNELS] or [0])
        for k in self.CHANNELS:
            arr = list(getattr(self, k))
            if len(arr) >= n:
                setattr(self, k, arr)
                continue
            pad = None if k in ("rx", "bx", "cx") else 0.0
            setattr(self, k, arr + [pad] * (n - len(arr)))

    # ------------------------------------------------------------ 尺寸
    @property
    def n(self) -> int:
        """拍数（数组长度）。"""
        return len(self.low)

    def __len__(self) -> int:
        return self.n

    # ------------------------------------------------------------ 角色
    def role(self, i: int) -> str:
        """第 `i` 拍的画面主体。越界返回 :data:`ROLE_N`。

        阈值与判定顺序见 :class:`RoleThresholds`；这是 `Feats.role` 的泛化，
        唯一的差别是像素阈值不再写死。
        """
        if i < 0 or i >= self.n:
            return ROLE_N
        r, b = self.rn[i], self.bn[i]
        th = self.thresholds
        if r > th.both_px and b > th.both_px:
            return ROLE_RB
        if r > th.strong_px and self.warmth[i] > th.warm_hi and r > b * th.red_share:
            return ROLE_R
        if b > th.strong_px and self.warmth[i] < th.warm_lo:
            return ROLE_B
        if b > th.solo_px:
            return ROLE_B
        if r > th.solo_px:
            return ROLE_R
        return ROLE_N

    def roles(self) -> list[str]:
        """全部拍的角色序列（调试 / 测试用）。"""
        return [self.role(i) for i in range(self.n)]

    # ------------------------------------------------------------ 单拍取样
    def sample(self, i: int) -> "BeatFeatures":
        """取第 `i` 拍的单拍特征（便于按拍断言）。越界会被钳到边界。"""
        j = max(0, min(self.n - 1, int(i))) if self.n else 0

        def pick(arr: list) -> list:
            return [arr[j]] if arr else []

        return BeatFeatures(
            bpm=self.bpm,
            phase=self.phase,
            duration=self.duration,
            sec=pick(self.sec),
            low=pick(self.low),
            mid=pick(self.mid),
            high=pick(self.high),
            full=pick(self.full),
            motion=pick(self.motion),
            warmth=pick(self.warmth),
            rn=pick(self.rn),
            bn=pick(self.bn),
            rx=pick(self.rx),
            bx=pick(self.bx),
            cx=pick(self.cx),
            thresholds=self.thresholds,
            smooth_window=0,
            slew_limit=0.0,
            center_slew_limit=0.0,
        )

    # ------------------------------------------------------------ 序列化
    def to_json(self) -> dict:
        """导出成与 `beats.json` 兼容的结构（可再被 :func:`load_beats` 读回）。"""
        beats = []
        for i in range(self.n):
            beats.append({
                "beat": i,
                "t": round(self.sec[i], 4) if i < len(self.sec) else round(
                    self.phase + i * (60.0 / self.bpm if self.bpm else 0.0), 4),
                "low": round(self.low[i], 3),
                "mid": round(self.mid[i], 3),
                "high": round(self.high[i], 3),
                "full": round(self.full[i], 3),
                "motion": round(self.motion[i], 2),
                "warmth": round(self.warmth[i], 1),
                "rn": int(self.rn[i]),
                "bn": int(self.bn[i]),
                "rx": round(self.rx[i], 1),
                "bx": round(self.bx[i], 1),
                "cx": round(self.cx[i], 1),
            })
        return {
            "bpm": self.bpm,
            "phase": self.phase,
            "period": (60.0 / self.bpm) if self.bpm else 0.0,
            "duration": self.duration,
            "role_thresholds": self.thresholds.to_json(),
            "beats": beats,
        }

    @classmethod
    def from_json(cls, data: _t.Mapping[str, _t.Any]) -> "BeatFeatures":
        """从 `beats.json` 结构构造（也接受已经载入的 `dict`）。"""
        beats = data.get("beats") or []

        def col(k: str) -> list:
            return [b.get(k) for b in beats]

        def num(k: str) -> list:
            return [float(b.get(k) or 0.0) for b in beats]

        return cls(
            bpm=float(data.get("bpm") or 120.0),
            phase=float(data.get("phase") or 0.0),
            duration=float(data.get("duration") or 0.0),
            sec=[float(b.get("t") or 0.0) for b in beats],
            low=num("low"),
            mid=num("mid"),
            high=num("high"),
            full=num("full"),
            motion=num("motion"),
            warmth=num("warmth"),
            rn=num("rn"),
            bn=num("bn"),
            rx=col("rx"),
            bx=col("bx"),
            cx=col("cx"),
            thresholds=RoleThresholds.from_json(data.get("role_thresholds")),
        )


# ---------------------------------------------------------------- 文件 IO
def load_beats(path: _t.Union[str, Path]) -> BeatFeatures:
    """读 `beats.json` → :class:`BeatFeatures`。"""
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8-sig"))
    return BeatFeatures.from_json(raw)


def save_beats(features: BeatFeatures, path: _t.Union[str, Path]) -> Path:
    """把 :class:`BeatFeatures` 写回 `beats.json` 结构（UTF-8，不转义中文）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(features.to_json(), ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return p
