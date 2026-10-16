"""仓库自举：把 repo root 放到 ``sys.path`` 上，让 ``import rpechart`` 成立。

**为什么需要这个模块。** 一个项目里只要有脚本要「从自己的位置找到仓库根、再把根放进
``sys.path``」，这段逻辑就很容易被抄上几十遍，而且每次写法都略有出入：

* 有的用 ``os.path.dirname(os.path.dirname(os.path.abspath(__file__)))``；
* 有的干脆硬编码一个绝对路径（换台机器 / 换个盘符就失效）；
* 有的把根目录写成模块级常量，让数据文件的位置也跟着那个常量走。

同一件事写几十遍的代价不是「重复」本身，而是**没有唯一权威**：探测失败时的行为各不相同，
有的静默回退到错误目录，有的直接 ImportError。本模块把这件事收敛成一个函数，并且刻意
**fail-closed** —— 找不到仓库根目录就抛 :class:``RuntimeError``，绝不回退到任何硬编码路径。
回退比失败更糟：它会让脚本在错误的目录上「成功」运行，然后写出一堆看起来正常、其实放错
地方的文件。

用法（脚本头部的固定套路）::

    import sys
    from pathlib import Path

    # 引导：此刻 rpechart 还没被导入，只能用最朴素的方式先让它可导入
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from rpechart._bootstrap import ensure_importable

    ROOT = ensure_importable(__file__)

那一行 ``sys.path.insert`` 是**自举的鸡生蛋问题**：``rpechart._bootstrap`` 本身在 ``rpechart`` 包里，
所以必须先把 repo root 放上 ``sys.path`` 才能 import 到它。这一步之后，``ensure_importable`` 只是
把同一件事登记成幂等的、带检查的正式调用（并且返回 repo root 供脚本定位数据文件，
取代各自写死的绝对路径）。
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["find_repo_root", "ensure_importable"]

#: 判定「这个目录就是仓库根」的哨兵文件（相对路径）。
#: 用 ``rpechart/__init__.py`` 而不是 ``.git``：包目录一定跟着源码走，
#: 而从源码树里拷出来的子集也照样能找到自己（``.git`` 不一定在）。
_MARKER = Path("rpechart") / "__init__.py"


def find_repo_root(start=None) -> Path:
    """从 *start* 向上找含 ``rpechart/__init__.py`` 的目录。

    :param start: 起点，可以是目录也可以是文件；默认是**本文件**
        （``rpechart/_bootstrap.py``），所以默认调用总能找到自己所在的仓库。
    :returns: 仓库根目录的绝对路径。
    :raises RuntimeError: 一路走到文件系统根都没找到 —— **fail-closed**，
        绝不回退到任何硬编码路径。
    """
    here = Path(__file__).resolve() if start is None else Path(start).resolve()
    if here.is_file():
        here = here.parent
    for d in (here, *here.parents):
        if (d / _MARKER).is_file():
            return d
    raise RuntimeError(
        "找不到仓库根目录：从 "
        f"{here} 向上没有任何目录含 {_MARKER.as_posix()!r}。"
        "（_bootstrap 是 fail-closed 的，不会回退到硬编码路径；"
        "请确认 rpechart/ 没有被移出仓库，或显式传入 start。）"
    )


def ensure_importable(start=None) -> Path:
    """把仓库根目录插到 ``sys.path[0]`` 并返回它。

    幂等：已经在 ``sys.path`` 里就**不动**（不重复插入，也不调整它已有的位置）。
    这样脚本里那句自举用的 ``sys.path.insert`` 不会被重复执行成两条。
    """
    root = find_repo_root(start)
    entry = str(root)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return root
