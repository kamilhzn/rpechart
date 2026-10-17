"""音符编排 —— 从「特征 + 段表」到「音符列表」。

这是 原生成脚本里 `plan_notes` 的泛化。原实现把三件事焊在一起：

1. **节奏型表**（`OFFSETS`）—— 这个是真的通用，原样搬过来；
2. **角色 → 线/键位**的映射 —— 原来是 `if role == "RB": ... elif role == "R": ...`
   一串写死的分支，线号 0/1/2 直接出现在代码里。泛化成 :class:`RolePlan` 表；
3. **Hold 的落点** —— 原来写死 `t = 194.0; while t < 226.0 ... t += 8.0` 以及
   收束段的 `232` 长按。泛化成 :class:`Section` 上的 `holds` 声明。

两条**不能碰**的语义
--------------------
* **中性段（`role == "N"`）不能整段跳过。** 原注释：*「中性镜头，两个人都不是
  主体：降密度但仍要出音符，否则转场镜头会变成整段空白（曾经的 bug）」*。所以 N 段
  走降密度分支，而不是 `continue`。
* **Hold 与 Tap 叠加而不是替换。** 原注释：*「终局段用 Hold 表现持续对峙。
  **和 Tap 叠加而不是替换** —— 之前用替换写法，把整段 Tap 删光，只剩每 4 拍一个
  Hold，直接毁掉这一段。」* 所以 :meth:`NotePlanner.plan` 先把 Tap/Flick/Drag
  全部铺完，再把 Hold **追加**进列表。

确定性
------
本模块**不引入 random**。需要「抖动」时用 :func:`stable_jitter`：由
`(line, 拍, 角色)` 经 FNV-1a 哈希得到 `[-1, 1]` 的确定性系数，同一输入
永远给同一结果（否则两次生成同一 spec 会得到不同字节，违反「完全确定性」）。
"""

from __future__ import annotations

import typing as _t
from dataclasses import dataclass, field, replace

from .features import ROLE_B, ROLE_N, ROLE_R, ROLE_RB, BeatFeatures
from .lanes import GenNote, LaneGrid
from .structure import Section

__all__ = [
    "OFFSETS",
    "PATTERN_OFFSETS",
    "RolePlan",
    "NotePlanner",
    "plan_notes",
    "stable_jitter",
    "stable_unit",
]


#: 节奏型：每拍在哪些拍内偏移上落音符。
#: 真实谱面不是匀速堆密度，而是「常规型 + 密集型」交替：常规型铺底，遇到重拍
#: （底鼓局部极大）就换成密集型做爆发。
#:
#: 数值校验（重要）：改动后必须跑 `python -m rpechart.render stats`，确认没有空段。
#: 曾经的事故：长镜头的实现把整段 Tap 删掉只留每 4 拍一个 Hold，导致 196-232 拍
#: 几乎没有音符，播放到后面就像「谱面没写完」。
OFFSETS: dict[str, tuple] = {
    "sparse": (0.0,),                                              # 每 2 拍 1 个
    "basic": (0.0,),                                               # 每拍 1 个
    "half": (0.0, 0.5),                                            # 每拍 2 个
    "gallop": (0.0, 0.25, 0.5),                                    # 三连感
    "dense": (0.0, 0.25, 0.5, 0.75),                               # 每拍 4 个（十六分）
    "burst": (0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75, 0.875),   # 三十二分
}

#: 别名，语义更直白：节奏型名 → 拍内偏移。
PATTERN_OFFSETS = OFFSETS


# ---------------------------------------------------------------- 确定性抖动
def stable_unit(*parts: _t.Any) -> float:
    """由任意键得到 `[0, 1)` 的确定性伪随机数（FNV-1a，无 random）。

    同一组键在任何进程、任何平台都给同一个值 —— 这是「完全确定性」要求下
    唯一允许的「随机」来源。
    """
    h = 2166136261
    for part in parts:
        for ch in repr(part).encode("utf-8"):
            h ^= ch
            h = (h * 16777619) & 0xFFFFFFFF
    return h / 4294967296.0


def stable_jitter(*parts: _t.Any, scale: float = 1.0) -> float:
    """由键得到 `[-scale, scale]` 的确定性抖动。"""
    return (stable_unit(*parts) * 2.0 - 1.0) * float(scale)


# ---------------------------------------------------------------- 角色分派
@dataclass
class RolePlan:
    """一个角色 → 音符分派规则。

    `RB`（红蓝同场）需要**两条线交替出手**，所以比其它角色多一组
    `alt_*` 字段：落在 :attr:`alt_offsets` 里的偏移走备选线，其余走主线。

    默认表见 :data:`DEFAULT_PLANS`，它复刻了 原生成脚本里那串分支：

    ===========  =========================================================
    角色          行为
    ===========  =========================================================
    `B`         线 0 `Tap`，x 取蓝团质心 `bx`
    `R`         线 1 `Flick`，x 取红团质心 `rx`
    `RB`        正拍与半拍 → 线 0 `Tap`（蓝），其余 → 线 1 `Flick`（红）
    `N`         降密度 + 稀疏 `Tap` + 偶发 `Drag`（**不跳过**）
    ===========  =========================================================
    """

    line: int = 0
    kind: str = "tap"
    x_source: str = "bx"
    x_scale: float = 1.0
    #: 备选线（仅 RB 用得上）：落在 :attr:`alt_offsets` 的偏移走这条线。
    alt_line: _t.Optional[int] = None
    alt_kind: str = "flick"
    alt_x_source: str = "rx"
    alt_x_scale: float = 1.0
    alt_offsets: tuple = ()
    #: 该角色的密度倍率（`< 1` 就抽稀）。
    density_scale: float = 1.0

    def to_json(self) -> dict:
        return {
            "line": self.line,
            "kind": self.kind,
            "x_source": self.x_source,
            "x_scale": self.x_scale,
            "alt_line": self.alt_line,
            "alt_kind": self.alt_kind,
            "alt_x_source": self.alt_x_source,
            "alt_x_scale": self.alt_x_scale,
            "alt_offsets": list(self.alt_offsets),
            "density_scale": self.density_scale,
        }

    @classmethod
    def from_json(cls, data: _t.Mapping[str, _t.Any]) -> "RolePlan":
        d = dict(data)
        if "alt_offsets" in d and d["alt_offsets"] is not None:
            d["alt_offsets"] = tuple(d["alt_offsets"])
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})


#: 默认角色分派表（复刻 原生成脚本的写死分支）。
DEFAULT_PLANS: dict[str, RolePlan] = {
    ROLE_B: RolePlan(line=0, kind="tap", x_source="bx"),
    ROLE_R: RolePlan(line=1, kind="flick", x_source="rx"),
    ROLE_RB: RolePlan(
        line=0, kind="tap", x_source="bx",
        alt_line=1, alt_kind="flick", alt_x_source="rx",
        alt_offsets=(0.25, 0.75),
    ),
    ROLE_N: RolePlan(line=0, kind="tap", x_source="bx", density_scale=0.5),
}


# ---------------------------------------------------------------- 编排器
@dataclass
class NotePlanner:
    """把 :class:`BeatFeatures` 与段表 变成音符列表。

    参数全部可配，且**没有一条曲子相关的常量**（原脚本的 BPM / 段落名 /
    线号全部变成了参数或段表字段）。
    """

    grid: LaneGrid = field(default_factory=LaneGrid)
    # 必须逐个复制 RolePlan：`dict(DEFAULT_PLANS)` 是浅拷贝，两个 planner
    # 会共享同一个模块级 RolePlan 对象，改一个等于改全部（WebUI 进程内反复
    # regenerate 会累积污染）。
    plans: dict = field(
        default_factory=lambda: {k: replace(v) for k, v in DEFAULT_PLANS.items()}
    )
    #: 重拍判定：低频 >= 该值且是局部极大。
    accent_threshold: float = 0.55
    #: 中性段：每隔几拍出一个稀疏 Tap（`0` 表示不额外加）。
    neutral_stride: int = 2
    #: 中性段：每隔几拍在 `neutral_drag_line` 上补一个 Drag 让「旁观者」露面。
    neutral_drag_every: int = 4
    neutral_drag_line: int = 2
    neutral_drag_source: str = "cx"
    #: 确定性抖动的幅度（轨道步长的比例）。`0` = 完全对齐网格（默认）。
    jitter: float = 0.0
    #: 上限保护：单段音符数超过该值就抽稀（防止 `burst` 加长段炸内存）。
    max_notes_per_section: int = 4000

    # ------------------------------------------------------------ 重拍
    def accents(self, f: BeatFeatures) -> set:
        """低频（底鼓）局部极大点 = 重拍，用来决定密集型落在哪些拍上。"""
        low = f.low or []
        out = set()
        for i in range(1, len(low) - 1):
            if (low[i] >= self.accent_threshold
                    and low[i] >= low[i - 1] and low[i] >= low[i + 1]):
                out.add(i)
        return out

    # ------------------------------------------------------------ 取值
    def _x(self, f: BeatFeatures, source: str, i: int, scale: float) -> float:
        """取第 `i` 拍的 `source` 通道值（bx/rx/cx）并缩放。"""
        arr = getattr(f, source, None) or []
        if not arr:
            return 0.0
        v = float(arr[min(max(i, 0), len(arr) - 1)] or 0.0)
        return v * float(scale)

    def _place(self, f: BeatFeatures, plan: RolePlan, i: int, *,
               alt: bool = False, jitter_key: _t.Any = None) -> float:
        """按分派规则算出落点轨道坐标（含可选确定性抖动）。"""
        src = plan.alt_x_source if alt else plan.x_source
        scale = plan.alt_x_scale if alt else plan.x_scale
        x = self._x(f, src, i, scale)
        if self.jitter:
            x += stable_jitter(plan.line, i, src, jitter_key,
                               scale=self.jitter * self.grid.step)
        return self.grid.lane(self.grid.clamp(x))

    # ------------------------------------------------------------ 主流程
    def plan(self, f: BeatFeatures, sections: _t.Sequence[Section]) -> list[GenNote]:
        """返回音符列表（已吸附到轨道，但**尚未**做三道合法化）。

        调用方应当接着跑 :meth:`LaneGrid.legalize` —— 见
        :mod:`rpechart.gen.lanes` 的模块 docstring。
        """
        accent = self.accents(f)
        out: list[GenNote] = []

        for sec in sections:
            if sec.end <= sec.start:
                continue
            normal = OFFSETS.get(sec.normal) or OFFSETS["basic"]
            dense = OFFSETS.get(sec.dense) or normal
            plan = (self.plans.get(sec.role)
                    or DEFAULT_PLANS.get(sec.role)
                    or DEFAULT_PLANS[ROLE_N])
            stop = min(int(sec.end), f.n) if f.n else int(sec.end)
            stride = max(1, int(sec.stride))
            every = max(1, int(sec.dense_every))

            for beat in range(int(sec.start), stop, stride):
                i = min(beat, max(0, f.n - 1))
                role = f.role(i)
                is_dense = (i in accent) and (i % every == 0)
                offs = dense if is_dense else normal

                # ---- 中性段：不整段跳过，只用较疏的落点，并让「旁观者」线露面。
                if role == ROLE_N:
                    offs = offs[: max(1, len(offs) // 2)]
                    if self.neutral_drag_every and beat % self.neutral_drag_every == 0:
                        out.append(GenNote(
                            float(beat), "drag",
                            self.grid.lane(self.grid.clamp(
                                self._x(f, self.neutral_drag_source, i, 1.0))),
                            self.neutral_drag_line, 0.0))
                    if self.neutral_stride and beat % self.neutral_stride == 0:
                        out.append(GenNote(
                            float(beat), plan.kind,
                            self._place(f, plan, i, jitter_key="n"),
                            plan.line, 0.0))
                    continue

                if not offs:
                    continue
                for off in offs:
                    t = beat + float(off)
                    if t >= sec.end:
                        continue
                    if plan.alt_line is not None and float(off) in plan.alt_offsets:
                        out.append(GenNote(
                            t, plan.alt_kind,
                            self._place(f, plan, i, alt=True, jitter_key=off),
                            int(plan.alt_line), 0.0))
                    else:
                        out.append(GenNote(
                            t, plan.kind,
                            self._place(f, plan, i, jitter_key=off),
                            int(plan.line), 0.0))

        # ---- Hold：**叠加**在 Tap 之上，绝不替换。
        holds: list[GenNote] = []
        for sec in sections:
            holds += self.plan_holds(f, sec)
        out += holds

        out.sort(key=lambda n: (n.t, n.line, n.x))
        return self._cap(out, sections)

    def _cap(
        self, notes: list[GenNote], sections: _t.Sequence[Section]
    ) -> list[GenNote]:
        """把超过 :attr:`max_notes_per_section` 的段抽稀到上限以内。

        逐拍抽稀（而不是「从尾巴砍掉」）：保留的稀疏版仍然覆盖整段，
        不会出现「后半段没有音符」。上限 <= 0 表示不限制。
        """
        limit = int(self.max_notes_per_section)
        if limit <= 0:
            return notes
        # 段按 start 升序且互不重叠（normalize_sections 的保证），所以单次扫描
        # 就能把音符分到所属段；段外的音符（例如 `"at"` 写在段边界上的 Hold）
        # 原样保留，不参与抽稀。
        bounds = [
            (float(s.start), float(s.end)) for s in sections if s.end > s.start
        ]
        if not bounds:
            return notes
        bucketed: list[list[GenNote]] = [[] for _ in bounds]
        outside: list[GenNote] = []
        k = 0
        for n in notes:
            while k < len(bounds) and n.t >= bounds[k][1]:
                k += 1
            if k < len(bounds) and bounds[k][0] <= n.t < bounds[k][1]:
                bucketed[k].append(n)
            else:
                outside.append(n)
        kept: list[GenNote] = list(outside)
        for group in bucketed:
            if len(group) <= limit:
                kept.extend(group)
                continue
            step = (len(group) + limit - 1) // limit
            kept.extend(group[::step][:limit])
        kept.sort(key=lambda n: (n.t, n.line, n.x))
        return kept

    # ------------------------------------------------------------ Hold
    def plan_holds(self, f: BeatFeatures, sec: Section) -> list[GenNote]:
        """按 :attr:`Section.holds` 声明生成 Hold（追加，不替换 Tap）。

        `holds` 是一个 dict 列表，支持两种写法：

        * 循环：`{"from":194.0,"to":226.0,"every":8.0,"dur":3.0,"line":0,
          "x_source":"bx","x_scale":0.85}`
        * 单点：`{"at":232.0,"dur":4.0,"line":0,"x_source":"bx","x_scale":0.7}`

        `x_source` 与 `x_scale` 复刻原脚本的 `lane(bx*0.85)` 与
        `lane(bx*0.7)` —— 缩放在量化之前，所以 Hold 与 Tap 的落点天然不同
        （这曾经是「不必避让」的理由，量化之后仍需
        :meth:`LaneGrid.free_holds` 兜底）。
        """
        if not sec.holds:
            return []
        out: list[GenNote] = []
        for spec in sec.holds:
            if not isinstance(spec, dict):
                continue
            line = int(spec.get("line", 0))
            dur = float(spec.get("dur", 1.0))
            src = str(spec.get("x_source", "bx"))
            scale = float(spec.get("x_scale", 1.0))
            fixed_x = spec.get("x", None)

            def place(beat: float, _src=src, _scale=scale, _fx=fixed_x) -> float:
                if _fx is not None:
                    return self.grid.lane(self.grid.clamp(float(_fx)))
                i = min(max(int(beat), 0), max(0, f.n - 1))
                return self.grid.lane(self.grid.clamp(self._x(f, _src, i, _scale)))

            if "at" in spec:
                beat = float(spec["at"])
                out.append(GenNote(beat, "hold", place(beat), line, dur))
                continue
            t = float(spec.get("from", sec.start))
            t_to = float(spec.get("to", sec.end))
            # 下限 1 拍：旧实现只挡 every <= 0，于是 {"every": 0.001}
            # 会在 1000 拍里生成约 1e6 个 Hold，直接把内存打爆。
            every = max(float(spec.get("every", 8.0)), 1.0)
            while t < t_to and (not f.n or t < f.n):
                out.append(GenNote(t, "hold", place(t), line, dur))
                t += every
        return out


# ---------------------------------------------------------------- 函数式入口
def plan_notes(f: BeatFeatures, sections: _t.Sequence[Section], *,
               planner: _t.Optional[NotePlanner] = None) -> list[GenNote]:
    """`NotePlanner.plan` 的函数式别名。

    对应 原生成脚本的 `plan_notes(f)`，但段落表从参数传入
    （原脚本读模块级 `SECTIONS`）。
    """
    return (planner or NotePlanner()).plan(f, sections)
