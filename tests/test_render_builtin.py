"""无官方素材时的内置键形 —— 回归用例。

真机/官方贴图缺失时，舞台预览曾经只剩白色圆角块（note.color 默认白）。
这里钉住兜底路径的三件事：

* Tap 蓝 / Flick 红 / Drag 黄 / Hold 青蓝长条，且保留白色描边；
* 多押时键外沿要多出一圈黄色光，单押没有；
* 官方素材仍然优先，不能被内置键形顶掉。

用例自造一个空的 Texture/ 目录把 RPECHART_ASSETS 指过去，不依赖本机
是否装了官方素材。
"""

import os
import tempfile

from run_tests import test

TAP = (10, 195, 255)
FLICK = (255, 66, 101)
DRAG = (240, 237, 105)
HOLD = (153, 230, 252)
GLOW = (255, 255, 102)


class _texture_root:
    """把 RPECHART_ASSETS 指到一个只有空 Texture/ 的临时目录。"""

    def __init__(self, files=None):
        self.files = files or {}
        self.root = tempfile.mkdtemp(prefix="rpechart_builtin_")
        self.old = None

    def __enter__(self):
        from PIL import Image
        from rpechart.render import assets, raster

        tex = os.path.join(self.root, "Texture")
        os.makedirs(tex, exist_ok=True)
        for name, (size, rgb) in self.files.items():
            Image.new("RGBA", size, tuple(rgb) + (255,)).save(
                os.path.join(tex, name))
        self.old = os.environ.get("RPECHART_ASSETS")
        os.environ["RPECHART_ASSETS"] = self.root
        assets.clear_cache()
        raster.clear_cache()
        return self

    def __exit__(self, *exc):
        from rpechart.render import assets, raster

        if self.old is None:
            os.environ.pop("RPECHART_ASSETS", None)
        else:
            os.environ["RPECHART_ASSETS"] = self.old
        assets.clear_cache()
        raster.clear_cache()
        return False


def _line(notes, name="L"):
    return {
        "Name": name,
        "notes": notes,
        "eventLayers": [{
            "moveXEvents": [{"startTime": [0, 0, 1], "endTime": [0, 0, 1],
                             "start": 0.0, "end": 0.0, "easingType": 1,
                             "easingLeft": 0.0, "easingRight": 1.0}],
        }],
    }


def _raw(kind, start=(4, 0, 1), end=None, x=0.0):
    return {"type": kind, "startTime": list(start),
            "endTime": list(end or start), "positionX": x,
            "above": 1, "alpha": 255, "isFake": 0, "size": 1.0}


def _model(lines, bpm=120.0):
    from rpechart.model import Chart
    from rpechart.render.timeline import RenderModel

    return RenderModel(Chart.from_json({
        "BPMList": [{"bpm": bpm, "startTime": [0, 0, 1]}],
        "META": {"name": "builtin"},
        "judgeLineList": lines,
    }))


def _theme():
    from rpechart.render.theme import HudTheme, Theme

    return Theme(line_color=(0, 0, 0), hud=HudTheme(enabled=False))


def _frame(model, theme=None):
    from rpechart.render import raster

    th = theme or _theme()
    return raster.rasterize(model.scene_at(1.5, 960, 540, th), th)


def _count(image, rgb, tol=40):
    px = image.convert("RGB").load()
    n = 0
    for y in range(image.size[1]):
        for x in range(image.size[0]):
            r, g, b = px[x, y]
            if abs(r - rgb[0]) <= tol and abs(g - rgb[1]) <= tol and abs(b - rgb[2]) <= tol:
                n += 1
    return n


def _bbox_of_visible(image):
    px = image.convert("RGB").load()
    xs = []
    ys = []
    for y in range(image.size[1]):
        for x in range(image.size[0]):
            r, g, b = px[x, y]
            if r + g + b > 24:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _count_yellowish(image):
    px = image.convert("RGB").load()
    n = 0
    for y in range(image.size[1]):
        for x in range(image.size[0]):
            r, g, b = px[x, y]
            if r > 60 and g > 60 and b < min(r, g) * 0.8:
                n += 1
    return n


@test
def builtin_notes_have_standard_colors_and_white_outlines():
    """无官方素材：四种键都必须有标准填充色与白描边，形状不是大方块。"""
    cases = [
        (1, TAP, None, 4.0),       # Tap：宽而扁
        (3, FLICK, None, 2.0),     # Flick：带分裂的红键
        (4, DRAG, None, 5.0),      # Drag：更扁的黄键
        (2, HOLD, (12, 0, 1), 1.5),  # Hold：竖向长条
    ]
    with _texture_root():
        for kind, color, end, min_ratio in cases:
            model = _model([_line([_raw(kind, end=end)])])
            frame = _frame(model)
            colored = _count(frame, color, tol=45)
            white = _count(frame, (255, 255, 255), tol=24)
            assert colored > 200, (kind, "标准色像素太少", colored)
            assert white > 80, (kind, "白色描边太少", white)

            box = _bbox_of_visible(frame)
            assert box is not None, (kind, "画面里没有键")
            w = box[2] - box[0] + 1
            h = box[3] - box[1] + 1
            if kind == 2:
                assert h > w * 1.5, ("Hold 应该是长条", w, h)
            else:
                assert w > h * min_ratio, ("普通键应该是横向扁键", kind, w, h)


@test
def builtin_multi_notes_add_a_yellow_outline_glow():
    """多押才加黄光；单押不应该凭空多出一圈黄。"""
    with _texture_root():
        for kind in (1, 3, 4, 2):
            end = (12, 0, 1) if kind == 2 else None
            single = _model([_line([_raw(kind, end=end, x=-135.0)])])
            multi = _model([
                _line([_raw(kind, end=end, x=-135.0)]),
                _line([_raw(kind, end=end, x=135.0)], "L2"),
            ])
            s = _count_yellowish(_frame(single))
            m = _count_yellowish(_frame(multi))
            assert m > s + 200, ("多押没有黄光", kind, s, m)


@test
def official_textures_still_win_over_builtin_shapes():
    """有官方贴图时仍用官方贴图，内置键形只是缺图兜底。"""
    magenta = (255, 0, 255)
    with _texture_root({
        "Tap2.png": ((40, 20), magenta),
        "line.png": ((8, 2), (0, 0, 0)),
    }):
        frame = _frame(_model([_line([_raw(1)])]))
        assert _count(frame, magenta, tol=10) > 200, "官方 Tap2 没有被使用"
        assert _count(frame, TAP, tol=10) == 0, "内置蓝键顶掉了官方贴图"
