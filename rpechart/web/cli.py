"""`python -m rpechart.web` 的命令行。

用法::

    python -m rpechart.web --spec build/demo.spec.json
    python -m rpechart.web --spec demo.spec.json --port 9000 --size 1280x720
    python -m rpechart.web --print-spec > demo.spec.json   # 拿一份模板
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..gen.spec import ChartSpec
from .server import create_app, serve

__all__ = ["build_parser", "parse_size", "default_spec", "main"]


def parse_size(text: str) -> tuple:
    """``"1920x1080"`` / ``"640*360"`` → ``(1920, 1080)``。"""
    parts = str(text).lower().replace("*", "x").split("x")
    if len(parts) != 2:
        raise ValueError(f"--size 要写成 1920x1080，收到 {text!r}")
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"--size 要写成 1920x1080，收到 {text!r}") from None
    if w <= 0 or h <= 0:
        raise ValueError(f"--size 必须是正整数，收到 {text!r}")
    return (w, h)


def default_spec() -> dict:
    """没有 spec 文件时用的最小模板（保证页面一打开就有东西看）。"""
    return {
        "name": "未命名",
        "charter": "rpechart",
        "bpm": 150.0,
        "last_beat": 64.0,
        "duration": 25.6,
        "lines": [
            {"name": "主线", "y": -200.0},
            {"name": "副线", "y": 180.0},
        ],
        "out": "build/web_demo.json",
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m rpechart.web",
        description="起一个本地 Web UI：改参数 → 立刻看谱面与画面",
    )
    ap.add_argument("--spec", default=None, help="spec.json 路径（不给就用内置模板）")
    ap.add_argument("--out", default=None, help="生成出的 chart.json 放哪")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--size", default="960x540", help="预览分辨率（默认 960x540）")
    ap.add_argument("--no-open", action="store_true", help="不要自动开浏览器")
    ap.add_argument("--print-spec", action="store_true",
                    help="只打印一份 spec 模板然后退出（不启服务）")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.print_spec:
        print(json.dumps(default_spec(), ensure_ascii=False, indent=2))
        return 0

    spec_path = Path(args.spec) if args.spec else None
    if spec_path is not None and not spec_path.is_file():
        print(f"找不到 spec 文件：{spec_path}", file=sys.stderr)
        return 2
    spec = ChartSpec.from_json(spec_path) if spec_path else ChartSpec.from_json(default_spec())

    out = args.out or spec.out or (spec_path.parent / "web_chart.json"
                                   if spec_path else Path("build/web_chart.json"))
    try:
        size = parse_size(args.size)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    app = create_app(spec, chart_path=out, render_size=size, spec_path=spec_path)
    if app.error:
        print(f"首次生成失败（页面里会显示）：{app.error}", file=sys.stderr)
    else:
        s = app.summary()
        print(f"《{s['name']}》 线数 {s['lines']} 物量 {s['notes']} "
              f"校验错误 {s['report']['counts']['error']}")

    try:
        httpd = serve(app, args.host, args.port)
    except OSError as exc:
        print(f"端口 {args.port} 起不来：{exc}", file=sys.stderr)
        return 2

    url = f"http://{args.host}:{httpd.server_address[1]}/"
    if not args.no_open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:                                     # pragma: no cover
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止。")
    finally:
        httpd.server_close()
    return 0
