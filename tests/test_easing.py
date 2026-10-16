"""29 种缓动的实现正确性测试。

基准值取自 <https://easings.net/zh-cn> 的公开数值（四舍五入到 6 位），
以及 phira-docs 官方 Python 参考实现的定义。
"""

import math

from run_tests import approx, test

from rpechart.easing import (
    EASE_FUNCS,
    EASE_NAMES,
    MAX_EASING,
    OVERSHOOT,
    bezier,
    ease,
    easing_id,
    easing_name,
    overshoot_bounds,
)


@test
def table_sizes():
    assert len(EASE_FUNCS) == 29
    assert len(EASE_NAMES) == 29
    assert MAX_EASING == 29


@test
def every_easing_hits_both_ends():
    """所有缓动都应该满足 f(0)=0、f(1)=1（elastic/back/bounce 也不例外）。"""
    for i in range(1, 30):
        f = EASE_FUNCS[i - 1]
        approx(f(0.0), 0.0, 1e-12, f"easing {i} ({EASE_NAMES[i-1]}) at 0")
        approx(f(1.0), 1.0, 1e-12, f"easing {i} ({EASE_NAMES[i-1]}) at 1")


@test
def every_easing_is_monotone_ish_bounded():
    """所有缓动的取值都必须落在实测过冲包络内。

    包络不是拍脑袋的常数：back 族（20/21/23）标准过冲 ±10%，elastic 族
    （24/25/29）按 easings.net 的 c4 = 2π/3 构造最大到 1.3731 —— 所以
    「统一 1.2 上界」是错的，out_elastic 在 t≈0.15 处合法地到 1.373。
    """
    for i in range(1, 30):
        f = EASE_FUNCS[i - 1]
        lo, hi = overshoot_bounds(i)
        for step in range(0, 2001):
            t = step / 2000.0
            v = f(t)
            assert math.isfinite(v), f"easing {i} 在 t={t} 得到 {v}"
            assert lo <= v <= hi, (
                f"easing {i} ({EASE_NAMES[i-1]}) t={t} -> {v} 越出包络 [{lo}, {hi}]"
            )


@test
def overshoot_table_matches_measurement():
    """OVERSHOOT 表必须与 2001 点实测结果一致（防止表被手改后失效）。"""
    for i in range(1, 30):
        f = EASE_FUNCS[i - 1]
        vs = [f(k / 2000.0) for k in range(2001)]
        lo, hi = OVERSHOOT[i - 1]
        approx(min(vs), lo, 1e-6, f"easing {i} 实测最小值")
        approx(max(vs), hi, 1e-6, f"easing {i} 实测最大值")


#: 三族「非平凡」缓动 —— 它们刻意不单调，需要各自单独的形状断言。
BACK = {20, 21, 23}
ELASTIC = {24, 25, 29}
BOUNCE = {26, 27, 28}


def _sample(f, n: int = 2000) -> list[float]:
    return [f(k / n) for k in range(n + 1)]


def _sign_flips(vs: list[float], eps: float = 1e-12) -> int:
    signs = [1 if b > a + eps else (-1 if b < a - eps else 0) for a, b in zip(vs, vs[1:])]
    signs = [s for s in signs if s]
    return sum(1 for a, b in zip(signs, signs[1:]) if a != b)


def _extrema(vs: list[float]) -> list[tuple[float, int]]:
    """平台感知的极值检测，返回 [(值, 1=峰 / -1=谷)]。"""
    out: list[tuple[float, int]] = []
    k, n = 1, len(vs)
    while k < n - 1:
        start = k
        while k < n - 1 and vs[k + 1] == vs[k]:
            k += 1
        prev_v, next_v = vs[start - 1], vs[k + 1]
        if vs[start] > prev_v and vs[start] > next_v:
            out.append((vs[start], 1))
        elif vs[start] < prev_v and vs[start] < next_v:
            out.append((vs[start], -1))
        k += 1
    return out


@test
def plain_easings_are_monotone_and_bounded():
    """除 back/elastic/bounce 三族外，缓动必须单调且严格落在 [0,1]。"""
    for i in range(1, 30):
        if i in BACK | ELASTIC | BOUNCE:
            continue
        vs = _sample(EASE_FUNCS[i - 1])
        lo, hi = min(vs), max(vs)
        assert lo >= -1e-12 and hi <= 1 + 1e-12, f"easing {i} 越界: [{lo}, {hi}]"
        for a, b in zip(vs, vs[1:]):
            assert b >= a - 1e-9, f"easing {i} 非单调 @t≈{a}"
        assert _sign_flips(vs) == 0, f"easing {i} 出现方向翻转"


@test
def back_family_has_expected_overshoot():
    """back 族：越过边界后单次回摆 —— 翻转次数有确定上界。"""
    # 20 out_back    : 上行冲过 1 → 回落至 1       （越上界，1 次翻转）
    # 21 in_back     : 先跌破 0 → 上行至 1         （越下界，1 次翻转）
    # 23 in_out_back : 跌破 0 → 冲过 1 → 回落至 1  （两侧都越界，2 次翻转）
    cases = {20: (None, 1.0, 1), 21: (0.0, None, 1), 23: (0.0, 1.0, 2)}
    for i, (below, above, max_flips) in cases.items():
        vs = _sample(EASE_FUNCS[i - 1])
        lo, hi = min(vs), max(vs)
        if below is not None:
            assert lo < below - 1e-9, f"easing {i} 未跌破 {below}: min={lo}"
        if above is not None:
            assert hi > above + 1e-9, f"easing {i} 未冲过 {above}: max={hi}"
        flips = _sign_flips(vs)
        assert flips == max_flips, f"easing {i} 方向翻转 {flips} 次，期望 {max_flips}"
        # back 的过冲幅度有解析上界 |c1|/(c1+1) ≈ 0.1000（c1 = 1.70158）
        assert lo >= -0.1002 and hi <= 1.1002, f"easing {i} 过冲超出 back 解析上界"


@test
def elastic_family_decays_after_peak():
    """elastic 族：围绕 1.0 振荡，包络在过最大偏差点后单调衰减。"""
    for i in ELASTIC:
        vs = _sample(EASE_FUNCS[i - 1])
        lo, hi = min(vs), max(vs)
        assert lo >= -0.3732 and hi <= 1.3732, f"easing {i} 越界: [{lo}, {hi}]"
        ext = _extrema(vs)
        assert len(ext) >= 3, f"easing {i} 仅 {len(ext)} 个极值，不像 elastic"
        for (_, da), (_, db) in zip(ext, ext[1:]):
            assert da != db, f"easing {i} 连续同类极值"
        amp = [abs(v - 1.0) for v, _ in ext]
        peak_at = amp.index(max(amp))
        for a, b in zip(amp[peak_at:], amp[peak_at + 1 :]):
            assert b < a - 1e-12, f"easing {i} 峰值后振幅未衰减: {a} -> {b}"
        assert max(amp) > 0.1, f"easing {i} 最大偏差仅 {max(amp)}"
        approx(vs[-1], 1.0, 1e-12, f"easing {i} 末值")


@test
def bounce_family_oscillates_within_unit_interval():
    """bounce 族：永不越界，但内部有多次「掉下去再弹回」的振荡。"""
    expected_flips = {26: 6, 27: 6, 28: 12}
    for i in BOUNCE:
        vs = _sample(EASE_FUNCS[i - 1], 4000)
        lo, hi = min(vs), max(vs)
        assert lo >= -1e-12 and hi <= 1 + 1e-12, f"easing {i} 越界: [{lo}, {hi}]"
        flips = _sign_flips(vs)
        assert flips == expected_flips[i], f"easing {i} 翻转 {flips} 次，期望 {expected_flips[i]}"
        approx(vs[0], 0.0, 1e-12)
        approx(vs[-1], 1.0, 1e-12)


@test
def in_out_mirror_identities_hold():
    """In/Out 互为镜像：``f_in(t) == 1 - f_out(1 - t)``。

    这是两个独立实现（PhiStudio `case 27/25/21`、phira `f2!` 宏
    ``|x| 1 - fn(1 - x)``）都严格成立的构造性恒等式，因此可以当成硬断言。
    """
    for out_id, in_id in [(20, 21), (24, 25), (26, 27)]:
        f_out, f_in = EASE_FUNCS[out_id - 1], EASE_FUNCS[in_id - 1]
        for k in range(1001):
            t = k / 1000.0
            approx(f_in(t), 1.0 - f_out(1.0 - t), 1e-12, f"in{in_id}({t}) vs 1-out{out_id}(1-{t})")


@test
def in_out_family_shared_invariants():
    """所有 InOut 缓动共有的不变量：端点、中点、点对称。

    注意这里**刻意不断言** InOut 的具体构造。InOut-Back 存在两个都公开发表的
    变体，且两个实现各自选了不同的一支：

    * easings.net / PhiStudio `case 23`：``c2 = c1 * 1.525``（本库采用）
    * phira ``RPE_TWEEN_MAP`` 的 ``e(Back, InOut)``：通用 ``f3!`` 宏，
      即「把 In 函数压缩到两半」``f_in(2t)/2``

    两者在 t=0.1 处相差约 0.066，在 t=0.5/0/1 处一致，且都满足下面的
    点对称与中点条件 —— 所以这些才是可以无条件断言的共同性质。
    """
    for i in (6, 7, 12, 13, 22, 23, 28, 29):
        f = EASE_FUNCS[i - 1]
        approx(f(0.0), 0.0, 1e-12, f"easing {i} 起点")
        approx(f(1.0), 1.0, 1e-12, f"easing {i} 终点")
        approx(f(0.5), 0.5, 1e-12, f"easing {i} 中点")
        for k in range(1001):
            t = k / 1000.0
            approx(f(t) + f(1.0 - t), 1.0, 1e-12, f"easing {i} 点对称 @{t}")


@test
def in_out_back_uses_easings_net_constant():
    """锁死本库对 #23 的选择：``c2 = 1.525 * c1``，而不是 phira 的 ``f3!(back)``。

    这条测试的作用是「防止无意间漂移」：如果将来有人把实现改成 phira 的
    ``f3!`` 版本，这里会失败，提醒他这是一个**有意的语义选择**、需要同时更新
    文档与 goldens，而不是一次普通的重构。
    """
    c1 = 1.70158
    c2 = c1 * 1.525

    def easings_net_io_back(t: float) -> float:
        if t < 0.5:
            return (2 * t) ** 2 * ((c2 + 1) * 2 * t - c2) / 2
        return ((2 * t - 2) ** 2 * ((c2 + 1) * (t * 2 - 2) + c2) + 2) / 2

    f = EASE_FUNCS[22]
    for k in range(1001):
        t = k / 1000.0
        approx(f(t), easings_net_io_back(t), 1e-12, f"io back @{t}")

    # 与 phira 的 f3!(in_back) 变体必须**不同** —— 否则说明上面那段注释已经过期
    in_back = EASE_FUNCS[20]

    def phira_f3_in_back(t: float) -> float:
        return in_back(2 * t) / 2 if t < 0.5 else 1.0 - in_back(2 - 2 * t) / 2

    gap = max(abs(f(k / 1000.0) - phira_f3_in_back(k / 1000.0)) for k in range(1001))
    assert gap > 0.05, f"与 phira f3! 变体的差异只有 {gap}，两者本应明显不同"


@test
def known_values_from_easings_net():
    approx(ease(1, 0.5), 0.5)  # linear
    approx(ease(2, 0.5), math.sin(math.pi / 4))  # out sine
    approx(ease(3, 0.5), 1 - math.cos(math.pi / 4))  # in sine
    approx(ease(4, 0.5), 0.75)  # out quad
    approx(ease(5, 0.5), 0.25)  # in quad
    approx(ease(6, 0.5), 0.5)  # in out sine
    approx(ease(7, 0.5), 0.5)  # in out quad
    approx(ease(8, 0.5), 1 - 0.5**3)  # out cubic = 0.875
    approx(ease(9, 0.5), 0.125)  # in cubic
    approx(ease(16, 0.5), 1 - 2**-5)  # out expo = 0.96875
    approx(ease(17, 0.5), 2**-5)  # in expo = 0.03125
    approx(ease(18, 0.5), math.sqrt(1 - 0.25))  # out circ
    approx(ease(19, 0.5), 1 - math.sqrt(1 - 0.25))  # in circ
    approx(ease(26, 1.0), 1.0)  # out bounce


@test
def symmetric_pairs():
    """In X 与 Out X 应互为镜像：in(t) = 1 - out(1-t)。"""
    pairs = [(2, 3), (4, 5), (8, 9), (10, 11), (14, 15), (16, 17), (18, 19), (20, 21)]
    for out_id, in_id in pairs:
        for step in range(0, 21):
            t = step / 20.0
            a = ease(in_id, t)
            b = 1.0 - ease(out_id, 1.0 - t)
            approx(a, b, 1e-9, f"in{in_id}({t}) vs 1-out{out_id}(1-{t})")


@test
def name_lookup_roundtrip():
    for i, name in enumerate(EASE_NAMES, start=1):
        assert easing_id(name) == i, f"{name} -> {easing_id(name)} != {i}"
        assert easing_name(i) == name
    # 别名与容错
    assert easing_id("Linear") == 1
    assert easing_id("2") == 2
    assert easing_name(29) == "in_out_elastic"
    # 未知缓动回退 linear（PhiStudio 的行为）
    assert easing_id("no_such_easing") == 1
    assert easing_id(None) == 1
    assert easing_id(999) == 1


@test
def bezier_is_monotone_and_clamped():
    p = (0.25, 0.1, 0.25, 1.0)  # CSS ease
    approx(bezier(0.0, p), 0.0)
    approx(bezier(1.0, p), 1.0)
    prev = -1.0
    for step in range(0, 21):
        v = bezier(step / 20.0, p)
        assert v >= prev - 1e-9, f"贝塞尔在该参数下非单调: t={step/20} {prev} -> {v}"
        prev = v
