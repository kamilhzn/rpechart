"""声明式谱面参数 —— `ChartSpec`。

**这是整个生成层的门面。** 目标：让 AI（或任何调用方）只写一个 JSON 就能出谱，
不需要碰 Python。

最小可用 spec
-------------
::

    {
      "name": "示例曲",
      "bpm": 150.0,
      "last_beat": 128.0,
      "sections": [
        {"start": 0,   "end": 32,  "role": "N", "density": 0.5, "tone": "dark"},
        {"start": 32,  "end": 96,  "role": "B", "density": 2.0, "tone": "blue"},
        {"start": 96,  "end": 128, "role": "R", "density": 2.5, "tone": "red"}
      ],
      "lines": [
        {"name": "主角", "y": -200.0},
        {"name": "对手", "y": 180.0}
      ],
      "out": "build/demo.json"
    }

给 `beats.json` 路径（`"beats": "work/beats.json"`）时会用真实特征驱动角色；
不给就按段落声明的角色出谱。

字段总览
--------
=============================  ================================================
`name` / `charter` /         元信息（写进 META）
`composer` / `level`
`song` / `background` /      资源文件名
`illustrator`
`offset`                      META.offset（毫秒，负值表示谱面提前）
`rpe_version`                 RPE 版本（默认 160）
`chart_id`                    装进 PhiEdit 时用的谱面 id
`bpm`                         主 BPM
`phase`                       beats.json 的相位（秒），也用于「秒 → 拍」换算
`duration`                    歌曲时长（秒），写进 `META.duration`
`last_beat`                   谱面最后一个拍（末段必须盖到这里）
`beats`                       beats.json 路径（可选）
`features`                    内联的 beats.json 内容（可选，优先于 `beats`）
`lanes`                       轨道网格（见 :class:`LaneGrid`）
`sections`                    段表；为空则自动切段
`auto_sections`               段表为空时是否自动切段（默认 true）
`lines`                       判定线定义（见 :class:`LineSpec`）
`motion`                      摆动 / 门限 / 线速（见 :class:`MotionSpec`）
`style`                       音符编排参数（见 :class:`NotePlanner`）
`palette`                     色调 → `[起始RGB, 结束RGB]`（线的 color_fade）
`captions`                    秒 → 文字卡点
`special`                     缩放 / 倾斜 / 闪烁等演出
`out`                         输出路径（CLI 的 `-o` 覆盖它）
`install`                     写出后是否装进 PhiEdit
`video`                       视频参数（见 :class:`VideoSpec`）
=============================  ================================================

确定性
------
:meth:`ChartSpec.to_json` 的输出是**稳定排序**的普通 dict，同一份 spec 生成两次
必然逐字节相同（守门测试 `gen_spec_round_trips_and_is_deterministic`）。
"""

from __future__ import annotations

import json
import typing as _t
from dataclasses import dataclass, field, fields as _fields
from pathlib import Path

from .features import RoleThresholds
from .lanes import LaneGrid
from .motion import MotionSpec, SwaySpec
from .notes import DEFAULT_PLANS, NotePlanner, RolePlan
from .structure import Section

__all__ = ["ChartSpec", "LineSpec", "VideoSpec"]


# ---------------------------------------------------------------- 判定线
@dataclass
class LineSpec:
    """一条判定线。

    对应 原生成脚本的 `LINE_SPEC` 六元组
    `(索引, 名称, 初始y, 纹理, 覆盖, zOrder)`，外加 `x` / `group` / `anchor`。

    `index` 是**可选**的：不给就按列表顺序自动编号（与
    :meth:`ChartBuilder.line` 的行为一致）。显式给出时用于复现原脚本那种
    「索引必须严格递增」的写法。
    """

    name: str = ""
    x: float = 0.0
    y: float = 0.0
    texture: str = "line.png"
    cover: bool = True
    z_order: int = 0
    group: int = 0
    index: _t.Optional[int] = None
    #: 是否参与音符落点（原脚本里线 3「领域」只做演出，不落键）。
    playable: bool = True
    attach_ui: _t.Optional[str] = None
    father: _t.Optional[int] = None

    def __post_init__(self) -> None:
        self.x = float(self.x)
        self.y = float(self.y)
        self.z_order = int(self.z_order)
        self.group = int(self.group)
        if self.index is not None:
            self.index = int(self.index)

    def to_json(self) -> dict:
        d = {
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "texture": self.texture,
            "cover": self.cover,
            "z_order": self.z_order,
            "group": self.group,
            "playable": self.playable,
        }
        if self.index is not None:
            d["index"] = self.index
        if self.attach_ui is not None:
            d["attach_ui"] = self.attach_ui
        if self.father is not None:
            d["father"] = self.father
        return d

    @classmethod
    def from_json(cls, data: _t.Mapping[str, _t.Any]) -> "LineSpec":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------- 视频
@dataclass
class VideoSpec:
    """视频导出参数（原生成脚本的 `--video*` 一族）。"""

    enabled: bool = False
    out: _t.Optional[str] = None
    fps: float = 60.0
    size: str = "1920x1080"
    start: float = 0.0
    end: _t.Optional[float] = None
    audio: _t.Optional[str] = None
    cover: _t.Optional[str] = None

    def __post_init__(self) -> None:
        self.fps = float(self.fps)
        self.start = float(self.start)
        if self.end is not None:
            self.end = float(self.end)

    @property
    def width_height(self) -> tuple:
        """`1920x1080` → `(1920, 1080)`（`*` 也接受）。"""
        w, h = self.size.lower().replace("*", "x").split("x")
        return int(w), int(h)

    def to_json(self) -> dict:
        d = {
            "enabled": self.enabled,
            "fps": self.fps,
            "size": self.size,
            "start": self.start,
        }
        if self.out is not None:
            d["out"] = self.out
        if self.end is not None:
            d["end"] = self.end
        if self.audio is not None:
            d["audio"] = self.audio
        if self.cover is not None:
            d["cover"] = self.cover
        return d

    @classmethod
    def from_json(cls, data: _t.Mapping[str, _t.Any]) -> "VideoSpec":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# ---------------------------------------------------------------- 主 spec
@dataclass
class ChartSpec:
    """一张谱面的**全部**参数。"""

    # ---- 元信息
    name: str = "Unknow"
    charter: str = "rpechart"
    composer: str = "Unknow"
    level: str = "Unknow"
    song: str = "song.mp3"
    background: str = "background.png"
    illustrator: _t.Optional[str] = None
    offset: int = 0
    rpe_version: int = 160
    chart_id: _t.Optional[str] = None

    # ---- 时间
    bpm: float = 120.0
    phase: float = 0.0
    duration: float = 0.0
    last_beat: float = 0.0

    # ---- 特征来源
    beats: _t.Optional[str] = None
    features: _t.Optional[dict] = None

    # ---- 结构
    lanes: dict = field(default_factory=dict)
    sections: list = field(default_factory=list)
    auto_sections: bool = True
    auto_min_len: int = 8
    auto_target_len: int = 32

    # ---- 线 / 运动 / 风格
    lines: list = field(default_factory=list)
    motion: dict = field(default_factory=dict)
    style: dict = field(default_factory=dict)
    palette: dict = field(default_factory=dict)
    captions: dict = field(default_factory=dict)
    special: dict = field(default_factory=dict)

    # ---- 输出
    out: _t.Optional[str] = None
    install: bool = False
    video: dict = field(default_factory=dict)

    #: 角色阈值（原 `Feats.role` 的 3000/1200/… 一组）。
    role_thresholds: dict = field(default_factory=dict)

    # ============================================================ 构造
    def __post_init__(self) -> None:
        self.bpm = float(self.bpm)
        self.phase = float(self.phase)
        self.duration = float(self.duration)
        self.last_beat = float(self.last_beat)
        self.offset = int(self.offset)
        self.rpe_version = int(self.rpe_version)
        self.auto_min_len = max(1, int(self.auto_min_len))
        self.auto_target_len = max(1, int(self.auto_target_len))
        if not (self.lines and isinstance(self.lines[0], LineSpec)):
            self.lines = [ln if isinstance(ln, LineSpec) else LineSpec.from_json(ln)
                          for ln in self.lines]

    # ============================================================ 派生对象
    def lane_grid(self) -> LaneGrid:
        """把 `lanes` 字典变成 :class:`LaneGrid`。"""
        return LaneGrid(**{k: v for k, v in self.lanes.items()
                           if k in LaneGrid.__dataclass_fields__})

    def section_list(self, features=None) -> list:
        """段表。`sections` 非空就用它；否则（且 `auto_sections`）自动切段。

        自动切段需要特征；`features` 为 `None` 时返回一个覆盖
        `[0, last_beat]` 的单段（角色 `N`），保证「永远有段可编排」——
        这是守门测试 `anime_sections_tile_the_whole_song_without_gaps` 的
        泛化版本：**段表必须铺满整首歌**，不能留缝。
        """
        if self.sections:
            from .structure import normalize_sections
            out = [s if isinstance(s, Section) else Section.from_json(s)
                   for s in self.sections]
            return normalize_sections(out)
        last = self.last_beat or (features.n if features is not None else 0.0)
        if features is not None and self.auto_sections:
            from .structure import auto_sections
            secs = auto_sections(features, last_beat=last,
                                 min_len=self.auto_min_len,
                                 target_len=self.auto_target_len)
            if secs:
                return secs
        return [Section(start=0.0, end=float(last or 1.0), role="N",
                        density=1.0, tone="dark")]

    def motion_spec(self) -> MotionSpec:
        """把 `motion` 字典变成 :class:`MotionSpec`（sways 支持嵌套 dict）。"""
        data = dict(self.motion or {})
        known = set(MotionSpec.__dataclass_fields__)
        kwargs = {k: v for k, v in data.items() if k in known}
        sways = kwargs.get("sways") or {}
        kwargs["sways"] = {
            int(k): (v if isinstance(v, SwaySpec) else SwaySpec.from_json(v))
            for k, v in sways.items()
        }
        # JSON 的键都是字符串，线索引 / 拍号要转回数字
        for key in ("y_tracks", "angle_tracks", "speed_boosts"):
            table = kwargs.get(key) or {}
            conv: dict = {}
            for k, v in table.items():
                if key == "speed_boosts":
                    conv[int(k)] = [list(map(float, iv)) for iv in v]
                else:
                    conv[int(k)] = {float(kk): float(vv) for kk, vv in v.items()}
            kwargs[key] = conv
        if "speed_range" in kwargs and kwargs["speed_range"] is not None:
            kwargs["speed_range"] = tuple(kwargs["speed_range"])
        return MotionSpec(**kwargs)

    def planner(self) -> NotePlanner:
        """把 `style` 字典变成 :class:`NotePlanner`。"""
        data = dict(self.style or {})
        known = set(NotePlanner.__dataclass_fields__)
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["grid"] = self.lane_grid()
        plans = kwargs.get("plans")
        if plans:
            merged = dict(DEFAULT_PLANS)
            for role, p in plans.items():
                merged[role] = p if isinstance(p, RolePlan) else RolePlan.from_json(p)
            kwargs["plans"] = merged
        return NotePlanner(**kwargs)

    def thresholds(self) -> RoleThresholds:
        """把 `role_thresholds` 字典变成 :class:`RoleThresholds`。"""
        return RoleThresholds.from_json(self.role_thresholds)

    def video_spec(self) -> VideoSpec:
        """把 `video` 字典变成 :class:`VideoSpec`。"""
        if isinstance(self.video, VideoSpec):
            return self.video
        return VideoSpec.from_json(self.video or {})

    # ============================================================ 序列化
    def to_json(self) -> dict:
        """稳定排序的普通 dict（同一 spec 两次生成逐字节相同）。

        `None` 值会被省略而不是写成 `null` —— 这样 JSON 更干净，且
        round-trip 时 `from_json` 的默认值能正确接住。
        """
        d: dict = {}
        for f in _fields(self):
            v = getattr(self, f.name)
            if v is None:
                continue
            if f.name == "lines":
                d["lines"] = [ln.to_json() if isinstance(ln, LineSpec) else _sorted(ln)
                              for ln in v]
            elif f.name == "video":
                d["video"] = (v.to_json() if isinstance(v, VideoSpec) else _sorted(v))
            elif isinstance(v, dict):
                d[f.name] = _sorted(v)
            elif isinstance(v, (list, tuple)):
                d[f.name] = [_sorted(x) if isinstance(x, dict) else x for x in v]
            else:
                d[f.name] = v
        return d

    @classmethod
    def from_json(cls, data: _t.Union[_t.Mapping[str, _t.Any], str, Path]) -> "ChartSpec":
        """从 dict / 路径 / JSON 文本构造。

        传字符串时：若它是一个存在的文件路径就读文件，否则当作 JSON 文本解析
        —— 这样 `ChartSpec.from_json(path)` 与 `ChartSpec.from_json(text)`
        都能用（CLI 与测试都受益）。
        """
        if isinstance(data, (str, Path)):
            s = str(data)
            p = Path(s)
            if p.is_file():
                raw = json.loads(p.read_text(encoding="utf-8-sig"))
            else:
                raw = json.loads(s)
        else:
            raw = dict(data)
        known = set(cls.__dataclass_fields__)
        kwargs = {k: v for k, v in raw.items() if k in known}
        if isinstance(kwargs.get("video"), bool):
            kwargs["video"] = {"enabled": bool(kwargs["video"])}
        if "sections" in kwargs:
            kwargs["sections"] = [
                s if isinstance(s, Section) else dict(s)
                for s in (kwargs["sections"] or [])
            ]
        if "lines" in kwargs:
            kwargs["lines"] = [
                ln if isinstance(ln, LineSpec) else LineSpec.from_json(ln)
                for ln in (kwargs["lines"] or [])
            ]
        return cls(**kwargs)

    def write(self, path: _t.Union[str, Path], *, indent: int = 2) -> Path:
        """把 spec 本身写成 JSON（便于「生成 spec → 人工微调 → 再出谱」）。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=indent),
                     encoding="utf-8")
        return p

    # ============================================================ 校验
    def problems(self) -> list:
        """明显自相矛盾的参数（CLI 在生成前先跑一遍，fail fast）。"""
        out: list = []
        if self.bpm <= 0:
            out.append(f"bpm 必须为正，当前 {self.bpm}")
        if self.last_beat < 0:
            out.append(f"last_beat 不能为负，当前 {self.last_beat}")
        if self.duration < 0:
            out.append(f"duration 不能为负，当前 {self.duration}")
        for i, ln in enumerate(self.lines):
            if not ln.name:
                out.append(f"第 {i} 条判定线缺少 name")
        secs = self.section_list()
        for i, s in enumerate(secs):
            if s.end < s.start:
                out.append(f"第 {i} 段 end < start：{s.start}..{s.end}")
        if secs and self.last_beat and secs[-1].end < self.last_beat - 1e-6:
            out.append(f"末段只写到 {secs[-1].end:g} 拍，但 last_beat={self.last_beat:g}"
                       "（后面的谱会「没写完」）")
        return out


def _sorted(v: _t.Any) -> _t.Any:
    """递归把 dict 的键排序（JSON 输出的确定性）。"""
    if isinstance(v, dict):
        return {k: _sorted(v[k]) for k in sorted(v, key=str)}
    if isinstance(v, (list, tuple)):
        return [_sorted(x) for x in v]
    return v
