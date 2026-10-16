"""本地 Web UI 的 HTTP 服务（标准库实现，无第三方依赖）。

路由
----

======================  ==================================================
``GET  /``              单页界面（``static/index.html``）
``GET  /api/spec``      当前 spec（JSON）
``POST /api/spec``      覆盖当前 spec，**并立刻重新生成**，返回统计
``GET  /api/chart``     当前生成出的 chart.json（RPE 风格原文）
``GET  /api/preview``   渲染一帧 PNG（``?t=3.125&w=960&h=540&hud=1``）
``GET  /api/stats``     物量表 + 校验报告 + 分线统计
``GET  /api/health``    探活
======================  ==================================================

状态全在 :class:`WebApp` 上：``spec`` / ``builder`` / ``chart_path`` /
``report``。每次 POST 都走同一条流水线：``ChartSpec → build → validate →
save_chart（RPE 风格）`` —— 也就是命令行那条链路，只是搬进了进程里。
"""

from __future__ import annotations

import io
import json
import threading
import traceback
import typing as _t
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..gen.assemble import build
from ..gen.spec import ChartSpec
from ..io import save_chart

__all__ = ["WebApp", "create_app", "serve", "AppError"]


class AppError(Exception):
    """可以直接回给浏览器的错误（参数不合法、缺 Pillow…）。"""


def _jsonable(value: _t.Any) -> _t.Any:
    """把 report / Counter / Path 之类转成可 JSON 化的普通对象。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "to_json"):
        try:
            return _jsonable(value.to_json())
        except Exception:                                     # pragma: no cover
            pass
    if hasattr(value, "__dict__"):
        return {k: _jsonable(v) for k, v in vars(value).items()
                if not k.startswith("_")}
    return str(value)


def _report_payload(report) -> dict:
    """校验报告 → 前端能直接渲染的结构。"""
    issues = []
    for issue in getattr(report, "issues", ()) or ():
        issues.append({
            "severity": str(getattr(issue, "severity", "")),
            "code": str(getattr(issue, "code", "")),
            "message": str(getattr(issue, "message", "")),
            "line": getattr(issue, "line", None),
            "note_index": getattr(issue, "note_index", None),
            "time": str(getattr(issue, "time", "") or ""),
        })
    counts = {"error": 0, "warning": 0, "caution": 0}
    for it in issues:
        key = it["severity"].strip().lower()
        if key in counts:
            counts[key] += 1
    return {"issues": issues, "counts": counts, "total": len(issues)}


@dataclass
class WebApp:
    """一个「spec ↔ 谱面 ↔ 画面」的会话。

    非线程安全的部分（生成、渲染）都在 :attr:`lock` 里跑，因为
    :class:`ThreadingHTTPServer` 会并发处理请求。
    """

    spec: ChartSpec
    chart_path: Path
    render_size: tuple = (960, 540)
    spec_path: _t.Optional[Path] = None
    builder: _t.Any = None
    report: _t.Any = None
    error: _t.Optional[str] = None
    lock: _t.Any = field(default_factory=threading.RLock)
    _renderer: _t.Any = None

    # ------------------------------------------------------------ 生成
    def regenerate(self, spec: _t.Optional[ChartSpec] = None) -> dict:
        """跑一遍 ``build → validate → save_chart``，返回统计 dict。

        失败时把异常写进 :attr:`error` 并保留上一份可用的谱面。
        """
        with self.lock:
            if spec is not None:
                self.spec = spec
            try:
                cb = build(self.spec)
                report = cb.validate(
                    song_duration=(self.spec.duration or None))
                self.chart_path.parent.mkdir(parents=True, exist_ok=True)
                save_chart(cb.chart, self.chart_path)
                self.builder = cb
                self.report = report
                self.error = None
                self._renderer = None          # 谱面变了，渲染器要重建
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                return {"ok": False, "error": self.error,
                        "traceback": traceback.format_exc(limit=6)}
            return {"ok": True, **self.summary()}

    def summary(self) -> dict:
        """物量 / 分线 / 校验计数 —— 页面上的「观察」部分。"""
        from collections import Counter

        chart = self.builder.chart if self.builder is not None else None
        kinds: Counter = Counter()
        per_line: list = []
        if chart is not None:
            for i, ln in enumerate(chart.judgeLineList):
                kinds_i: Counter = Counter()
                for n in ln.notes:
                    kinds[n.type_name] += 1
                    kinds_i[n.type_name] += 1
                per_line.append({"index": i,
                                 "name": str(getattr(ln, "Name", "")),
                                 "notes": len(ln.notes),
                                 "kinds": dict(kinds_i)})
        timing = getattr(self.builder, "timing", None) if self.builder else None
        duration = None
        if timing is not None and chart is not None:
            try:
                duration = float(timing.beat_to_second(
                    chart.duration_beats()))
            except Exception:
                duration = None
        return {
            "name": str(getattr(self.spec, "name", "")),
            "bpm": float(getattr(self.spec, "bpm", 0.0) or 0.0),
            "lines": len(chart.judgeLineList) if chart is not None else 0,
            "notes": sum(kinds.values()),
            "kinds": dict(kinds),
            "per_line": per_line,
            "duration": duration,
            "last_beat": float(getattr(self.spec, "last_beat", 0.0) or 0.0),
            "chart_path": str(self.chart_path),
            "report": _report_payload(self.report),
            "error": self.error,
        }

    # ------------------------------------------------------------ 渲染
    def renderer(self):
        """懒建渲染器（没装 Pillow 时抛 :class:`AppError`）。"""
        with self.lock:
            if self._renderer is None:
                try:
                    from ..render.renderer import Renderer
                except Exception as exc:
                    raise AppError(
                        f"渲染子系统不可用（通常是没装 Pillow）：{exc}") from exc
                self._renderer = Renderer(self.chart_path, theme=None)
            return self._renderer

    def preview_png(self, t: float, w: int, h: int, *, hud: bool = True) -> bytes:
        """渲染一帧，返回 PNG 字节。"""
        r = self.renderer()
        try:
            img = r.frame(t, size=(w, h), hud=hud)
        except Exception as exc:
            raise AppError(f"渲染失败：{exc}") from exc
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def chart_text(self) -> str:
        """当前谱面的 RPE 风格原文（就是 ``save_chart`` 写出的字节）。"""
        return self.chart_path.read_text(encoding="utf-8")

    def spec_json(self) -> dict:
        return _jsonable(self.spec.to_json())


# ---------------------------------------------------------------- HTTP
_STATIC = Path(__file__).resolve().parent / "static"
_MIME = {".html": "text/html; charset=utf-8",
         ".css": "text/css; charset=utf-8",
         ".js": "application/javascript; charset=utf-8",
         ".png": "image/png",
         ".json": "application/json; charset=utf-8"}


def _handler_for(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "rpechart-web/0.1"

        def log_message(self, fmt, *args):        # 静音（测试里会刷屏）
            pass

        # ---- helpers
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, code: int, payload: _t.Any):
            body = json.dumps(_jsonable(payload), ensure_ascii=False).encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def _error(self, code: int, message: str, **extra):
            self._send_json(code, {"ok": False, "error": message, **extra})

        # ---- routes
        def do_GET(self):                                     # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            try:
                if path == "/":
                    return self._serve_static("index.html")
                if path == "/api/health":
                    return self._send_json(200, {"ok": True})
                if path == "/api/spec":
                    return self._send_json(200, {"ok": True, "spec": app.spec_json()})
                if path == "/api/stats":
                    return self._send_json(200, {"ok": True, **app.summary()})
                if path == "/api/chart":
                    return self._send(200, app.chart_text().encode("utf-8"),
                                      "application/json; charset=utf-8")
                if path == "/api/preview":
                    t = float(query.get("t", ["0"])[0])
                    w = int(query.get("w", [app.render_size[0]])[0])
                    h = int(query.get("h", [app.render_size[1]])[0])
                    hud = query.get("hud", ["1"])[0] not in ("0", "false", "no")
                    w = max(64, min(3840, w))
                    h = max(64, min(2160, h))
                    return self._send(200, app.preview_png(t, w, h, hud=hud),
                                      "image/png")
                if path.startswith("/static/"):
                    return self._serve_static(path[len("/static/"):])
                return self._error(404, f"没有这个路由：{path}")
            except AppError as exc:
                return self._error(400, str(exc))
            except Exception as exc:                       # pragma: no cover
                return self._error(500, f"{type(exc).__name__}: {exc}",
                                   traceback=traceback.format_exc(limit=6))

        def do_POST(self):                                    # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            if path != "/api/spec":
                return self._error(404, f"没有这个路由：{path}")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception as exc:
                return self._error(400, f"请求体不是合法 JSON：{exc}")
            if isinstance(data, dict) and "spec" in data:
                data = data["spec"]
            try:
                spec = ChartSpec.from_json(data)
            except Exception as exc:
                return self._error(400, f"spec 不合法：{exc}")
            result = app.regenerate(spec)
            return self._send_json(200 if result.get("ok") else 400, result)

        # ---- static
        def _serve_static(self, rel: str):
            target = (_STATIC / rel).resolve()
            if _STATIC.resolve() not in target.parents and target != _STATIC.resolve():
                return self._error(403, "越界路径")
            if not target.is_file():
                return self._error(404, f"没有这个文件：{rel}")
            ctype = _MIME.get(target.suffix.lower(), "application/octet-stream")
            return self._send(200, target.read_bytes(), ctype)

    return Handler


def create_app(spec, *, chart_path=None, render_size=(960, 540),
               spec_path=None) -> WebApp:
    """建一个 :class:`WebApp`（``spec`` 可以是 ChartSpec / dict / 路径 / JSON 文本）。"""
    if not isinstance(spec, ChartSpec):
        spec = ChartSpec.from_json(spec)
    if chart_path is None:
        base = Path(spec_path).parent if spec_path else Path.cwd()
        chart_path = base / "web_chart.json"
    app = WebApp(spec=spec, chart_path=Path(chart_path),
                 render_size=tuple(render_size),
                 spec_path=Path(spec_path) if spec_path else None)
    app.regenerate()
    return app


def serve(app: WebApp, host: str = "127.0.0.1", port: int = 8770,
          *, quiet: bool = False):
    """起 HTTP 服务（阻塞）。返回 :class:`ThreadingHTTPServer` 以便测试关掉。"""
    httpd = ThreadingHTTPServer((host, port), _handler_for(app))
    if not quiet:
        print(f"rpechart web → http://{host}:{httpd.server_address[1]}/")
    return httpd
