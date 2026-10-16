"""`rpechart.web` 的 HTTP 契约测试。

Web UI 是「AI 写参数 → 立刻看结果」的那一层，所以这里测的是**页面依赖的
每个路由的返回值**，不是 HTML 长什么样。真的起一个 `ThreadingHTTPServer`
（端口 0 让内核挑），用 `urllib` 打请求 —— 端到端，不 mock。
"""

import contextlib
import io as _io
import json
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

from run_tests import test

from rpechart.gen.assemble import build
from rpechart.gen.spec import ChartSpec
from rpechart.web.cli import build_parser, default_spec, main, parse_size
from rpechart.web.server import create_app, serve

CR = chr(13)
LF = chr(10)
PNG_MAGIC = bytes([137, 80, 78, 71])


class _Server:
    """上下文管理器：起服务 → 给 base url → 关服务。"""

    def __init__(self, app):
        self.app = app
        self.httpd = None
        self.port = None

    def __enter__(self):
        self.httpd = serve(self.app, "127.0.0.1", 0, quiet=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=60) as res:
            return res.status, res.headers.get("Content-Type", ""), res.read()

    def post(self, path, payload):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url(path), data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                return res.status, json.loads(res.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


def _tiny_spec(**over):
    data = dict(default_spec())
    data.update(over)
    return data


def _app(tag, **over):
    """建一个用临时目录当输出的 app（不污染仓库）。"""
    tmp = Path(tempfile.mkdtemp(prefix="rpeweb_" + tag + "_"))
    spec = ChartSpec.from_json(_tiny_spec(**over))
    return create_app(spec, chart_path=tmp / "chart.json",
                      render_size=(320, 180))


@test
def web_parse_size_accepts_x_and_star():
    """`--size` 接受 1920x1080 与 640*360，拒绝坏输入。"""
    assert parse_size("1920x1080") == (1920, 1080)
    assert parse_size("640*360") == (640, 360)
    assert parse_size(" 1280X720 ") == (1280, 720)
    for bad in ("1920", "abc", "0x100", "-2x10", "10x-1", "x", ""):
        try:
            parse_size(bad)
        except ValueError:
            continue
        raise AssertionError("应该拒绝 " + repr(bad))


@test
def web_default_spec_builds_without_errors():
    """内置模板必须能直接生成、且零校验错误（页面一打开就有东西看）。"""
    spec = ChartSpec.from_json(default_spec())
    assert spec.problems() == []
    report = build(spec).validate(song_duration=spec.duration)
    assert report.errors == [], [str(i) for i in report.errors]


@test
def web_create_app_writes_a_chart_and_reports_stats():
    """`create_app` 一步到位：生成 → 校验 → 落盘 → 有统计。"""
    app = _app("create")
    assert app.error is None, app.error
    assert app.chart_path.is_file()
    s = app.summary()
    assert s["lines"] == 2
    assert s["notes"] > 0
    assert s["report"]["counts"]["error"] == 0
    assert len(s["per_line"]) == 2
    assert s["duration"] > 0


@test
def web_chart_is_written_in_the_rpe_serializer_style():
    """写出的 chart.json 必须是 RPE 风格（3 空格缩进、key : value、LF）。"""
    app = _app("style")
    text = app.chart_text()
    assert CR not in text, "必须是 LF，不能有 CR"
    assert text.endswith("}" + LF)
    assert chr(34) + "name" + chr(34) + " : " in text, "冒号两侧要有空格"
    body_lines = text.split(LF)
    assert body_lines[1].startswith("   "), "顶层缩进 3 空格"
    assert not body_lines[1].startswith("    "), "不能是 4 空格缩进"


@test
def web_routes_serve_the_page_assets_and_health():
    """/ 、/static/* 、/api/health 、/api/spec 都要能取到。"""
    app = _app("routes")
    with _Server(app) as srv:
        status, ctype, body = srv.get("/")
        assert status == 200 and "text/html" in ctype
        assert b"<title>" in body

        for rel, expect in (("/static/app.css", "text/css"),
                            ("/static/app.js", "javascript")):
            status, ctype, body = srv.get(rel)
            assert status == 200 and expect in ctype and body

        status, ctype, body = srv.get("/api/health")
        assert status == 200 and json.loads(body)["ok"] is True

        status, ctype, body = srv.get("/api/spec")
        spec = json.loads(body)["spec"]
        assert spec["name"] == app.spec.name
        assert spec["bpm"] == app.spec.bpm


@test
def web_preview_returns_a_png_frame():
    """`/api/preview` 返回真 PNG。"""
    app = _app("preview")
    with _Server(app) as srv:
        status, ctype, body = srv.get("/api/preview?t=1.0&w=320&h=180")
        assert status == 200 and ctype == "image/png"
        assert body[:4] == PNG_MAGIC
        assert len(body) > 500
        from PIL import Image
        img = Image.open(_io.BytesIO(body))
        assert img.size == (320, 180), img.size


@test
def web_post_spec_regenerates_and_returns_new_stats():
    """POST /api/spec 换一份 spec，返回的统计必须反映新谱面。"""
    app = _app("post")
    with _Server(app) as srv:
        status, data = srv.post("/api/spec", _tiny_spec(
            name="换过的", bpm=200.0, last_beat=32.0, duration=12.0,
            lines=[{"name": "唯一线", "y": 0.0}]))
        assert status == 200 and data["ok"] is True, data
        assert data["name"] == "换过的"
        assert data["lines"] == 1
        assert data["bpm"] == 200.0
        assert app.spec.name == "换过的"
        assert "换过的" in app.chart_text()


@test
def web_post_broken_spec_keeps_the_previous_chart():
    """坏 spec 必须报错、且**不破坏**上一份能用的谱面。"""
    app = _app("broken")
    before = app.chart_text()
    with _Server(app) as srv:
        status, data = srv.post("/api/spec", _tiny_spec(
            name="坏的", bpm=0.0, last_beat=0.0, sections=[]))
        assert status == 400, (status, data)
        assert data["ok"] is False
        assert data["error"]
    assert app.chart_text() == before, "坏 spec 不能覆盖上一份谱面"


@test
def web_unknown_route_returns_json_404():
    """未注册的路由给 JSON 404（前端靠 error 字段显示）。"""
    app = _app("404")
    with _Server(app) as srv:
        try:
            srv.get("/api/nope")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            payload = json.loads(exc.read())
            assert payload["ok"] is False and "nope" in payload["error"]
            return
        raise AssertionError("404 应该抛 HTTPError")


@test
def web_static_path_traversal_is_refused():
    """`/static/../server.py` 之类必须被挡掉。"""
    app = _app("traversal")
    with _Server(app) as srv:
        for bad in ("/static/../server.py", "/static/..%2fserver.py",
                    "/static/../../io.py"):
            try:
                srv.get(bad)
            except urllib.error.HTTPError as exc:
                assert exc.code in (403, 404), (bad, exc.code)
                continue
            raise AssertionError(bad + " 应该被拒绝")


@test
def web_cli_print_spec_emits_a_usable_template():
    """`--print-spec` 打出来的 JSON 必须能直接喂回 ChartSpec。"""
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(["--print-spec"])
    assert code == 0
    spec = ChartSpec.from_json(json.loads(buf.getvalue()))
    assert spec.problems() == []
    opts = {a.dest for a in build_parser()._actions}
    for want in ("spec", "host", "port", "size", "print_spec"):
        assert want in opts, want
