"""谱面校验器 —— 复刻 RPE 编辑器自身的纠错规则。

规则来源：RPE 1.4.1 说明书第 037 页「纠错种类列表」，以及 phira-docs 的字段约束。

分三级：

* ``error``：RPE 会标红，多数情况下谱面无法正常运行；
* ``warning``：能跑但会出问题（如 Tap 落在 Flick 后面）；
* ``caution``：只是提示不规范。

用法::

    issues = validate(chart)
    for issue in issues.errors:
        print(issue)
"""

from __future__ import annotations

import typing as _t
from dataclasses import dataclass

from .beat import Beat
from .model import (
    COMMON_EVENT_FIELDS,
    EVENT_ALPHA,
    EVENT_MOVE_X,
    EVENT_MOVE_Y,
    EVENT_ROTATE,
    NOTE_DRAG,
    NOTE_FLICK,
    NOTE_HOLD,
    NOTE_TAP,
    Chart,
    JudgeLine,
)
from .interp import value_at_resolved
from .timing import Timing

__all__ = ["Issue", "ValidationReport", "validate", "ERROR", "WARNING", "CAUTION"]

ERROR = "error"
WARNING = "warning"
CAUTION = "caution"

_SEVERITY_ORDER = {ERROR: 0, WARNING: 1, CAUTION: 2}

#: 判定线可见坐标范围（phira-docs：视界 1350x900，中心为原点）
VIEW_X = (-675.0, 675.0)
VIEW_Y = (-450.0, 450.0)
#: 音符 X 的建议上限（超出视界会被判为 X Too Large）
NOTE_X_LIMIT = 675.0
#: 音符默认事件速度 120 单位/秒 —— 用于估读 time
DEFAULT_SPEED = 1.0
#: 判定线数量上限（RPE wiki 反复强调的 1551 条）
MAX_LINES = 1551
#: zOrder 允许范围（RPE 说明书 ±100）
ZORDER_LIMIT = 100
#: attachUI 合法取值（RPE 设置里的 1~7）
ATTACH_UI_VALUES = frozenset(
    {"pause", "combonumber", "combo", "score", "bar", "name", "level"}
)
#: Tap 紧跟 Drag/Flick 的判定窗口（毫秒）—— RPE 的 Tap After Drag / Tap After Flick
TAP_AFTER_WINDOW_MS = (40.0, 160.0)
#: 坐标比较容差
_EPS = 0.01
#: 毫秒窗口的比较容差 —— ``40``/``160`` 这类整数毫秒在浮点换算后会带上
#: 1e-13 量级的尾巴（120BPM 下 160ms 实测 160.00000000000014），
#: 严格闭区间比较会把窗口边界整个漏掉。
_MS_EPS = 1e-6


@dataclass(frozen=True)
class Issue:
    """一条校验结果。"""

    severity: str
    code: str
    message: str
    line: _t.Optional[int] = None
    note_index: _t.Optional[int] = None
    layer: _t.Optional[int] = None
    event_kind: _t.Optional[str] = None
    event_index: _t.Optional[int] = None
    time: _t.Optional[Beat] = None

    @property
    def location(self) -> str:
        parts: list[str] = []
        if self.line is not None:
            parts.append(f"线{self.line}")
        if self.layer is not None:
            parts.append(f"层{self.layer}")
        if self.event_kind:
            parts.append(f"{self.event_kind}[{self.event_index}]")
        if self.note_index is not None:
            parts.append(f"音符[{self.note_index}]")
        if self.time is not None:
            parts.append(f"t={self.time}")
        return "@".join(parts)

    def __str__(self) -> str:
        tag = {ERROR: "错误", WARNING: "警告", CAUTION: "提示"}.get(
            self.severity, self.severity
        )
        loc = self.location
        return f"[{tag}] {self.code}: {self.message}" + (f"  ({loc})" if loc else "")


class ValidationReport:
    """一组 :class:`Issue`，可按级别过滤。"""

    def __init__(self, issues: _t.Iterable[Issue]) -> None:
        self.issues: list[Issue] = sorted(
            issues, key=lambda i: (_SEVERITY_ORDER.get(i.severity, 9), i.line or -1)
        )

    def __iter__(self) -> _t.Iterator[Issue]:
        return iter(self.issues)

    def __len__(self) -> int:
        return len(self.issues)

    def __bool__(self) -> bool:
        return bool(self.issues)

    def of(self, severity: str) -> list[Issue]:
        return [i for i in self.issues if i.severity == severity]

    @property
    def errors(self) -> list[Issue]:
        return self.of(ERROR)

    @property
    def warnings(self) -> list[Issue]:
        return self.of(WARNING)

    @property
    def cautions(self) -> list[Issue]:
        return self.of(CAUTION)

    @property
    def ok(self) -> bool:
        """没有 error 即视为可导入。"""
        return not self.errors

    def summary(self) -> str:
        return (
            f"错误 {len(self.errors)} / 警告 {len(self.warnings)} / 提示 {len(self.cautions)}"
        )

    def format(self, max_lines: int = 50) -> str:
        lines = [self.summary()]
        for issue in self.issues[:max_lines]:
            lines.append("  " + str(issue))
        if len(self.issues) > max_lines:
            lines.append(f"  …… 另有 {len(self.issues) - max_lines} 条")
        return "\n".join(lines)


def _beat_ms(timing: Timing, start: Beat, end: Beat) -> float:
    return (timing.beat_to_second(end) - timing.beat_to_second(start)) * 1000.0


def validate(
    chart: Chart,
    *,
    song_duration: _t.Optional[float] = None,
    check_overlap_types: bool = True,
) -> ValidationReport:
    """校验整张谱面。

    ``song_duration`` 为音乐时长（秒）；给了它才能检查 Time Out of Range。
    """
    issues: list[Issue] = []
    timing = Timing(chart.BPMList)

    if not chart.BPMList:
        issues.append(Issue(ERROR, "NoBPM", "BPMList 为空，谱面无法运行"))
    for item in chart.BPMList:
        if item.bpm <= 0:
            issues.append(
                Issue(
                    ERROR,
                    "BadBPM",
                    f"BPM 必须为正数，当前 {item.bpm}（RPE 会拒绝打开）",
                    time=item.startTime,
                )
            )
    if chart.META.RPEVersion <= 0:
        issues.append(Issue(CAUTION, "BadRPEVersion", "META.RPEVersion 非法"))

    for li, line in enumerate(chart.judgeLineList):
        issues.extend(
            _validate_line(line, li, timing, song_duration, check_overlap_types, chart)
        )

    # 结构性问题（数量、取值范围、层级）
    issues.extend(_validate_structure(chart))

    # 父线成环
    issues.extend(_validate_father(chart))
    return ValidationReport(issues)


def _validate_structure(chart: Chart) -> list[Issue]:
    """判定线层面的结构性检查：数量上限、层级数、zOrder、attachUI、bpmfactor。"""
    issues: list[Issue] = []
    if len(chart.judgeLineList) > MAX_LINES:
        issues.append(
            Issue(
                ERROR,
                "TooManyLines",
                f"判定线 {len(chart.judgeLineList)} 条，超过 RPE 上限 {MAX_LINES} 条",
            )
        )
    for li, line in enumerate(chart.judgeLineList):
        if len(line.eventLayers) > 5:
            issues.append(
                Issue(
                    ERROR,
                    "TooManyEventLayers",
                    f"事件层 {len(line.eventLayers)} 层，RPE 最多 5 层（第 5 层才是特殊事件层）",
                    line=li,
                )
            )
        if abs(line.zOrder) > ZORDER_LIMIT:
            issues.append(
                Issue(
                    CAUTION,
                    "ZOrderOutOfRange",
                    f"zOrder={line.zOrder} 超出 ±{ZORDER_LIMIT}",
                    line=li,
                )
            )
        if line.attachUI is not None and line.attachUI not in ATTACH_UI_VALUES:
            issues.append(
                Issue(
                    ERROR,
                    "BadAttachUI",
                    f"attachUI={line.attachUI!r} 不是合法 UI（可用 {sorted(ATTACH_UI_VALUES)}）",
                    line=li,
                )
            )
        if line.bpmfactor is not None and line.bpmfactor <= 0:
            issues.append(
                Issue(
                    ERROR,
                    "BadBpmFactor",
                    f"bpmfactor={line.bpmfactor:g} 必须为正数（线的 BPM = 谱面 BPM / bpmfactor）",
                    line=li,
                )
            )
        if line.father >= len(chart.judgeLineList):
            issues.append(
                Issue(ERROR, "BadFather", f"父线索引 {line.father} 不存在", line=li)
            )
        # 线的 Group 是 judgeLineGroup 的**下标**，越界等于引用了不存在的组名。
        # 实测 601 条真实判定线全部落在范围内；本库早期默认写空数组，
        # 会让每条线默认的 Group=0 悬空（按分组筛选时一条线都列不出来）。
        if not isinstance(line.Group, int) or not (
            0 <= line.Group < len(chart.judgeLineGroup)
        ):
            issues.append(
                Issue(
                    ERROR,
                    "BadLineGroup",
                    f"线分组下标 Group={line.Group!r} 越界；"
                    f"judgeLineGroup 里有 {len(chart.judgeLineGroup)} 个组"
                    f"（{'/'.join(chart.judgeLineGroup) or '空'}）",
                    line=li,
                )
            )
        # 注意：RPE 1.5+ 把特殊事件放在 judgeLine 的 ``extended`` 键上，
        # 并不要求存在第 5 层事件层（实测 577 条真实判定线里 535 条如此，
        # 其中 447 条只带垫底的 inclineEvents）。所以此处**不能**因为
        # 「extended 非空但没有第 5 层」而报警 —— 那是正常写法。
    return issues


# ------------------------------------------------------------------ 判定线
def _validate_line(
    line: JudgeLine,
    li: int,
    timing: Timing,
    song_duration: _t.Optional[float],
    check_overlap_types: bool,
    chart: Chart,
) -> list[Issue]:
    issues: list[Issue] = []

    # ---- 音符
    for ni, note in enumerate(line.notes):
        x = note.positionX
        if abs(x) > NOTE_X_LIMIT:
            issues.append(
                Issue(
                    CAUTION,
                    "XTooLarge",
                    f"音符 X={x:g} 超出视界 ±{NOTE_X_LIMIT:g}",
                    line=li,
                    note_index=ni,
                    time=note.startTime,
                )
            )
        if note.startTime < Beat(0):
            issues.append(
                Issue(ERROR, "TimeOutOfRange", "音符时间小于 0", line=li, note_index=ni, time=note.startTime)
            )
        if song_duration is not None:
            # 非 Hold 的 ``endTime`` 在模型里不强制跟着 ``startTime`` 走（只有
            # ``from_json`` 会归位旧文件的脏数据），所以程序化构造的
            # ``Note(type=NOTE_TAP, startTime=Beat(20))`` 的 ``endTime`` 仍是 ``Beat(0)``。
            # 判定「超出音乐时长」必须用音符真正出场的那个时刻：
            # Hold 看尾巴，其余看点。
            effective_end = note.endTime if note.type == NOTE_HOLD else note.startTime
            if timing.beat_to_second(effective_end) > song_duration + 0.001:
                issues.append(
                    Issue(
                        ERROR,
                        "TimeOutOfRange",
                        f"音符时间超出音乐时长（{timing.beat_to_second(effective_end):.3f}s > {song_duration:.3f}s）",
                        line=li,
                        note_index=ni,
                        time=effective_end,
                    )
                )
        if note.type == NOTE_HOLD:
            if note.endTime <= note.startTime:
                issues.append(
                    Issue(
                        ERROR,
                        "IllegalHold",
                        "Hold 的结束时间必须晚于开始时间",
                        line=li,
                        note_index=ni,
                        time=note.startTime,
                    )
                )
            else:
                ms = _beat_ms(timing, note.startTime, note.endTime)
                if ms < 40.0:
                    issues.append(
                        Issue(
                            CAUTION,
                            "ShortReadtime",
                            f"Hold 时长仅 {ms:.1f}ms，可能读不出来",
                            line=li,
                            note_index=ni,
                            time=note.startTime,
                        )
                    )
        else:
            # 非 Hold 的 endTime 必须等于 startTime
            if note.endTime != note.startTime:
                issues.append(
                    Issue(
                        WARNING,
                        "NonHoldEndTime",
                        f"{note.type_name} 的 endTime 与 startTime 不一致，已按 startTime 处理",
                        line=li,
                        note_index=ni,
                    )
                )
        if note.isFake:
            issues.append(
                Issue(
                    CAUTION,
                    "UsingFakeNote",
                    "使用了假音符（不参与判定/计分）",
                    line=li,
                    note_index=ni,
                    time=note.startTime,
                )
            )
        if note.size != 1.0:
            issues.append(
                Issue(
                    CAUTION,
                    "CustomizedNoteSize",
                    f"自定义音符宽度 size={note.size:g}",
                    line=li,
                    note_index=ni,
                )
            )
        if note.visibleTime != 999999.0:
            issues.append(
                Issue(
                    CAUTION,
                    "CustomizedVisibleTime",
                    f"自定义可视时间 visibleTime={note.visibleTime:g}",
                    line=li,
                    note_index=ni,
                )
            )
        if note.alpha < 0 or note.alpha > 255:
            issues.append(
                Issue(
                    WARNING,
                    "NoteAlphaOutOfRange",
                    f"音符 alpha={note.alpha} 超出 [0,255]",
                    line=li,
                    note_index=ni,
                )
            )

    if check_overlap_types:
        issues.extend(_validate_note_overlap(line, li, timing))
    issues.extend(_validate_note_sequence(line, li, timing))
    issues.extend(_validate_out_of_screen(line, li, timing))

    # ---- 事件
    linear_counts: dict[str, tuple[int, int]] = {}
    for layer_index, layer in enumerate(line.eventLayers):
        for kind in COMMON_EVENT_FIELDS:
            events = getattr(layer, kind)
            if not events:
                continue
            issues.extend(
                _validate_events(events, kind, li, layer_index, timing, song_duration)
            )
            if kind in (EVENT_MOVE_X, EVENT_MOVE_Y, EVENT_ROTATE):
                # 只统计「真的产生了位移/旋转」的事件：start == end 的常量事件
                # 在真实谱面里占绝大多数（实测 641/1803 行的非常量事件为 0），
                # 把它们算进分母会让这条提示变成噪音。
                moving = [
                    e
                    for e in events
                    if isinstance(e.start, (int, float))
                    and isinstance(e.end, (int, float))
                    and abs(float(e.start) - float(e.end)) > 1e-12
                ]
                linear = sum(1 for e in moving if getattr(e, "easingType", 1) == 1)
                prev_linear, prev_total = linear_counts.get(kind, (0, 0))
                linear_counts[kind] = (prev_linear + linear, prev_total + len(moving))

    for kind, (linear, total) in linear_counts.items():
        if total and linear / total > 0.25:
            issues.append(
                Issue(
                    CAUTION,
                    "TooManyLinearEvent",
                    f"{kind} 中线性缓动占比 {linear}/{total} 超过 25%",
                    line=li,
                    event_kind=kind,
                )
            )

    # ---- 特殊事件
    for kind in (
        "scaleXEvents",
        "scaleYEvents",
        "colorEvents",
        "textEvents",
        "paintEvents",
        "gifEvents",
        "inclineEvents",
    ):
        events = getattr(line.extended, kind, [])
        if events:
            issues.extend(_validate_events(events, kind, li, 4, timing, song_duration))
    return issues


def _validate_events(
    events: _t.Sequence,
    kind: str,
    li: int,
    layer_index: int,
    timing: Timing,
    song_duration: _t.Optional[float],
) -> list[Issue]:
    issues: list[Issue] = []
    ordered = sorted(events, key=lambda e: e.startTime.frac)
    for ei, event in enumerate(ordered):
        if event.endTime <= event.startTime:
            issues.append(
                Issue(
                    ERROR,
                    "IllegalEvent",
                    f"{kind} 结束时间不大于开始时间",
                    line=li,
                    layer=layer_index,
                    event_kind=kind,
                    event_index=ei,
                    time=event.startTime,
                )
            )
        if song_duration is not None and timing.beat_to_second(event.endTime) > song_duration + 0.001:
            issues.append(
                Issue(
                    ERROR,
                    "EventTimeOutOfRange",
                    f"{kind} 结束时间超出音乐时长",
                    line=li,
                    layer=layer_index,
                    event_kind=kind,
                    event_index=ei,
                )
            )
        if kind == EVENT_ALPHA:
            for bound in (event.start, event.end):
                if isinstance(bound, (int, float)) and (bound < 0 or bound > 255):
                    issues.append(
                        Issue(
                            WARNING,
                            "AlphaEventOverRange",
                            f"alpha 事件取值 {bound:g} 超出 [0,255]",
                            line=li,
                            layer=layer_index,
                            event_kind=kind,
                            event_index=ei,
                        )
                    )
                    break
        # 同层同类事件重叠
        nxt = ordered[ei + 1] if ei + 1 < len(ordered) else None
        if nxt is not None and event.endTime > nxt.startTime:
            issues.append(
                Issue(
                    ERROR,
                    "EventOverLapped",
                    f"{kind} 与下一个事件时间重叠",
                    line=li,
                    layer=layer_index,
                    event_kind=kind,
                    event_index=ei,
                )
            )
    return issues


def _validate_note_overlap(line: JudgeLine, li: int, timing: Timing) -> list[Issue]:
    """同一条线上、X 相同、时间相撞的音符组合检查。

    RPE 说明书第 037 页只列了三种相撞：``Tap&Hold`` / ``Tap&Tap`` / ``Hold&Hold``。
    **Drag 与 Flick 不在其中** —— 实测 24 份真实谱面里 ``Drag`` 与 ``Hold`` 同刻同 X
    共 1283 处，是「在 Hold 下面垫 Drag」这类合法写法，绝不能被报成 Overlapped。
    所以这里只考察 Tap 与 Hold 两类。

    相撞的判定按半开区间语义：

    * 两个 Tap：起始时间相同才算相撞（点区间只有重合才相交）；
    * Tap 与 Hold：Tap 落在 Hold 的 ``[start, end)`` 内（含起点、不含终点）；
    * 两个 Hold：``max(start) < min(end)`` 才算真的重叠。
    """
    issues: list[Issue] = []
    notes = sorted(line.notes, key=lambda n: (n.startTime.frac, n.positionX))
    for i in range(len(notes)):
        a = notes[i]
        # 扫描窗口必须按 a 的「最晚影响范围」算：一个 4 拍的 Hold 后面紧跟
        # 一个同位置的 Tap，两点 startTime 相差 4 拍以上，若用 a.startTime
        # 做基准会直接 break，把正要抓的 Tap&Hold 漏掉。notes 按 startTime
        # 升序，后面的 b 只会更晚，所以 break 仍然安全。
        a_extent = a.endTime if a.type == NOTE_HOLD else a.startTime
        for j in range(i + 1, len(notes)):
            b = notes[j]
            if b.startTime > a_extent:
                break
            if abs(a.positionX - b.positionX) > _EPS:
                continue
            if a.isFake or b.isFake:
                continue
            if a.type not in (NOTE_TAP, NOTE_HOLD) or b.type not in (NOTE_TAP, NOTE_HOLD):
                continue  # Drag / Flick 不参与相撞判定

            a_hold, b_hold = a.type == NOTE_HOLD, b.type == NOTE_HOLD
            if not a_hold and not b_hold:
                collide = a.startTime == b.startTime
                code, msg = "TapTapOverlapped", "两个 Tap 在相同位置同时出现"
            elif a_hold and b_hold:
                collide = max(a.startTime, b.startTime) < min(a.endTime, b.endTime)
                code, msg = "HoldHoldOverlapped", "两个 Hold 在相同位置重叠"
            else:
                hold, other = (a, b) if a_hold else (b, a)
                collide = hold.startTime <= other.startTime < hold.endTime
                code, msg = "TapHoldOverlapped", "Tap 落在 Hold 的判定区间内"
            if collide:
                issues.append(
                    Issue(ERROR, code, msg, line=li, note_index=i, time=a.startTime)
                )
    return issues


def _validate_note_sequence(line: JudgeLine, li: int, timing: Timing) -> list[Issue]:
    """检查音符的先后手顺序问题。

    RPE 说明书第 037 页列出的两类：

    * ``Caution: Hold Sticked?`` —— 两个 X 同号的 Hold 首尾相接；
    * ``Warning: Tap After Drag`` / ``Error: Tap After Flick`` —— Tap 追在同一条线的
      Drag/Flick 后面 40~160ms 内，玩家会漏拍（Flick 后面更严重，直接从 Error 起步）。

    这些规则只在**同一条判定线、X 相近、时间相邻**时才成立。
    """
    issues: list[Issue] = []
    # 记录排序后的原始下标，报错时要指回 line.notes 里的位置而不是排序位置
    indexed = sorted(
        enumerate(line.notes), key=lambda pair: (pair[1].startTime.frac, pair[1].positionX)
    )
    lo, hi = TAP_AFTER_WINDOW_MS

    for i, (_, note) in enumerate(indexed):
        if note.isFake:
            continue
        # 向后找相邻音符（时间窗口按 ±4 拍保守截断）
        for j in range(i + 1, len(indexed)):
            other_index, other = indexed[j]
            if other.startTime - note.startTime > Beat(4):
                break
            if other.isFake:
                continue
            if abs(other.positionX - note.positionX) > _EPS:
                continue
            ms = _beat_ms(timing, note.startTime, other.startTime)
            # 毫秒窗的边界要容忍浮点噪声：120BPM 下正好 160ms 会算成
            # 160.00000000000014，用 ``ms <= hi`` 会把它漏掉。
            if not (lo - _MS_EPS <= ms <= hi + _MS_EPS):
                continue
            if note.type == NOTE_DRAG and other.type == 1:
                issues.append(
                    Issue(
                        WARNING,
                        "TapAfterDrag",
                        f"Tap 紧跟 Drag 之后 {ms:.1f}ms，玩家容易漏拍",
                        line=li,
                        note_index=other_index,
                        time=other.startTime,
                    )
                )
            elif note.type == NOTE_FLICK and other.type == 1:
                issues.append(
                    Issue(
                        ERROR,
                        "TapAfterFlick",
                        f"Tap 紧跟 Flick 之后 {ms:.1f}ms（fj8），必须修改",
                        line=li,
                        note_index=other_index,
                        time=other.startTime,
                    )
                )

    # Hold Sticked?：两个 X 同号的 Hold 首尾相接
    holds = [(k, n) for k, n in indexed if n.type == NOTE_HOLD and not n.isFake]
    for i in range(len(holds)):
        for j in range(i + 1, len(holds)):
            _, a = holds[i]
            b_index, b = holds[j]
            # 扫描窗口要按「a 的尾巴」算，不能按 a 的开头算：一个 4 拍的 Hold
            # 后面紧接一个 Hold 时，两点 startTime 相差 4 拍以上会被误当成
            # 已经走远而 break，正是 Hold Sticked? 想抓的情形。
            # holds 按 startTime 升序，后面的 b 只会更晚，所以 break 安全。
            if b.startTime - a.endTime > Beat(4):
                break
            if a.positionX * b.positionX <= 0:
                continue  # X 异号（跨过判定线中心）不算
            gap = b.startTime - a.endTime
            if gap <= Beat(0):
                continue  # 真重叠由 _validate_note_overlap 报错
            # 首尾相接：间隔小到读不出来
            touch_ms = _beat_ms(timing, a.endTime, b.startTime)
            if touch_ms <= 40.0 + _MS_EPS:
                issues.append(
                    Issue(
                        CAUTION,
                        "HoldSticked",
                        f"两个 X 同号的 Hold 首尾相接（间隔 {touch_ms:.1f}ms），可能粘在一起",
                        line=li,
                        note_index=b_index,
                        time=b.startTime,
                    )
                )
    return issues


def _validate_out_of_screen(line: JudgeLine, li: int, timing: Timing) -> list[Issue]:
    """判断音符是否可能飞出轨：可见时间够不够从屏幕外落到判定线。

    下落速度 = **120 Y/s × 判定线流速 × 音符自身 speed**。120 是 RPE 的基准
    （说明书：1 RPESpeed = 120 个 Y 单位每秒），与 BPM 无关，所以本检查不需要
    ``timing``；``timing`` 参数保留是为了与其他 ``_validate_*`` 签名一致。

    这里必须把判定线的 ``speedEvents`` 算进去：旧实现只看 ``note.speed``，而
    真实谱面的线流速几乎从不等于 1（实测 24 份 pezc 语料 8116 个流速事件里
    8115 个非 1），于是 59 个 ``visibleTime < 999999`` 的音符里有 16 个判定翻转。
    ``note.speed == 0`` 或线流速为 0 时音符根本不下落，永远到不了判定线。
    没有 ``speedEvents`` 的判定线按 RPE 默认的线速 1.0 处理。
    """
    issues: list[Issue] = []
    speed_events: list = []
    for layer in line.eventLayers:
        speed_events.extend(getattr(layer, "speedEvents", []) or [])

    for ni, note in enumerate(line.notes):
        if abs(note.positionX) > NOTE_X_LIMIT:
            continue  # 已在 XTooLarge 报过
        visible = note.visibleTime if note.visibleTime < 999999.0 else None
        if visible is None:
            continue
        # 线基准速度：RPE 的默认值是 1.0（与 note.speed 默认 1.00 一致），没有
        # speedEvents 的线按 1.0 处理。不能按 0.0 —— 库自己的 ChartBuilder 默认
        # 不写 speedEvents，按 0.0 会把所有手写谱面误报成「永远落不到判定线」。
        # 真实谱面不依赖这个兜底：617 条判定线 100% 显式给出 speedEvents，且首
        # 事件都在第 0 拍。
        line_speed = float(value_at_resolved(speed_events, note.startTime, 1.0))
        rate = 120.0 * line_speed * abs(note.speed)
        travel = 900.0 / rate if rate > _MS_EPS else float("inf")
        if visible < travel:
            need = "永远" if travel == float("inf") else f"约需 {travel:.3f}s"
            issues.append(
                Issue(
                    WARNING,
                    "NoteJudgedOutScreen",
                    f"音符可见时间 {visible:g}s 不足以从屏幕外落到判定线（{need}）",
                    line=li,
                    note_index=ni,
                    time=note.startTime,
                )
            )
    return issues


def _validate_father(chart: Chart) -> list[Issue]:
    issues: list[Issue] = []
    n = len(chart.judgeLineList)
    for li, line in enumerate(chart.judgeLineList):
        seen: set[int] = set()
        cur = line.father
        while cur != -1:
            if cur in seen or cur == li:
                issues.append(
                    Issue(ERROR, "FatherLoop", f"父线成环（{li} → … → {cur}）", line=li)
                )
                break
            seen.add(cur)
            if cur < 0 or cur >= n:
                issues.append(
                    Issue(ERROR, "BadFather", f"父线索引 {cur} 不存在", line=li)
                )
                break
            cur = chart.judgeLineList[cur].father
    return issues
