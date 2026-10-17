"""光栅化：把 :class:`~rpechart.render.scene.Scene` 画成 PIL 图像。

这是唯一依赖 PIL 的渲染层。场景层（timeline）完全不碰 PIL，所以几何与时间可以
在无图形环境下测试。
"""

from __future__ import annotations

import math
import typing as _t

from PIL import Image, ImageDraw, ImageFilter

from . import assets, builtin
from .scene import Scene
from .theme import Theme

_RESAMPLE = getattr(Image, "Resampling", Image).BICUBIC

#: 缩放/着色后的贴图缓存。渲染一帧要摆几百个音符，逐个 resize 会成为瓶颈；
#: 音符宽度在一张谱里是常量，所以缓存命中率接近 100%。
_SCALED_CACHE: dict = {}
_SCALED_LIMIT = 512


def clear_cache() -> None:
    _SCALED_CACHE.clear()
    _ROT_CACHE.clear()
    _GLOW_CACHE.clear()
    _GLOW_ROT_CACHE.clear()
    builtin.clear_cache()


def _texture_source(name: str):
    """官方贴图优先；素材包缺图时退回程序化内置键形。"""
    src = assets.load_texture(name)
    if src is None:
        src = builtin.texture(name)
    return src


def _scaled_texture(name: str, width: float, height: float, rgb=None):
    """取贴图并缩放到给定像素尺寸（带缓存）。"""
    src = _texture_source(name)
    if src is None:
        return None
    w = max(1, int(round(width)))
    h = max(1, int(round(height)))
    key = (name, w, h, rgb)
    cached = _SCALED_CACHE.get(key)
    if cached is not None:
        return cached
    out = src if (w, h) == src.size else src.resize((w, h), _RESAMPLE)
    if rgb is not None and tuple(rgb) != (255, 255, 255):
        out = _tint(out, rgb)
    if len(_SCALED_CACHE) >= _SCALED_LIMIT:
        _SCALED_CACHE.clear()
    _SCALED_CACHE[key] = out
    return out


def _scaled_texture_in(src: Image.Image, width: float, height: float, rgb=None):
    """把**已经加载好的**贴图缩放到给定尺寸（带缓存，键用贴图自身身份）。"""
    w = max(1, int(round(width)))
    h = max(1, int(round(height)))
    rgb = tuple(rgb) if rgb is not None else (255, 255, 255)
    key = (id(src), w, h, rgb)
    cached = _SCALED_CACHE.get(key)
    if cached is not None:
        return cached
    out = src if (w, h) == src.size else src.resize((w, h), _RESAMPLE)
    if rgb != (255, 255, 255):
        out = _tint(out, rgb)
    if len(_SCALED_CACHE) >= _SCALED_LIMIT:
        _SCALED_CACHE.clear()
    _SCALED_CACHE[key] = out
    return out


#: 旋转后的精灵缓存。同一时刻同一判定线上的所有音符角度相同（线旋转 + 0/180），
#: 所以按「量化角度」缓存后，一帧里的旋转次数从「音符数」降到「判定线数」。
_ROT_CACHE: dict = {}
_ROT_LIMIT = 512
_ROT_STEP = 0.125  # 度；量化误差 < 0.07 度，肉眼不可见


def _rotated_sprite(name: str, width: float, height: float, rgb, angle: float,
                    alpha: float, scale: float = 1.0):
    """取「已缩放 + 已着色 + 已旋转」的精灵；返回 ``None`` 表示贴图缺失。"""
    key = (name, int(round(width)), int(round(height)), rgb,
           int(round(angle / _ROT_STEP)) if angle else 0)
    cached = _ROT_CACHE.get(key)
    if cached is None:
        sprite = _scaled_texture(name, width, height, rgb)
        if sprite is None:
            return None
        qangle = key[4] * _ROT_STEP
        if qangle:
            sprite = sprite.rotate(qangle, expand=True, resample=_RESAMPLE)
        if len(_ROT_CACHE) >= _ROT_LIMIT:
            _ROT_CACHE.clear()
        _ROT_CACHE[key] = sprite
        cached = sprite
    if alpha < 1.0:
        cached = cached.copy()
        cached.putalpha(cached.getchannel("A").point(lambda v: int(v * alpha)))
    return cached


def _paste_rotated(base: Image.Image, image: Image.Image, cx: float, cy: float,
                   angle: float, alpha: float) -> None:
    if alpha <= 0.0 or image is None:
        return
    if angle:
        image = image.rotate(angle, expand=True, resample=_RESAMPLE)
    if alpha < 1.0:
        image = image.copy()
        image.putalpha(image.getchannel("A").point(lambda v: int(v * alpha)))
    left = int(round(cx - image.width * 0.5))
    top = int(round(cy - image.height * 0.5))
    base.alpha_composite(image, (left, top))


def _paste_sprite(base: Image.Image, sprite: Image.Image, cx: float, cy: float,
                  alpha: float) -> None:
    """把已旋转的精灵按中心贴上去（带 alpha 调制）。"""
    if sprite is None or alpha <= 0.0:
        return
    if alpha < 1.0:
        sprite = sprite.copy()
        sprite.putalpha(sprite.getchannel("A").point(lambda v: int(v * alpha)))
    base.alpha_composite(sprite, (int(round(cx - sprite.width * 0.5)),
                                  int(round(cy - sprite.height * 0.5))))


def _note_texture(name: str, width: float, height_ratio: float):
    """音符贴图按「宽度固定、高度随贴图比例」缩放（phira draw_center 的语义）。"""
    src = _texture_source(name)
    if src is None:
        return None, 1.0
    ratio = src.height / float(src.width) if src.width else 1.0
    return src, ratio


#: 多押「外泛光」缓存。官方 Texture/ 只给了 Tap/Flick/Drag 三个 HL 变体，长按
#: 没有；此外自定义主题/残缺素材包也可能缺 HL。缺的时候按同一套规则现场合成：
#: 把贴图轮廓的 alpha 做高斯模糊、乘增益，再染成光晕色。
_GLOW_CACHE: dict = {}
#: 旋转后的光晕（长条的角度在一首歌里是常量，值得单独缓存一层旋转结果）。
_GLOW_ROT_CACHE: dict = {}
_GLOW_LIMIT = 256


def _build_glow(name: str, width: float, height: float, radius: float, gain: float,
                glow_rgb, pad: float, rgb=None):
    """合成一圈外泛光（未旋转；画布在四边各留 pad 像素给光晕）。

    参数是从官方 HL 贴图反解出来的：``radius`` 约为贴图短边的 0.24、``gain`` 约 2.0、
    光晕色 ``(255, 255, 102)`` —— 用它与 Tap2HL/DragHL 的官方光带逐像素比对，
    平均差 7/255。返回 RGBA 图；贴图缺失返回 ``None``。
    """
    src = _texture_source(name)
    if src is None:
        return None
    w = max(1, int(round(width)))
    h = max(1, int(round(height)))
    p = max(1, int(round(pad)))
    r = max(1, int(round(radius)))
    key = (name, w, h, r, round(float(gain), 3), tuple(glow_rgb), p, rgb)
    cached = _GLOW_CACHE.get(key)
    if cached is not None:
        return cached
    base = src if (w, h) == src.size else src.resize((w, h), _RESAMPLE)
    if rgb is not None and tuple(rgb) != (255, 255, 255):
        base = _tint(base, rgb)
    # 关键：模糊必须在**带 padding 的画布**上做。只模糊 w*h 那一块的话，
    # 模糊的扩散全落在贴图内部，贴图边缘会有半透明的一圈，但画布外一个像素都
    # 不会亮 —— 那就不是「外泛光」了。官方 HL 之所以看起来外扩，正是因为它的
    # 画布本身比普通贴图高，光晕有地方可去。
    padded = Image.new("L", (w + p * 2, h + p * 2), 0)
    padded.paste(base.getchannel("A"), (p, p))
    alpha = padded.filter(ImageFilter.GaussianBlur(r))
    alpha = alpha.point(lambda v: min(255, int(round(v * float(gain)))))
    out = Image.new("RGBA", (w + p * 2, h + p * 2), tuple(glow_rgb) + (0,))
    out.putalpha(alpha)
    if len(_GLOW_CACHE) >= _GLOW_LIMIT:
        _GLOW_CACHE.clear()
    _GLOW_CACHE[key] = out
    return out


def _glow_params(theme: Theme, span: float):
    """由主题参数推出（模糊半径, 增益, 光晕色）；``span`` 是贴图的短边像素数。"""
    frac = float(getattr(theme, "multi_glow_radius", 0.24) or 0.24)
    gain = float(getattr(theme, "multi_glow_gain", 2.0) or 2.0)
    color = tuple(getattr(theme, "multi_glow_color", (255, 255, 102)))
    return max(1.0, frac * max(1.0, span)), gain, color


def _paste_glow(canvas: Image.Image, name: str, width: float, height: float, rgb,
                angle: float, alpha: float, theme: Theme, x: float, y: float,
                span=None) -> None:
    """在 (x, y) 中心先铺一圈外泛光（多押高亮用）。"""
    if alpha <= 0.0:
        return
    radius, gain, glow_rgb = _glow_params(theme, span if span else min(width, height))
    # 旋转一张长条光晕（几百像素见方）比模糊本身还贵，而同一根长按在整首歌里
    # 的角度是常量、长度又被量化过，所以按「与 _rotated_sprite 相同的角度量化」
    # 缓存旋转结果。
    quant = int(round(float(angle) / _ROT_STEP)) if angle else 0
    key = (name, int(round(width)), int(round(height)), int(round(radius)),
           round(float(gain), 3), tuple(glow_rgb), int(round(radius * 2.0)), rgb, quant)
    glow = _GLOW_ROT_CACHE.get(key)
    if glow is None:
        glow = _build_glow(name, width, height, radius, gain, glow_rgb, radius * 2.0, rgb)
        if glow is None:
            return
        if quant:
            glow = glow.rotate(quant * _ROT_STEP, expand=True, resample=_RESAMPLE)
        if len(_GLOW_ROT_CACHE) >= _GLOW_LIMIT:
            _GLOW_ROT_CACHE.clear()
        _GLOW_ROT_CACHE[key] = glow
    _paste_sprite(canvas, glow, x, y, alpha)

def rasterize(scene: Scene, theme: _t.Optional[Theme] = None,
              base: _t.Optional[Image.Image] = None, *,
              base_changed: bool = True) -> Image.Image:
    """把场景画成 RGB 图像。

    ``base`` 给一个不透明底图（曲绘 / 视频帧 / 渐变）时，谱面直接画在它上面；
    不给就用自己的纯色底（``scene.background``）。

    ``base_changed=False`` 是对**专用缓冲**（每次渲染都同一张、且不要求保留内容）的
    承诺：这时跳过 ``convert("RGBA")`` 与 ``resize``，只做一次写时复制的 ``copy()``。
    底图每帧都一样时（``Renderer.frame`` 的渐变兜底），这一项原来是每帧最贵的
    固定开销；代价是调用方不能指望 ``base`` 渲染后还是原样。
    """
    theme = theme or Theme()
    if base is None:
        canvas = Image.new("RGB", (scene.width, scene.height),
                           tuple(scene.background)).convert("RGBA")
    elif (not base_changed and base.mode == "RGBA"
          and base.size == (scene.width, scene.height)):
        # 复用同一张 RGBA 缓冲：PIL 的 copy() 是写时复制，复制本身几乎免费
        canvas = base.copy()
    else:
        canvas = base.convert("RGBA")
        if canvas.size != (scene.width, scene.height):
            canvas = canvas.resize((scene.width, scene.height), _RESAMPLE)

    # ---- 判定线（普通线用 ImageDraw 画，保证任意角度都有稳定线宽）
    if scene.lines:
        draw = ImageDraw.Draw(canvas)
        for line in scene.lines:
            color = tuple(line.color) + (int(round(max(0.0, min(1.0, line.alpha)) * 255)),)
            width = max(1, int(round(line.thickness)))
            draw.line([(line.x0, line.y0), (line.x1, line.y1)], fill=color, width=width)

    # ---- 判定线贴图 / HUD 贴图
    for item in scene.images:
        tex = _scaled_texture(item.name, item.width, item.height)
        if tex is None:
            continue
        _paste_rotated(canvas, tex, item.x + item.width * 0.5, item.y + item.height * 0.5,
                       item.angle, item.alpha)

    # ---- 打击特效（官方 HE 序列；贴在判定位置，在音符之下）
    for hit in getattr(scene, 'hits', ()) or ():
        name = hit.texture
        if name.startswith('HE/'):
            tex = assets.load_texture(name[3:], subdir='HE')
            if tex is None:
                continue
            sprite = _scaled_texture_in(tex, hit.size, hit.size, hit.color)
            _paste_sprite(canvas, sprite, hit.x, hit.y, hit.alpha)
            continue
        sprite = _scaled_texture(name, hit.size, hit.size)
        _paste_sprite(canvas, sprite, hit.x, hit.y, hit.alpha)

    # ---- Hold（先画身，再画头尾）
    for hold in scene.holds:
        _draw_hold(canvas, hold, theme)

    # ---- 音符
    for note in scene.notes:
        name = note.texture
        ratio = None
        if note.multi and note.texture_mh:
            # 官方高亮贴图：只比普通贴图高（宽相同），所以照旧「宽度定、高度随比例」
            # 就能复现 phira 的 draw_center 几何，不需要额外缩放。
            hl, hl_ratio = _note_texture(note.texture_mh, note.width, note.height)
            if hl is not None:
                name, ratio = note.texture_mh, hl_ratio
        if ratio is None:
            src, ratio = _note_texture(name, note.width, note.height)
            if src is None:
                # 兜底色块也要参与多押高亮：素材包缺贴图时画面上不能悄悄少一圈光
                if note.multi:
                    w_px, h_px = _fallback_size(note)
                    _paste_glow(canvas, name, w_px, h_px, note.color,
                                note.angle, note.alpha, theme, note.x, note.y,
                                span=max(h_px, w_px * 0.16))
                _draw_fallback_note(canvas, note)
                continue
            if note.multi:
                # 没有 HL 变体（长按、或素材包不全）时按官方光带反解出的规则现场合成
                height_px = max(1.0, note.width * ratio)
                # 内置键形和官方 Tap2 一样很扁，纯按高度取模糊半径会让黄光
                # 细到看不见；用宽度的 16% 兜住短边下限。
                _paste_glow(canvas, name, note.width, height_px, note.color,
                            note.angle, note.alpha, theme, note.x, note.y,
                            span=max(height_px, note.width * 0.16))
        sprite = _rotated_sprite(name, note.width, max(1.0, note.width * ratio),
                                 note.color, note.angle, note.alpha)
        _paste_sprite(canvas, sprite, note.x, note.y, 1.0)

    # ---- HUD 文本
    if scene.texts:
        draw = ImageDraw.Draw(canvas)
        for text in scene.texts:
            font = assets.load_font(text.font, max(1, int(round(text.size))))
            color = tuple(text.color) + (int(round(max(0.0, min(1.0, text.alpha)) * 255)),)
            try:
                draw.text((text.x, text.y), text.text, font=font, fill=color,
                          anchor=text.anchor)
            except (ValueError, KeyError):
                draw.text((text.x, text.y), text.text, font=font, fill=color)

    return canvas.convert("RGB")


def _tint(image: Image.Image, rgb) -> Image.Image:
    """按 RGB 给贴图乘性着色（保留原 alpha 与明暗层次）。

    只在谱面显式给了 tint 时调用；官方贴图本身就是正确颜色，不做着色。
    """
    lum = image.convert("L")
    r = lum.point(lambda v: min(255, v * int(rgb[0]) // 255))
    g = lum.point(lambda v: min(255, v * int(rgb[1]) // 255))
    b = lum.point(lambda v: min(255, v * int(rgb[2]) // 255))
    out = Image.merge("RGB", (r, g, b)).convert("RGBA")
    out.putalpha(image.getchannel("A"))
    return out


def _draw_hold(canvas: Image.Image, hold, theme: Theme) -> None:
    body, ratio = _note_texture(hold.texture_body, hold.width, 1.0)
    if body is None:
        draw = ImageDraw.Draw(canvas)
        color = tuple(hold.color) + (int(round(hold.alpha * 255)),)
        draw.line([(hold.x_head, hold.y_head), (hold.x_tail, hold.y_tail)],
                  fill=color, width=max(2, int(hold.width * 0.35)))
        return
    length = math.hypot(hold.x_tail - hold.x_head, hold.y_tail - hold.y_head)
    if length < 1.0:
        length = 1.0
    angle = math.degrees(math.atan2(hold.y_tail - hold.y_head,
                                    hold.x_tail - hold.x_head))
    # 长按体是一根柔和的渐变条纹：旋转它很贵（几百像素见方），所以先在 1/4 分辨率
    # 上旋转并按 32px 量化长度（结果跨帧复用），再放大贴回。渐变条纹放大看不出差别，
    # 而旋转开销降到 1/16。
    length_q = max(32.0, round(length / 32.0) * 32.0)
    tex = _rotated_sprite(hold.texture_body, max(4.0, hold.width * 0.25),
                          max(8.0, length_q * 0.25), hold.color, -angle, 1.0,
                          scale=4.0)
    if tex is None:
        return
    tw = max(1, int(round(hold.width)))
    th = max(1, int(round(length_q)))
    tex = tex.resize((tw, th), _RESAMPLE)
    alpha = hold.alpha * theme.hold_opacity
    cx = (hold.x_head + hold.x_tail) * 0.5
    cy = (hold.y_head + hold.y_tail) * 0.5
    if getattr(hold, "multi", False):
        # 长按没有官方高亮贴图，外泛光一律现场合成（同一套模糊 + 染黄规则）。
        # 模糊半径按短边取：长条宽度是短边，但直接拿整条宽度会得到一大团黄雾，
        # 用和普通键一致的 16% 宽度才能得到贴着轮廓的一圈光。
        # 体纹理已经先旋转、再拉成 (tw, th) 的轴对齐盒子，所以光晕也按最终
        # 盒子生成，不再转一次 —— 否则长条的光晕会被转成横着的一大团。
        _paste_glow(canvas, hold.texture_body, tw, th, hold.color, 0.0, alpha,
                    theme, cx, cy, span=max(1.0, hold.width * 0.16))
    if alpha < 1.0:
        tex = tex.copy()
        tex.putalpha(tex.getchannel("A").point(lambda v: int(v * alpha)))
    canvas.alpha_composite(tex, (int(round(cx - tex.width * 0.5)),
                                 int(round(cy - tex.height * 0.5))))
    for name, x, y in ((hold.texture_head, hold.x_head, hold.y_head),
                       (hold.texture_tail, hold.x_tail, hold.y_tail)):
        src, ratio2 = _note_texture(name, hold.width, 1.0)
        if src is None:
            continue
        head_h = max(1.0, hold.width * ratio2)
        if getattr(hold, "multi", False):
            _paste_glow(canvas, name, hold.width, head_h, hold.color, hold.angle,
                        hold.alpha, theme, x, y, span=head_h)
        sprite = _rotated_sprite(name, hold.width, head_h,
                                 hold.color, hold.angle, hold.alpha)
        _paste_sprite(canvas, sprite, x, y, 1.0)


def _fallback_size(note) -> tuple:
    """兜底色块的像素尺寸（宽, 高）—— 与圆角几何共用一份算术。"""
    w = max(2.0, note.width)
    return w, max(2.0, w * 0.35)


def _draw_fallback_note(canvas: Image.Image, note) -> None:
    """贴图缺失时的兜底：画一个圆角色块，保证画面仍有音符。"""
    draw = ImageDraw.Draw(canvas)
    w, h = _fallback_size(note)
    color = tuple(note.color) + (int(round(max(0.0, min(1.0, note.alpha)) * 255)),)
    draw.rounded_rectangle([note.x - w * 0.5, note.y - h * 0.5,
                            note.x + w * 0.5, note.y + h * 0.5],
                           radius=h * 0.5, fill=color)