"""官方资源定位与缓存：PhiEdit 的贴图与字体。

贴图来自 ``<repo>/PhiEdit/Resources/Texture/``，字体来自 ``<repo>/PhiEdit/Resources/fonts/``。
这些是**官方素材**（不是 phira 自带的那套），是复现真实游戏画面的关键。
"""

from __future__ import annotations

import os
import typing as _t
from pathlib import Path

from PIL import Image, ImageFont

#: 资源根目录的环境变量。指向 ``PhiEdit/Resources`` 或其父目录。
ASSETS_ENV = "RPECHART_ASSETS"

FONT_ALIASES = {
    "exo": ("Exo-Regular.otf",),
    "cmdysj": ("cmdysj.ttf",),
    "sourcehan": ("cmdysj.ttf",),
}

_TEXTURE_CACHE: dict = {}
_FONT_CACHE: dict = {}
_MISSING = object()

#: ``find_resources`` 的目录查找结果缓存。渲染一帧要查 9 次以上（每个音符贴图、
#: 每次文本绘制、``has_official_assets``），而每次查找都要对每个父目录做两次
#: ``is_dir()`` 系统调用（Windows 上约 8ms）。键含 ``(环境变量值, start)``：
#: 运行时改 ``RPECHART_ASSETS`` 或换 ``start`` 都取到各自的条目，``None`` 与路径
#: 也不会互相串味。正结果（Path）与负结果（None）都缓存。
_RESOURCE_CACHE: dict = {}


def find_resources(start=None):
    """向上查找 ``PhiEdit/Resources``（含 ``Texture`` 目录的那一级）。"""
    env = os.environ.get(ASSETS_ENV)
    key = (env, str(start) if start is not None else None)
    cached = _RESOURCE_CACHE.get(key, _MISSING)
    if cached is not _MISSING:
        return cached
    candidates = []
    if env:
        candidates.append(Path(env))
    here = Path(start).resolve() if start else Path(__file__).resolve()
    for base in [here] + list(here.parents):
        candidates.append(base / "PhiEdit" / "Resources")
        candidates.append(base)
    for path in candidates:
        try:
            if path.is_dir() and (path / "Texture").is_dir():
                _RESOURCE_CACHE[key] = path
                return path
        except OSError:
            continue
    _RESOURCE_CACHE[key] = None
    return None


def find_texture_dir(start=None):
    root = find_resources(start)
    if root is None:
        return None
    tex = root / "Texture"
    return tex if tex.is_dir() else None


def find_font_dir(start=None):
    root = find_resources(start)
    if root is None:
        return None
    fonts = root / "fonts"
    return fonts if fonts.is_dir() else None


def load_texture(name: str, start=None, subdir=None):
    """按文件名取贴图（RGBA）。找不到返回 ``None`` 并缓存该结果。

    ``subdir`` 用于 Texture 下的子目录（打击特效序列在 ``Texture/HE/``）。
    """
    key = (str(name), str(subdir) if subdir else "")
    cached = _TEXTURE_CACHE.get(key, _MISSING)
    if cached is not _MISSING:
        return cached
    image = None
    tex_dir = find_texture_dir(start)
    if tex_dir is not None:
        base = tex_dir / str(subdir) if subdir else tex_dir
        path = base / str(name)
        if not path.is_file() and not str(name).lower().endswith(".png"):
            path = base / (str(name) + ".png")
        try:
            if path.is_file():
                image = Image.open(path).convert("RGBA")
        except OSError:
            image = None
    _TEXTURE_CACHE[key] = image
    return image


def hit_texture_names(start=None):
    """``Texture/HE/`` 里的打击特效序列帧名（按帧号排序）。找不到返回空列表。"""
    tex_dir = find_texture_dir(start)
    if tex_dir is None:
        return []
    he = tex_dir / "HE"
    if not he.is_dir():
        return []
    names = [p.name for p in he.glob("img-*.png")]
    if not names:
        return []

    def _num(n):
        try:
            return int(n.split("-")[1].split(".")[0])
        except (IndexError, ValueError):
            return 10 ** 6

    return sorted(names, key=_num)


def clear_cache() -> None:
    """清空贴图/字体缓存与资源目录查找缓存（换资源目录时用）。"""
    _TEXTURE_CACHE.clear()
    _FONT_CACHE.clear()
    _RESOURCE_CACHE.clear()


def load_font(alias: str, size: int, start=None):
    """按别名取字体对象；找不到时回退 PIL 内置位图字体。"""
    key = (str(alias).lower(), int(size))
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    names = FONT_ALIASES.get(str(alias).lower(), (str(alias),))
    font_dir = find_font_dir(start)
    font = None
    if font_dir is not None:
        for name in names:
            path = font_dir / name
            try:
                if path.is_file():
                    font = ImageFont.truetype(str(path), int(size))
                    break
            except OSError:
                continue
    if font is None:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def has_official_assets(start=None) -> bool:
    """官方贴图目录里是否有音符贴图（判断能否渲染真实画面）。"""
    tex_dir = find_texture_dir(start)
    if tex_dir is None:
        return False
    return (tex_dir / "Tap2.png").is_file()