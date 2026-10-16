"""极简测试运行器 —— 不依赖 pytest（本机没有装）。

用法::

    python tests/run_tests.py           # 跑全部
    python tests/run_tests.py beat      # 只跑名字含 beat 的

每个 ``test_*.py`` 里用 :func:`test` 装饰器注册用例；断言直接写 ``assert``。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# 引导：先让 rpechart 可导入，再由 _bootstrap 幂等地登记 repo root。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rpechart._bootstrap import ensure_importable  # noqa: E402

ROOT = ensure_importable(__file__)

_REGISTRY: list[tuple[str, object]] = []
_FAILURES: list[tuple[str, BaseException, str]] = []


def test(fn):
    """把一个函数注册为测试用例。"""
    _REGISTRY.append((fn.__name__, fn))
    return fn


def approx(a, b, tol=1e-6, msg=""):
    if abs(a - b) > tol:
        raise AssertionError(f"{a} != {b} (tol={tol}) {msg}".strip())


def raises(exc_type, fn, *a, **kw):
    """断言 fn 会抛 exc_type。"""
    try:
        fn(*a, **kw)
    except exc_type as e:
        return e
    except BaseException as e:  # noqa: BLE001
        raise AssertionError(f"期望 {exc_type.__name__}，实际 {type(e).__name__}: {e}") from e
    raise AssertionError(f"期望 {exc_type.__name__}，但没有抛异常")


def _load(path: str) -> None:
    name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    # 把模块名记进文件名里，这样 ``run_tests.py timing`` 能按文件筛选
    for i, (n, fn) in enumerate(_REGISTRY):
        if fn.__module__ == name:
            _REGISTRY[i] = (f"{name}::{n}", fn)


def main(argv: list[str]) -> int:
    # 关键：以 __main__ 运行时，把自身登记为 ``run_tests``，保证 registry 唯一
    sys.modules.setdefault("run_tests", sys.modules[__name__])
    pattern = argv[1] if len(argv) > 1 else ""
    files = sorted(
        f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py")
    )
    for f in files:
        _load(os.path.join(HERE, f))

    selected = [(n, fn) for n, fn in _REGISTRY if pattern in n]
    if not selected:
        print(f"没有匹配的用例 (pattern={pattern!r}, 共注册 {len(_REGISTRY)} 个)")
        return 1

    t0 = time.time()
    passed = 0
    for name, fn in selected:
        try:
            fn()
        except BaseException as e:  # noqa: BLE001
            _FAILURES.append((name, e, traceback.format_exc()))
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        else:
            passed += 1
            print(f"  ok    {name}")
    dt = time.time() - t0

    print("-" * 60)
    if _FAILURES:
        print(f"{len(_FAILURES)} 个失败 / {len(selected)} 个用例  ({dt:.2f}s)\n")
        for name, _e, tb in _FAILURES:
            print(f"=== {name} ===")
            print(tb)
        return 1
    print(f"全部通过: {passed} 个用例  ({dt:.2f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
