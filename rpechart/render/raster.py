"""光栅化：把 :class:`~rpechart.render.scene.Scene` 画成 PIL 图像。

这是唯一依赖 PIL 的渲染层。场景层（timeline）完全不碰 PIL，所以几何与时间可以
在无图形环境下测试。
"""

from __future__ import annotations

import math
import typing as _t

from PIL import Image, ImageDraw

from . import assets
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


def _scaled_texture(name: str, width: float, height: float, rgb=None):
    """取贴图并缩放到给定像素尺寸（带缓存）。"""
    src = assets.load_texture(name)
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
    src = assets.load_texture(name)
    if src is None:
        return None, 1.0
    ratio = src.height / float(src.width) if src.width else 1.0
    return src, ratio


def rasterize(scene: Scene, theme: _t.Optional[Theme] = None,
              base: _t.Optional[Image.Image] = None) -> Image.Image:
    """把场景画成 RGB 图像。

    ``base`` 给一个不透明底图（曲绘 / 视频帧 / 渐变）时，谱面直接画在它上面；
    不给就用自己的纯色底（``scene.background``）。
    """
    theme = theme or Theme()
    if base is None:
        canvas = Image.new("RGB", (scene.width, scene.height),
                           tuple(scene.background)).convert("RGBA")
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
        src, ratio = _note_texture(note.texture, note.width, note.height)
        if src is None:
            _draw_fallback_note(canvas, note)
            continue
        sprite = _rotated_sprite(note.texture, note.width, max(1.0, note.width * ratio),
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
    if alpha < 1.0:
        tex = tex.copy()
        tex.putalpha(tex.getchannel("A").point(lambda v: int(v * alpha)))
    cx = (hold.x_head + hold.x_tail) * 0.5
    cy = (hold.y_head + hold.y_tail) * 0.5
    canvas.alpha_composite(tex, (int(round(cx - tex.width * 0.5)),
                                 int(round(cy - tex.height * 0.5))))
    for name, x, y in ((hold.texture_head, hold.x_head, hold.y_head),
                       (hold.texture_tail, hold.x_tail, hold.y_tail)):
        src, ratio2 = _note_texture(name, hold.width, 1.0)
        if src is None:
            continue
        sprite = _rotated_sprite(name, hold.width, max(1.0, hold.width * ratio2),
                                 hold.color, hold.angle, hold.alpha)
        _paste_sprite(canvas, sprite, x, y, 1.0)


def _draw_fallback_note(canvas: Image.Image, note) -> None:
    """贴图缺失时的兜底：画一个圆角色块，保证画面仍有音符。"""
    draw = ImageDraw.Draw(canvas)
    w = max(2.0, note.width)
    h = max(2.0, w * 0.35)
    color = tuple(note.color) + (int(round(max(0.0, min(1.0, note.alpha)) * 255)),)
    draw.rounded_rectangle([note.x - w * 0.5, note.y - h * 0.5,
                            note.x + w * 0.5, note.y + h * 0.5],
                           radius=h * 0.5, fill=color)