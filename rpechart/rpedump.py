"""把谱面写成 RPE 自己的 JSON 风格。

RPE 1.6.0 用 RapidJSON 的 PrettyWriter 写出 chart.json，风格与我们平时用的
json.dumps(indent=2) 完全不同。实测把两份真实 .pez 的 chart.json 用本模块的
:func:`dumps` 重新序列化，可以得到**逐字节相同**的文本，所以这就是「导出标准谱面」
所需的写出器。

规则（全部由实测反推，见 真实谱面语料 与 真实 .pez 样本）：

1. 缩进是 **3 个空格**一层；
2. 键值分隔写作 ``"key" : value``（冒号**两侧都有空格**）；
3. 对象的所有键按 **字节序** 排序（RapidJSON 用 std::map 存对象）；
4. 数组元素之间是 ``,`` 加换行，收尾的 ] 与**父级**缩进对齐；
5. **纯标量数组内联**成一行：[ 0, 0, 1 ]、[ "Default" ]（RapidJSON 的
   kFormatSingleLineArray）；含对象/数组的则逐行展开；
6. 空对象写 {}、空数组写 []；
7. 浮点用 %.17g，结果里没有点号/e 就补 .0（0.8 → 0.80000000000000004，
   148.119995 → 148.11999499999999）；**负零要写成 -0.0**（真实谱面里
   ``"start" : -0.0`` 很常见，而 '%.17g' % -0.0 得到的是 '-0'，
   不补 .0 就丢了负号语义）；
8. 整数原样按整数写，布尔写 true/false，None 写 null。
"""

from __future__ import annotations

import json
import typing as _t

__all__ = ["dumps", "dumps_chart"]

#: 一层缩进的宽度。RPE 用 3 个空格，不是 2 也不是 4。
INDENT = "   "


def _format_float(value: float) -> str:
    """按 RPE（RapidJSON）的写法格式化一个浮点。"""
    text = "%.17g" % value
    if "." not in text and "e" not in text and "E" not in text:
        # ``%.17g`` 对整数值浮点（0.0 / -0.0 / 1.0）不给小数点，RPE 会补上。
        text += ".0"
    return text


def _format_scalar(value: _t.Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _format_float(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError(f"不能写进谱面 JSON 的类型: {type(value).__name__}")


def _is_container(value: _t.Any) -> bool:
    return isinstance(value, (dict, list, tuple))


def _dump(value: _t.Any, level: int) -> str:
    pad = INDENT * level
    inner = INDENT * (level + 1)
    if isinstance(value, dict):
        if not value:
            return "{}"
        parts = []
        for key in sorted(value.keys()):
            parts.append(
                f'{inner}{json.dumps(str(key), ensure_ascii=False)}'
                f" : {_dump(value[key], level + 1)}"
            )
        return "{\n" + ",\n".join(parts) + "\n" + pad + "}"
    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        if not any(_is_container(v) for v in value):
            # kFormatSingleLineArray：纯标量数组压成一行
            return "[ " + ", ".join(_format_scalar(v) for v in value) + " ]"
        parts = [f"{inner}{_dump(v, level + 1)}" for v in value]
        return "[\n" + ",\n".join(parts) + "\n" + pad + "]"
    return _format_scalar(value)


def dumps(obj: _t.Any) -> str:
    """把 obj 序列化成 RPE 风格的 JSON 文本。

    返回的字符串**不带**结尾换行；RPE 写出的 chart.json 末尾恰好是一个换行，
    调用方补上即可（:func:`dumps_chart` 已经补好）。
    """
    return _dump(obj, 0)


def dumps_chart(chart: _t.Any) -> str:
    """把一个 Chart 写成 RPE 风格的 chart.json 文本（末尾带一个换行）。"""
    if hasattr(chart, "to_json") and callable(chart.to_json):
        data = chart.to_json()
    else:
        data = chart
    return dumps(data) + "\n"
