"""谱面保真测试 —— 拿样本谱面验证本库能无损往返。

样本在 ``tests/fixtures/``：三份谱面（现代 / 老版本 / 多判定线）+ 一个 ``.pez``，
**全部由 ``tests/fixtures/make_fixtures.py`` 用本库自己生成**，不含任何第三方素材。
它们刻意覆盖了 RPE 写出风格里最容易踩坑的地方：``-0.0``、整数 ``alphaEvents``、
末尾 4 个 ``null`` 层占位、17 位浮点、``RPEVersion 113`` 的老字段集。

测试目标只有一条：load → to_json 必须与原文语义一致。
键序、缩进、`"key" : value` 这种空格风格属于序列化审美，不在保真范围内，
但**字段是否存在**（空层省略、speedEvents 无 bezier）属于语义，必须一致；
``tests/test_rpedump.py`` 另外把「逐字节相同」也钉死了。

样本缺失时**跳过而不是失败**（``.pez`` 可以单独分发）。
"""

import json
import os
import zipfile

from run_tests import test

from rpechart.io import load_chart, load_json
from rpechart.model import Chart

_FIXTURES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures"
)
_CHARTS = os.path.join(_FIXTURES, "charts")
_PEZ = os.path.join(_FIXTURES, "demo.pez")

# 收集样本：.pez 直接读，chart.json 直接读
_SAMPLES = []


def _collect_samples():
    if os.path.isdir(_CHARTS):
        for name in sorted(os.listdir(_CHARTS)):
            if name.endswith(".json"):
                _SAMPLES.append((name, os.path.join(_CHARTS, name)))
    if os.path.isfile(_PEZ):
        _SAMPLES.append(("demo.pez", _PEZ))


_collect_samples()


def _normalize(obj):
    """把 float/int 的等价表示归一，便于比较语义而非字面。

    RPE 不同版本对同一数值有的写 135 有的写 135.0，这不算差异。
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    return obj


def _diff(a, b, path="", out=None, limit=40):
    """递归 diff，返回人类可读的差异行列表。"""
    if out is None:
        out = []
    if len(out) >= limit:
        return out
    if type(a) is not type(b) and not (
        isinstance(a, (int, float)) and isinstance(b, (int, float))
    ):
        out.append(f"{path or '/'}: 类型 {type(a).__name__} != {type(b).__name__}")
        return out
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}/{k}: 往返后多出该键")
            elif k not in b:
                out.append(f"{path}/{k}: 往返后丢失该键")
            else:
                _diff(a[k], b[k], f"{path}/{k}", out, limit)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: 列表长度 {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            _diff(x, y, f"{path}[{i}]", out, limit)
    elif isinstance(a, float) and isinstance(b, float):
        if abs(a - b) > 1e-9:
            out.append(f"{path}: {a!r} != {b!r}")
    elif a != b:
        out.append(f"{path}: {a!r} != {b!r}")
    return out


def _raw_json_of(name, path):
    if path.endswith(".pez"):
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".json")]
            names.sort(key=lambda n: (n.count("/"), len(n)))
            return json.loads(zf.read(names[0]).decode("utf-8-sig"))
    return load_json(path)


def _roundtrip_case(name, path):
    def run():
        raw = _raw_json_of(name, path)
        chart = Chart.from_json(raw)
        again = chart.to_json()
        diffs = _diff(_normalize(raw), _normalize(again))
        assert not diffs, f"{name} 往返不一致:\n  " + "\n  ".join(diffs[:15])

    return run


def _counts_case(name, path):
    """顺手统计一下样本规模，样本缺失时不会假装通过。"""

    def run():
        chart = load_chart(path)
        assert chart.BPMList, f"{name} 没有 BPMList"
        assert isinstance(chart.META.name, str)

    return run


# 动态注册到 runner：每个样本一个往返用例
for _name, _path in _SAMPLES:
    _fn = _roundtrip_case(_name, _path)
    _fn.__name__ = "roundtrip_" + _name.replace(".", "_").replace("-", "_")
    _fn.__doc__ = f"{_name} 的 load→to_json 必须无损"
    test(_fn)


@test
def fixture_samples_are_discovered():
    """哨兵：样本目录在就必须真的发现样本，避免测试静默空跑。"""
    if not os.path.isdir(_CHARTS):
        return  # 样本目录不存在 → 跳过
    assert _SAMPLES, f"{_CHARTS} 存在但没有发现样本"
    assert any(name.endswith(".pez") for name, _ in _SAMPLES), "缺少 .pez 样本"
