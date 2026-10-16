"""RPE 的时间单位 Beat（带分数）。

RPEJSON 中所有时间字段都是 ``int[3]``，形如 ``[a, b, c]``，在编辑器里显示为
``a:b/c``，数值等于 ``a + b/c``。``c`` 为 0 时按 1 处理（避免 ZeroDivisionError）。

本模块内部用 :class:`fractions.Fraction` 保存精确值，这样多 BPM 换算、事件首尾
对齐都不会累积浮点误差 —— 这是程序化制谱最容易出问题的地方。
"""

from __future__ import annotations

import typing as _t
from fractions import Fraction

__all__ = ["Beat", "beat", "MAX_DENOMINATOR"]

#: 把浮点拍号还原成有理数时允许的最大分母。
#: RPE 的横线密度最高 100 条，192 分音符也已远超常规，1e6 足够且能吃掉浮点噪声。
MAX_DENOMINATOR = 1_000_000


def _to_fraction(value: _t.Any) -> Fraction:
    """把任意合理的输入统一转成 :class:`Fraction`。"""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, Beat):
        return value.frac
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"Beat 不接受非有限浮点数: {value!r}")
        # 用 limit_denominator 吃掉 0.1 -> 3602879701896397/36028797018963968 这类噪声
        return Fraction(value).limit_denominator(MAX_DENOMINATOR)
    if isinstance(value, str):
        return _parse_str(value)
    if isinstance(value, (list, tuple)):
        return _parse_seq(value)
    raise TypeError(f"无法把 {type(value).__name__} 解释为 Beat: {value!r}")


def _parse_seq(seq: _t.Sequence) -> Fraction:
    if len(seq) == 1:
        return _to_fraction(seq[0])
    if len(seq) == 2:
        return _parse_seq([seq[0], seq[1], 1])
    if len(seq) < 3:
        raise ValueError(f"Beat 序列至少 1 个元素: {seq!r}")
    a, b, c = seq[0], seq[1], seq[2]
    if c == 0:
        # RPE 在 c==0 时按 1 处理；b 也必须为 0，否则视为非法输入
        # （phira-docs 称之为 ZeroDenominator）。统一抛 ValueError，方便调用方
        # 用一个 except 接住所有「输入不合法」的情况。
        if b not in (0, None):
            raise ValueError(f"非法 Beat（分母为 0 且分子非 0）: {seq!r}")
        c = 1
    return Fraction(int(a) * int(c) + int(b), int(c))


def _parse_str(text: str) -> Fraction:
    s = text.strip()
    if not s:
        raise ValueError("空的 Beat 字符串")
    # 支持 "a:b/c" / "a+b/c" / "b/c" / "a" / "1.5"
    if ":" in s:
        head, _, tail = s.partition(":")
        head = head.strip()
        base = Fraction(head) if head else Fraction(0)
        tail = tail.strip()
        if not tail:
            return base
        return base + _to_fraction(tail)
    return Fraction(s).limit_denominator(MAX_DENOMINATOR)


class Beat:
    """精确的 RPE 拍号。

    可直接与 ``int`` / ``float`` / ``Beat`` 比较和四则运算（结果仍是 ``Beat``）。

    ``[a, b, c]`` 的字面写法会被保留：RPE 自己并不约分，同一个拍号在不同谱面里
    可能是 ``[2, 0, 4]`` 也可能是 ``[2, 0, 1]``。数值相等，但对「载入真实谱面再原样
    存回」来说必须照抄——:meth:`to_list` 会优先返回载入时的原始三元组。
    """

    __slots__ = ("frac", "raw")

    def __init__(self, value: _t.Any = 0) -> None:
        self.frac: Fraction = _to_fraction(value)
        #: 载入时的原始 ``[a, b, c]`` 字面量（有则原样写回，否则为 None）
        self.raw: _t.Optional[list[int]] = None
        if isinstance(value, (list, tuple)) and len(value) >= 3:
            # 记住字面量：RPE 不约分，[2,0,4] 与 [2,0,1] 数值相同但写回应当一致
            self.raw = [int(value[0]), int(value[1]), int(value[2]) or 1]

    # ---------------------------------------------------------------- 构造
    @classmethod
    def of(cls, value: _t.Any = 0) -> "Beat":
        return value if isinstance(value, Beat) else cls(value)

    @classmethod
    def from_list(cls, seq: _t.Sequence) -> "Beat":
        return cls(seq)

    @classmethod
    def parse(cls, text: str) -> "Beat":
        return cls(_parse_str(text))

    # ---------------------------------------------------------------- 转换
    @property
    def value(self) -> float:
        """浮点拍数。"""
        return float(self.frac)

    def to_list(self) -> list[int]:
        """序列化成 RPE 的 ``[a, b, c]``（a 为整数部分，b/c 为真分数部分）。

        载入过真实谱面时返回原始字面量，确保「打开再保存」不会把
        ``[2, 0, 4]`` 改写成 ``[2, 0, 1]``。
        """
        if self.raw is not None:
            return list(self.raw)
        n, d = self.frac.numerator, self.frac.denominator
        a = n // d
        b = n - a * d
        return [a, b, d]

    def __float__(self) -> float:
        return float(self.frac)

    def __int__(self) -> int:
        return int(self.frac)

    # ---------------------------------------------------------------- 比较
    def __eq__(self, other: object) -> bool:
        try:
            return self.frac == _to_fraction(other)
        except (TypeError, ValueError):
            return NotImplemented

    def __hash__(self) -> int:
        return hash(self.frac)

    def __lt__(self, other: _t.Any) -> bool:
        return self.frac < _to_fraction(other)

    def __le__(self, other: _t.Any) -> bool:
        return self.frac <= _to_fraction(other)

    def __gt__(self, other: _t.Any) -> bool:
        return self.frac > _to_fraction(other)

    def __ge__(self, other: _t.Any) -> bool:
        return self.frac >= _to_fraction(other)

    # ---------------------------------------------------------------- 运算
    def __add__(self, other: _t.Any) -> "Beat":
        return Beat(self.frac + _to_fraction(other))

    __radd__ = __add__

    def __sub__(self, other: _t.Any) -> "Beat":
        return Beat(self.frac - _to_fraction(other))

    def __rsub__(self, other: _t.Any) -> "Beat":
        return Beat(_to_fraction(other) - self.frac)

    def __mul__(self, other: _t.Any) -> "Beat":
        if isinstance(other, (int, float, Fraction)) and not isinstance(other, bool):
            return Beat(self.frac * Fraction(other).limit_denominator(MAX_DENOMINATOR))
        return NotImplemented

    __rmul__ = __mul__

    def __truediv__(self, other: _t.Any) -> "Beat":
        return Beat(self.frac / _to_fraction(other))

    def __neg__(self) -> "Beat":
        return Beat(-self.frac)

    # ---------------------------------------------------------------- 展示
    def __repr__(self) -> str:
        a, b, c = self.to_list()
        return f"Beat({a}:{b}/{c})"

    def __str__(self) -> str:
        a, b, c = self.to_list()
        if b == 0:
            return str(a)
        return f"{a}:{b}/{c}"

    def __bool__(self) -> bool:
        return bool(self.frac)


def beat(value: _t.Any = 0) -> Beat:
    """便捷构造：``beat(0.5)`` / ``beat([1, 1, 4])`` / ``beat("1:1/4")``。"""
    return Beat.of(value)
