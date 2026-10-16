"""渲染子系统的统计与几何测试。

``rpechart.render.RenderModel`` 是制谱的收尾检查工具：``stats`` 会报出
「哪些区间完全没有音符」。**这个能力本身就是一次真实缺陷的产物** —— 生成器
曾经把一整段 Tap 删光只留 Hold，谱面后半截空白，在 PhiEdit 里播放才被发现。
有了 ``stats`` 就能在安装前抓出来。

这里钉住的性质：

* ``content_beats`` 必须是**内容真正结束处**，不能把渲染尾巴（+8 拍）当成
  「谱面缺了一段」报出来（那会造成假告警，让人去修不存在的 bug）；
* 空段必须真的数得出来（否则工具失效而无人察觉）；
* 下落高度**按秒积分**（旧渲染器按拍积分，整体快 ``BPM/106.5`` 倍）。
"""

from run_tests import approx, test


def _load_render():
    """返回渲染子系统模块（保留旧名字，用例仍在用）。"""
    from rpechart.render import timeline

    return timeline


def _chart_with_notes(*beat_ranges):
    """造一张只有一条线的最小谱面，notes 落在给定的 (起拍, 止拍) 区间里。"""
    from rpechart import Beat, Chart, JudgeLine, Note, BPMItem

    notes = []
    for a, b in beat_ranges:
        t = float(a)
        while t < b:
            notes.append(Note(type=1, startTime=Beat(t), endTime=Beat(t)))
            t += 1.0
    ch = Chart(BPMList=[BPMItem(bpm=120.0, startTime=Beat(0))],
               judgeLineList=[JudgeLine(Name="L", notes=notes)])
    return ch


@test
def renderer_content_end_excludes_the_render_pad():
    """内容结束处是最后一个音符，不是「最后一个音符 + 8 拍」。"""
    mod = _load_render()
    if mod is None:
        return
    ch = _chart_with_notes((0, 41))          # 最后一个音符落在第 40 拍
    m = mod.RenderModel(ch)
    assert m.content_beats == 40.0, m.content_beats
    assert m.n_beats == 48.0, m.n_beats


@test
def renderer_stats_counts_empty_ranges():
    """中间挖空的谱面必须被 stats 数出空段。"""
    mod = _load_render()
    if mod is None:
        return
    ch = _chart_with_notes((0, 8), (24, 32))     # 8~24 拍是空的
    rows = mod.RenderModel(ch).stats(8.0)

    empties = [b for _, b, tot, _, _ in rows if tot == 0]
    assert empties, f"应当报出空段，实际 stats 输出: {rows}"
    # 8~24 拍之间必然落着一个空桶
    assert any(8.0 <= b < 24.0 for b in empties), f"空段应落在 8~24 拍: {empties}"


@test
def renderer_stats_reports_no_empty_for_contiguous_chart():
    """连续写满的谱面不能报出任何空段（假告警会让人去修不存在的 bug）。"""
    mod = _load_render()
    if mod is None:
        return
    ch = _chart_with_notes((0, 40))
    rows = mod.RenderModel(ch).stats(8.0)

    assert rows, "stats 应该至少产出一行"
    empties = [b for _, b, tot, _, _ in rows if tot == 0]
    assert not empties, f"连续谱面不该有空段，却报了 {empties}"


@test
def renderer_falls_at_the_rpe_speed_not_per_beat():
    """下落高度按**秒**积分：线速 1 时每秒约 120.23 RPE-Y。

    旧渲染器按拍积分（``acc += v * (1/64)``），在 120 BPM 下 1 秒是 2 拍，
    于是下落变成 2 倍速；一般地快 ``BPM / 106.5`` 倍。这条测试用 120 BPM
    的谱面把两种口径区分开：正确值 ≈ 120.23 Y，按拍积分会给出 ≈ 240 Y。
    """
    mod = _load_render()
    if mod is None:
        return
    from rpechart import (BPMItem, Beat, Chart, EventLayer, JudgeLine, Note,
                          SpeedEvent)

    line = JudgeLine(
        Name="L",
        notes=[Note(type=1, startTime=Beat(0), endTime=Beat(0))],
        eventLayers=[EventLayer(speedEvents=[SpeedEvent(
            startTime=Beat(0), endTime=Beat(16), start=1.0, end=1.0)])])
    ch = Chart(BPMList=[BPMItem(bpm=120.0, startTime=Beat(0))], judgeLineList=[line])
    floor = mod.RenderModel(ch).lines[0].floor

    approx(floor.at(1.0), 120.229, 0.5, "线速 1 时 1 秒应落约 120.23 RPE-Y")
    approx(floor.at(2.0), 240.458, 1.0, "两秒应是两倍")


@test
def renderer_uses_the_charts_own_timing():
    """统计窗口的秒数换算要用谱面自己的 BPM，不能写死 153.6。"""
    mod = _load_render()
    if mod is None:
        return
    ch = _chart_with_notes((0, 16))          # 造的是 120 BPM
    rows = mod.RenderModel(ch).stats(4.0)

    # 120 BPM ⇒ 每拍 0.5s ⇒ 第 8 拍是 4.0s
    row = [x for x in rows if x[1] == 8.0][0]
    approx(row[0], 4.0, 1e-6, "第 8 拍应是 4.0s（120 BPM）")


@test
def scene_at_is_a_pure_function_of_time():
    """同一个 ``t`` 反复求值必须得到同一幅场景（渲染可以并行/续渲）。"""
    mod = _load_render()
    if mod is None:
        return
    ch = _chart_with_notes((0, 8))
    m = mod.RenderModel(ch)
    a = m.scene_at(2.0, 320, 180)
    b = m.scene_at(2.0, 320, 180)
    assert len(a.notes) == len(b.notes)
    for x, y in zip(a.notes, b.notes):
        approx(x.x, y.x, 1e-9, "同一时刻的音符 x 必须一致")
        approx(x.y, y.y, 1e-9, "同一时刻的音符 y 必须一致")
        approx(x.alpha, y.alpha, 1e-9, "同一时刻的音符 alpha 必须一致")


@test
def scene_at_draws_the_hud_from_the_chart_meta():
    """HUD 的曲名/难度取自 META，不是写死的字符串。"""
    mod = _load_render()
    if mod is None:
        return
    from rpechart import Meta

    ch = _chart_with_notes((0, 8))
    ch.META = Meta(name="测试曲", level="IN Lv.15")
    scene = mod.RenderModel(ch).scene_at(1.0, 978, 642)
    texts = {t.text for t in scene.texts}
    assert "测试曲" in texts, f"HUD 里应当出现曲名，实际 {texts}"
    assert "IN Lv.15" in texts, f"HUD 里应当出现难度，实际 {texts}"
    assert any(t.text == "combo" for t in scene.texts), f"缺少 combo 标签: {texts}"


