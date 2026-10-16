"""`rpechart.gen` 的测试 —— spec / 确定性 / 三道合法化 / 角色 / 半正弦 / 校验 / IO。

运行::

    cd /f/Phi && "$PY" tests/run_tests.py gen

这里的断言不是「跑通就行」，每条都钉住一条**真实事故**：

* spec round-trip / 确定性 —— 「同一份 spec 两次生成必须逐字节相同」；
* 三道合法化 —— 「键在乱飞」「TapHoldOverlapped」「Tap&Tap Overlapped」；
* 中性段不跳过 —— 「转场镜头整段空白」；
* Hold 叠加不替换 —— 「整段 Tap 被删光」；
* 半正弦用 cos —— 「判定线不动」；
* validate 零 ERROR / save+load round-trip —— 「RPE 打不开 / 读回来不一样」。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

from run_tests import approx, test

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from rpechart import ease, load_chart, save_chart  # noqa: E402
from rpechart import NOTE_DRAG, NOTE_FLICK, NOTE_HOLD, NOTE_TAP  # noqa: E402
from rpechart.gen import (  # noqa: E402
    ROLE_B,
    ROLE_N,
    ROLE_R,
    ROLE_RB,
    BeatFeatures,
    ChartSpec,
    GenNote,
    LaneGrid,
    MotionSpec,
    NotePlanner,
    Section,
    SwaySpec,
    assemble,
    auto_sections,
    build,
    dedupe_same_line,
    free_holds,
    generate,
    sections_tile,
    sway_waypoint,
    tame_lanes,
)


# ---------------------------------------------------------------- 夹具
def _spec_dict():
    """一份最小但完整的 spec：两段、两条线、一段 Hold。"""
    return {
        "name": "测试曲",
        "charter": "tester",
        "composer": "someone",
        "level": "IN Lv.15",
        "bpm": 150.0,
        "phase": 0.0,
        "duration": 60.0,
        "last_beat": 64.0,
        "sections": [
            {"start": 0, "end": 32, "role": "B", "density": 2.0, "tone": "blue",
             "holds": [{"from": 4.0, "to": 28.0, "every": 8.0, "dur": 3.0,
                        "line": 0, "x_source": "bx", "x_scale": 0.85}]},
            {"start": 32, "end": 64, "role": "R", "density": 2.5, "tone": "red"},
        ],
        "lines": [
            {"name": "主角", "y": -180.0},
            {"name": "对手", "y": 120.0},
            {"name": "旁观", "y": 0.0},
        ],
        "motion": {
            "sways": {
                "0": {"x_amp": 120.0, "x_period": 32.0, "y_amp": 40.0,
                      "y_period": 48.0, "rot_amp": 5.0, "rot_period": 24.0},
                "1": {"x_amp": 90.0, "x_period": 24.0, "y_amp": 0.0,
                      "y_period": 0.0, "rot_amp": 3.0, "rot_period": 16.0},
                "2": {"x_amp": 70.0, "x_period": 48.0, "y_amp": 0.0,
                      "y_period": 0.0, "rot_amp": 0.0, "rot_period": 0.0},
            },
            "gates": {"B": 0.9, "R": 0.8, "N": 0.4},
            "speeds": {"B": 12.0, "R": 11.0, "N": 9.5},
        },
        "out": os.path.join(tempfile.gettempdir(), "rpe_gen_test.json"),
    }


def _features():
    """构造一份逐拍特征（含 B / R / RB / N 四种角色）。"""
    n = 64
    rn, bn, warmth, low = [], [], [], []
    for i in range(n):
        if 8 <= i < 20:          # RB：红蓝同场
            rn.append(4000.0); bn.append(4000.0); warmth.append(0.0)
        elif 20 <= i < 36:       # R：红主导
            rn.append(5000.0); bn.append(100.0); warmth.append(3.0)
        elif 36 <= i < 52:       # B：蓝主导
            rn.append(100.0); bn.append(5000.0); warmth.append(-6.0)
        else:                    # N：中性
            rn.append(0.0); bn.append(0.0); warmth.append(0.0)
        low.append(1.0 if i % 2 == 0 else 0.2)
    return BeatFeatures(
        bpm=150.0,
        sec=[i * 0.4 for i in range(n)],
        low=low,
        motion=[0.2] * n,
        warmth=warmth,
        rn=rn,
        bn=bn,
        bx=[405.0 * ((i % 7) / 6.0 - 0.5) * 2 for i in range(n)],
        rx=[405.0 * ((i % 5) / 4.0 - 0.5) * 2 for i in range(n)],
        cx=[0.0] * n,
        # 关掉平滑与限幅，让角色严格按上面的构造来（测试才可断言）
        smooth_window=0,
        slew_limit=0.0,
        center_slew_limit=0.0,
    )


# ================================================================ spec
@test
def gen_spec_round_trips_through_json():
    """`ChartSpec.to_json → from_json → to_json` 必须完全一致（逐字节）。"""
    spec = ChartSpec.from_json(_spec_dict())
    a = spec.to_json()
    again = ChartSpec.from_json(a).to_json()
    assert json.dumps(a, ensure_ascii=False, sort_keys=True) == \
        json.dumps(again, ensure_ascii=False, sort_keys=True), "spec round-trip 不一致"
    # 关键字段必须活下来
    assert again["name"] == "测试曲"
    assert again["bpm"] == 150.0
    assert len(again["sections"]) == 2
    assert len(again["lines"]) == 3
    assert again["lines"][0]["name"] == "主角"
    assert again["motion"]["sways"]["0"]["x_amp"] == 120.0


@test
def gen_spec_accepts_a_file_path_and_json_text():
    """`ChartSpec.from_json` 必须同时吃「路径」和「JSON 文本」。"""
    d = _spec_dict()
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "s.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
        from_path = ChartSpec.from_json(p)
        from_text = ChartSpec.from_json(json.dumps(d, ensure_ascii=False))
    assert from_path.name == from_text.name == "测试曲"
    assert from_path.to_json() == from_text.to_json()


@test
def gen_spec_problems_catch_a_truncated_last_section():
    """末段没盖到 `last_beat` 必须被 :meth:`ChartSpec.problems` 抓到。

    守的是用户报的「后面的谱压根没有」：段表只写到 32 拍，音乐却到 64 拍。
    """
    d = _spec_dict()
    d["sections"] = [{"start": 0, "end": 32, "role": "B"}]
    d["last_beat"] = 64.0
    probs = ChartSpec.from_json(d).problems()
    assert any("没写完" in p or "末段" in p for p in probs), probs
    assert ChartSpec.from_json(_spec_dict()).problems() == []


@test
def gen_video_spec_parses_size():
    """`VideoSpec.width_height` 要能吃 `1920x1080` 与 `1920*1080`。"""
    from rpechart.gen import VideoSpec

    assert VideoSpec(size="1920x1080").width_height == (1920, 1080)
    assert VideoSpec(size="854*480").width_height == (854, 480)


# ================================================================ 确定性
@test
def gen_is_deterministic_across_two_builds():
    """同一份 spec 生成两次，`chart.to_json()` 必须逐字节相同。

    「完全确定性」是硬约束：生成层不引入 `random`，需要抖动时用确定性哈希
    （:func:`rpechart.gen.notes.stable_jitter`）。
    """
    spec = ChartSpec.from_json(_spec_dict())
    a = json.dumps(build(spec).chart.to_json(), ensure_ascii=False, sort_keys=True)
    b = json.dumps(build(spec).chart.to_json(), ensure_ascii=False, sort_keys=True)
    assert a == b, "两次生成的结果不一致（引入了 random？）"
    # 顺序也必须是确定的（同一段内存里再生成一次）
    c = json.dumps(build(ChartSpec.from_json(_spec_dict())).chart.to_json(),
                   ensure_ascii=False, sort_keys=True)
    assert a == c, "从 JSON 重建 spec 后结果变了（存在隐藏状态？）"


@test
def gen_stable_jitter_is_a_pure_function():
    """确定性抖动必须只由键决定（同键同值，异键异值）。"""
    from rpechart.gen import stable_jitter, stable_unit

    assert stable_unit("a", 1, 2.0) == stable_unit("a", 1, 2.0)
    assert 0.0 <= stable_unit("x") < 1.0
    assert stable_jitter("a") == stable_jitter("a")
    assert stable_jitter("a") != stable_jitter("b")
    assert abs(stable_jitter("k", scale=3.0)) <= 3.0


# ================================================================ 轨道合法化
@test
def gen_tame_lanes_limits_adjacent_jumps():
    """`tame_lanes`：紧邻音符最多跨 `max_step` 轨（治「键在乱飞」）。"""
    grid = LaneGrid()
    notes = [
        GenNote(0.0, "tap", grid.lanes[0], 0, 0.0),
        GenNote(0.5, "tap", grid.lanes[6], 0, 0.0),   # 0.5 拍后跳 6 轨
    ]
    out = tame_lanes(notes, grid)
    xs = [n.x for n in out if n.line == 0]
    assert max(xs) - min(xs) <= grid.max_step * grid.step + 1e-6, xs
    # 间隔 >= tight_sec 时不限幅
    far = [GenNote(0.0, "tap", grid.lanes[0], 0, 0.0),
           GenNote(grid.tight_sec + 1.0, "tap", grid.lanes[6], 0, 0.0)]
    out2 = tame_lanes(far, grid)
    assert any(abs(n.x - grid.lanes[6]) < 1e-6 for n in out2), [n.x for n in out2]


@test
def gen_free_holds_avoids_tap_lanes_inside_the_window():
    """`free_holds`：Hold 在位期间同轨不能有 Tap（`TapHoldOverlapped`）。"""
    grid = LaneGrid()
    tap = GenNote(10.0, "tap", grid.lanes[0], 0, 0.0)
    hold = GenNote(9.0, "hold", grid.lanes[0], 0, 4.0)   # 与 Tap 同轨且在窗内
    out = free_holds([tap, hold], grid)
    h = [n for n in out if n.kind == "hold"][0]
    assert abs(h.x - tap.x) > 1e-6, f"Hold 没避开 Tap: {h}"
    # 窗外的 Tap 不必避让
    outside = GenNote(20.0, "tap", grid.lanes[0], 0, 0.0)
    out2 = free_holds([outside, hold], grid)
    h2 = [n for n in out2 if n.kind == "hold"][0]
    assert abs(h2.x - hold.x) < 1e-6, "窗外 Tap 不该把 Hold 挤走"


@test
def gen_dedupe_same_line_moves_or_drops_duplicates():
    """`dedupe_same_line`：同线同拍同 x 必须错开（沿网格）或丢弃。"""
    grid = LaneGrid()
    dup = GenNote(3.0, "tap", 0.0, 0, 0.0)
    out = dedupe_same_line([dup, dup, dup], grid)
    assert len(out) == len({(n.line, n.t, n.x) for n in out}), "去重后仍有重复"
    assert len(out) <= 3
    for n in out:
        i = (n.x - grid.lanes[0]) / grid.step
        assert abs(i - round(i)) < 1e-6, f"错开后的 x 不在网格上: {n.x}"


@test
def gen_legalize_order_is_tame_then_free_then_dedupe():
    """三道合法化的顺序必须是 tame → free_holds → dedupe。

    顺序写反会复现 `TapHoldOverlapped`：`tame` 会把刚挪开的 Hold 又推回
    Tap 的轨道上。
    """
    grid = LaneGrid()
    notes = [
        GenNote(0.0, "tap", grid.lanes[3], 0, 0.0),
        GenNote(0.2, "tap", grid.lanes[6], 0, 0.0),
        GenNote(0.0, "hold", grid.lanes[3], 0, 5.0),
    ]
    out = grid.legalize(notes)
    # 结果里不允许出现「同线、同 x、Tap 落在 Hold 窗内」
    for h in [n for n in out if n.kind == "hold"]:
        for t in [n for n in out if n.kind == "tap" and n.line == h.line]:
            if abs(t.x - h.x) < 1e-6:
                assert not (h.t <= t.t < h.t + h.dur), (h, t)


# ================================================================ 角色分派
@test
def gen_role_assignment_maps_roles_to_lines_and_kinds():
    """角色 → 线 / 键位：B→线0 Tap、R→线1 Flick、RB→两线交替、N→稀疏但不空。"""
    f = _features()
    assert f.role(0) == ROLE_N
    assert f.role(24) == ROLE_R
    assert f.role(40) == ROLE_B
    assert f.role(12) == ROLE_RB
    secs = [Section(0, 8, ROLE_N, density=0.5),
            Section(8, 20, ROLE_RB, density=1.0, normal="dense", dense="dense"),
            Section(20, 36, ROLE_R, density=2.0, normal="basic", dense="basic"),
            Section(36, 52, ROLE_B, density=2.0, normal="basic", dense="basic"),
            Section(52, 64, ROLE_N, density=0.5, normal="basic", dense="basic")]
    planner = NotePlanner(grid=LaneGrid(), plans=dict(NotePlanner().plans))
    notes = planner.plan(f, secs)

    r_notes = [n for n in notes if 20.0 <= n.t < 36.0 and n.kind != "hold"]
    b_notes = [n for n in notes if 36.0 <= n.t < 52.0 and n.kind != "hold"]
    rb_notes = [n for n in notes if 8.0 <= n.t < 20.0 and n.kind != "hold"]
    n_notes = [n for n in notes if 0.0 <= n.t < 8.0 and n.kind != "hold"]

    assert r_notes and all(n.kind == "flick" and n.line == 1 for n in r_notes), r_notes[:4]
    assert b_notes and all(n.kind == "tap" and n.line == 0 for n in b_notes), b_notes[:4]
    # RB：正拍走线 0 的 Tap，其余走线 1 的 Flick
    assert {n.line for n in rb_notes} == {0, 1}, {n.line for n in rb_notes}
    assert {n.kind for n in rb_notes} == {"tap", "flick"}
    # 中性段**不跳过**：必须有音符，且带 Drag（让「旁观者」露面）
    assert n_notes, "中性段整段被跳过了（历史 bug）"
    assert any(n.kind == "drag" and n.line == 2 for n in n_notes), n_notes[:6]


@test
def gen_neutral_section_still_emits_notes():
    """全中性段也必须出音符（「转场镜头整段空白」的历史 bug）。"""
    n = 32
    f = BeatFeatures(low=[1.0] * n, rn=[0.0] * n, bn=[0.0] * n,
                     bx=[0.0] * n, cx=[0.0] * n, smooth_window=0, slew_limit=0.0)
    secs = [Section(0, 32, ROLE_N, density=0.5, normal="basic", dense="basic")]
    notes = NotePlanner().plan(f, secs)
    assert notes, "中性段一个音符都没有"
    assert all(n.line in (0, 2) for n in notes), {n.line for n in notes}


@test
def gen_holds_stack_on_top_of_taps_instead_of_replacing_them():
    """Hold 必须**叠加**在 Tap 上，不能替换掉整段 Tap（曾经毁掉终局段）。"""
    spec = ChartSpec.from_json(_spec_dict())
    notes = NotePlanner().plan(_features(), spec.section_list(_features()))
    holds = [n for n in notes if n.kind == "hold"]
    taps = [n for n in notes if n.kind in ("tap", "flick", "drag")]
    assert holds, "没有生成任何 Hold"
    assert taps, "Hold 把整段 Tap 替换掉了（历史 bug）"
    # Hold 的时间窗内仍然必须有非 Hold 音符（叠加而非替换）
    h = holds[0]
    assert any(h.t <= t.t < h.t + h.dur for t in taps), "Hold 窗口内一个 Tap 都没有"


# ================================================================ 半正弦
@test
def gen_sway_waypoint_uses_cosine_not_sine():
    """`sway_waypoint` 端点必须落在波峰/波谷（用 cos）。

    用 sin 时 `sin(πk)` 恒为 0，`phase=0` 的轴整条不动 —— 这是「判定线没有
    动作」的第二个根因。
    """
    vals = [sway_waypoint(100.0, 30.0, 0.0, k, 1.0) for k in range(8)]
    assert max(abs(v) for v in vals) > 99.0, f"端点全塌成 0（用了 sin？）: {vals}"
    for v in vals:
        assert abs(abs(v) - 100.0) < 1e-6, f"端点应全在幅值上: {vals}"

@test
def gen_half_sine_event_peaks_at_ends_and_crosses_zero_at_midpoint():
    """半正弦事件：**两端点 = ±振幅，区间中点 = 0**。

    形状澄清（别被「中点 = 振幅」的旧说法带偏）：`build_line_motion` 每 `period/2`
    拍写一个事件，端点的取值来自 :func:`sway_waypoint` ——
    `v_k = amp * cos(2π(k/2 + phase)) * gate`，所以 `k` 为偶数时是 `+amp`、奇数时是
    
    `-amp`：**振幅落在事件的端点上**。区间内部由 `easing="in_out_sine"` 插值，
    即 `v(u) = v0 + (v1 - v0) * (1 - cos(π·u))/2`，于是 `u = 0.5` 处
    `v = (v0 + v1)/2 = 0` —— **中点过零**，而且半程处的速度最大。

    这就是「用 cos 不用 sin」的物理含义：`sin(πk)` 恒为 0，端点的振幅会整个塌掉
    （判定线不动）。
    """
    # 直接对 easing 曲线采样：in_out_sine 端点 0/1、中点 0.5（半波对称）
    assert abs(ease("in_out_sine", 0.0) - 0.0) < 1e-9
    assert abs(ease("in_out_sine", 1.0) - 1.0) < 1e-9
    assert abs(ease("in_out_sine", 0.5) - 0.5) < 1e-9

    # 端点值：k=0 → +A，k=1 → -A（相隔半个周期，cos(π) = -1）
    a0 = sway_waypoint(100.0, 30.0, 0.0, 0, 1.0)
    a1 = sway_waypoint(100.0, 30.0, 0.0, 1, 1.0)
    approx(a0, 100.0, 1e-6)
    approx(a1, -100.0, 1e-6)
    # 一个周期（k=0..3）必须回到起点，且振幅只在端点取到
    approx(sway_waypoint(100.0, 30.0, 0.0, 2, 1.0), 100.0, 1e-6)
    for k in (0, 1, 2, 3):
        assert abs(abs(sway_waypoint(100.0, 30.0, 0.0, k, 1.0)) - 100.0) < 1e-6

    # ---- 真实谱面：线 0 的第一个 moveX 事件
    spec = ChartSpec.from_json(_spec_dict())
    cb = build(spec)
    ln = cb.chart.judgeLineList[0]
    moves = [e for lay in ln.eventLayers if lay for e in (lay.moveXEvents or [])]
    assert moves, "线 0 没有 moveX 事件"
    first = moves[0]
    # 端点 = 振幅 × 该时刻的门限。门限在段间**线性插值**：
    #   拍 0  → B 段 gate = 0.9
    #   拍 16 → 0.9 + (16/32)·(0.8 - 0.9) = 0.85（B→R 的插值中点）
    # 所以两端不是同一个数 —— 这正是「门限插值」这条特性本身。
    amp = 120.0
    v0, v1 = float(first.start), float(first.end)
    assert abs(v0 - amp * 0.9) < 1e-6, f"起点没跟上 B 段门限: {v0}"
    assert abs(v1 + amp * 0.85) < 1e-6, f"终点没走门限插值: {v1}"
    assert v0 * v1 < 0.0, f"两端同号说明不是半个波: {v0}, {v1}"
    # 两端都必须贴在振幅量级上（不是被采样成折线的小扰动）
    assert abs(v0) > amp * 0.5 and abs(v1) > amp * 0.5, f"半波幅度太小: {v0} → {v1}"
    # 区间中点（u=0.5）由 in_out_sine 插值到 (v0+v1)/2 —— 半波对称。
    # 这里两端门限不同（0.9 / 0.85），所以中点不是 0 而是两者均值 3.0；
    # 「过零」只在门限相等时成立，见下面用 sway_waypoint 的合成检查。
    u = 0.5
    mid = v0 + (v1 - v0) * ease("in_out_sine", u)
    approx(mid, (v0 + v1) / 2.0, 1e-9)
    approx(mid, 3.0, 1e-6)
    # 门限相等（gate=1）时，半波中点严格过零：+A → -A 的对称半波
    b0, b1 = sway_waypoint(amp, 32.0, 0.0, 0, 1.0), sway_waypoint(amp, 32.0, 0.0, 1, 1.0)
    bmid = b0 + (b1 - b0) * ease("in_out_sine", u)
    assert abs(bmid) < 1e-9, f"对称半波的中点必须过零: {bmid}"
    # 每个事件都必须用 in_out_sine，而不是采样成折线（TooManyLinearEvent）
    from rpechart import easing_id, easing_name

    def _easing_of(ev):
        v = ev.easingType
        return v if isinstance(v, str) else easing_name(int(v))

    for e in moves:
        assert _easing_of(e) == "in_out_sine", e.easingType
    assert easing_id("in_out_sine") != 1, "in_out_sine 不该等于线性 1"


@test
def gen_line_motion_is_decoupled_from_note_tracks():
    """判定线运动不能由音符 x 驱动（解耦）。

    做法：把音符落点整体改到另一条轨道，线的 moveX 事件必须**逐字节不变**。
    """
    spec = ChartSpec.from_json(_spec_dict())
    cb1 = build(spec)
    d2 = _spec_dict()
    # 把段落节奏型改成完全不同的密度（音符会完全不同），但线运动参数一字不改
    for s in d2["sections"]:
        s["normal"] = "burst"
        s["dense"] = "burst"
    cb2 = build(ChartSpec.from_json(d2))
    assert json.dumps(build(ChartSpec.from_json(_spec_dict())).chart.to_json(),
                      sort_keys=True) != json.dumps(cb2.chart.to_json(),
                                                    sort_keys=True), \
        "改了节奏型却没改音符，这条测试就没在测解耦"

    def moves(cb):
        ln = cb.chart.judgeLineList[0]
        return [(float(e.start), float(e.end), float(e.startTime.value),
                 float(e.endTime.value))
                for lay in ln.eventLayers if lay for e in (lay.moveXEvents or [])]

    m1, m2 = moves(cb1), moves(cb2)
    assert m1 == m2, f"音符变化影响了判定线运动（未解耦）:\n{m1[:3]}\n{m2[:3]}"


@test
def gen_speed_ramp_intervals_never_overlap():
    """`line_speed` 不过 `_splice`，区间必须自证互不重叠。"""
    from rpechart.gen import assert_disjoint, speed_ramp

    secs = [Section(0, 16, ROLE_B), Section(16, 48, ROLE_R), Section(48, 64, ROLE_N)]
    ms = MotionSpec(speeds={ROLE_B: 12.0, ROLE_R: 11.0, ROLE_N: 9.5})
    ramp = speed_ramp(secs, ms.speeds, 64.0, ms.default_speed)
    assert ramp, "线速 ramp 为空"
    for (a0, a1, _v0, _v1), (b0, b1, _w0, _w1) in zip(ramp, ramp[1:]):
        assert b0 >= a1 - 1e-9, f"线速区间重叠: {a0}-{a1} 与 {b0}-{b1}"
    # assert_disjoint 必须真的会抛
    from rpechart.gen import assert_disjoint

    assert_disjoint(ramp)
    try:
        assert_disjoint([(0.0, 10.0, 1, 1), (5.0, 15.0, 1, 1)])
    except ValueError:
        pass
    else:
        raise AssertionError("重叠区间没有抛 ValueError")


@test
def gen_speed_stays_inside_the_real_range():
    """线速必须锁在真实谱面区间 [8, 15]（旧实现硬阶跃到 16/18）。"""
    spec = ChartSpec.from_json(_spec_dict())
    cb = build(spec)
    for li, ln in enumerate(cb.chart.judgeLineList):
        for lay in ln.eventLayers:
            if not lay:
                continue
            for e in (lay.speedEvents or []):
                for v in (float(e.start), float(e.end)):
                    assert 8.0 <= v <= 15.0, f"线 {li} 速度 {v} 越界"


@test
def gen_line_speed_never_writes_a_constant_zero_moveX():
    """判定线必须真的横向动（「判定线没有动作」的根因）。"""
    spec = ChartSpec.from_json(_spec_dict())
    cb = build(spec)
    for li, ln in enumerate(cb.chart.judgeLineList):
        moves = [e for lay in ln.eventLayers if lay for e in (lay.moveXEvents or [])]
        if not moves:
            continue   # 未配摆动的线允许静止
        vals = [float(e.start) for e in moves] + [float(e.end) for e in moves]
        span = max(vals) - min(vals)
        assert span > 50.0, f"线 {li} 的 moveX 跨度只有 {span:.1f}（等于没动作）"


# ================================================================ 校验 / IO
@test
def gen_generated_chart_has_zero_validation_errors():
    """生成出的谱面必须通过 `rpechart.validate.validate()`，零 ERROR。"""
    spec = ChartSpec.from_json(_spec_dict())
    cb = build(spec)
    rep = cb.validate(song_duration=spec.duration)
    assert not rep.errors, rep.format(max_lines=20)


@test
def gen_rpechart_validate_module_reports_zero_errors():
    """直接调 `rpechart.validate.validate()`（而非 builder 的快捷方式）也必须零 ERROR。

    注意 `import rpechart.validate as V` 拿到的是 `rpechart/__init__.py` 重导出的
    **函数**；要拿模块必须 `importlib.import_module("rpechart.validate")`。
    """
    import importlib

    V = importlib.import_module("rpechart.validate")
    spec = ChartSpec.from_json(_spec_dict())
    cb = build(spec)
    rep = V.validate(cb.chart, song_duration=spec.duration)
    assert not rep.errors, rep.format(max_lines=20)
    assert len(cb.chart.judgeLineList) == 3


@test
def gen_chart_survives_save_and_load_round_trip():
    """`save_chart` 写出后 `load_chart` 读回，语义必须相等。"""
    spec = ChartSpec.from_json(_spec_dict())
    cb = build(spec)
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "chart.json")
        save_chart(cb.chart, p)
        back = load_chart(p)
    assert len(back.judgeLineList) == len(cb.chart.judgeLineList)
    a = json.dumps(cb.chart.to_json(), ensure_ascii=False, sort_keys=True)
    b = json.dumps(back.to_json(), ensure_ascii=False, sort_keys=True)
    assert a == b, "save → load 之后谱面变了"
    # 音符数量与类型分布也必须一致
    ta = [n.type for ln in cb.chart.judgeLineList for n in ln.notes]
    tb = [n.type for ln in back.judgeLineList for n in ln.notes]
    assert ta == tb, "音符序列在 round-trip 后变了"


@test
def gen_generate_writes_a_file_and_reports():
    """`generate` 要真的写出文件，并回一份带 report 的统计。"""
    spec = ChartSpec.from_json(_spec_dict())
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "out.json")
        res = generate(spec, out)
        assert os.path.isfile(out), "generate 没有写出文件"
        assert res["notes"] > 0
        assert res["lines"] == len(spec.lines)
        assert not res["report"].errors
        assert res["path"] == out
        # 写出的东西必须能被 load_chart 读回
        ch = load_chart(out)
        assert len(ch.judgeLineList) == len(spec.lines)


@test
def gen_notes_are_quantized_to_the_lane_grid():
    """落点必须量化到轨道网格（「键在乱飞」的根因）。"""
    spec = ChartSpec.from_json(_spec_dict())
    cb = build(spec)
    grid = spec.lane_grid()
    off = []
    for ln in cb.chart.judgeLineList:
        for n in ln.notes:
            x = float(n.positionX)
            i = (x - grid.lanes[0]) / grid.step
            if abs(i - round(i)) > 1e-6:
                off.append(x)
    assert not off, f"这些音符不在 {grid.step} 步长的轨道上: {sorted(set(off))[:10]}"


@test
def gen_adjacent_notes_never_jump_more_than_max_step_lanes():
    """时间紧邻的同线音符最多跨 `max_step` 轨。"""
    spec = ChartSpec.from_json(_spec_dict())
    cb = build(spec)
    grid = spec.lane_grid()
    for li, ln in enumerate(cb.chart.judgeLineList):
        notes = sorted(ln.notes, key=lambda n: float(n.startTime.value))
        for a, b in zip(notes, notes[1:]):
            dt = float(b.startTime.value) - float(a.startTime.value)
            if dt >= grid.tight_sec:
                continue
            gap = abs(float(b.positionX) - float(a.positionX)) / grid.step
            assert gap <= grid.max_step + 1e-6, (
                f"线 {li} 拍 {float(a.startTime.value):.3f}→{float(b.startTime.value):.3f} "
                f"跨了 {gap:.1f} 轨")


@test
def gen_holds_never_sit_on_a_tap_lane_inside_their_window():
    """Hold 在位期间不能有同轨 Tap（`TapHoldOverlapped` Error）。"""
    spec = ChartSpec.from_json(_spec_dict())
    cb = build(spec)
    for li, ln in enumerate(cb.chart.judgeLineList):
        holds = [n for n in ln.notes if n.type == NOTE_HOLD]
        taps = [n for n in ln.notes if n.type == NOTE_TAP]
        for h in holds:
            hs, he = float(h.startTime.value), float(h.endTime.value)
            hx = float(h.positionX)
            for t in taps:
                ts, tx = float(t.startTime.value), float(t.positionX)
                if abs(tx - hx) > 1e-6:
                    continue
                assert not (hs <= ts < he), (
                    f"线 {li} Tap@拍{ts}(x={tx}) 落在 Hold[{hs},{he})(x={hx}) 内")


@test
def gen_notes_are_sorted_non_hold_first_then_hold():
    """线内排序：非 Hold 全部在前、Hold 全部在后，各自按时间升序。

    这是 RPE v150+ 的落盘约定，也是原脚本 notes.sort(key=lambda n:
    (n.type == NOTE_HOLD, n.startTime.frac, n.positionX)) 的行为。所以整条线的
    startTime 序列**不是**全局升序 —— 断言必须分组看，否则这条测试会误报。
    """
    spec = ChartSpec.from_json(_spec_dict())
    cb = build(spec)
    saw_hold = False
    for li, ln in enumerate(cb.chart.judgeLineList):
        plain, holds = [], []
        in_hold = False
        for n in ln.notes:
            if n.type == NOTE_HOLD:
                saw_hold = in_hold = True
                holds.append(float(n.startTime.value))
            else:
                assert not in_hold, f"线 {li}: Hold 之后又出现了非 Hold 音符（排序错了）"
                plain.append(float(n.startTime.value))
        assert plain == sorted(plain), f"线 {li} 的非 Hold 段没有按时间升序"
        assert holds == sorted(holds), f"线 {li} 的 Hold 段没有按时间升序"
    assert saw_hold, "夹具里本该有 Hold，排序断言等于没测"


# ================================================================ 结构 / 自动切段
@test
def gen_auto_sections_tile_the_whole_song():
    """自动切段必须铺满整首歌（首段从 0 起、相邻无缝、末段盖到 last_beat）。"""
    n = 200
    rn, bn, warmth, low = [], [], [], []
    for i in range(n):
        phase = (i // 16) % 4
        if phase == 0:
            rn.append(0.0); bn.append(0.0); warmth.append(0.0)
        elif phase == 1:
            rn.append(0.0); bn.append(5000.0); warmth.append(-6.0)
        elif phase == 2:
            rn.append(5000.0); bn.append(0.0); warmth.append(3.0)
        else:
            rn.append(4000.0); bn.append(4000.0); warmth.append(0.0)
        low.append(1.0 if i % 4 == 0 else 0.2)
    f = BeatFeatures(low=low, motion=[0.2] * n, rn=rn, bn=bn, warmth=warmth,
                     bx=[0.0] * n, rx=[0.0] * n, cx=[0.0] * n,
                     smooth_window=0, slew_limit=0.0, center_slew_limit=0.0)
    secs = auto_sections(f, last_beat=float(n))
    assert secs, "自动切段返回空"
    assert secs[0].start == 0.0, f"首段不从 0 拍开始: {secs[0]}"
    for a, b in zip(secs, secs[1:]):
        assert abs(a.end - b.start) < 1e-9, f"段间有缝: {a} 与 {b}"
    assert secs[-1].end >= n - 1e-9, f"末段只到 {secs[-1].end}，音乐到 {n} 拍"
    assert sections_tile(secs, float(n)), "sections_tile 判定失败"
    # 每个段都必须能出音符（节奏型名必须在 OFFSETS 里）
    from rpechart.gen import OFFSETS

    for s in secs:
        assert OFFSETS.get(s.normal), f"{s} 的常规型 {s.normal!r} 没有落点"
        assert OFFSETS.get(s.dense), f"{s} 的密集型 {s.dense!r} 没有落点"


@test
def gen_section_carries_role_not_a_chinese_name():
    """段的「身份」是角色枚举，不是中文名（换曲子不必重写查表）。"""
    s = Section(0, 16, ROLE_B, density=2.0)
    assert s.role == "B"
    assert s.tone == "blue"          # 角色 → 默认色调
    d = s.to_json()
    assert d["role"] == "B"
    back = Section.from_json(d)
    assert back.role == "B" and back.tone == "blue"
    # 未知角色必须立刻报错（而不是静默回落）
    try:
        Section(0, 16, "主角")
    except ValueError:
        pass
    else:
        raise AssertionError("未知角色没有抛 ValueError")


@test
def gen_modules_hardcode_no_song_name_bpm_or_absolute_path():
    """生成层不允许写死曲名 / BPM / 段落边界 / 线数 / 绝对路径。

    这是把 原生成脚本泛化的**唯一理由**：原脚本把曲名、`153.6`、
    `LAST_BEAT=232`、四条线全焊在源码里。任何一条回流都算回归。

    文档字符串里**引用**历史事故是允许的（本模块的注释就在引用），所以先剔除
    docstring 再查真正的代码行。
    """
    import ast
    import re

    import rpechart.gen as G

    base = os.path.dirname(os.path.abspath(G.__file__))
    banned = [
        (re.compile(r"[A-Za-z]:\\\\"), "绝对 Windows 路径"),
        (re.compile("\u5492\u672f|\u5bbf\u50a9|\u4e94\u6761\u609f"), "曲子/角色名"),
        (re.compile(r"\b153\.6\b|\b92\.58\b"), "该曲专属数值"),
    ]

    def _docstring_lines(src):
        lines = set()
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                continue
            body = getattr(node, "body", None) or []
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                for ln in range(first.lineno, (first.end_lineno or first.lineno) + 1):
                    lines.add(ln)
        return lines

    checked = 0
    for name in sorted(os.listdir(base)):
        if not name.endswith(".py"):
            continue
        src = open(os.path.join(base, name), encoding="utf-8").read()
        docs = _docstring_lines(src)
        for i, line in enumerate(src.splitlines(), 1):
            if i in docs:
                continue
            code = line.split("#", 1)[0]
            for pat, why in banned:
                assert not pat.search(code), f"{name}:{i} 写死了{why}: {line.strip()}"
            checked += 1
    assert checked > 500, f"只扫到 {checked} 行，扫描逻辑本身出错了"

@test
def gen_cli_dry_run_writes_nothing_and_returns_zero():
    """`--dry` 必须不写文件、不编码视频（干跑要快）。"""
    from rpechart.gen import cli

    spec = ChartSpec.from_json(_spec_dict())
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "s.json")
        spec.write(sp)
        out = os.path.join(td, "should_not_exist.json")
        rc = cli.main([sp, "-o", out, "--dry", "--quiet"])
        assert rc == 0, f"--dry 应返回 0，实际 {rc}"
        assert not os.path.exists(out), "--dry 却写出了文件"


@test
def gen_cli_writes_a_chart_and_parses_video_flags():
    """CLI 必须能写出谱面，且 `--video*` 参数被 argparse 收下。"""
    from rpechart.gen import cli

    spec = ChartSpec.from_json(_spec_dict())
    with tempfile.TemporaryDirectory() as td:
        sp = os.path.join(td, "s.json")
        spec.write(sp)
        out = os.path.join(td, "out.json")
        rc = cli.main([sp, "-o", out, "--quiet"])
        assert rc == 0, f"CLI 返回 {rc}"
        assert os.path.isfile(out), "CLI 没有写出 chart.json"
        # --video* 参数表必须存在且类型正确
        ns = cli.build_parser().parse_args(
            [sp, "--video", "--video-out", "x.mp4", "--video-fps", "24",
             "--video-size", "854x480", "--video-from", "10", "--video-to", "13"])
        assert ns.video is True and ns.video_out == "x.mp4"
        assert ns.video_fps == 24.0 and ns.video_size == "854x480"
        assert ns.video_from == 10.0 and ns.video_to == 13.0


@test
def gen_cli_video_uses_the_same_chart_file():
    """视频与谱面必须来自**同一份 chart.json**（双输出正确性）。"""
    import inspect

    from rpechart.gen import cli

    src = inspect.getsource(cli.main)
    assert "render_video(out" in src, "没把写出的谱面路径交给视频渲染"
    rv = inspect.getsource(cli.render_video)
    assert "render_chart_video" in rv, "视频入口没走 rpechart 的通用编码器"


@test
def gen_palette_and_captions_come_from_the_spec():
    """色调 / 文字卡点必须来自 spec，不能写死段名或秒数。"""
    d = _spec_dict()
    d["captions"] = {"8.0": "第一节"}
    d["palette"] = {"blue": [[1, 2, 3], [4, 5, 6]]}
    spec = ChartSpec.from_json(d)
    cb = build(spec)
    ln = cb.chart.judgeLineList[-1]
    # 特殊事件挂在 judgeLine.extended 上（不是 eventLayers 的第五层）
    texts = list(ln.extended.textEvents or [])
    assert texts, "captions 没有生成 textEvents"
    assert str(texts[0].start) == "第一节", texts[0].start
    colors = list(ln.extended.colorEvents or [])
    assert colors, "palette 没有生成 colorEvents"
    assert list(colors[0].start) == [1, 2, 3], colors[0].start


@test
def gen_synth_features_roles_follow_the_sections():
    """没有 beats.json 时，合成特征的角色必须与段表逐拍一致。"""
    spec = ChartSpec.from_json({
        "name": "synth", "bpm": 120.0, "last_beat": 32.0,
        "sections": [{"start": 0, "end": 16, "role": "B"},
                     {"start": 16, "end": 32, "role": "R"}],
        "lines": [{"name": "L0"}],
    })
    secs = spec.section_list(None)
    f = assemble.synth_features(spec, secs)
    assert f.n >= 32
    assert f.role(0) == ROLE_B, f.role(0)
    assert f.role(15) == ROLE_B
    assert f.role(16) == ROLE_R
    assert f.role(31) == ROLE_R


@test
def gen_full_generate_from_a_bare_spec_has_no_errors():
    """「AI 只写一个 spec.json 就能出谱」：不给 beats、不给 sections 也要零 ERROR。"""
    spec = ChartSpec.from_json({
        "name": "裸 spec 也能出谱",
        "bpm": 174.0,
        "duration": 45.0,
        "last_beat": 128.0,
        "auto_sections": True,
        "lines": [{"name": "L0", "y": -160.0}, {"name": "L1", "y": 160.0}],
        "motion": {"sways": {"0": {"x_amp": 100.0, "x_period": 34.0,
                                   "y_amp": 30.0, "y_period": 51.0,
                                   "rot_amp": 4.0, "rot_period": 21.0}},
                   "gates": {"B": 0.9, "R": 0.8, "N": 0.4},
                   "speeds": {"B": 12.0, "R": 11.0, "N": 9.5}},
    })
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "bare.json")
        res = generate(spec, out)
        assert res["notes"] > 0, "裸 spec 一个音符都没出"
        assert not res["report"].errors, res["report"].format(max_lines=20)
        assert len(load_chart(out).judgeLineList) == 2
