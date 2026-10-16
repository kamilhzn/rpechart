"""rpechart —— Re:PhiEdit (RPE) / Phigros 谱面的程序化读写与构建库。

为什么是「写 JSON」而不是「注入进程」：RPE（PhiEdit.exe）经过加壳，静态字符串里
取不到任何字段名，也没有可用的脚本/插件接口；但它的一切数据都落在磁盘上的
**纯文本 JSON**，并且菜单里就有「重载谱面 / 导入谱面 json」。所以程序化制谱的正确
做法是：用本库生成/修改 ``chart.json``，再让 RPE 重载。

快速上手::

    from rpechart import ChartBuilder

    cb = ChartBuilder(bpm=174, name="Demo", charter="me")
    line = cb.line(name="主判定线")
    line.at(0, 0)
    line.move_to(0, 400, t=0, dur=4, easing="out_sine")
    for i in range(16):
        line.tap(t=i * 0.5, x=-400 + i * 50)
    cb.save("chart.json")
    print(cb.validate().format())

已经有一张谱面时要读进来改::

    from rpechart import load_chart, ChartBuilder
    cb = ChartBuilder(existing=load_chart("Resources/0/chart.json"))
    cb.line_builder(0).spin(360, t=8, dur=4)
    cb.save("Resources/0/chart.json")
"""

from __future__ import annotations

from .audio import AudioInfo, duration_of, ffmpeg_path, parse_lrc, probe_audio
from .beat import Beat, beat
from .builders import ChartBuilder, JudgeLineBuilder, NoteSpec, note_type_id
from .easing import (
    EASE_FUNCS,
    EASE_NAMES,
    MAX_EASING,
    bezier,
    ease,
    easing_id,
    easing_name,
)
from .interp import interpolate_value, resolve_timeline, value_at, value_at_resolved
from .io import (
    ChartFolder,
    ChartIOError,
    export_pez,
    find_rpe_root,
    import_pez,
    install_chart,
    load_chart,
    read_chartlist,
    save_chart,
    scan_chart_ids,
    write_chartlist,
)
from .model import (
    EVENT_ALPHA,
    EVENT_MOVE_X,
    EVENT_MOVE_Y,
    EVENT_ROTATE,
    EVENT_SPEED,
    NOTE_DRAG,
    NOTE_FLICK,
    NOTE_HOLD,
    NOTE_TAP,
    NOTE_TYPE_ALIASES,
    NOTE_TYPE_IDS,
    NOTE_TYPE_NAMES,
    BPMItem,
    Chart,
    ControlKeyframe,
    Controls,
    Event,
    EventLayer,
    ExtendedEvents,
    JudgeLine,
    Meta,
    Note,
    SpeedEvent,
    TimeTag,
)
from .timing import Timing, beat_to_second, build_bpm_list, second_to_beat
from .tempo import COMMON_BPMS, TempoEstimate, best_grid_fit, estimate_tempo
from .validate import CAUTION, ERROR, WARNING, Issue, ValidationReport, validate
from .video import (
    VideoEncodeError,
    VideoInfo,
    encode_frames,
    ffmpeg_available,
    render_chart_video,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # 时间
    "Beat",
    "beat",
    "Timing",
    "beat_to_second",
    "second_to_beat",
    "build_bpm_list",
    # 缓动
    "ease",
    "bezier",
    "easing_name",
    "easing_id",
    "EASE_FUNCS",
    "EASE_NAMES",
    "MAX_EASING",
    # 模型
    "Chart",
    "JudgeLine",
    "Note",
    "Event",
    "SpeedEvent",
    "EventLayer",
    "ExtendedEvents",
    "Controls",
    "ControlKeyframe",
    "BPMItem",
    "Meta",
    "TimeTag",
    "NOTE_TAP",
    "NOTE_HOLD",
    "NOTE_FLICK",
    "NOTE_DRAG",
    "NOTE_TYPE_NAMES",
    "NOTE_TYPE_IDS",
    "NOTE_TYPE_ALIASES",
    "EVENT_MOVE_X",
    "EVENT_MOVE_Y",
    "EVENT_ROTATE",
    "EVENT_ALPHA",
    "EVENT_SPEED",
    # 求值
    "value_at",
    "value_at_resolved",
    "interpolate_value",
    "resolve_timeline",
    # 构建
    "ChartBuilder",
    "JudgeLineBuilder",
    "NoteSpec",
    "note_type_id",
    # IO
    "load_chart",
    "save_chart",
    "install_chart",
    "ChartFolder",
    "export_pez",
    "import_pez",
    "find_rpe_root",
    "read_chartlist",
    "write_chartlist",
    "scan_chart_ids",
    "ChartIOError",
    # 校验
    "validate",
    "Issue",
    "ValidationReport",
    "ERROR",
    "WARNING",
    "CAUTION",
    # 音频
    "AudioInfo",
    "probe_audio",
    "duration_of",
    "ffmpeg_path",
    "parse_lrc",
    # 测速
    "TempoEstimate",
    "estimate_tempo",
    "best_grid_fit",
    "COMMON_BPMS",
    # 视频
    "VideoInfo",
    "VideoEncodeError",
    "encode_frames",
    "render_chart_video",
    "ffmpeg_available",
]
