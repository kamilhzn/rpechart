"""判定线父链（father）在渲染侧的行为 —— 回归用例。

两条都是**渲染期**缺陷，`validate` 抓不到（`Renderer`/`RenderModel` 从不调用它）：

* `LineRuntime` 曾用 `int(getattr(line, "father", -1) or -1)` 解析父线，
  裸 `or` 把 falsy 的 `0` 吞成 `-1`：`father = 0` 是合法的「父线是第 0 条」，
  而 `tests/fixtures/charts/multi.json` 的 `child` 线正是 `father: 0`，
  于是父链在渲染时静默断开、子线不跟随父线变换。
* `_line_pose` 与 `line_origin` 互相递归且没有深度保护，谱面里一旦有
  `A→B→C→B` 这类环（`validate` 会报 `FatherLoop`，渲染路径不经过它），
  每一帧都抛 `RecursionError` —— 整个 `render video` 直接崩。
"""

import os

from run_tests import approx, test

_MAX = 64  # 与 timeline.py 的深度上限无关，这里只用来构造够深的合法链

_FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests",
    "fixtures",
    "charts",
    "multi.json",
)


def _line(name, father, *, rot_end=0.0, x=0.0, y=0.0, rotate_with_father=True):
    """一条最小判定线：固定起点 x/y 与一段 0→rot_end 的旋转事件。"""
    return {
        "Name": name,
        "father": father,
        "rotateWithFather": rotate_with_father,
        "eventLayers": [{
            "moveXEvents": [{"startTime": [0, 0, 1], "endTime": [1, 0, 1], "start": x,
                             "end": x, "easingType": 1, "easingLeft": 0.0,
                             "easingRight": 1.0}],
            "moveYEvents": [{"startTime": [0, 0, 1], "endTime": [1, 0, 1], "start": y,
                             "end": y, "easingType": 1, "easingLeft": 0.0,
                             "easingRight": 1.0}],
            "rotateEvents": [{"startTime": [0, 0, 1], "endTime": [1, 0, 1], "start": 0.0,
                              "end": rot_end, "easingType": 1, "easingLeft": 0.0,
                              "easingRight": 1.0}],
        }],
        "notes": [{"type": 1, "startTime": [0, 0, 1], "endTime": [0, 0, 1],
                   "positionX": 0.0, "above": 1, "alpha": 255, "isFake": 0, "size": 1.0,
                   "speed": 1.0, "visibleTime": 999999.0, "yOffset": 0.0}],
    }


def _chart(lines):
    """用 dict 构造谱面再走 Chart.from_json。

    直接 JudgeLine(...) 不会自动建 eventLayers，渲染时会 IndexError，
    所以用例一律从 JSON 形状出发（与真实读盘路径一致）。
    """
    from rpechart.model import Chart

    return Chart.from_json({
        "BPMList": [{"bpm": 150.0, "startTime": [0, 0, 1]}],
        "META": {"name": "father-cases"},
        "judgeLineList": lines,
    })


@test
def render_model_keeps_father_zero():
    """father = 0 必须原样保留，不能被当成「没有父线」。"""
    from rpechart.render.timeline import RenderModel

    chart = _chart([_line("parent", -1), _line("child", 0)])
    assert chart.judgeLineList[1].father == 0, chart.judgeLineList[1].father
    model = RenderModel(chart)
    assert model.lines[0].father == -1, model.lines[0].father
    assert model.lines[1].father == 0, (
        "渲染侧把 father=0 改写成了 %r，父链静默断开" % (model.lines[1].father,)
    )


@test
def render_model_keeps_the_fixture_father_link():
    """官方样本 multi.json 的 child 线就是 father: 0，父链必须活着。"""
    if not os.path.isfile(_FIXTURE):
        return  # 样本可单独分发，缺失时跳过而不是失败
    from rpechart.io import load_chart
    from rpechart.render.timeline import RenderModel

    chart = load_chart(_FIXTURE)
    model = RenderModel(chart)
    fathers = [rt.father for rt in model.lines]
    assert 0 in fathers[1:], "样本里的 father=0 没活下来: %r" % (fathers,)


@test
def render_model_child_origin_includes_the_parent_pose():
    """子线原点必须带上父线的旋转 —— 这是 or -1 吞掉 0 之后丢掉的效果。"""
    from rpechart.render.timeline import RenderModel

    # 父线转 90°，子线不动且在 (0, 0)：子线原点应等于父线原点，角度叠加 90°
    chart = _chart([_line("parent", -1, rot_end=90.0), _line("child", 0)])
    model = RenderModel(chart)
    px, py, pang = model.line_origin(0, 0.5, 160, 90)
    cx, cy, cang = model.line_origin(1, 0.5, 160, 90)
    approx(cx, px, 1e-6, "子线在 (0,0)，原点应等于父线原点")
    approx(cy, py, 1e-6, "子线在 (0,0)，原点应等于父线原点")
    # 屏幕 Y 轴朝下，geometry.screen_angle 把 RPE 的 +90° 映射成 −90°；
    # 子线自己没转，它的角度应当**就是**父线角度（继承），而不是再加一遍。
    approx(cang, pang, 1e-6, "子线应继承父线旋转")
    approx(pang, -90.0, 1e-6, "父线 90° 在屏幕坐标里是 −90°")

    # 子线离父线原点 100 RPE-X：父线转了 90°，这个偏移必须跟着一起转
    from rpechart.render import geometry as G

    chart2 = _chart([_line("parent", -1, rot_end=90.0), _line("child", 0, x=100.0)])
    model2 = RenderModel(chart2)
    px2, py2, _ = model2.line_origin(0, 0.5, 160, 90)
    cx2, cy2, _ = model2.line_origin(1, 0.5, 160, 90)
    dx = 100.0 * G.px_per_rpe_x(160)
    dy = -0.0 * G.px_per_rpe_y(90)
    rdx, rdy = G.rotate_point(dx, dy, -90.0)  # 父线 90° 在屏幕上是 −90°
    approx(cx2, px2 + rdx, 1e-6, "子线偏移要按父线角度旋转")
    approx(cy2, py2 + rdy, 1e-6, "子线偏移要按父线角度旋转")
    assert (cx2, cy2) != (px2, py2), "偏移没跟着父线转，父链等于没接上"
    # 父线不转时，偏移必须是纯平移 —— 证明上面那个差值是旋转带来的
    chart_t = _chart([_line("parent", -1, rot_end=0.0), _line("child", 0, x=100.0)])
    model_t = RenderModel(chart_t)
    tx, ty, _ = model_t.line_origin(1, 0.5, 160, 90)
    approx(tx, 80.0 + dx, 1e-6, "父线不转时偏移不旋转")
    approx(ty, 45.0, 1e-6, "父线不转时偏移不旋转")

    # rotateWithFather=False 只关掉旋转的**继承**，原点仍跟父线走
    chart3 = _chart([_line("parent", -1, rot_end=90.0),
                     _line("child", 0, x=100.0, rotate_with_father=False)])
    model3 = RenderModel(chart3)
    px3, py3, pang3 = model3.line_origin(0, 0.5, 160, 90)
    cx3, cy3, cang3 = model3.line_origin(1, 0.5, 160, 90)
    approx(cang3, G.screen_angle(0.0), 1e-6, "关掉继承后子线用自己的角度")
    assert abs(cang3 - pang3) > 1.0, "关掉继承后不应再等于父线角度"
    assert (cx3, cy3) != (px3, py3), "只关旋转，原点仍要跟父线走"


@test
def render_model_survives_a_father_loop():
    """A→B→C→B 这类环在渲染侧必须截断，而不是 RecursionError。"""
    from rpechart.render.timeline import RenderModel

    chart = _chart([_line("A", 1), _line("B", 2), _line("C", 1)])
    model = RenderModel(chart)
    for index in range(3):
        x, y, angle = model.line_origin(index, 0.5, 160, 90)
        for value in (x, y, angle):
            assert isinstance(value, float) and value == value, value  # 非 NaN
    scene = model.scene_at(0.5, 160, 90, None)
    assert scene is not None, "环谱面仍要能求出一帧"


@test
def render_model_survives_a_self_father():
    """father = 0 指向自己的单线谱面同样不能递归爆栈。"""
    from rpechart.render.timeline import RenderModel

    model = RenderModel(_chart([_line("self", 0)]))
    x, y, angle = model.line_origin(0, 0.5, 160, 90)
    assert isinstance(x, float) and isinstance(y, float) and isinstance(angle, float)
    assert model.scene_at(0.5, 160, 90, None) is not None


@test
def render_model_survives_a_deep_legal_father_chain():
    """合法但很深的父链（远超深度上限）必须仍然能渲染，只是不再往上叠。"""
    from rpechart.render.timeline import RenderModel

    lines = [_line("L0", -1, rot_end=1.0)]
    for i in range(1, _MAX):
        lines.append(_line("L%d" % i, i - 1, rot_end=1.0))
    model = RenderModel(_chart(lines))
    x, y, angle = model.line_origin(_MAX - 1, 0.5, 160, 90)
    assert isinstance(x, float) and isinstance(y, float) and isinstance(angle, float)
    assert model.scene_at(0.5, 160, 90, None) is not None
