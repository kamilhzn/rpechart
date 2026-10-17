"""段落模型 —— 把「一首曲子」切成若干编排段。

在 原生成脚本里，段落是一个写死的七元组表：:

    SECTIONS = [(0,16,"序章·能量球",0.5,"dark"), (16,64,"铺陈·对峙",1.0,"blue"), ...]

四件事被塞进一个元组：**起拍、止拍、中文名、密度、演出色**。而且下游
（`PATTERNS` / `SWAY_GATE` / `SECTION_SPEED` / `DENSE_EVERY`）全靠
**中文名当键**去查表 —— 换一首曲子，这些表必须整体重写，且一旦某个名字
拼错就静默回落到默认值。

泛化后的 :class:`Section` 把「身份」和「编排」分开：

* `role` 是**枚举**（:data:`ROLE_B` / :data:`ROLE_R` / :data:`ROLE_RB` /
  :data:`ROLE_N`，见 :mod:`rpechart.gen.features`），不是中文名；
* `tone` 是演出色调（`dark`/`blue`/`red`/`neutral`）；
* `density` 是密度系数；
* 节奏型（`normal`/`dense`/`stride`/`dense_every`）直接挂在段上，
  下游不再需要「按名字查表」。

两种建段模式
------------
1. **手动**：调用方直接给 `[Section(...), ...]`（等价于原来的硬编码表）；
2. **自动**：:func:`auto_sections` 从 :class:`BeatFeatures` 的角色序列与
   低频能量里切段。算法完全确定性（无 random），且保证**首段从 0 拍起、
   相邻段无缝、末段盖到最后一拍** —— 这正是 原生成脚本的守门测试
   `anime_sections_tile_the_whole_song_without_gaps` 在防的「后面的谱压根没有」。
"""

from __future__ import annotations

import typing as _t
from dataclasses import dataclass, field, replace

from .features import ROLE_B, ROLE_N, ROLE_R, ROLE_RB, ROLES, BeatFeatures

__all__ = [
    "Section",
    "TONE_BY_ROLE",
    "DEFAULT_DENSITY",
    "auto_sections",
    "section_at",
    "sections_tile",
    "normalize_sections",
]


#: 角色 → 默认演出色调。换曲子可以在 spec 里逐段覆盖 `tone`。
TONE_BY_ROLE = {
    ROLE_B: "blue",
    ROLE_R: "red",
    ROLE_RB: "blue",
    ROLE_N: "neutral",
}

#: 自动切段时，密度系数映射到的区间。
DEFAULT_DENSITY = (0.5, 3.0)


@dataclass
class Section:
    """一个编排段。

    时间单位是**拍**（与 RPE 一致，不是秒）。`[start, end)` 半开区间，
    所以相邻段可以严格首尾相接而不重叠。

    节奏字段说明（它们原本是 原生成脚本里按中文名查的四张表）：

    * `normal`：常规节奏型名（键进 :data:`rpechart.gen.notes.OFFSETS`）；
    * `dense`：重拍上替换用的密集型；
    * `dense_every`：每隔几拍才允许出现一次密集型（越大越稀疏）；
    * `stride`：常规型每隔几拍落实一次（`2` 就是「每 2 拍 1 个音符」）。
    """

    start: float
    end: float
    role: str = ROLE_N
    density: float = 1.0
    tone: str = ""
    name: str = ""
    normal: str = "basic"
    dense: str = "half"
    dense_every: int = 2
    stride: int = 1
    #: 该段是否生成 Hold（长按）。默认不生成，由 spec 逐段打开。
    holds: _t.Optional[list] = None
    #: 预留的自由参数袋：风格插件可以往里塞任意键，序列化时原样保留。
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.start = float(self.start)
        self.end = float(self.end)
        if self.end < self.start:
            raise ValueError(f"Section.end ({self.end}) < start ({self.start})")
        if self.role not in ROLES:
            raise ValueError(f"未知角色 {self.role!r}，可选 {ROLES}")
        if not self.tone:
            self.tone = TONE_BY_ROLE[self.role]
        self.density = float(self.density)
        self.dense_every = max(1, int(self.dense_every))
        self.stride = max(1, int(self.stride))

    # ------------------------------------------------------------ 查询
    @property
    def length(self) -> float:
        """段长（拍）。"""
        return self.end - self.start

    def contains(self, beat: float) -> bool:
        """`beat` 是否落在 `[start, end)` 内。"""
        return self.start <= float(beat) < self.end

    def __len__(self) -> int:
        return int(round(self.length))

    def __repr__(self) -> str:
        label = self.name or self.role
        return (f"Section({self.start:g}..{self.end:g} {label} "
                f"role={self.role} density={self.density:g} tone={self.tone})")

    # ------------------------------------------------------------ 序列化
    def to_json(self) -> dict:
        d = {
            "start": self.start,
            "end": self.end,
            "role": self.role,
            "density": self.density,
            "tone": self.tone,
            "normal": self.normal,
            "dense": self.dense,
            "dense_every": self.dense_every,
            "stride": self.stride,
        }
        if self.name:
            d["name"] = self.name
        if self.holds:
            d["holds"] = self.holds
        if self.extra:
            d["extra"] = self.extra
        return d

    @classmethod
    def from_json(cls, data: _t.Mapping[str, _t.Any]) -> "Section":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------- 段表操作
def normalize_sections(sections: _t.Sequence[Section]) -> list[Section]:
    """排序 + 去重叠 + 补缝。

    这是「段落必须铺满整首歌」的兜底：自动切段偶尔会因为浮点舍入留下
    1e-9 的缝，RPE 里看不出来，但守门测试会挂。所以统一按起点排序，
    把前一段的 `end` 拉到后一段的 `start`。
    """
    out = [replace(s) for s in sorted(sections, key=lambda s: (s.start, s.end))]
    for a, b in zip(out, out[1:]):
        if b.start != a.end:
            # 真重叠（b 更早）截断前一段；真空隙（b 更晚）填平。
            # 空隙不再静默：ChartSpec.problems() 会在**原始**段表上
            # 用 sections_tile 报出来。
            a.end = b.start
    return [s for s in out if s.end > s.start or s.length == 0]


def section_at(sections: _t.Sequence[Section], beat: float) -> _t.Optional[Section]:
    """`beat` 落在哪一段。落在最后一段之后时返回最后一段（末端兜底）。"""
    if not sections:
        return None
    for s in sections:
        if s.contains(beat):
            return s
    return sections[-1] if beat >= sections[-1].end else sections[0]


def sections_tile(sections: _t.Sequence[Section], last_beat: float,
                  tol: float = 1e-6) -> bool:
    """段表是否无缝铺满 `[0, last_beat]`。

    等价于 原生成脚本的守门测试
    `anime_sections_tile_the_whole_song_without_gaps` 的四条断言：
    非空、首段从 0 起、相邻段 `b1 == a2`、末段末尾 >= `last_beat`。
    """
    if not sections:
        return False
    if abs(sections[0].start - 0.0) > tol:
        return False
    for a, b in zip(sections, sections[1:]):
        if abs(a.end - b.start) > tol:
            return False
    return sections[-1].end >= last_beat - tol


# ---------------------------------------------------------------- 自动切段
def _activity(f: BeatFeatures) -> list[float]:
    """逐拍「能量」：低频起音 + 画面运动。切段只用它，不用角色（角色会抖）。"""
    n = f.n
    if not n:
        return []
    low = f.low or [0.0] * n
    motion = f.motion or [0.0] * n
    raw = [float(low[i] or 0.0) + 0.01 * float(motion[i] or 0.0) for i in range(n)]
    # 归一化到 0..1（全平时取 0.5），保证换曲子时密度映射稳定
    lo, hi = min(raw), max(raw)
    if hi - lo < 1e-9:
        return [0.5] * n
    return [(v - lo) / (hi - lo) for v in raw]


def auto_sections(
    f: BeatFeatures,
    *,
    last_beat: _t.Optional[float] = None,
    min_len: int = 8,
    target_len: int = 32,
    density_range: _t.Tuple[float, float] = DEFAULT_DENSITY,
    pattern: _t.Optional[_t.Mapping[str, _t.Tuple[str, str, int, int]]] = None,
    tones: _t.Optional[_t.Mapping[str, str]] = None,
) -> list[Section]:
    """从特征自动切段（确定性）。

    算法（三步，全部可复现）：

    1. **按角色分块**：逐拍取 :meth:`BeatFeatures.role`，角色变化就开新块；
    2. **合并/拆分**：短于 `min_len` 的块并入前一块；长于 `2*target_len` 的块
       在能量局部极小处切成两半（找不到极小就按 `target_len` 等分）；
    3. **赋属性**：角色取块内众数，密度取块内平均能量线性映射到
       `density_range`，色调取 :data:`TONE_BY_ROLE`。

    `pattern` 是 `{角色: (normal, dense, dense_every, stride)}`，默认见
    :func:`_default_pattern`；`tones` 覆盖角色 → 色调。
    """
    n = f.n
    if n == 0:
        return []
    end_limit = float(last_beat if last_beat is not None else n)
    end_limit = min(end_limit, float(n)) if end_limit > 0 else float(n)

    roles = [f.role(i) for i in range(n)]
    act = _activity(f)
    pattern = dict(pattern or {})
    tones = dict(tones or {})

    # ---- 1. 按角色分块
    blocks: list[list[int]] = [[0, 1]]  # [start, end) 拍
    for i in range(1, n):
        if roles[i] != roles[blocks[-1][0]]:
            blocks.append([i, i + 1])
        else:
            blocks[-1][1] = i + 1

    # ---- 2a. 短块并入前一块（首块并入后一块）
    merged: list[list[int]] = []
    for blk in blocks:
        if merged and (blk[1] - blk[0]) < min_len:
            merged[-1][1] = blk[1]
        else:
            merged.append(list(blk))
    if len(merged) >= 2 and (merged[0][1] - merged[0][0]) < min_len:
        merged[1][0] = merged[0][0]
        merged.pop(0)

    # ---- 2b. 过长的块再切（在能量极小处）
    split: list[list[int]] = []
    for a, b in merged:
        while b - a > 2 * target_len:
            lo = a + target_len // 2
            hi = min(b - target_len // 2, a + 2 * target_len)
            if hi <= lo:
                cut = a + target_len
            else:
                cut = min(range(lo, hi), key=lambda i: (act[i], i))
            if cut <= a or cut >= b:
                break
            split.append([a, cut])
            a = cut
        split.append([a, b])

    # ---- 3. 赋属性
    out: list[Section] = []
    for a, b in split:
        if a >= end_limit:
            break
        b = min(b, int(end_limit)) if end_limit == int(end_limit) else min(b, int(end_limit) + 1)
        if b <= a:
            continue
        seg_roles = roles[a:b]
        role = max(ROLES, key=lambda r: (seg_roles.count(r), -ROLES.index(r)))
        mean_act = sum(act[a:b]) / max(1, len(seg_roles))
        d0, d1 = density_range
        normal, dense, every, stride = pattern.get(role, _default_pattern(role))
        out.append(Section(
            start=float(a),
            end=float(b),
            role=role,
            density=round(d0 + (d1 - d0) * mean_act, 3),
            tone=tones.get(role, TONE_BY_ROLE[role]),
            name="",
            normal=normal,
            dense=dense,
            dense_every=every,
            stride=stride,
        ))
    out = normalize_sections(out)
    # 自动切段必须铺满：把末段拉到 end_limit
    if out and out[-1].end < end_limit:
        out[-1].end = end_limit
    return out


def _default_pattern(role: str) -> _t.Tuple[str, str, int, int]:
    """角色 → (常规型, 密集型, 每隔几拍, 落实间隔)。

    这是 原生成脚本里 `PATTERNS` / `DENSE_EVERY` / `STRIDE`
    三张表的「角色化」版本：高潮段的角色（`B`/`R`）用十六分密集，中性段降一档。
    """
    if role in (ROLE_B, ROLE_R):
        return ("half", "dense", 2, 1)
    if role == ROLE_RB:
        return ("half", "gallop", 3, 1)
    return ("basic", "half", 4, 1)
