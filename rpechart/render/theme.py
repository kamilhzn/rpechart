"""渲染主题：颜色、尺寸、HUD 开关。全部是纯数据，方便场景层与光栅层分离。"""

from __future__ import annotations

import typing as _t

RGB = _t.Tuple[int, int, int]


class HudTheme:
    """HUD 的开关与版式。

    归一化坐标取自真机截图（978x642）的实测 bbox，不是 phira 的版式 —— phira 把分数/
    曲名放在垂直中部，与真实游戏完全不同。
    """

    __slots__ = ("enabled", "pause", "combo", "score", "song", "level", "line_number",
                 "combo_font", "text_font", "show_line_numbers", "pause_scale")

    def __init__(self, *, enabled: bool = True,
                 combo_font: str = "exo", text_font: str = "cmdysj",
                 show_line_numbers: bool = True, pause_scale: float = 1.0) -> None:
        self.enabled = bool(enabled)
        self.combo_font = combo_font
        self.text_font = text_font
        self.show_line_numbers = bool(show_line_numbers)
        self.pause_scale = float(pause_scale)
        # 暂停图标（左上）：nx0 0.0082 ny0 0.0249 宽 0.0256W 高 0.0467H
        self.pause = {"nx": 0.0082, "ny": 0.0249, "h": 0.0467}
        # combo 数字：居中 nx0.5 顶部 ny0.0187 字号 0.0783H
        self.combo = {"nx": 0.5, "ny": 0.0187, "size": 0.0783, "label_size": 0.0415,
                      "label_ny": 0.0794}
        # 分数：右对齐 nx0.9826 顶部 ny0.0374 字号 0.0575H
        self.score = {"nx": 0.9826, "ny": 0.0374, "size": 0.0575, "digits": 7}
        # 曲名：左下 nx0.0082 底 ny1.0 字号 0.0491H
        self.song = {"nx": 0.0082, "ny": 1.0, "size": 0.0491}
        # 难度：右下 nx0.9836 底 ny1.0 字号 0.0340H
        self.level = {"nx": 0.9836, "ny": 1.0, "size": 0.0340}
        # 判定线编号：字号 0.0234H，画在线**下方** 0.010H 处
        # （真机 978x642：线 y=383..389、label y=396..411；phira 同样在下方）
        self.line_number = {"size": 0.0234, "dx": 0.0, "dy": 0.010}

    def copy(self, **changes) -> "HudTheme":
        other = HudTheme(enabled=self.enabled, combo_font=self.combo_font,
                         text_font=self.text_font,
                         show_line_numbers=self.show_line_numbers,
                         pause_scale=self.pause_scale)
        for key, value in changes.items():
            setattr(other, key, value)
        return other


class Theme:
    """一次渲染用到的全部视觉参数。"""

    __slots__ = ("background", "line_color", "line_thickness", "line_length_world",
                 "note_scale", "fadeout", "hud", "judge_ball", "hold_opacity",
                 "textures", "note_textures", "hold_textures", "note_textures_mh",
                 "multi_hint", "multi_glow_radius", "multi_glow_gain",
                 "multi_glow_color", "hit_effects", "hit_duration", "hit_size",
                 "hit_color")

    def __init__(self, *, background: RGB = (0, 0, 0),
                 line_color: RGB = (237, 236, 176),
                 line_thickness: float = 0.0078,
                 line_length_world: float = 6.0,
                 note_scale: float = 1.0,
                 fadeout: float = 0.16,
                 hud: _t.Optional[HudTheme] = None,
                 judge_ball: bool = False,
                 hold_opacity: float = 1.0,
                 hit_effects: bool = True,
                 hit_duration: float = 0.32,
                 hit_size: float = 0.29,
                 hit_color: _t.Optional[RGB] = None,
                 multi_hint: bool = True,
                 multi_glow_radius: float = 0.24,
                 multi_glow_gain: float = 2.0,
                 multi_glow_color: RGB = (255, 255, 102)) -> None:
        self.background = tuple(background)
        self.line_color = tuple(line_color)
        self.line_thickness = float(line_thickness)
        self.line_length_world = float(line_length_world)
        self.note_scale = float(note_scale)
        self.fadeout = float(fadeout)
        self.hud = hud if hud is not None else HudTheme()
        self.judge_ball = bool(judge_ball)
        self.hold_opacity = float(hold_opacity)
        #: 打击特效（判定环）：贴图是官方 Texture/HE/img-N.png 序列。
        #: 真机截图里音符命中后出现的旋转判定环与它逐帧匹配（误差 0.071），
        #: 与 judgeball.png（纯白圆环）形态不符。
        self.hit_effects = bool(hit_effects)
        #: 一个判定环播多久（秒）。官方序列 31 帧，Phigros 里约 0.3s 播完。
        self.hit_duration = float(hit_duration)
        #: 判定环贴图边长 / 屏幕高。真机 978x642 的**正方形线框**实测 132px；
        #: HE 贴图里该线框占 178/255，故贴图边长 = 132*255/178 ≈ 189px → 0.29H。
        self.hit_size = float(hit_size)
        #: 判定环着色。官方 HE 序列是灰白的，RPE 用判定线色给它上色；
        #: 真机实测环色 (167,154,105) ≈ 判定线色 (237,236,176) 压暗后的结果。
        self.hit_color = tuple(hit_color if hit_color is not None else line_color)
        self.textures = {"line": "line.png", "judge_ball": "judgeball.png",
                         "pause": "Pause.png"}
        # 音符贴图：与 RPE 的 type 常量一致（1 Tap / 2 Hold / 3 Flick / 4 Drag）
        self.note_textures = {1: "Tap2.png", 2: "Hold.png", 3: "Flick2.png", 4: "Drag2.png"}
        self.hold_textures = {"head": "HoldHead.png", "body": "Hold.png", "tail": "HoldEnd.png"}
        #: 多押高亮贴图。官方 Texture/ 只给了 Flick/Tap/Drag 三个 HL 变体，
        #: **长按没有**，所以这里不含 2(Hold) —— 缺项的由 raster 现场合成外泛光。
        self.note_textures_mh = {1: "Tap2HL.png", 3: "Flick2HL.png", 4: "DragHL.png"}
        #: 多押高亮总开关。语义对齐 phira 的 config.double_hint（默认 true）：
        #: 开着时「同一时刻 >= 2 个音符」的键全部换成高亮贴图/加外泛光。
        self.multi_hint = bool(multi_hint)
        #: 合成外泛光的参数（长按用；以及官方 HL 贴图缺失时的兜底）。
        #: 半径按内层贴图高度取比例，增益与颜色由官方 HL 贴图反解而来：
        #: Tap/Drag 的官方光带 ≈ blur(alpha, 0.24H) * 2.00，(255,255,102)。
        self.multi_glow_radius = float(multi_glow_radius)
        self.multi_glow_gain = float(multi_glow_gain)
        self.multi_glow_color = tuple(multi_glow_color)

    def copy(self, **changes) -> "Theme":
        other = Theme(background=self.background, line_color=self.line_color,
                      line_thickness=self.line_thickness,
                      line_length_world=self.line_length_world,
                      note_scale=self.note_scale, fadeout=self.fadeout,
                      hud=self.hud, judge_ball=self.judge_ball,
                      hold_opacity=self.hold_opacity,
                      hit_effects=self.hit_effects,
                      hit_duration=self.hit_duration, hit_size=self.hit_size,
                      hit_color=self.hit_color, multi_hint=self.multi_hint,
                      multi_glow_radius=self.multi_glow_radius,
                      multi_glow_gain=self.multi_glow_gain,
                      multi_glow_color=self.multi_glow_color)
        # 贴图表是**每实例**的（copy() 也复制一份），否则改一个主题会串到别的主题。
        other.note_textures = dict(self.note_textures)
        other.hold_textures = dict(self.hold_textures)
        other.note_textures_mh = dict(self.note_textures_mh)
        for key, value in changes.items():
            setattr(other, key, value)
        return other


DEFAULT_THEME = Theme()