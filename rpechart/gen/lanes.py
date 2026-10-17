"""轨道网格与三道合法化。

「落点设计」的核心是一个**离散轨道网格**：真实谱面的 `positionX` 几乎只落在
135 步长的格点上（`-405, -270, -135, 0, 135, 270, 405`），而旧实现直接把画面
质心当坐标写进去，导致相邻音符一次跳 360 单位 —— 用户的原话是「键在乱飞」。

本模块提供：

* :class:`LaneGrid`：可配的轨道网格（`LANES` 与 `LANE_STEP` 从模块常量变成参数）；
* :class:`GenNote`：生成层的音符中间表示（`(t, kind, x, line, dur)` 五元组）；
* 三道**顺序不可换**的合法化：

  1. :meth:`LaneGrid.tame`（`tame_lanes`）—— 限幅相邻音符的跨轨幅度；
  2. :meth:`LaneGrid.free_holds` —— 把 Hold 挪出「在位期间有同轨 Tap」的轨道；
  3. :meth:`LaneGrid.dedupe`（`dedupe_same_line`）—— 同线同拍同 x 去重。

顺序为什么不能换
----------------
`tame` 会改 `x`，改完可能把两个音符挤到同一条轨道上 —— 由 `dedupe` 兜住；
而 `free_holds` 必须**在 x 定下来之后**才能判断 Hold 与 Tap 是否同轨。
若先 `free_holds` 再 `tame`，`tame` 会把刚挪开的 Hold 又推回 Tap 的轨道上，
`TapHoldOverlapped` 立刻复现（这是一次真实事故）。

对应 原生成脚本的 `tame_lanes` 与 `free_holds` 与 `dedupe_same_line`
以及 `_lane` 与 `_lane_index` 与 `_nudge_candidates`。
"""

from __future__ import annotations

import bisect
import typing as _t
from dataclasses import dataclass

__all__ = ["GenNote", "LaneGrid", "KINDS", "tame_lanes", "free_holds", "dedupe_same_line"]

#: 生成层会产出的音符类型（字符串形式，写进 builder 时再映射成 id）。
KINDS = ("tap", "flick", "drag", "hold")


class GenNote(_t.NamedTuple):
    """生成层的音符。

    就是一个五元组 `(t, kind, x, line, dur)`，但带字段名，既能
    `for t, kind, x, line, dur in notes` 解包，也能 `note.x` 直接取。

    * `t`：起始拍；
    * `kind`：`"tap"` / `"flick"` / `"drag"` / `"hold"`；
    * `x`：轨道上的 `positionX`（已吸附到 :class:`LaneGrid` 的格点）；
    * `line`：判定线索引；
    * `dur`：仅 Hold 有意义（拍）。
    """

    t: float
    kind: str
    x: float
    line: int
    dur: float = 0.0

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"GenNote(t={self.t:g} {self.kind} x={self.x:g} "
                f"line={self.line} dur={self.dur:g})")


@dataclass
class LaneGrid:
    """轨道网格 + 合法化参数。

    默认值来自真实谱面取证（24 份 `真实谱面语料 pezc-*.json`）：步长 135、
    七条轨道、相邻紧邻音符最多跨 2 轨、`2.0` 拍以内算「紧邻」。

    `span` 是网格的半宽：轨道数由 `span` 与 `step` 推出，所以换曲子只要给
    `lanes` 或 `span` 就能改变轨道数，不必改代码。
    """

    lanes: _t.Optional[_t.Sequence[float]] = None
    step: float = 135.0
    span: float = 405.0
    max_step: int = 2
    tight_sec: float = 2.0

    def __post_init__(self) -> None:
        if self.lanes is None:
            k = int(round(self.span / self.step))
            self.lanes = [round(-k * self.step + i * self.step, 6) for i in range(2 * k + 1)]
        else:
            self.lanes = [float(v) for v in self.lanes]
        if len(self.lanes) < 2:
            raise ValueError("LaneGrid 至少需要 2 条轨道")
        if self.step <= 0:
            raise ValueError("LaneGrid.step 必须为正")

    # ------------------------------------------------------------ 基本查询
    @property
    def n(self) -> int:
        """轨道数。"""
        return len(self.lanes)

    @property
    def first(self) -> float:
        return self.lanes[0]

    @property
    def last(self) -> float:
        return self.lanes[-1]

    def index(self, x: float) -> int:
        """把连续坐标吸附到最近轨道的**索引**（越界钳到边界）。

        直接在 `lanes` 上做最近邻，不按 `step` 反推下标：自定义网格的间距
        未必等于 `step`（例如 `lanes=[-100, 0, 100]` 配默认 `step=135`），
        `round((x - lanes[0]) / step)` 会把 x=100 算成下标 1（吸到 0.0）。
        """
        value = float(x)
        pos = bisect.bisect_left(self.lanes, value)
        if pos <= 0:
            return 0
        if pos >= self.n:
            return self.n - 1
        before, after = self.lanes[pos - 1], self.lanes[pos]
        return pos if (after - value) < (value - before) else pos - 1

    def lane(self, x: float) -> float:
        """把连续坐标吸附到最近轨道的**坐标**（落点设计）。"""
        return self.lanes[self.index(x)]

    #: `_lane` 的别名，方便沿用原脚本的叫法。
    snap = lane

    def clamp(self, x: float, lo: float = -660.0, hi: float = 660.0) -> float:
        """把坐标钳进 RPE 的可见视界（默认 ±660）。"""
        return max(lo, min(hi, float(x)))

    def nudge_candidates(self, x: float) -> list[float]:
        """同拍冲突时的备选轨道：从最近邻开始，一路退到中场。

        与 `_nudge_candidates` 等价。**必须沿网格走** —— 随便 +60 会把音符
        挪到格子外面，破坏「落点量化到轨道」这个设计。
        """
        i = self.index(x)
        order = sorted(range(self.n), key=lambda j: (abs(j - i), j))
        return [self.lanes[j] for j in order if self.lanes[j] != x]

    # ------------------------------------------------------------ 三道合法化
    def tame(self, notes: _t.Sequence[GenNote]) -> list[GenNote]:
        """限制**同一线上时间紧邻**的音符跨轨幅度，治「键在乱飞」。

        取证：旧实现相邻音符 x 最大跳变 360 单位（= 3 轨），而真实谱面靠轨道
        量化天然是 1 轨为主。规则：同一线按时间排序后，若两个音符间隔 <
        :attr:`tight_sec` 拍，则后者最多比前者多跨 :attr:`max_step` 轨；
        超出就拉回来。
        """
        by_line: dict[int, list[GenNote]] = {}
        for n in notes:
            by_line.setdefault(n.line, []).append(n)
        out: list[GenNote] = []
        for line, group in by_line.items():
            group = sorted(group, key=lambda n: (n.t, n.x))
            prev_t: _t.Optional[float] = None
            prev_i: _t.Optional[int] = None
            fixed: list[GenNote] = []
            for n in group:
                i = self.index(n.x)
                if prev_i is not None and prev_t is not None and (n.t - prev_t) < self.tight_sec:
                    if i > prev_i + self.max_step:
                        i = prev_i + self.max_step
                    elif i < prev_i - self.max_step:
                        i = prev_i - self.max_step
                    i = max(0, min(self.n - 1, i))
                fixed.append(n._replace(x=self.lanes[i]))
                prev_t, prev_i = n.t, i
            out += fixed
        out.sort(key=lambda n: (n.t, n.line, n.x))
        return out

    def free_holds(self, notes: _t.Sequence[GenNote]) -> list[GenNote]:
        """把 Hold 挪到「它在位期间没有同轨 Tap」的轨道上。

        **这是一次真实缺陷的产物**：把落点量化到轨道之后，Hold 用的
        `lane(bx*0.85)` 与 Tap 用的 `lane(bx)` 可能落到同一条轨道上。而 RPE 的
        `TapHoldOverlapped` 规则是：**同一条线、positionX 相同、Tap 落在 Hold 的
        `[start, end)` 内** ⇒ Error（`rpechart/validate.py:576`）。量化之前两者因
        0.85 的系数差而数值必不相等，所以从未触发；量化之后必须显式避让。

        做法：按线收集所有 Tap 的 (拍, 轨道)，对每个 Hold 检查它时间窗内是否有
        同轨 Tap，有就沿轨道向最近的空位退。

        注意：Hold **不参与**这里的避让判定 —— 两个 Hold 叠在同轨是合法的
        （Drag/Hold 不参与「相撞」判定），只有 Tap 会杀 Hold。
        """
        by_line: dict[int, list[GenNote]] = {}
        for n in notes:
            by_line.setdefault(n.line, []).append(n)

        out: list[GenNote] = []
        for line, group in by_line.items():
            taps = sorted((n.t, self.index(n.x)) for n in group if n.kind == "tap")
            for n in group:
                if n.kind != "hold":
                    out.append(n)
                    continue
                lo, hi = n.t, n.t + max(n.dur, 0.0)
                taken = {lane for tt, lane in taps if lo <= tt < hi}
                i = self.index(n.x)
                if i in taken:
                    for j in sorted(range(self.n), key=lambda j: (abs(j - i), j)):
                        if j not in taken:
                            i = j
                            break
                out.append(n._replace(x=self.lanes[i]))
        out.sort(key=lambda n: (n.t, n.line, n.x))
        return out

    def dedupe(self, notes: _t.Sequence[GenNote]) -> list[GenNote]:
        """同一线同一拍不允许有两个同 x 的音符（RPE 报 `Tap&Tap Overlapped`）。

        注意：错开必须**沿轨道网格走**（±`step`），不能随便 +60 之类 —— 那会把
        音符挪到格子外面，破坏「落点量化到轨道」这个设计（真实谱面的 positionX
        只在 135 步长的格点上）。实在找不到空位就丢弃该音符（宁缺毋滥）。
        """
        seen: set = set()
        out: list[GenNote] = []
        for n in notes:
            key = (n.line, round(n.t, 6), round(n.x, 1))
            if key in seen:
                # 由近及远找同一线上的空格点（优先向中场收缩，观感更稳）
                for cand in self.nudge_candidates(n.x):
                    key = (n.line, round(n.t, 6), round(cand, 1))
                    if key not in seen:
                        n = n._replace(x=cand)
                        break
                else:
                    continue
            seen.add(key)
            out.append(n)
        return out

    def legalize(self, notes: _t.Sequence[GenNote]) -> list[GenNote]:
        """三道合法化按**固定顺序**跑完：tame → free_holds → dedupe。

        顺序不可换，原因见模块 docstring。所有调用方都应当走这里，而不是
        自己拼 —— 原生成脚本的事故就是因为顺序写反。
        """
        return self.dedupe(self.free_holds(self.tame(notes)))


# ---------------------------------------------------------------- 函数式入口
#: 以下三个薄封装保持与 原生成脚本相同的函数名，方便对照阅读与
#: 迁移既有脚本；它们都接受一个 :class:`LaneGrid`（默认用标准网格）。

DEFAULT_GRID = LaneGrid()


def tame_lanes(notes: _t.Sequence[GenNote], grid: _t.Optional[LaneGrid] = None) -> list[GenNote]:
    """`LaneGrid.tame` 的函数式别名（默认网格）。"""
    return (grid or DEFAULT_GRID).tame(notes)


def free_holds(notes: _t.Sequence[GenNote], grid: _t.Optional[LaneGrid] = None) -> list[GenNote]:
    """`LaneGrid.free_holds` 的函数式别名（默认网格）。"""
    return (grid or DEFAULT_GRID).free_holds(notes)


def dedupe_same_line(notes: _t.Sequence[GenNote],
                     grid: _t.Optional[LaneGrid] = None) -> list[GenNote]:
    """`LaneGrid.dedupe` 的函数式别名（默认网格）。"""
    return (grid or DEFAULT_GRID).dedupe(notes)
