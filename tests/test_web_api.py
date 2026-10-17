"""rpechart.web 新增 API 的契约测试：能力探测 / 时间轴 / JPEG / 导出。

风格与 tests/test_web.py 一致：真起一个 ThreadingHTTPServer（端口 0），用 urllib
打请求 —— 端到端，不 mock。helper 是自己的一份副本，不 import test_web，避免
新老用例互相耦合。

不依赖 tests/fixtures 的任何内容，也不写死本机路径：谱面一律用
rpechart.web.cli.default_spec() 在临时目录里现场生成。导出用例用极小参数
（fps=2 / 64x64 / 2 秒）或「起一个大任务 → 立刻取消」，不真跑 60fps 全片。
"""

import io as _io
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from run_tests import test

import rpechart.video as video
from rpechart.gen.spec import ChartSpec
from rpechart.timing import Timing
from rpechart.web import server as web_server
from rpechart.web.cli import default_spec
from rpechart.web.server import AppError, create_app, serve

PNG_MAGIC = bytes([137, 80, 78, 71])
JPEG_MAGIC = bytes([0xFF, 0xD8, 0xFF])


# ---------------------------------------------------------------- helpers
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


def _tiny_spec(**over):
    data = dict(default_spec())
    data.update(over)
    return data


def _app(tag, **over):
    """建一个用临时目录当输出的 app（不污染仓库）。"""
    tmp = Path(tempfile.mkdtemp(prefix="rpeapi_" + tag + "_"))
    spec = ChartSpec.from_json(_tiny_spec(**over))
    return create_app(spec, chart_path=tmp / "chart.json", render_size=(320, 180))


def _get_json(srv, path):
    try:
        status, ctype, body = srv.get(path)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read()), ""
    return status, json.loads(body), ctype


def _close(a, b, tol=1e-6):
    return abs(float(a) - float(b)) <= tol


def _ffmpeg_ok():
    try:
        return bool(video.ffmpeg_available())
    except Exception:
        return False


def _wait_terminal(srv, timeout=90.0):
    """轮询 /api/export/status，直到 job 进入终态。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        _s, data, _c = _get_json(srv, "/api/export/status")
        last = data.get("job")
        if last and last["state"] != "running":
            return last
        time.sleep(0.1)
    raise AssertionError("导出任务没有在 %ss 内结束：%r" % (timeout, last))


def _wait_no_new_ffmpeg(before, timeout=15.0):
    """等本次导出新起的 ffmpeg 消失；返回超时后仍在跑的 PID 集合。"""
    deadline = time.time() + timeout
    leaked = set()
    while time.time() < deadline:
        leaked = web_server._ffmpeg_pids() - set(before)
        if not leaked:
            return set()
        time.sleep(0.1)
    return leaked


# ================================================================ 1 能力探测
@test
def web_api_capabilities_reports_the_machine():
    """/api/capabilities 字段齐全，且每项与独立探测一致。"""
    app = _app("caps")
    with _Server(app) as srv:
        status, ctype, body = srv.get("/api/capabilities")
        assert status == 200 and "application/json" in ctype, (status, ctype)
        caps = json.loads(body)

    for key in ("ok", "pillow", "ffmpeg", "ffmpeg_path", "assets", "assets_path",
                "renderer", "fallback_blocks", "sizes", "fps", "default_fps",
                "render_size", "chart_path", "spec_path", "python"):
        assert key in caps, key
    assert caps["ok"] is True
    assert caps["renderer"] == "rpechart.render"
    assert caps["sizes"] == [[960, 540], [1280, 720], [1920, 1080]]
    assert caps["fps"] == [30, 60]
    assert caps["default_fps"] == 60
    assert caps["render_size"] == [320, 180]
    assert caps["chart_path"] == str(app.chart_path)
    assert caps["spec_path"] is None
    assert caps["fallback_blocks"] is (not caps["assets"])
    assert caps["pillow"] is bool(importlib.util.find_spec("PIL"))
    assert caps["ffmpeg"] is bool(video.ffmpeg_available())
    if caps["ffmpeg"]:
        assert caps["ffmpeg_path"], caps
    else:
        assert caps["ffmpeg_path"] is None
    assert isinstance(caps["assets"], bool)
    assert caps["assets_path"] is None or isinstance(caps["assets_path"], str)
    assert caps["python"].count(".") == 2


@test
def web_api_capabilities_degrade_instead_of_500():
    """某一项探测炸了也只降级成 false/null，不能整个路由 500。"""
    app = _app("capsbad")
    real = web_server.ffmpeg_available

    def boom():
        raise RuntimeError("探测炸了")

    web_server.ffmpeg_available = boom
    try:
        with _Server(app) as srv:
            status, _ctype, body = srv.get("/api/capabilities")
        caps = json.loads(body)
        assert status == 200, body
        assert caps["ok"] is True
        assert caps["ffmpeg"] is False
        assert caps["ffmpeg_path"] is None
        assert caps["pillow"] is bool(importlib.util.find_spec("PIL"))
    finally:
        web_server.ffmpeg_available = real


# ================================================================ 2 时间轴
@test
def web_api_timeline_returns_notes_and_events():
    """秒数必须由库自己的 Timing 算；音符升序；事件是 [起, 止, 起值, 止值]。"""
    app = _app("timeline")
    chart = app.builder.chart
    timing = Timing(chart.BPMList)
    with _Server(app) as srv:
        status, ctype, body = srv.get("/api/timeline")
        tl = json.loads(body)

    assert status == 200 and "application/json" in ctype, (status, ctype)
    assert tl["ok"] is True
    assert _close(tl["bpm"], 150.0)
    assert tl["bpm_list"] == [{"bpm": 150.0, "start_beat": 0.0, "start_sec": 0.0}]

    assert _close(tl["duration_sec"], timing.beat_to_second(chart.duration_beats()))
    assert _close(tl["duration_sec"], 25.6)
    assert _close(tl["last_beat"], 64.0)
    assert _close(tl["content_beats"], float(chart.duration_beats()))
    assert _close(tl["n_beats"], tl["content_beats"] + 8.0)

    total_notes = sum(len(ln.notes) for ln in chart.judgeLineList)
    assert tl["note_count"] == total_notes == 112
    assert tl["truncated"] is False
    assert len(tl["lines"]) == len(chart.judgeLineList) == 2
    assert tl["lines"][0]["index"] == 0
    assert tl["lines"][0]["name"] == "主线"
    assert len(tl["lines"]) == 2 and tl["lines"][1]["name"] == "副线"
    # 物量分布看 builder 自己怎么排的，不写死（默认 spec 全在 0 号线上）
    want_per_line = [len(ln.notes) for ln in chart.judgeLineList]
    assert [ln["note_count"] for ln in tl["lines"]] == want_per_line
    assert [len(ln["notes"]) for ln in tl["lines"]] == want_per_line
    assert sum(want_per_line) == 112

    line0 = chart.judgeLineList[0]
    want_move = sorted(
        [[timing.beat_to_second(e.startTime), timing.beat_to_second(e.endTime),
          float(e.start), float(e.end)] for lay in line0.eventLayers
         for e in lay.moveXEvents],
        key=lambda r: (r[0], r[1]))
    got_move = tl["lines"][0]["events"]["move_x"]
    assert len(got_move) == len(want_move) and want_move
    for got, want in zip(got_move, want_move):
        assert len(got) == 4
        for g, w in zip(got, want):
            assert _close(g, w), (got, want)
    for kind in ("move_x", "move_y", "rotate", "alpha", "speed"):
        assert kind in tl["lines"][0]["events"], kind
        assert isinstance(tl["lines"][0]["events"][kind], list)

    note = tl["lines"][0]["notes"][0]
    for key in ("type", "start", "end", "start_beat", "end_beat", "x", "above",
                "speed", "fake", "hold", "visible_time"):
        assert key in note, (key, sorted(note))
    assert note["type"] in ("tap", "hold", "flick", "drag")
    assert isinstance(note["fake"], bool) and isinstance(note["hold"], bool)
    assert isinstance(note["above"], int)

    for line in tl["lines"]:
        starts = [n["start"] for n in line["notes"]]
        assert starts == sorted(starts), "音符必须按 start 升序"


@test
def web_api_timeline_clamps_max_notes_and_keeps_the_earliest():
    """max_notes 夹在 100..20000；超上限保留最早的并置 truncated。"""
    app = _app("trunc")
    chart = app.builder.chart
    timing = Timing(chart.BPMList)
    all_starts = sorted(timing.beat_to_second(n.startTime)
                        for ln in chart.judgeLineList for n in ln.notes)
    assert len(all_starts) > 100

    with _Server(app) as srv:
        _s, tl, _c = _get_json(srv, "/api/timeline?max_notes=100")
        _s2, tl_min, _c2 = _get_json(srv, "/api/timeline?max_notes=5")
        _s3, tl_max, _c3 = _get_json(srv, "/api/timeline?max_notes=999999")
        _s4, tl_200, _c4 = _get_json(srv, "/api/timeline?max_notes=200")

    kept = [n["start"] for line in tl["lines"] for n in line["notes"]]
    assert len(kept) == 100
    assert tl["truncated"] is True
    assert tl["note_count"] == len(all_starts)
    assert max(kept) <= all_starts[99] + 1e-9, "超上限时必须保留最早的"

    assert sum(len(l["notes"]) for l in tl_min["lines"]) == 100
    assert tl_min["truncated"] is True          # 5 被夹到 100
    assert sum(len(l["notes"]) for l in tl_max["lines"]) == len(all_starts)
    assert tl_max["truncated"] is False         # 999999 被夹到 20000
    assert tl_200["truncated"] is False
    assert sum(len(l["notes"]) for l in tl_200["lines"]) == len(all_starts)


@test
def web_api_timeline_caps_events_per_track():
    """每条线每类事件最多 EVENTS_PER_TRACK_MAX 个，超了截断并置 truncated。"""
    app = _app("evcap")
    real = web_server.EVENTS_PER_TRACK_MAX
    web_server.EVENTS_PER_TRACK_MAX = 1
    try:
        with _Server(app) as srv:
            _s, tl, _c = _get_json(srv, "/api/timeline")
    finally:
        web_server.EVENTS_PER_TRACK_MAX = real

    assert tl["truncated"] is True
    caps = {k: len(v) for k, v in tl["lines"][0]["events"].items()}
    assert caps["move_x"] == 1, caps
    assert all(v <= 1 for v in caps.values()), caps

    chart = app.builder.chart
    timing = Timing(chart.BPMList)
    earliest = min(timing.beat_to_second(e.startTime)
                   for lay in chart.judgeLineList[0].eventLayers
                   for e in lay.moveXEvents)
    assert _close(tl["lines"][0]["events"]["move_x"][0][0], earliest)


@test
def web_api_timeline_follows_a_new_spec():
    """POST /api/spec 之后，时间轴必须反映新谱面。"""
    app = _app("timelinepost")
    with _Server(app) as srv:
        status, data = srv.post("/api/spec", _tiny_spec(
            name="换过的", bpm=200.0, last_beat=32.0, duration=12.0,
            lines=[{"name": "唯一线", "y": 0.0}]))
        assert status == 200 and data["ok"] is True, data
        _s, tl, _c = _get_json(srv, "/api/timeline")
    assert _close(tl["bpm"], 200.0)
    assert len(tl["lines"]) == 1
    assert tl["lines"][0]["name"] == "唯一线"
    assert tl["truncated"] is False


@test
def web_api_timeline_needs_neither_pillow_nor_the_render_package():
    """时间轴只准用 model / timing / beat：不得顺带 import PIL 或 rpechart.render。"""
    root = Path(__file__).resolve().parents[1]
    lines = [
        "import json, sys, tempfile",
        "from pathlib import Path",
        "sys.path.insert(0, %r)" % str(root),
        "from rpechart.gen.spec import ChartSpec",
        "from rpechart.web.cli import default_spec",
        "from rpechart.web.server import create_app",
        "tmp = Path(tempfile.mkdtemp(prefix='rpeapi_sub_'))",
        "app = create_app(ChartSpec.from_json(default_spec()), chart_path=tmp / 'c.json')",
        "tl = app.timeline(4000)",
        "assert tl['ok'] is True and tl['note_count'] > 0",
        "bad = [m for m in ('PIL', 'rpechart.render') if m in sys.modules]",
        "out = {'bad': bad, 'notes': tl['note_count'], 'duration': tl['duration_sec']}",
        "print(json.dumps(out))",
    ]
    out = subprocess.run([sys.executable, "-c", chr(10).join(lines)],
                         capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout.strip().splitlines()[-1])
    assert data["bad"] == [], data["bad"]
    assert data["notes"] == 112
    assert _close(data["duration"], 25.6)


# ================================================================ 3/4 JPEG
@test
def web_api_frame_jpg_is_jpeg_and_clamps_the_size():
    from PIL import Image

    app = _app("frame")
    with _Server(app) as srv:
        status, ctype, body = srv.get("/api/frame.jpg?t=1.0&w=320&h=180")
        assert status == 200 and ctype == "image/jpeg", ctype
        assert body[:3] == JPEG_MAGIC
        assert Image.open(_io.BytesIO(body)).size == (320, 180)

        _s, _c, small = srv.get("/api/frame.jpg?t=1.0&w=10&h=10")
        assert Image.open(_io.BytesIO(small)).size == (64, 64)
        _s, _c, big = srv.get("/api/frame.jpg?t=1.0&w=99999&h=99999")
        assert Image.open(_io.BytesIO(big)).size == (3840, 2160)

        # 未知参数忽略
        _s, _c, ignored = srv.get("/api/frame.jpg?t=1.0&w=200&h=120&bogus=1&nope=x")
        assert ignored[:3] == JPEG_MAGIC

        # hud 的真假写法都接受，且假的写法确实不画 HUD
        for hud in ("0", "false", "no", "False"):
            _s, _c, one = srv.get("/api/frame.jpg?t=0.5&w=96&h=64&hud=" + hud)
            assert one[:3] == JPEG_MAGIC, hud
        for hud in ("1", "true", "yes"):
            _s, _c, one = srv.get("/api/frame.jpg?t=0.5&w=96&h=64&hud=" + hud)
            assert one[:3] == JPEG_MAGIC, hud
        _s, _c, no_hud = srv.get("/api/frame.jpg?t=0.5&w=320&h=180&hud=0")
        _s, _c, yes_hud = srv.get("/api/frame.jpg?t=0.5&w=320&h=180&hud=1")
        assert len(no_hud) != len(yes_hud), "hud=0 与 hud=1 应该是不同的画面"

        # q 夹在 30..95：1 变成 30、999 变成 95，低质量一定更小
        _s, _c, q30 = srv.get("/api/frame.jpg?t=2.0&w=320&h=180&q=1")
        _s, _c, q95 = srv.get("/api/frame.jpg?t=2.0&w=320&h=180&q=999")
        assert len(q30) < len(q95), (len(q30), len(q95))


@test
def web_api_filmstrip_is_one_wide_image():
    from PIL import Image

    app = _app("strip")
    with _Server(app) as srv:
        status, ctype, body = srv.get("/api/filmstrip.jpg?w=160&h=90&n=8")
        assert status == 200 and ctype == "image/jpeg", ctype
        assert body[:3] == JPEG_MAGIC
        assert Image.open(_io.BytesIO(body)).size == (8 * 160, 90)

        _s, _c, default = srv.get("/api/filmstrip.jpg")
        assert Image.open(_io.BytesIO(default)).size == (12 * 160, 90)

        _s, _c, low = srv.get("/api/filmstrip.jpg?w=10&h=10&n=1")
        assert Image.open(_io.BytesIO(low)).size == (4 * 64, 64)
        _s, _c, high = srv.get("/api/filmstrip.jpg?w=99999&h=99999&n=999")
        assert Image.open(_io.BytesIO(high)).size == (64 * 640, 360)


@test
def web_api_filmstrip_samples_the_middle_of_each_slot():
    """第 i 张取 t = (i + 0.5) / n * duration（duration 为 0/None 时取 0）。"""
    def close_all(got, want):
        assert len(got) == len(want), (got, want)
        for g, w in zip(got, want):
            assert _close(g, w, 1e-9), (got, want)

    close_all(web_server._filmstrip_times(4, 8.0), [1.0, 3.0, 5.0, 7.0])
    close_all(web_server._filmstrip_times(2, 25.6), [6.4, 19.2])
    assert web_server._filmstrip_times(12, 0.0) == [0.0] * 12
    assert web_server._filmstrip_times(4, None) == [0.0] * 4
    assert web_server._filmstrip_times(0, 25.6) == []


@test
def web_api_preview_is_still_exactly_png():
    """回归：/api/preview 必须仍是恰好 image/png、PNG 魔数、尺寸等于请求值。"""
    from PIL import Image

    app = _app("png")
    with _Server(app) as srv:
        status, ctype, body = srv.get("/api/preview?t=1.0&w=320&h=180")
        assert status == 200 and ctype == "image/png", ctype
        assert body[:4] == PNG_MAGIC
        assert Image.open(_io.BytesIO(body)).size == (320, 180)
        _s, ctype_jpg, jpg = srv.get("/api/frame.jpg?t=1.0&w=320&h=180")
        assert ctype_jpg == "image/jpeg"
        assert jpg[:3] == JPEG_MAGIC


@test
def web_api_concurrent_previews_all_come_back_intact():
    """并发预览（render_lock）不能互相踩：每张图的尺寸都必须正确。"""
    from PIL import Image

    app = _app("concurrent")
    sizes = [(320, 180), (160, 90), (96, 64), (240, 135)]
    errors = []
    with _Server(app) as srv:
        def hit(i):
            w, h = sizes[i % len(sizes)]
            try:
                status, ctype, body = srv.get(
                    "/api/preview?t=%s&w=%d&h=%d" % (i * 0.25, w, h))
                if status != 200 or ctype != "image/png" or body[:4] != PNG_MAGIC:
                    errors.append((i, status, ctype))
                    return
                if Image.open(_io.BytesIO(body)).size != (w, h):
                    errors.append((i, "size"))
            except Exception as exc:
                errors.append((i, repr(exc)))

        threads = [threading.Thread(target=hit, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(180)
    assert not errors, errors


# ================================================================ 5 导出
@test
def web_api_export_params_normalize_and_clamp():
    """导出参数：缺省即默认、crf 夹取、坏参数抛 AppError。"""
    p = web_server.export_params({})
    assert p["fps"] == 60.0 and p["size"] == (1280, 720)
    assert p["start"] == 0.0 and p["end"] is None and p["hud"] is True
    assert p["audio"] is None and p["out"] == "build/web_export.mp4"
    assert p["crf"] == 18 and p["preset"] == "medium"

    p2 = web_server.export_params({"fps": "30", "size": "640*360", "crf": 99,
                                   "preset": "ultrafast", "hud": "no",
                                   "start": 1, "end": 2, "out": "a.mp4"})
    assert p2["fps"] == 30.0 and p2["size"] == (640, 360)
    assert p2["crf"] == 51 and p2["preset"] == "ultrafast"
    assert p2["hud"] is False and p2["start"] == 1.0 and p2["end"] == 2.0
    assert web_server.export_params({"crf": 0})["crf"] == 0
    # crf 下界夹到 0；fps 上界夹到 1000，下界夹到 1
    assert web_server.export_params({"crf": -5})["crf"] == 0
    assert web_server.export_params({"fps": 5000})["fps"] == 1000.0
    assert web_server.export_params({"fps": 0.5})["fps"] == 1.0
    assert web_server.export_params({"fps": 999})["fps"] == 999.0
    # 大小写 / 空格无关，preset 归一化
    assert web_server.export_params({"preset": "MEDIUM "})["preset"] == "medium"

    for bad in ({"size": "abc"}, {"size": "0x0"}, {"size": "10"}, {"size": "-4x4"},
                {"fps": 0}, {"fps": "xx"}, {"fps": -1},
                {"start": -1.0}, {"start": 5.0, "end": 1.0},
                {"end": "zz"}, {"audio": "no_such_file_anything.mp3"},
                {"preset": "bogus"}, {"preset": ""}, {"out": ""},
                {"out": "   "}, {"crf": "abc"}):
        try:
            web_server.export_params(bad)
        except AppError as exc:
            assert str(exc), bad
            continue
        raise AssertionError("应该拒绝 %r" % (bad,))


@test
def web_api_export_without_ffmpeg_is_a_400():
    """缺 ffmpeg 时 400，并且错误里要引导装 imageio-ffmpeg。"""
    app = _app("noffmpeg")
    real = web_server.ffmpeg_available
    web_server.ffmpeg_available = lambda: False
    try:
        with _Server(app) as srv:
            status, data = srv.post("/api/export",
                                    {"fps": 1, "size": "64x64", "start": 0, "end": 1})
    finally:
        web_server.ffmpeg_available = real
    assert status == 400, (status, data)
    assert data["ok"] is False
    assert "ffmpeg" in data["error"]
    assert "imageio-ffmpeg" in data["error"]


@test
def web_api_export_rejects_bad_parameters():
    """参数不合法一律 400，并且不比 ffmpeg 探测更晚发生。"""
    app = _app("badparams")
    with _Server(app) as srv:
        for payload in ({"size": "abc"}, {"size": "10"}, {"size": "0x0"},
                        {"fps": 0}, {"fps": "xx"}, {"fps": -1},
                        {"start": 5.0, "end": 1.0}, {"start": -3.0},
                        {"audio": "no_such_file_here.mp3"},
                        {"preset": "bogus"}, {"out": ""}):
            status, data = srv.post("/api/export", payload)
            assert status == 400, (payload, status, data)
            assert data["ok"] is False and data["error"], (payload, data)

        status, data = srv.post("/api/export", {})
        assert status in (200, 400, 409), (status, data)


@test
def web_api_export_status_and_cancel_without_a_job():
    app = _app("nojob")
    with _Server(app) as srv:
        status, _ctype, body = srv.get("/api/export/status")
        assert status == 200
        assert json.loads(body) == {"ok": True, "job": None}
        status, data = srv.post("/api/export/cancel", {})
        assert status == 200, (status, data)
        assert data == {"ok": True, "cancelled": False}


@test
def web_api_export_409_then_cancel_stops_cleanly():
    """有任务在跑 → 409；取消后状态 cancelled、半成品删掉、没有孤儿 ffmpeg。"""
    if not _ffmpeg_ok():
        return                       # 没 ffmpeg 的机器跑不了真实编码
    app = _app("export")
    out = Path(tempfile.mkdtemp(prefix="rpeapi_out_")) / "out.mp4"
    before = web_server._ffmpeg_pids()
    payload = {"fps": 60, "size": "640x360", "start": 0.0, "end": None,
               "hud": False, "out": str(out), "crf": 28, "preset": "ultrafast"}
    with _Server(app) as srv:
        status, data = srv.post("/api/export", payload)
        assert status == 200, (status, data)
        assert data["ok"] is True
        job = data["job"]
        assert job["state"] == "running", job
        assert job["out"] == str(out)
        assert job["chart_rev"] and job["argv_hint"]
        assert job["started_at"] > 0 and job["progress"] >= 0.0
        assert job["bytes"] is None and job["error"] is None
        assert job["total"] > 0

        status2, data2 = srv.post("/api/export", payload)
        assert status2 == 409, (status2, data2)
        assert data2["ok"] is False
        assert "运行" in data2["error"]

        _s, live, _c = _get_json(srv, "/api/export/status")
        assert live["ok"] is True and live["job"]["id"] == job["id"]
        assert live["job"]["state"] in ("running", "done", "failed", "cancelled")

        status3, data3 = srv.post("/api/export/cancel", {})
        assert status3 == 200 and data3 == {"ok": True, "cancelled": True}, data3

        final = _wait_terminal(srv, timeout=120.0)
        assert final["state"] == "cancelled", final
        assert final["bytes"] is None
        assert final["elapsed"] >= 0.0
        assert final["error"], "取消也要给个可读的原因"
        assert not out.exists(), "取消后不能留下半成品 mp4"

        # 任务已经结束，现在可以再接新任务（只是参数会被拒，不能是 409）
        status4, data4 = srv.post("/api/export", {"size": "abc"})
        assert status4 == 400, (status4, data4)

    leaked = _wait_no_new_ffmpeg(before, timeout=15.0)
    assert leaked == set(), "取消后仍有 ffmpeg 在跑：%r" % (sorted(leaked),)


@test
def web_api_export_tiny_run_completes_and_reports_bytes():
    """极小参数（fps=2 / 64x64 / 2 秒）真跑一次，done 时给 bytes。"""
    if not _ffmpeg_ok():
        return
    app = _app("tinyexport")
    out = Path(tempfile.mkdtemp(prefix="rpeapi_tiny_")) / "tiny.mp4"
    with _Server(app) as srv:
        status, data = srv.post("/api/export", {
            "fps": 2, "size": "64x64", "start": 0.0, "end": 2.0,
            "out": str(out), "hud": False, "crf": 40, "preset": "ultrafast"})
        assert status == 200 and data["ok"] is True, (status, data)
        final = _wait_terminal(srv, timeout=240.0)
        if final["state"] != "done":
            raise AssertionError("极小导出应该成功：%r" % (final,))
        _s, after, _c = _get_json(srv, "/api/export/status")
    assert after["job"]["state"] == "done"
    assert final["bytes"] and final["bytes"] > 0
    assert final["done"] == final["total"] == 4
    assert final["progress"] >= 0.999
    assert final["error"] is None
    assert final["out"] == str(out)
    assert out.is_file() and out.stat().st_size == final["bytes"]
    assert final["elapsed"] >= 0.0


# ================================================================ 路由回归
@test
def web_api_unknown_routes_and_methods_stay_json_404():
    """未注册路由仍是 JSON 404；POST-only 路由用 GET 打也是 404。"""
    app = _app("404api")
    with _Server(app) as srv:
        for path in ("/api/nope", "/api/capabilities/x", "/api/frame.png",
                     "/api/export", "/api/export/cancel"):
            try:
                srv.get(path)
            except urllib.error.HTTPError as exc:
                assert exc.code == 404, (path, exc.code)
                payload = json.loads(exc.read())
                assert payload["ok"] is False and payload["error"], (path, payload)
            else:
                raise AssertionError(path + " 应该 404")
        status, data = srv.post("/api/capabilities", {})
        assert status == 404 and data["ok"] is False
        status, data = srv.post("/api/timeline", {})
        assert status == 404 and data["ok"] is False


@test
def web_api_bad_numeric_params_are_rejected_with_400():
    """数值参数写坏了要给 400（不是 500），并且是 JSON。"""
    app = _app("badnum")
    with _Server(app) as srv:
        for path in ("/api/timeline?max_notes=abc",
                     "/api/frame.jpg?t=abc",
                     "/api/filmstrip.jpg?n=xyz",
                     "/api/preview?w=abc",
                     "/api/preview?t=abc"):
            try:
                srv.get(path)
            except urllib.error.HTTPError as exc:
                assert exc.code == 400, (path, exc.code)
                payload = json.loads(exc.read())
                assert payload["ok"] is False and payload["error"], (path, payload)
            else:
                raise AssertionError(path + " 应该 400")


# ================================================================ 导出探测成本
@test
def web_api_ffmpeg_pid_probe_is_cheap():
    """_ffmpeg_pids() 必须走带 IMAGENAME 过滤的列举，不许退回全量 tasklist。

    实测背景：全量 tasklist /FO CSV /NH 在本机是 24.7s（403 行），带
    IMAGENAME eq <exe> 过滤只要 0.3-0.7s。取消导出要探测两次，退回全量会把
    「点取消」拖到 18s 以上，用户会以为界面卡死。

    这里**不**断言墙钟时间：同一份过滤代码在本机实测过 0.32s 到 13.7s 的
    抖动（机器忙时 tasklist 自己就会卡十几秒），时间断言会变成随机失败
    —— 早期版本正是这么写的，出现过 2.27s / 13.73s 的假失败。
    改成钉住实现本身：命令行必须带 /FI + IMAGENAME eq，且候选名只有一个。
    """
    calls = []
    real_run = subprocess.run

    def spy(cmd, *a, **kw):
        calls.append(list(cmd))
        return real_run(cmd, *a, **kw)

    saved = list(web_server._FFMPEG_NAME_CACHE)
    web_server._FFMPEG_NAME_CACHE.clear()
    web_server.subprocess.run = spy
    try:
        got = web_server._ffmpeg_pids()
    finally:
        web_server.subprocess.run = real_run
        web_server._FFMPEG_NAME_CACHE[:] = saved

    assert isinstance(got, set), type(got)
    assert all(isinstance(pid, int) for pid in got), got
    assert calls, "没有发出任何进程列举命令"
    flat = " ".join(" ".join(cmd) for cmd in calls)
    assert "/FI" in calls[0] and "IMAGENAME eq " in flat, (
        "探测命令 %r 没用 IMAGENAME 过滤，会退回 24s 的全量 tasklist" % (calls,))
    assert len(calls) == 1, (
        "一次探测发了 %d 条列举命令，候选名太多：%r" % (len(calls), calls))


@test
def web_api_ffmpeg_name_probe_stays_single_and_cached():
    """exe 名候选必须只有 1 个真实名字：每多一个候选就多一次 tasklist。"""
    names = web_server._ffmpeg_image_names()
    assert len(names) == 1, names
    assert "ffmpeg" in names[0].lower(), names
    assert web_server._ffmpeg_image_names() == names
