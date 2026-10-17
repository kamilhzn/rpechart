"""本地 Web UI 的 HTTP 服务（标准库实现，无第三方依赖）。

路由
----

==========================  =====================================================
GET  /                      单页界面（static/index.html）
GET  /api/spec              当前 spec（JSON）
POST /api/spec              覆盖当前 spec，并立刻重新生成，返回统计
GET  /api/chart             当前生成出的 chart.json（RPE 风格原文）
GET  /api/preview           渲染一帧 PNG（?t=3.125&w=960&h=540&hud=1）
GET  /api/stats             物量表 + 校验报告 + 分线统计
GET  /api/health            探活
GET  /api/capabilities      本机能力探测（Pillow / ffmpeg / 官方贴图 / 渲染尺寸）
GET  /api/timeline          时间轴数据（音符 + 常用事件，秒数已算好）
GET  /api/frame.jpg         渲染一帧 JPEG（?t=&w=&h=&hud=&q=）
GET  /api/filmstrip.jpg     一条横向缩略图胶片（?w=&h=&n=&hud=&q=&start=&end=）
GET  /api/se/<kind>         一个打击音素材（tap/flick/drag，供预览播放）
POST /api/export            起一个后台导出任务（线程）
GET  /api/export/status     查询导出任务状态（没任务也 200）
POST /api/export/cancel     取消正在跑的导出任务
==========================  =====================================================

设计要点
--------

* 零第三方依赖：本模块顶层只 import 标准库与包内纯 Python 模块。Pillow 一律
  在函数内懒加载（渲染器那次是包内既有做法，这里照抄）。
* 两把锁：lock 保护 spec/chart/任务状态这些短临界区；render_lock 专门串行化
  「帧渲染 + 编码」。原因是 Renderer 实例内部有可变缓存（_bg_cache），而旧代码
  在锁外调 r.frame(...)，两个并发 /api/preview 会共用同一个 Renderer。
  锁里只放纯 CPU 的渲染与编码，绝不放文件 IO。
* 导出线程不抢 render_lock：导出自己 Renderer(chart_path) 新建一份，
  否则一次长导出会把所有预览请求卡死。
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import platform
import subprocess
import threading
import time
import traceback
import typing as _t
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..gen.assemble import build
from ..gen.spec import ChartSpec
from ..io import save_chart
from ..model import (
    EVENT_ALPHA,
    EVENT_MOVE_X,
    EVENT_MOVE_Y,
    EVENT_ROTATE,
    EVENT_SPEED,
)
from ..audio import SE_VOLUME, find_se_dir, resolve_se_sources
from ..timing import Timing
from ..video import ffmpeg_available

__all__ = [
    "WebApp",
    "create_app",
    "serve",
    "AppError",
    "ExportCancelled",
    "export_params",
    "EVENTS_PER_TRACK_MAX",
    "EXPORT_DEFAULTS",
]


class AppError(Exception):
    """可以直接回给浏览器的错误（参数不合法、缺 Pillow…）。"""


class ExportCancelled(Exception):
    """导出被取消：由 progress 回调抛出，沿着 encode_frames 冒到导出线程。"""


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


# ---------------------------------------------------------------- 常量
#: /api/preview 与 /api/frame.jpg 共用的取景框夹取范围
FRAME_MIN_W, FRAME_MAX_W = 64, 3840
FRAME_MIN_H, FRAME_MAX_H = 64, 2160

#: /api/filmstrip.jpg 的单张缩略图夹取范围与默认值
STRIP_MIN_W, STRIP_MAX_W = 64, 640
STRIP_MIN_H, STRIP_MAX_H = 64, 360
STRIP_DEFAULT_W, STRIP_DEFAULT_H = 160, 90
STRIP_MIN_N, STRIP_MAX_N = 4, 64
STRIP_DEFAULT_N = 12

#: JPEG 质量夹取范围
QUALITY_MIN, QUALITY_MAX, QUALITY_DEFAULT = 30, 95, 80

#: /api/timeline 的音符总量夹取范围
NOTES_MIN, NOTES_MAX, NOTES_DEFAULT = 100, 20000, 4000

#: 每条判定线、每一类事件最多回多少条（前端画得过来就行，别把几百 MB JSON 推过去）
EVENTS_PER_TRACK_MAX = 512

#: 与 rpechart.render.timeline.RenderModel 的 pad_beats 默认值保持一致。
#: 这里不 import 渲染子包（时间轴要在没装 Pillow 的机器上工作），所以自己补。
RENDER_PAD_BEATS = 8.0

#: 导出可选的 x264 preset（其余一律拒绝，免得拼出非法命令行）
EXPORT_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast",
                  "medium", "slow", "slower", "veryslow")

#: 并发导出的拒绝理由：路由层用它把 AppError 翻成 HTTP 409
BUSY_EXPORT_ERROR = "已有导出任务在运行"

#: 导出默认参数（与前端约定的字段名一一对应）
EXPORT_DEFAULTS = {
    "fps": 60.0,
    "size": (1280, 720),
    "start": 0.0,
    "end": None,
    "hud": True,
    "audio": None,
    "out": "build/web_export.mp4",
    "crf": 18,
    "preset": "medium",
    "hitsounds": None,
    "se_volume": SE_VOLUME,
}


# ---------------------------------------------------------------- 参数解析
def _as_float(value) -> float:
    if isinstance(value, bool):
        raise ValueError("布尔不是数字")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("空字符串不是数字")
        return float(text)
    return float(value)


def _as_int(value) -> int:
    if isinstance(value, bool):
        raise ValueError("布尔不是整数")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("空字符串不是整数")
        return int(float(text)) if ("." in text or "e" in text.lower()) else int(text)
    return int(value)


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in ("", "0", "false", "no", "off", "none")


def _parse_size(value, default=(1280, 720)) -> tuple:
    """把 '1280x720' / [1280, 720] 解析成 (w, h)；都给不出就抛 AppError。"""
    if value is None:
        return tuple(default)
    if isinstance(value, (tuple, list)) and len(value) == 2:
        try:
            w, h = _as_int(value[0]), _as_int(value[1])
        except (TypeError, ValueError) as exc:
            raise AppError(f"尺寸不合法：{value!r}") from exc
    else:
        text = str(value).strip().lower().replace("*", "x").replace("×", "x")
        parts = text.split("x")
        if len(parts) != 2:
            raise AppError(f"尺寸要写成 宽x高：{value!r}")
        try:
            w, h = _as_int(parts[0]), _as_int(parts[1])
        except (TypeError, ValueError) as exc:
            raise AppError(f"尺寸里不是整数：{value!r}") from exc
    if w <= 0 or h <= 0:
        raise AppError(f"尺寸必须为正：{value!r}")
    return w, h


def _query_num(query, key, default, cast):
    """从 GET 查询串取一个数字；写坏了抛 AppError（→ 400，而不是 500）。"""
    raw = query.get(key)
    if not raw:
        return default
    try:
        return cast(raw[0])
    except (TypeError, ValueError) as exc:
        raise AppError(f"参数 {key} 不是合法数字：{raw[0]!r}") from exc


def _parse_hitsounds(value) -> _t.Any:
    """打击音参数：None/"" → None；{"dir": ...} → 字符串路径；{类型: 路径} 映射原样收。

    真正的可用性（目录在不在、tap.mp3 有没有）交给渲染线程去判——找不到素材
    只会让导出退回无音效的老路径，不该在这里拦成 400。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        raw_dir = value.get("dir", value.get("path", value.get("se_dir")))
        if raw_dir is not None and str(raw_dir).strip():
            return str(raw_dir).strip()
        mapping = {kind: p for kind, p in value.items()
                   if kind in ("tap", "flick", "drag") and p}
        return mapping or None
    text = str(value).strip()
    if not text:
        return None
    if text.lower() in ("auto", "default", "1", "true", "yes", "on"):
        return "auto"
    return text


def _hitsounds_label(value) -> str:
    """给 argv_hint 用的一行人类可读描述。"""
    if value == "auto":
        return "自动查找官方 SE"
    if isinstance(value, dict):
        return "映射 " + "/".join(sorted(value))
    return str(value)


def export_params(mapping) -> dict:
    """把 POST /api/export 的请求体规范化成一份可直接下锅的参数。

    缺省即默认；crf 夹在 0..51、fps 夹在 1..1000；其余非法值一律抛
    AppError（路由层会翻成 400）。纯函数，不碰 app 状态。
    """
    data = mapping if isinstance(mapping, dict) else {}
    params = dict(EXPORT_DEFAULTS)

    try:
        fps = _as_float(data.get("fps", EXPORT_DEFAULTS["fps"]))
    except (TypeError, ValueError) as exc:
        raise AppError(f"fps 不是合法数字：{data.get('fps')!r}") from exc
    if fps <= 0:
        raise AppError(f"fps 必须大于 0：{fps!r}")
    params["fps"] = float(max(1.0, min(1000.0, fps)))

    params["size"] = _parse_size(data.get("size"), EXPORT_DEFAULTS["size"])

    try:
        start = _as_float(data.get("start", EXPORT_DEFAULTS["start"]))
    except (TypeError, ValueError) as exc:
        raise AppError(f"start 不是合法数字：{data.get('start')!r}") from exc
    if start < 0:
        raise AppError(f"start 不能是负数：{start!r}")
    params["start"] = float(start)

    raw_end = data.get("end", None)
    if raw_end is None or (isinstance(raw_end, str) and not raw_end.strip()):
        params["end"] = None
    else:
        try:
            end = _as_float(raw_end)
        except (TypeError, ValueError) as exc:
            raise AppError(f"end 不是合法数字：{raw_end!r}") from exc
        if end <= start:
            raise AppError(f"end({end!r}) 必须大于 start({start!r})")
        params["end"] = float(end)

    params["hud"] = _as_bool(data.get("hud", EXPORT_DEFAULTS["hud"]))

    audio = data.get("audio", None)
    if audio is None or (isinstance(audio, str) and not audio.strip()):
        params["audio"] = None
    else:
        path = Path(str(audio))
        if not path.is_file():
            raise AppError(f"音频文件不存在：{audio!r}")
        params["audio"] = str(path)

    raw_out = data.get("out", None)
    if raw_out is None:
        params["out"] = EXPORT_DEFAULTS["out"]
    else:
        text = str(raw_out).strip()
        if not text:
            raise AppError("out 不能是空字符串")
        params["out"] = text

    try:
        crf = _as_int(data.get("crf", EXPORT_DEFAULTS["crf"]))
    except (TypeError, ValueError) as exc:
        raise AppError(f"crf 不是合法整数：{data.get('crf')!r}") from exc
    params["crf"] = int(max(0, min(51, crf)))

    preset = str(data.get("preset", EXPORT_DEFAULTS["preset"]) or "").strip().lower()
    if preset not in EXPORT_PRESETS:
        raise AppError(f"preset 不认识：{preset!r}（可选 {'/'.join(EXPORT_PRESETS)}）")
    params["preset"] = preset

    params["hitsounds"] = _parse_hitsounds(data.get("hitsounds", None))

    raw_vol = data.get("se_volume", EXPORT_DEFAULTS["se_volume"])
    if raw_vol is None or raw_vol == "":
        raw_vol = EXPORT_DEFAULTS["se_volume"]
    try:
        volume = _as_float(raw_vol)
    except (TypeError, ValueError) as exc:
        raise AppError(f"se_volume 不是合法数字：{raw_vol!r}") from exc
    if volume < 0:
        raise AppError(f"se_volume 不能是负数：{volume!r}")
    params["se_volume"] = float(min(4.0, volume))
    return params


# ---------------------------------------------------------------- 小组件
def _filmstrip_times(n: int, duration_sec, start=None, end=None) -> list:
    """胶片第 i 张的取帧时刻：默认 t = (i + 0.5) / n * duration。

    start / end 给定时只采样这个时间窗口（放大后按视野重取胶片，
    避免把整条低分辨率缩略图硬拉长）。
    """
    count = int(n)
    if count <= 0:
        return []
    try:
        total = float(duration_sec or 0.0)
    except (TypeError, ValueError):
        total = 0.0
    if not math.isfinite(total) or total <= 0.0:
        return [0.0] * count
    if start is None and end is None:
        return [(i + 0.5) / count * total for i in range(count)]
    try:
        lo = float(start or 0.0)
        hi = float(end if end is not None else total)
    except (TypeError, ValueError):
        return [0.0] * count
    lo = max(0.0, min(total, lo))
    hi = max(0.0, min(total, hi))
    if hi <= lo:
        return [lo] * count
    span = hi - lo
    return [lo + (i + 0.5) / count * span for i in range(count)]


_FFMPEG_NAME_CACHE: list = []


def _ffmpeg_image_names() -> list:
    """导出实际会用的 ffmpeg 进程名候选（tasklist 的 IMAGENAME，不含路径）。

    名字必须从 ffmpeg_path("ffmpeg") 推，不能写死 "ffmpeg.exe"：
    imageio-ffmpeg 的捆绑版本机叫 ffmpeg-win-x86_64-v7.1.exe。取不到也保留
    "ffmpeg.exe" / "ffmpeg" 兜底（此时机器通常根本没有可用的 ffmpeg）。
    """
    if _FFMPEG_NAME_CACHE:
        return list(_FFMPEG_NAME_CACHE)
    names = []
    try:
        from ..audio import ffmpeg_path

        exe = ffmpeg_path("ffmpeg")
    except Exception:
        exe = None
    if exe:
        try:
            base = Path(exe).name
        except Exception:
            base = ""
        if base:
            names.append(base)
    if not names:
        # 推不出真实文件名（这台机器没有可用 ffmpeg，导出必然 400）时给个常规名。
        names.append("ffmpeg.exe")
    # 只查这一个名字。每多一个候选就是一次 tasklist：实测 1 个候选 0.40s、
    # 2 个 1.05s、3 个 1.66s，而取消导出要反复探测，所以候选数必须克制。
    # 导出用的就是 ffmpeg_path("ffmpeg") 返回的那个可执行文件，
    # 它的 basename 正是运行中进程的 IMAGENAME，不需要再撒网。
    _FFMPEG_NAME_CACHE[:] = names[:1]
    return list(_FFMPEG_NAME_CACHE)
    _FFMPEG_NAME_CACHE[:] = names
    return list(names)


def _parse_tasklist_pids(text: str) -> set:
    """从 tasklist /FO CSV /NH 的输出里挑出 ffmpeg 行的 PID。"""
    found = set()
    for line in text.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and "ffmpeg" in parts[0].lower():
            try:
                found.add(int(parts[1]))
            except ValueError:
                pass
    return found


def _ffmpeg_pids() -> set:
    """当前在跑的 ffmpeg 进程 PID 集合（只用标准库，不依赖 psutil）。

    性能是这里硬要求：**必须**带 IMAGENAME 过滤。实测本机全量列举
    （tasklist /FO CSV /NH，403 行）要 24.7s，过滤式只要 0.4s；取消导出要探测
    两次，用全量列举会把「点取消」拖到 18s 以上，用户会以为界面卡死。
    没有匹配时 tasklist 回一句「信息: 没有运行的任务匹配指定标准。」，
    按 CSV 解析自然被忽略（parts[0] 不含 ffmpeg）。

    Windows 依次查几个精确名字（常数级查询，不用通配以避开 2s+ 抖动），
    其他平台退到 ps -A -o pid=,comm=，再退到扫 /proc/*/comm。
    任何异常都返回空集合：探测失败不能让取消路径崩掉。
    """
    candidates = _ffmpeg_image_names()
    try:
        if os.name == "nt":
            found = set()
            for name in candidates:
                raw = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq " + name,
                     "/FO", "CSV", "/NH"],
                    capture_output=True, timeout=20).stdout
                found |= _parse_tasklist_pids(raw.decode("gbk", "replace"))
            return found
        raw = subprocess.run(["ps", "-A", "-o", "pid=,comm="],
                             capture_output=True, timeout=20).stdout
        found = set()
        for line in raw.decode("utf-8", "replace").splitlines():
            bits = line.split(None, 1)
            if len(bits) == 2 and "ffmpeg" in bits[1].lower():
                try:
                    found.add(int(bits[0]))
                except ValueError:
                    pass
        if found:
            return found
        for entry in Path("/proc").iterdir():          # Linux 兜底
            if not entry.name.isdigit():
                continue
            try:
                comm = (entry / "comm").read_text(
                    encoding="utf-8", errors="replace").strip()
            except Exception:
                continue
            if "ffmpeg" in comm.lower():
                found.add(int(entry.name))
        return found
    except Exception:
        return set()


def _wait_ffmpeg_exit(before, timeout: float = 20.0) -> set:
    """等本次导出新起的 ffmpeg 退出；返回超时后仍活着的 PID 集合。

    取消走的是「progress 回调抛异常」这条路：异常会从 encode_frames 里冒出来，
    但子进程要等 stdin 关闭（EOF）之后才会收尾 —— 这期间它**仍然占着 mp4 文件
    句柄**，此时 unlink 在 Windows 上会 PermissionError（文件还在），于是留下
    半成品。所以必须先确认进程真的没了，再删文件。
    """
    known = set(before)
    deadline = time.time() + max(0.0, float(timeout))
    leaked = _ffmpeg_pids() - known
    while leaked and time.time() < deadline:
        time.sleep(0.1)
        leaked = _ffmpeg_pids() - known
    return leaked


_EXTENDED_EVENT_FIELDS = ("scaleXEvents", "scaleYEvents", "colorEvents",
                          "textEvents", "paintEvents", "gifEvents",
                          "inclineEvents")
_COMMON_EVENT_FIELDS = (EVENT_MOVE_X, EVENT_MOVE_Y, EVENT_ROTATE,
                        EVENT_ALPHA, EVENT_SPEED)
_EVENT_BUCKETS = (("move_x", EVENT_MOVE_X), ("move_y", EVENT_MOVE_Y),
                  ("rotate", EVENT_ROTATE), ("alpha", EVENT_ALPHA),
                  ("speed", EVENT_SPEED))


def _content_beats(chart) -> float:
    """谱面内容真正结束在多少拍。

    语义与 rpechart.render.timeline.RenderModel._max_beat 一致（音符 + 5 类常用
    事件 + 7 类扩展事件）。之所以自己算一份：/api/timeline 必须在没装 Pillow
    的机器上也能用，所以不能 import 渲染子包。
    """
    best = 0.0
    for ln in chart.judgeLineList or ():
        for note in ln.notes or ():
            best = max(best, float(note.startTime.frac), float(note.endTime.frac))
        for layer in ln.eventLayers or ():
            for field_name in _COMMON_EVENT_FIELDS:
                for ev in getattr(layer, field_name, None) or ():
                    best = max(best, float(ev.endTime.frac))
        ext = getattr(ln, "extended", None)
        if ext is not None:
            for field_name in _EXTENDED_EVENT_FIELDS:
                for ev in getattr(ext, field_name, None) or ():
                    best = max(best, float(ev.endTime.frac))
    return float(best)


@dataclass
class ExportJob:
    """一次导出任务的状态。所有字段只在 WebApp.lock 里读写。"""

    id: str
    out: str
    argv_hint: str
    chart_rev: str
    total: int
    state: str = "running"
    done: int = 0
    progress: float = 0.0
    bytes: _t.Optional[int] = None
    error: _t.Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: _t.Optional[float] = None
    cancel: _t.Any = field(default_factory=threading.Event)

    def snapshot(self) -> dict:
        ended = self.finished_at if self.finished_at is not None else time.time()
        return {
            "id": self.id,
            "state": self.state,
            "progress": float(self.progress),
            "done": int(self.done),
            "total": int(self.total),
            "out": self.out,
            "bytes": int(self.bytes) if (self.state == "done" and self.bytes) else None,
            "error": self.error,
            "started_at": float(self.started_at),
            "elapsed": max(0.0, float(ended - self.started_at)),
            "argv_hint": self.argv_hint,
            "chart_rev": self.chart_rev,
        }

@dataclass
class WebApp:
    """一个「spec ↔ 谱面 ↔ 画面」的会话。

    lock 保护 spec / chart / 导出任务状态这些短临界区；render_lock 只串行化
    「渲染一帧 + 编码」，因为 Renderer 实例内部带可变缓存（_bg_cache），
    同一个对象不能被两个线程同时 frame()。
    """

    spec: ChartSpec
    chart_path: Path
    render_size: tuple = (960, 540)
    spec_path: _t.Optional[Path] = None
    builder: _t.Any = None
    report: _t.Any = None
    error: _t.Optional[str] = None
    lock: _t.Any = field(default_factory=threading.RLock)
    render_lock: _t.Any = field(default_factory=threading.Lock)
    _renderer: _t.Any = None
    _export_job: _t.Any = None
    _export_seq: int = 0

    # ------------------------------------------------------------ 生成
    def regenerate(self, spec: _t.Optional[ChartSpec] = None) -> dict:
        """跑一遍 build → validate → save_chart，返回统计 dict。

        失败时把异常写进 self.error 并保留上一份可用的谱面。
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

    # ------------------------------------------------------------ 能力探测
    def capabilities(self) -> dict:
        """本机能力探测。任何一项炸了都只降级，绝不让整个路由 500。"""
        import importlib.util

        pillow = False
        try:
            pillow = importlib.util.find_spec("PIL") is not None
        except Exception:
            pillow = False

        ffmpeg = False
        try:
            ffmpeg = bool(ffmpeg_available())
        except Exception:
            ffmpeg = False

        ffmpeg_exe = None
        if ffmpeg:
            try:
                from ..audio import ffmpeg_path

                ffmpeg_exe = ffmpeg_path("ffmpeg") or None
            except Exception:
                ffmpeg_exe = None

        assets = False
        assets_path = None
        try:
            from ..render import assets as _assets      # 顶层不 import：它带 PIL

            assets = bool(_assets.has_official_assets())
            found = _assets.find_resources()
            assets_path = str(found) if found is not None else None
        except Exception:
            assets = False
            assets_path = None

        se = {}
        se_dir = None
        try:
            se_dir = find_se_dir()
            if se_dir:
                se = resolve_se_sources(se_dir)
        except Exception:                                # pragma: no cover - 防御
            se = {}
            se_dir = None

        try:
            python_version = platform.python_version()
        except Exception:                                # pragma: no cover
            python_version = "unknown"

        return {
            "ok": True,
            "pillow": pillow,
            "ffmpeg": ffmpeg,
            "ffmpeg_path": ffmpeg_exe,
            "assets": assets,
            "assets_path": assets_path,
            "renderer": "rpechart.render",
            "fallback_blocks": not assets,
            "se": sorted(se),
            "se_dir": str(se_dir) if se_dir else None,
            "se_volume": float(SE_VOLUME),
            "default_end": self.content_seconds(),
            "sizes": [[960, 540], [1280, 720], [1920, 1080]],
            "fps": [30, 60],
            "default_fps": 60,
            "render_size": [int(self.render_size[0]), int(self.render_size[1])],
            "chart_path": str(self.chart_path),
            "spec_path": str(self.spec_path) if self.spec_path else None,
            "python": python_version,
        }

    # ------------------------------------------------------------ 时间轴
    def _chart_and_spec(self):
        with self.lock:
            chart = self.builder.chart if self.builder is not None else None
            return chart, self.spec

    def _timing_and_duration(self, chart):
        """(Timing, 内容秒数) —— 秒数算不出来时给 None，不抛。"""
        if chart is None:
            return None, None
        try:
            timing = Timing(chart.BPMList)
        except Exception:
            return None, None
        try:
            duration = float(timing.beat_to_second(chart.duration_beats()))
        except Exception:
            duration = None
        return timing, duration

    def content_seconds(self):
        """谱面内容长度（秒）—— 与时间轴同源，供导出算总帧数。"""
        chart, _spec = self._chart_and_spec()
        if chart is None:
            return None
        try:
            timing = Timing(chart.BPMList)
            return float(timing.beat_to_second(_content_beats(chart)))
        except Exception:
            return None

    def timeline(self, max_notes: int = NOTES_DEFAULT) -> dict:
        """时间轴数据：BPM 表 + 每条判定线的音符与常用事件（秒数已算好）。

        不 import 任何渲染子包 —— 没装 Pillow 的机器也要能拿到时间轴。
        """
        chart, spec = self._chart_and_spec()
        try:
            limit = int(max_notes)
        except (TypeError, ValueError) as exc:
            raise AppError(f"max_notes 不是整数：{max_notes!r}") from exc
        limit = max(NOTES_MIN, min(NOTES_MAX, limit))

        timing, duration = self._timing_and_duration(chart)
        bpm = float(getattr(spec, "bpm", 0.0) or 0.0)
        last_beat = float(getattr(spec, "last_beat", 0.0) or 0.0)

        def _sec(beat):
            if timing is None:
                return None
            try:
                return float(timing.beat_to_second(beat))
            except Exception:
                return None

        if chart is None:
            return {"ok": True, "bpm": bpm, "bpm_list": [], "duration_sec": None,
                    "last_beat": last_beat, "content_beats": 0.0,
                    "n_beats": RENDER_PAD_BEATS, "note_count": 0,
                    "truncated": False, "lines": []}

        bpm_list = []
        for item in chart.BPMList or ():
            start_beat = float(getattr(item.startTime, "frac", 0.0) or 0.0)
            start_sec = _sec(item.startTime)
            bpm_list.append({
                "bpm": float(getattr(item, "bpm", 0.0) or 0.0),
                "start_beat": start_beat,
                "start_sec": start_sec if start_sec is not None else 0.0,
            })
        if bpm_list:
            bpm = bpm_list[0]["bpm"]

        # ---- 每条线的音符：先全量建好，再统一按「最早」截断
        rows = []            # (start_sec, line_index, note_index)
        events_truncated = False
        lines = []
        for i, ln in enumerate(chart.judgeLineList or ()):
            notes = list(ln.notes or ())
            payload = []
            for j, note in enumerate(notes):
                start = _sec(note.startTime)
                end = _sec(note.endTime)
                payload.append({
                    "type": str(getattr(note, "type_name", "")) or "tap",
                    "start": start if start is not None else 0.0,
                    "end": end if end is not None else (start if start else 0.0),
                    "start_beat": float(getattr(note.startTime, "frac", 0.0) or 0.0),
                    "end_beat": float(getattr(note.endTime, "frac", 0.0) or 0.0),
                    "x": float(getattr(note, "positionX", 0.0) or 0.0),
                    "above": int(getattr(note, "above", 0) or 0),
                    "speed": float(getattr(note, "speed", 1.0) or 0.0),
                    "fake": bool(getattr(note, "isFake", False)),
                    "hold": bool(getattr(note, "is_hold", False)),
                    "visible_time": float(getattr(note, "visibleTime", 0.0) or 0.0),
                })
                rows.append((payload[-1]["start"], i, j))

            buckets = {}
            for name, field_name in _EVENT_BUCKETS:
                found = []
                for layer in ln.eventLayers or ():
                    for ev in getattr(layer, field_name, None) or ():
                        s = _sec(ev.startTime)
                        e = _sec(ev.endTime)
                        if s is None:
                            continue
                        found.append([s, e if e is not None else s,
                                      float(getattr(ev, "start", 0.0) or 0.0),
                                      float(getattr(ev, "end", 0.0) or 0.0)])
                found.sort(key=lambda r: (r[0], r[1]))
                if len(found) > EVENTS_PER_TRACK_MAX:
                    found = found[:EVENTS_PER_TRACK_MAX]
                    events_truncated = True
                buckets[name] = found

            lines.append({"index": i,
                          "name": str(getattr(ln, "Name", "")),
                          "note_count": len(notes),
                          "notes": payload,
                          "events": buckets})

        total_notes = len(rows)
        notes_truncated = total_notes > limit
        if notes_truncated:
            rows.sort(key=lambda r: (r[0], r[1], r[2]))
            keep = set((i, j) for _s, i, j in rows[:limit])
            for i, line in enumerate(lines):
                line["notes"] = [n for j, n in enumerate(line["notes"])
                                 if (i, j) in keep]
        for line in lines:
            line["notes"].sort(key=lambda n: (n["start"], n["start_beat"]))

        content_beats = _content_beats(chart)
        return {
            "ok": True,
            "bpm": bpm,
            "bpm_list": bpm_list,
            "duration_sec": duration,
            "last_beat": last_beat,
            "content_beats": content_beats,
            "n_beats": content_beats + RENDER_PAD_BEATS,
            "note_count": total_notes,
            "truncated": bool(notes_truncated or events_truncated),
            "lines": lines,
        }

    # ------------------------------------------------------------ 渲染
    def renderer(self):
        """懒建渲染器（没装 Pillow 时抛 AppError）。"""
        with self.lock:
            if self._renderer is None:
                try:
                    from ..render.renderer import Renderer
                except Exception as exc:
                    raise AppError(
                        f"渲染子系统不可用（通常是没装 Pillow）：{exc}") from exc
                self._renderer = Renderer(self.chart_path, theme=None)
            return self._renderer

    def _encode_frame(self, renderer, t, w, h, *, hud, fmt, quality) -> bytes:
        """渲染一帧并编码。调用方必须已经持有 render_lock。"""
        try:
            img = renderer.frame(t, size=(w, h), hud=hud)
            if fmt == "JPEG" and getattr(img, "mode", "RGB") not in ("RGB", "L"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            if fmt == "JPEG":
                img.save(buf, format="JPEG", quality=int(quality))
            else:
                img.save(buf, format="PNG")
        except Exception as exc:
            raise AppError(f"渲染失败：{exc}") from exc
        return buf.getvalue()

    def preview_png(self, t: float, w: int, h: int, *, hud: bool = True) -> bytes:
        """渲染一帧，返回 PNG 字节。"""
        r = self.renderer()
        with self.render_lock:
            return self._encode_frame(r, t, w, h, hud=hud, fmt="PNG", quality=None)

    def frame_jpeg(self, t: float, w: int, h: int, *, hud: bool = True,
                   quality: int = QUALITY_DEFAULT) -> bytes:
        """渲染一帧，返回 JPEG 字节（与 preview_png 同一渲染器与参数语义）。"""
        q = int(max(QUALITY_MIN, min(QUALITY_MAX, int(quality))))
        r = self.renderer()
        with self.render_lock:
            return self._encode_frame(r, t, w, h, hud=hud, fmt="JPEG", quality=q)

    def filmstrip_jpeg(self, w: int, h: int, n: int, *, hud: bool = True,
                       quality: int = QUALITY_DEFAULT, start=None, end=None) -> bytes:
        """一张横向拼图：n 张缩略图各按 (w, h) 直接渲染，不缩放。

        start / end 可选：只采样 [start, end) 窗口（时间轴放大时按当前视野重取）。
        """
        q = int(max(QUALITY_MIN, min(QUALITY_MAX, int(quality))))
        count = int(max(STRIP_MIN_N, min(STRIP_MAX_N, int(n))))
        width = int(max(STRIP_MIN_W, min(STRIP_MAX_W, int(w))))
        height = int(max(STRIP_MIN_H, min(STRIP_MAX_H, int(h))))
        times = _filmstrip_times(count, self.content_seconds(), start=start, end=end)

        try:
            from PIL import Image
        except Exception as exc:
            raise AppError(f"渲染子系统不可用（通常是没装 Pillow）：{exc}") from exc

        r = self.renderer()
        canvas = Image.new("RGB", (count * width, height), (0, 0, 0))
        with self.render_lock:
            for i, t in enumerate(times):
                try:
                    img = r.frame(t, size=(width, height), hud=hud)
                except Exception as exc:
                    raise AppError(f"渲染失败：{exc}") from exc
                if getattr(img, "mode", "RGB") not in ("RGB", "L"):
                    img = img.convert("RGB")
                canvas.paste(img, (i * width, 0))
            buf = io.BytesIO()
            try:
                canvas.save(buf, format="JPEG", quality=q)
            except Exception as exc:                       # pragma: no cover
                raise AppError(f"编码失败：{exc}") from exc
        return buf.getvalue()

    def se_audio(self, kind: str) -> tuple:
        """一个打击音素材的原始字节与 Content-Type（供预览播放用）。

        目录在不在、文件缺不缺都按「这类没有」处理，由路由翻成 404；
        不在这里抛 500 —— 没素材是正常降级，不是服务故障。
        """
        if kind not in ("tap", "flick", "drag"):
            raise AppError(f"不认识的打击音：{kind!r}")
        sources = resolve_se_sources(find_se_dir())
        path = sources.get(kind)
        if not path:
            raise AppError(f"没有找到 {kind} 打击音素材")
        data = Path(path).read_bytes()
        ctype = {
            ".mp3": "audio/mpeg",
            ".ogg": "audio/ogg",
            ".oga": "audio/ogg",
            ".opus": "audio/ogg",
            ".wav": "audio/wav",
            ".m4a": "audio/mp4",
            ".flac": "audio/flac",
        }.get(Path(path).suffix.lower(), "application/octet-stream")
        return data, ctype

    # ------------------------------------------------------------ 导出
    def _chart_revision(self) -> str:
        """给这份谱面一个能认人的指纹（导出任务记录它，事后好对账）。"""
        try:
            text = self.chart_path.read_text(encoding="utf-8")
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
            mtime = self.chart_path.stat().st_mtime
            return (f"sha1:{digest} mtime:{mtime:.0f} "
                    f"bytes:{len(text.encode('utf-8'))}")
        except Exception as exc:
            return f"unavailable:{type(exc).__name__}"

    def export_start(self, params: dict) -> dict:
        """起一个后台导出线程；已有任务在跑时抛 AppError（路由翻成 409）。"""
        if not ffmpeg_available():
            raise AppError(
                "本机没有可用的 ffmpeg，无法导出视频：既不在 PATH 里，也没有安装 "
                "imageio-ffmpeg。装一个即可：pip install imageio-ffmpeg")

        start = float(params["start"])
        if params.get("hitsounds") == "auto":
            # 交给渲染线程前先把「自动」定死，省得它在后台线程里读环境变量。
            # 找不到就原样留着字符串：渲染层会打一行警告并退回无音效的老路径。
            params["hitsounds"] = find_se_dir() or "auto"
        end = params["end"]
        if end is None:
            end = self.content_seconds()
        if end is None:
            raise AppError("算不出谱面长度，无法导出：请显式给 end")
        end = float(end)
        if end <= start:
            raise AppError(f"导出区间不足一帧：end({end}) 必须大于 start({start})")

        w, h = params["size"]
        fps = float(params["fps"])
        total = int(round((end - start) * fps))
        if total <= 0:
            raise AppError(f"区间 {start}~{end}s 在 {fps}fps 下不足一帧")

        preset = params["preset"]
        crf = int(params["crf"])
        hint = (f"ffmpeg -f rawvideo -pix_fmt rgb24 -s {w}x{h} -r {fps:g} -i - "
                f"-c:v libx264 -preset {preset} -crf {crf} -pix_fmt yuv420p "
                f"{params['out']}")
        if params.get("hitsounds") is not None:
            hint += (f"  # 另叠打击音：{_hitsounds_label(params['hitsounds'])}"
                     f"（音量 {float(params.get('se_volume', SE_VOLUME)):g}）")

        with self.lock:
            current = self._export_job
            if current is not None and current.state == "running":
                raise AppError(BUSY_EXPORT_ERROR)
            self._export_seq += 1
            job = ExportJob(
                id=f"e{self._export_seq}",
                out=str(params["out"]),
                argv_hint=hint,
                chart_rev=self._chart_revision(),
                total=total,
            )
            self._export_job = job
            snapshot = job.snapshot()

        thread = threading.Thread(target=self._export_worker,
                                  args=(job, params, end),
                                  name=f"rpechart-export-{job.id}", daemon=True)
        thread.start()
        return snapshot

    def _export_worker(self, job, params, end) -> None:
        """导出线程。绝不在持锁状态下跑渲染（否则预览会卡死）。"""
        from ..video import render_chart_video

        out_path = Path(job.out)
        start = float(params["start"])
        fps = float(params["fps"])
        pids_before = _ffmpeg_pids()

        def on_progress(done, total_done):
            with self.lock:
                if job.cancel.is_set():
                    raise ExportCancelled("任务已被取消")
                job.done = int(done)
                if total_done:
                    job.total = int(total_done)
                    job.progress = min(1.0, done / float(total_done))
                elif job.total:
                    job.progress = min(1.0, done / float(job.total))
                else:                                       # pragma: no cover
                    job.progress = 0.0

        try:
            info = render_chart_video(
                self.chart_path,
                out_path,
                fps=fps,
                size=params["size"],
                start=start,
                end=end,
                audio=params["audio"],
                hud=params["hud"],
                progress=on_progress,
                hitsounds=params.get("hitsounds"),
                se_volume=float(params.get("se_volume", SE_VOLUME)),
                extra_args=["-crf", str(int(params["crf"])),
                            "-preset", params["preset"]],
            )
        except ExportCancelled as exc:
            leaked = _wait_ffmpeg_exit(pids_before)
            _unlink(out_path)
            detail = f"已取消：{exc}"
            if leaked:
                detail += f"（注意：ffmpeg pid {sorted(leaked)} 未在 20s 内退出）"
            self._finish_export(job, "cancelled", error=detail)
            return
        except BaseException as exc:                        # noqa: BLE001
            leaked = _wait_ffmpeg_exit(pids_before)
            _unlink(out_path)
            detail = f"{type(exc).__name__}: {exc}"
            if leaked:
                detail += f"（注意：ffmpeg pid {sorted(leaked)} 未在 20s 内退出）"
            self._finish_export(job, "failed", error=detail)
            return

        if not out_path.is_file() or out_path.stat().st_size <= 0:
            _unlink(out_path)
            self._finish_export(job, "failed", error="导出结束但没有产出有效文件")
            return
        self._finish_export(job, "done",
                            bytes=int(getattr(info, "bytesize", 0) or 0),
                            done=job.total, progress=1.0)

    def _finish_export(self, job, state, *, error=None, bytes=None, done=None,
                       progress=None) -> None:
        with self.lock:
            job.state = state
            job.error = error
            if bytes is not None:
                job.bytes = int(bytes)
            if done is not None:
                job.done = int(done)
            if progress is not None:
                job.progress = float(progress)
            if state == "done":
                job.progress = 1.0
            job.finished_at = time.time()

    def export_status(self) -> dict:
        with self.lock:
            job = self._export_job
            return {"ok": True, "job": job.snapshot() if job is not None else None}

    def export_cancel(self) -> dict:
        with self.lock:
            job = self._export_job
            if job is None or job.state != "running":
                return {"ok": True, "cancelled": False}
            job.cancel.set()
            return {"ok": True, "cancelled": True}

    # ------------------------------------------------------------ 杂项
    def chart_text(self) -> str:
        """当前谱面的 RPE 风格原文（就是 save_chart 写出的字节）。"""
        return self.chart_path.read_text(encoding="utf-8")

    def spec_json(self) -> dict:
        return _jsonable(self.spec.to_json())


def _unlink(path: Path) -> None:
    """删掉半成品；删不掉也不该把导出线程带崩。"""
    try:
        if path.is_file():
            path.unlink()
    except Exception:                                       # pragma: no cover
        pass


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

        def _hud_param(self, query) -> bool:
            return query.get("hud", ["1"])[0] not in ("0", "false", "no")

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
                if path == "/api/capabilities":
                    return self._send_json(200, app.capabilities())
                if path == "/api/timeline":
                    max_notes = _query_num(query, "max_notes", NOTES_DEFAULT, _as_int)
                    return self._send_json(200, app.timeline(max_notes))
                if path == "/api/preview":
                    t = _query_num(query, "t", 0.0, _as_float)
                    w = _query_num(query, "w", app.render_size[0], _as_int)
                    h = _query_num(query, "h", app.render_size[1], _as_int)
                    hud = self._hud_param(query)
                    w = max(FRAME_MIN_W, min(FRAME_MAX_W, w))
                    h = max(FRAME_MIN_H, min(FRAME_MAX_H, h))
                    return self._send(200, app.preview_png(t, w, h, hud=hud),
                                      "image/png")
                if path == "/api/frame.jpg":
                    t = _query_num(query, "t", 0.0, _as_float)
                    w = _query_num(query, "w", app.render_size[0], _as_int)
                    h = _query_num(query, "h", app.render_size[1], _as_int)
                    q = _query_num(query, "q", QUALITY_DEFAULT, _as_int)
                    hud = self._hud_param(query)
                    w = max(FRAME_MIN_W, min(FRAME_MAX_W, w))
                    h = max(FRAME_MIN_H, min(FRAME_MAX_H, h))
                    return self._send(200,
                                      app.frame_jpeg(t, w, h, hud=hud, quality=q),
                                      "image/jpeg")
                if path == "/api/filmstrip.jpg":
                    w = _query_num(query, "w", STRIP_DEFAULT_W, _as_int)
                    h = _query_num(query, "h", STRIP_DEFAULT_H, _as_int)
                    n = _query_num(query, "n", STRIP_DEFAULT_N, _as_int)
                    q = _query_num(query, "q", QUALITY_DEFAULT, _as_int)
                    start = _query_num(query, "start", None, _as_float)
                    end = _query_num(query, "end", None, _as_float)
                    hud = self._hud_param(query)
                    return self._send(
                        200, app.filmstrip_jpeg(w, h, n, hud=hud, quality=q,
                                                start=start, end=end),
                        "image/jpeg")
                if path.startswith("/api/se/"):
                    kind = path.rsplit("/", 1)[-1]
                    data, ctype = app.se_audio(kind)
                    return self._send(200, data, ctype)
                if path == "/api/export/status":
                    return self._send_json(200, app.export_status())
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
            try:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception as exc:
                return self._error(400, f"请求体不是合法 JSON：{exc}")
            try:
                if path == "/api/spec":
                    if isinstance(data, dict) and "spec" in data:
                        data = data["spec"]
                    try:
                        spec = ChartSpec.from_json(data)
                    except Exception as exc:
                        return self._error(400, f"spec 不合法：{exc}")
                    result = app.regenerate(spec)
                    return self._send_json(200 if result.get("ok") else 400, result)
                if path == "/api/export":
                    params = export_params(data)
                    try:
                        snapshot = app.export_start(params)
                    except AppError as exc:
                        if str(exc) == BUSY_EXPORT_ERROR:
                            return self._error(409, BUSY_EXPORT_ERROR)
                        raise
                    return self._send_json(200, {"ok": True, "job": snapshot})
                if path == "/api/export/cancel":
                    return self._send_json(200, app.export_cancel())
                return self._error(404, f"没有这个路由：{path}")
            except AppError as exc:
                return self._error(400, str(exc))
            except Exception as exc:                       # pragma: no cover
                return self._error(500, f"{type(exc).__name__}: {exc}",
                                   traceback=traceback.format_exc(limit=6))

        # ---- static
        def _serve_static(self, rel: str):
            target = (_STATIC / rel).resolve()
            if (_STATIC.resolve() not in target.parents
                    and target != _STATIC.resolve()):
                return self._error(403, "越界路径")
            if not target.is_file():
                return self._error(404, f"没有这个文件：{rel}")
            ctype = _MIME.get(target.suffix.lower(), "application/octet-stream")
            return self._send(200, target.read_bytes(), ctype)

    return Handler


def create_app(spec, *, chart_path=None, render_size=(960, 540),
               spec_path=None) -> WebApp:
    """建一个 WebApp（spec 可以是 ChartSpec / dict / 路径 / JSON 文本）。

    注意：**这不是只读 API** —— 建完立刻 `app.regenerate()`，而 regenerate() 会
    build → validate → `save_chart(self.chart_path)`，也就是往 `chart_path` 写盘。
    调试 / 探活请传临时目录里的路径；把只读样本或官方素材当 chart_path 传进来
    会被静默覆盖。
    """
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
    """起 HTTP 服务（阻塞）。返回 ThreadingHTTPServer 以便测试关掉。"""
    httpd = ThreadingHTTPServer((host, port), _handler_for(app))
    if not quiet:
        print(f"rpechart web → http://{host}:{httpd.server_address[1]}/")
    return httpd
