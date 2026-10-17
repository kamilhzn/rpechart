"""BPM 与秒/拍的互相换算 —— 程序化制谱里最关键的一块。

RPE 的一切时间字段都是**拍**，而音乐的时长、判定的时间窗口都是**秒**。
换算规则（phira-docs ``beat`` 节）：

* 单 BPM 时 ``seconds = 60 / BPM * beat``；
* 多 BPM 时按 ``BPMList`` 分段累加；
* 判定线带 ``bpmfactor`` 时，该线实际使用的 BPM 是 ``nowBpm / bpmfactor``
  （``bpmfactor > 1`` 表示这条线「变慢」）。

本模块同时提供两种风格：

* :func:`beat_to_second` / :func:`second_to_beat`：直接传 ``BPMList``；
* :class:`Timing`：把 ``BPMList`` + 可选 ``bpmfactor`` 打包成对象，带缓存。
"""

from __future__ import annotations

import typing as _t

from .beat import Beat

__all__ = ["Timing", "beat_to_second", "second_to_beat", "build_bpm_list"]


class Timing:
    """一张谱面的 BPM 表，负责秒 <-> 拍换算。

    ``bpmfactor`` 传判定线的 ``bpmfactor``；默认 1.0 表示按谱面本身 BPM。
    """

    __slots__ = ("_items", "_bpmfactor")

    def __init__(self, items: _t.Sequence, bpmfactor: float = 1.0) -> None:
        norm: list[tuple[float, Beat, float]] = []
        for item in items:
            if isinstance(item, (tuple, list)):
                bpm = float(item[0])
                start = Beat.of(item[1] if len(item) > 1 else 0)
            else:
                bpm = float(getattr(item, "bpm"))
                start = Beat.of(getattr(item, "startTime"))
            factor = float(bpmfactor) or 1.0
            # bpm <= 0 是明确的错误值，交给校验器报 BadBPM；但换算必须要有一个
            # 有限的分母，否则 Timing 会在 beat_to_second 里抛 ZeroDivisionError
            # （旧实现存 effective = 0.0 无任何 guard）。用 RPE 的默认 120 兜底，
            # 不要写 `bpm or 120.0`：那会把非零的非法值也悄悄换掉。
            if not bpm > 0:
                bpm = 120.0
            norm.append((bpm, start, bpm / factor))
        norm.sort(key=lambda x: x[1].frac)
        # 首段必须从 0 拍开始，否则 RPE 的语义不明确
        if not norm:
            norm = [(120.0, Beat(0), 120.0 / (float(bpmfactor) or 1.0))]
        elif norm[0][1] != Beat(0):
            norm.insert(0, (norm[0][0], Beat(0), norm[0][2]))
        self._items = norm
        self._bpmfactor = float(bpmfactor) or 1.0

    # ---------------------------------------------------------------- 属性
    @property
    def bpmfactor(self) -> float:
        return self._bpmfactor

    @property
    def base_bpm(self) -> float:
        return self._items[0][0]

    @property
    def effective_bpm(self) -> float:
        return self._items[0][2]

    @property
    def is_constant(self) -> bool:
        return len(self._items) == 1

    def segments(self) -> list[tuple[float, Beat, float]]:
        """返回 ``[(bpm, startBeats, effective_bpm), ...]``。"""
        return list(self._items)

    # ---------------------------------------------------------------- 换算
    def beat_to_second(self, beats: _t.Any) -> float:
        """拍 -> 秒。"""
        target = Beat.of(beats)
        if target <= Beat(0):
            return 0.0
        seconds = 0.0
        for i, (_bpm, start, effective) in enumerate(self._items):
            nxt = self._items[i + 1][1] if i + 1 < len(self._items) else None
            if nxt is None:
                seconds += (target - start).value * (60.0 / effective)
                return seconds
            if target >= nxt:
                seconds += (nxt - start).value * (60.0 / effective)
            else:
                seconds += (target - start).value * (60.0 / effective)
                return seconds
        return seconds

    def second_to_beat(self, seconds: float) -> Beat:
        """秒 -> 拍。"""
        if seconds <= 0.0:
            return Beat(0)
        remaining = float(seconds)
        total = Beat(0)
        for i, (_bpm, start, effective) in enumerate(self._items):
            nxt = self._items[i + 1][1] if i + 1 < len(self._items) else None
            seg_seconds = (
                (nxt - start).value * (60.0 / effective) if nxt is not None else None
            )
            if seg_seconds is None or remaining < seg_seconds:
                total = start + Beat(remaining / (60.0 / effective))
                return total
            remaining -= seg_seconds
            total = nxt
        return total

    # ------------------------------------------------------- 便捷名（常用）
    def sec(self, beats: _t.Any) -> float:
        return self.beat_to_second(beats)

    def beats(self, seconds: float) -> Beat:
        return self.second_to_beat(seconds)

    def seconds_per_beat(self, at: _t.Any = 0) -> float:
        """某拍处的「一拍等于多少秒」。"""
        target = Beat.of(at)
        value = self._items[0][2]
        for _bpm, start, effective in self._items:
            if start <= target:
                value = effective
            else:
                break
        return 60.0 / value

    def __repr__(self) -> str:
        segs = ", ".join(f"{bpm:g}@{start}" for bpm, start, _ in self._items)
        return f"Timing({segs}, bpmfactor={self._bpmfactor:g})"


# ------------------------------------------------------------------ 便捷函数
def beat_to_second(beats: _t.Any, bpm_list: _t.Sequence, bpmfactor: float = 1.0) -> float:
    return Timing(bpm_list, bpmfactor).beat_to_second(beats)


def second_to_beat(seconds: float, bpm_list: _t.Sequence, bpmfactor: float = 1.0) -> Beat:
    return Timing(bpm_list, bpmfactor).second_to_beat(seconds)


def build_bpm_list(entries: _t.Sequence) -> list:
    """把 ``[(bpm, beat), ...]`` 或 ``{"bpm":..., "startTime":...}`` 转成 BPMItem 列表。"""
    from .model import BPMItem

    out: list[BPMItem] = []
    for entry in entries:
        if isinstance(entry, BPMItem):
            out.append(entry)
        elif isinstance(entry, dict):
            out.append(BPMItem.from_json(entry))
        elif isinstance(entry, (tuple, list)):
            if len(entry) == 1:
                out.append(BPMItem(bpm=float(entry[0]), startTime=Beat(0)))
            else:
                out.append(BPMItem(bpm=float(entry[0]), startTime=Beat.of(entry[1])))
        else:
            out.append(BPMItem(bpm=float(entry), startTime=Beat(0)))
    if not out:
        # RPE 载入器在没有 BPMList 时用的默认值是 140
        out = [BPMItem(bpm=140.0, startTime=Beat(0))]
    out.sort(key=lambda b: b.startTime.frac)
    if out[0].startTime != Beat(0):
        out.insert(0, BPMItem(bpm=out[0].bpm, startTime=Beat(0)))
    return out
