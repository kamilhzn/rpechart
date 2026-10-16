"""`rpechart.web` —— 面向 AI 写谱的本地小软件（零依赖 HTTP 服务）。

设计目标（用户原话，m00648）：**AI 只输入参数就能生成谱面，同时让整个过程
可观察、可调整**。所以这里只有三件事：

1. **参数**：一个 `spec.json`（:class:`rpechart.gen.ChartSpec`）是唯一输入；
2. **观察**：页面右侧实时重渲的画面 + 校验报告 + 物量表 + 分线统计；
3. **调整**：改参数 → 重新生成 → 立刻看新画面（同一个 HTTP 请求里完成）。

不引入 Flask/FastAPI —— 只用标准库 :mod:`http.server`，这样「装好
Pillow 就能跑」，符合这个仓库「纯 Python 库」的边界。

启动::

    python -m rpechart.web --spec build/demo.spec.json --port 8770

然后浏览器打开 ``http://127.0.0.1:8770/``。
"""

from __future__ import annotations

from .server import WebApp, create_app, serve

__all__ = ["WebApp", "create_app", "serve"]
