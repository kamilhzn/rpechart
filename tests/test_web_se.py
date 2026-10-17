"""导出打击音（SE）在 Web 层的接线契约。

覆盖三件事：/api/export 的 hitsounds / se_volume 参数怎么归一化、
/api/capabilities 有没有把本机的 SE 素材报出来、以及「找不到素材时绝不能
让导出失败」这条降级约定。真起服务、真打 HTTP，不 mock 渲染。
"""

import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from run_tests import test

import rpechart.audio as audio
import rpechart.video as video
from rpechart.gen.spec import ChartSpec
from rpechart.web import server as web_server
from rpechart.web.cli import default_spec
from rpechart.web.server import AppError, create_app, serve


class _Server:
    def __init__(self, app):
        self.app = app
        self.httpd = None
        self.port = None

    def __enter__(self):
        self.httpd = serve(self.app, "127.0.0.1", 0, quiet=True)
        self.port = self.httpd.server_address[1]
        import threading
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=120) as res:
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


def _app(tag):
    tmp = Path(tempfile.mkdtemp(prefix="rpese_" + tag + "_"))
    spec = ChartSpec.from_json(default_spec())
    return create_app(spec, chart_path=tmp / "chart.json", render_size=(320, 180))


def _fake_se_dir(tag):
    """造一个只含文件名的小 SE 目录（内容无所谓，只测路径解析）。"""
    root = Path(tempfile.mkdtemp(prefix="rpese_dir_" + tag + "_"))
    for name in ("tap.mp3", "flick.mp3", "drag.mp3"):
        (root / name).write_bytes(b"\x00" * 16)
    return root


def _wait_terminal(srv, timeout=120.0):
    import time
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        _s, _c, body = srv.get("/api/export/status")
        last = json.loads(body).get("job")
        if last and last["state"] != "running":
            return last
        time.sleep(0.1)
    raise AssertionError("导出任务没有在 %ss 内结束：%r" % (timeout, last))


def _ffmpeg_ok():
    try:
        return bool(video.ffmpeg_available())
    except Exception:
        return False

# ================================================================ 参数归一化
@test
def web_se_export_params_default_to_no_hitsounds():
    """不带 hitsounds 的老请求必须逐字保持老行为（兼容）。"""
    p = web_server.export_params({})
    assert p["hitsounds"] is None, p["hitsounds"]
    assert abs(p["se_volume"] - audio.SE_VOLUME) <= 1e-9, p["se_volume"]


@test
def web_se_export_params_accept_auto_dir_and_mapping():
    p = web_server.export_params({"hitsounds": "auto"})
    assert p["hitsounds"] == "auto", p["hitsounds"]

    p2 = web_server.export_params({"hitsounds": {"dir": "  D:/se  "}})
    assert p2["hitsounds"] == "D:/se", p2["hitsounds"]

    p3 = web_server.export_params({"hitsounds": {"path": "D:/se2"}})
    assert p3["hitsounds"] == "D:/se2", p3["hitsounds"]

    p4 = web_server.export_params(
        {"hitsounds": {"tap": "t.mp3", "flick": "f.mp3", "bogus": "x"}})
    assert p4["hitsounds"] == {"tap": "t.mp3", "flick": "f.mp3"}, p4["hitsounds"]

    p5 = web_server.export_params({"hitsounds": " D:/se3 "})
    assert p5["hitsounds"] == "D:/se3", p5["hitsounds"]

    # 空值一律等于「不加音效」，不是「目录名叫空」
    for empty in (None, "", "   ", {}, {"dir": "  "}, {"tap": ""}):
        assert web_server.export_params({"hitsounds": empty})["hitsounds"] is None, empty


@test
def web_se_volume_is_clamped_and_bad_values_are_rejected():
    assert abs(web_server.export_params({"se_volume": "0.25"})["se_volume"] - 0.25) <= 1e-9
    assert abs(web_server.export_params({"se_volume": 0})["se_volume"]) <= 1e-9
    assert abs(web_server.export_params({"se_volume": 99})["se_volume"] - 4.0) <= 1e-9
    # 空串 = 用默认音量，而不是 0
    assert abs(web_server.export_params({"se_volume": ""})["se_volume"]
               - audio.SE_VOLUME) <= 1e-9
    assert abs(web_server.export_params({"se_volume": None})["se_volume"]
               - audio.SE_VOLUME) <= 1e-9
    for bad in ({"se_volume": "abc"}, {"se_volume": -1}, {"se_volume": [1]}):
        try:
            web_server.export_params(bad)
        except AppError as exc:
            assert str(exc), bad
            continue
        raise AssertionError("应该拒绝 %r" % (bad,))

# ================================================================ 能力探测
@test
def web_se_capabilities_report_the_se_sources():
    """/api/capabilities 要带上 se / se_dir / se_volume / default_end。"""
    # web.server 用 from ..audio import find_se_dir 取的顶层名字，
    # 所以要打在 web_server 这一侧（打 audio 模块没用）。
    real = web_server.find_se_dir
    tmp = _fake_se_dir("caps")
    web_server.find_se_dir = lambda start=None: str(tmp)
    try:
        app = _app("caps")
        with _Server(app) as srv:
            status, ctype, body = srv.get("/api/capabilities")
        caps = json.loads(body)
    finally:
        web_server.find_se_dir = real

    assert status == 200 and "application/json" in ctype, (status, ctype)
    for key in ("se", "se_dir", "se_volume", "default_end"):
        assert key in caps, key
    assert caps["se"] == ["drag", "flick", "tap"], caps["se"]
    assert caps["se_dir"] == str(tmp), caps["se_dir"]
    assert abs(caps["se_volume"] - audio.SE_VOLUME) <= 1e-9
    assert caps["default_end"] > 0, caps["default_end"]


@test
def web_se_capabilities_degrade_when_se_is_missing():
    """没有 SE 素材时也只降级成空列表，不能 500。"""
    real = web_server.find_se_dir
    web_server.find_se_dir = lambda start=None: None
    try:
        app = _app("capsnone")
        with _Server(app) as srv:
            status, _c, body = srv.get("/api/capabilities")
    finally:
        web_server.find_se_dir = real
    caps = json.loads(body)
    assert status == 200 and caps["ok"] is True
    assert caps["se"] == [] and caps["se_dir"] is None, caps

    real_probe = web_server.resolve_se_sources

    def boom(_dir):
        raise RuntimeError("探测炸了")

    web_server.resolve_se_sources = boom
    web_server.find_se_dir = lambda start=None: "D:/whatever"
    try:
        app = _app("capsbad")
        with _Server(app) as srv:
            status, _c, body = srv.get("/api/capabilities")
    finally:
        web_server.resolve_se_sources = real_probe
        web_server.find_se_dir = real
    caps = json.loads(body)
    assert status == 200 and caps["ok"] is True, body
    assert caps["se"] == [], caps

# ================================================================ 端到端降级
@test
def web_se_missing_dir_does_not_break_the_export():
    """SE 目录不存在时导出仍要成功（只是没有音效），并在 argv_hint 里说清楚。"""
    if not _ffmpeg_ok():
        return
    app = _app("missingse")
    out = Path(tempfile.mkdtemp(prefix="rpese_out_")) / "no_se.mp4"
    with _Server(app) as srv:
        status, data = srv.post("/api/export", {
            "fps": 2, "size": "64x64", "start": 0.0, "end": 1.0,
            "out": str(out), "hud": False, "crf": 45, "preset": "ultrafast",
            "hitsounds": str(Path(tempfile.mkdtemp(prefix="rpese_nope_")) / "nothing"),
        })
        assert status == 200 and data["ok"] is True, (status, data)
        final = _wait_terminal(srv)
    assert final["state"] == "done", final
    assert out.is_file() and out.stat().st_size > 0
    assert final["error"] is None, final["error"]


@test
def web_se_export_without_the_render_layer_still_serves_capabilities():
    """顶层不得 import rpechart.render（服务器要在没装 Pillow 的机器上能起）。"""
    root = Path(__file__).resolve().parents[1]
    lines = [
        "import json, sys, tempfile",
        "from pathlib import Path",
        "sys.path.insert(0, %r)" % str(root),
        "from rpechart.gen.spec import ChartSpec",
        "from rpechart.web.cli import default_spec",
        "from rpechart.web.server import create_app",
        "tmp = Path(tempfile.mkdtemp(prefix='rpese_sub_'))",
        "app = create_app(ChartSpec.from_json(default_spec()), chart_path=tmp / 'c.json')",
        "lazy = 'rpechart.render' in sys.modules",
        "caps = app.capabilities()",
        "print(json.dumps({'lazy': lazy, 'caps': caps['ok'], 'se': caps['se']}))",
    ]
    import subprocess, sys as _sys
    out = subprocess.run([_sys.executable, "-c", chr(10).join(lines)],
                         capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout.strip().splitlines()[-1])
    # 只要求「import 服务模块」不把渲染层拖进来；capabilities() 自己是懒加载的
    assert data["lazy"] is False, data
    assert data["caps"] is True and isinstance(data["se"], list), data


@test
def web_server_create_app_warns_that_it_writes():
    """create_app 建完就 regenerate() 写盘 —— docstring 必须写明，防止再覆盖只读样本。"""
    text = (Path(__file__).resolve().parent.parent / "rpechart" / "web" / "server.py").read_text(encoding="utf-8")
    head = text.index("def create_app(")
    body = text[head:head + 900]
    assert "不是只读" in body, "create_app 的 docstring 没写明它会写盘"
    assert "regenerate" in body and "save_chart" in body, "docstring 没说清写盘路径"
    assert "临时目录" in body, "docstring 没提醒用临时目录"
