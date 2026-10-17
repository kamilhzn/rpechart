"""多押高亮（HL）渲染 —— 回归用例。

口径来自 phira 的 prpr/src/parse.rs:20-71 的 process_lines：把所有判定线的音符
起点汇到一起，**出现次数 >= 2** 的时间点算多押（跨线全局、含 fake），命中的音符
全部换成高亮贴图。官方 Texture/ 只给了 Tap/Flick/Drag 三个 *HL 变体，**长按没有**，
所以长按（以及素材包不全时的任何音符）由 raster 现场合成同一套外泛黄光。

这些用例全都用**自造的迷你素材目录**（纯色贴图，颜色一眼可辨），不依赖
F:/Phi/PhiEdit/Resources：官方素材可能不在本机，缺了就该跳过而不是失败。
"""

import copy
import os
import tempfile

from run_tests import test

GLOW_RGB = (255, 255, 102)  # 官方 HL 光带的颜色（用 PIL 从官方贴图反解出来的）


def _beat(value):
    from rpechart import Beat

    return Beat(value).frac


def _assets():
    """造一个迷你素材目录，返回其路径（调用方负责用 _use_assets 装上）。"""
    from PIL import Image

    root = tempfile.mkdtemp(prefix="rpechart_hl_assets_")
    tex = os.path.join(root, "Texture")
    os.makedirs(tex, exist_ok=True)

    def save(name, size, rgb):
        Image.new("RGBA", size, tuple(rgb) + (255,)).save(os.path.join(tex, name))

    # 普通贴图 40x20，HL 变体 40x40（宽度相同、只是更高 —— 与官方一致）
    save("Tap2.png", (40, 20), (0, 255, 255))
    save("Tap2HL.png", (40, 40), (255, 0, 255))     # 品红 = 一眼看出用了 HL
    save("Flick2.png", (40, 20), (255, 0, 0))
    save("Flick2HL.png", (40, 40), (0, 255, 0))
    save("Drag2.png", (40, 20), (255, 255, 0))
    save("DragHL.png", (40, 40), (0, 0, 255))
    # 长按**故意不给 HL 变体**（官方也没有）
    save("Hold.png", (20, 40), (10, 195, 255))
    save("HoldHead.png", (20, 10), (80, 80, 80))
    save("HoldEnd.png", (20, 10), (80, 80, 80))
    save("line.png", (8, 2), (237, 236, 176))
    return root


class _use_assets:
    """上下文管理器：把 RPECHART_ASSETS 指到给定目录，退出时还原并清缓存。"""

    def __init__(self, root):
        self.root = root
        self.old = None

    def __enter__(self):
        from rpechart.render import assets

        self.old = os.environ.get("RPECHART_ASSETS")
        os.environ["RPECHART_ASSETS"] = self.root
        assets.clear_cache()
        return self

    def __exit__(self, *exc):
        from rpechart.render import assets

        if self.old is None:
            os.environ.pop("RPECHART_ASSETS", None)
        else:
            os.environ["RPECHART_ASSETS"] = self.old
        assets.clear_cache()
        return False


def _line(notes, name="L"):
    """一条最小判定线（固定 x=0 的一帧移动事件）。"""
    return {
        "Name": name,
        "notes": notes,
        "eventLayers": [{
            "moveXEvents": [{"startTime": [0, 0, 1], "endTime": [0, 0, 1], "start": 0.0,
                             "end": 0.0, "easingType": 1, "easingLeft": 0.0,
                             "easingRight": 1.0}],
        }],
    }


def _raw(start, end, kind=1, x=0.0, fake=False):
    """一个音符的 JSON 形状；start/end 是 RPE 的 [a,b,c] 拍表示。"""
    return {"type": kind, "startTime": start, "endTime": end, "positionX": x,
            "above": 1, "alpha": 255, "isFake": 1 if fake else 0}


def _chart(lines, bpm=120.0):
    from rpechart.model import Chart

    return Chart.from_json({
        "BPMList": [{"bpm": bpm, "startTime": [0, 0, 1]}],
        "META": {"name": "hl"},
        "judgeLineList": lines,
    })


def _model(lines, bpm=120.0):
    from rpechart.render.timeline import RenderModel

    return RenderModel(_chart(lines, bpm))


def _theme(**changes):
    from rpechart.render.theme import Theme

    return Theme(**changes)


def _scene(model, t, theme=None):
    return model.scene_at(t, 960, 540, theme if theme is not None else _theme())


def _frame(model, t, theme=None):
    """把某一时刻光栅化（RGB），用来做像素断言。"""
    from rpechart.render import raster

    th = theme if theme is not None else _theme()
    return raster.rasterize(model.scene_at(t, 960, 540, th), th)


def _count_color(image, rgb, tol=26, where=None):
    """数出与 rgb 足够接近的像素数；where(x, y) 可选，用来限定区域。"""
    px = image.convert("RGB").load()
    w, h = image.size
    n = 0
    for y in range(h):
        for x in range(w):
            if where is not None and not where(x, y):
                continue
            r, g, b = px[x, y]
            if abs(r - rgb[0]) <= tol and abs(g - rgb[1]) <= tol and abs(b - rgb[2]) <= tol:
                n += 1
    return n


@test
def hl_multi_hint_matches_phira_process_lines():
    """口径 = 跨线全局、起点完全相同且该起点音符数 >= 2。"""
    # RPE 的 [a, b, c] 表示 a + b/c 拍：[4,0,1] 与 [3,2,2] 都是整 4 拍，
    # 它们必须被认成**同一个起点**；[4,1,2] 是 4.5 拍，不是。
    model = _model([
        _line([_raw([4, 0, 1], [4, 0, 1]), _raw([3, 2, 2], [3, 2, 2])]),  # 同线同起点
        _line([_raw([4, 1, 2], [4, 1, 2])], "L2"),                        # 4.5 拍
    ])
    fracs = model._multi_fracs
    assert fracs == frozenset({_beat(4)}), fracs
    assert _beat(4.5) not in fracs, "4.5 拍与 4 拍不是同一个起点，不能算多押"

    # 跨线同起点同样算（真谱里大量多押就是不同判定线上的键同时出现）
    cross = _model([_line([_raw([4, 0, 1], [4, 0, 1])]),
                    _line([_raw([4, 0, 1], [4, 0, 1])], "L2")])
    assert cross._multi_fracs == frozenset({_beat(4)}), cross._multi_fracs


@test
def hl_texture_naming_matches_the_official_resource_pack():
    """贴图名对齐官方 Texture/：普通 Tap2/Flick2/Drag2，多押加 HL 后缀。"""
    from rpechart.render.theme import Theme

    theme = Theme()
    assert theme.note_textures == {1: "Tap2.png", 2: "Hold.png",
                                   3: "Flick2.png", 4: "Drag2.png"}
    assert theme.note_textures_mh == {1: "Tap2HL.png", 3: "Flick2HL.png",
                                      4: "DragHL.png"}
    assert 2 not in theme.note_textures_mh, "官方没有长按的 HL 变体"
    assert theme.multi_hint is True, "默认应等价于 phira 的 config.double_hint = true"


@test
def hl_fake_notes_still_count_and_single_notes_do_not():
    """phira 的 process_lines 不看 fake，所以 fake 也参与计数、也被标记。"""
    model = _model([_line([_raw([4, 0, 1], [4, 0, 1])]),
                    _line([_raw([4, 0, 1], [4, 0, 1], fake=True)], "L2")])
    assert _beat(4) in model._multi_fracs, model._multi_fracs
    scene = _scene(model, 1.0)
    assert [n.multi for n in scene.notes] == [True, True], scene.notes

    alone = _model([_line([_raw([4, 0, 1], [4, 0, 1])])])
    assert alone._multi_fracs == frozenset(), alone._multi_fracs
    assert [n.multi for n in _scene(alone, 1.0).notes] == [False]


@test
def hl_multi_tap_switches_to_the_official_hl_texture():
    """多押的 Tap 换用 Tap2HL.png；单押仍是 Tap2.png。"""
    with _use_assets(_assets()):
        multi = _model([_line([_raw([4, 0, 1], [4, 0, 1], x=-135.0)]),
                        _line([_raw([4, 0, 1], [4, 0, 1], x=135.0)], "L2")])
        single = _model([_line([_raw([4, 0, 1], [4, 0, 1], x=-135.0)])])

        scene_m = _scene(multi, 1.5)
        scene_s = _scene(single, 1.5)
        assert [n.multi for n in scene_m.notes] == [True, True], scene_m.notes
        assert [n.texture_mh for n in scene_m.notes] == ["Tap2HL.png", "Tap2HL.png"]
        assert [n.multi for n in scene_s.notes] == [False], scene_s.notes
        assert [n.texture_mh for n in scene_s.notes] == [""], scene_s.notes

        # 自造素材里 Tap2HL 是品红、Tap2 是青，像素上必须能分辨
        frame_m = _frame(multi, 1.5)
        frame_s = _frame(single, 1.5)
        assert _count_color(frame_s, (255, 0, 255), tol=10) == 0
        assert _count_color(frame_m, (255, 0, 255), tol=10) > 500
        assert _count_color(frame_s, (0, 255, 255), tol=10) > 500
        assert _count_color(frame_m, (0, 255, 255), tol=10) == 0


@test
def hl_hold_without_an_official_texture_gets_a_synthesized_glow():
    """官方没有 Hold 的 HL 变体，长按多押时必须现场合成外泛黄光。"""
    with _use_assets(_assets()):
        hold = _raw([4, 0, 1], [12, 0, 1], kind=2, x=-135.0)
        partner = _raw([4, 0, 1], [12, 0, 1], kind=2, x=135.0)
        multi = _model([_line([hold]), _line([partner], "L2")])
        single = _model([_line([hold])])

        assert [h.multi for h in _scene(multi, 1.5).holds] == [True, True]
        assert [h.multi for h in _scene(single, 1.5).holds] == [False]

        left = lambda x, y: x < 300       # 长条左边缘（x 约 321）之外
        right = lambda x, y: x > 470      # 长条右边缘（x 约 447）之外
        frame_m = _frame(multi, 1.5)
        frame_s = _frame(single, 1.5)
        glow_m = (_count_color(frame_m, GLOW_RGB, tol=30, where=left)
                  + _count_color(frame_m, GLOW_RGB, tol=30, where=right))
        glow_s = (_count_color(frame_s, GLOW_RGB, tol=30, where=left)
                  + _count_color(frame_s, GLOW_RGB, tol=30, where=right))
        assert glow_s == 0, glow_s
        assert glow_m > 500, "长按多押没有外泛黄光（长条两侧应各有一圈光）: %d" % glow_m


@test
def hl_glow_actually_spills_outside_the_sprite():
    """回归：模糊必须在带 padding 的画布上做，否则光晕一个像素都不外扩。"""
    from rpechart.render import raster

    with _use_assets(_assets()):
        w, h, radius = 40.0, 20.0, 10.0
        # padding 至少给到半径的 3 倍，模糊尾巴才有地方落干净
        pad = radius * 3.0
        glow = raster._build_glow("Tap2.png", w, h, radius, 2.0, GLOW_RGB, pad)
        assert glow is not None
        assert glow.size == (int(w + pad * 2), int(h + pad * 2)), glow.size
        alpha = glow.getchannel("A").load()
        cy = glow.size[1] // 2
        p = int(pad)
        assert alpha[0, cy] == 0, "画布最外圈应是完全透明的"
        assert alpha[p - 1, cy] > 0, (
            "光晕没有溢出贴图边界 —— 多半是忘了在带 padding 的画布上模糊"
        )
        # 半径越大、同样的边距处越亮（说明半径真的参与了扩散）
        small = raster._build_glow("Tap2.png", w, h, 2.0, 2.0, GLOW_RGB, 6.0)
        a_small = small.getchannel("A").load()
        sp = small.size[1] // 2
        assert alpha[2, cy] < a_small[2, sp], (alpha[2, cy], a_small[2, sp])


@test
def hl_theme_switch_turns_the_highlight_off():
    """Theme.multi_hint=False（对齐 phira 的 config.double_hint）时一律不高亮。"""
    with _use_assets(_assets()):
        off = _theme(multi_hint=False)
        multi = _model([_line([_raw([4, 0, 1], [4, 0, 1], x=-135.0)]),
                        _line([_raw([4, 0, 1], [4, 0, 1], x=135.0)], "L2")])
        assert _beat(4) in multi._multi_fracs, "口径本身不该受主题影响"
        scene = _scene(multi, 1.5, off)
        assert [(n.multi, n.texture_mh) for n in scene.notes] == [(False, ""), (False, "")]
        frame = _frame(multi, 1.5, off)
        assert _count_color(frame, (255, 0, 255), tol=10) == 0
        assert _count_color(frame, GLOW_RGB, tol=26) == 0


@test
def hl_copies_keep_the_glow_tuning():
    """Theme.copy() 要带上光晕参数，否则压暗主题会把外泛光一起丢掉。"""
    a = _theme(multi_glow_radius=0.4, multi_glow_gain=3.5, multi_glow_color=(1, 2, 3))
    b = a.copy(hud=a.hud)
    assert b.multi_glow_radius == 0.4, b.multi_glow_radius
    assert b.multi_glow_gain == 3.5, b.multi_glow_gain
    assert b.multi_glow_color == (1, 2, 3), b.multi_glow_color
    assert b.multi_hint is True


@test
def hl_glow_tuning_actually_changes_the_pixels():
    """把增益与颜色改掉，画出来的光晕必须真的跟着变。"""
    import rpechart.render.raster as _raster

    with _use_assets(_assets()):
        model = _model([_line([_raw([4, 0, 1], [4, 0, 1], x=-135.0)]),
                        _line([_raw([4, 0, 1], [4, 0, 1], x=135.0)], "L2")])
        # 清空 HL 贴图表，强制走「现场合成外泛光」那条路
        # （贴图名是建场景时就写进 NoteDraw 的，所以必须在 scene_at 之前清）
        base = _theme()
        base.note_textures_mh = {}
        scene = model.scene_at(1.5, 960, 540, base)
        default = _raster.rasterize(scene, base)
        tinted = _raster.rasterize(
            scene, base.copy(multi_glow_color=(0, 255, 0), multi_glow_radius=0.5))
        # 光晕是半透明的，叠在深色底上不会正好是纯色；按「哪个通道占优」来数
        def greenish(image):
            px = image.convert("RGB").load()
            n = 0
            for y in range(image.size[1]):
                for x in range(image.size[0]):
                    r, g, b = px[x, y]
                    if g >= 60 and g >= r + 24 and g >= b + 24:
                        n += 1
            return n

        def yellowish(image):
            px = image.convert("RGB").load()
            n = 0
            for y in range(image.size[1]):
                for x in range(image.size[0]):
                    r, g, b = px[x, y]
                    if r >= 60 and b <= r - 40 and abs(r - g) <= 20:
                        n += 1
            return n

        # 黄像素里也包含判定线本身（线色 237,236,176），所以只比「谁更多」：
        # 默认主题的光晕加了一大片黄，改成绿光之后黄应该明显变少。
        assert greenish(default) == 0, greenish(default)
        assert greenish(tinted) > 200, "改了颜色却没有变化：%d" % greenish(tinted)
        assert yellowish(default) > yellowish(tinted) + 200, (
            yellowish(default), yellowish(tinted))


@test
def hl_copies_keep_their_own_texture_tables():
    """Theme.copy() 要给每个实例一份贴图表，不能共享可变字典。"""
    a = _theme()
    b = a.copy()
    assert a.note_textures_mh == b.note_textures_mh
    b.note_textures_mh[1] = "Other.png"
    b.hold_textures["body"] = "Other.png"
    b.note_textures[1] = "Other.png"
    assert a.note_textures_mh[1] == "Tap2HL.png", a.note_textures_mh
    assert a.hold_textures["body"] == "Hold.png", a.hold_textures
    assert a.note_textures[1] == "Tap2.png", a.note_textures
