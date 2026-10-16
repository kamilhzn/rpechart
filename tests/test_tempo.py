"""BPM 估计测试（rpechart.tempo）。

这里的音频全部是**程序合成的**，不依赖任何外部文件：用衰减正弦在整拍位置
打点，得到一个 BPM 已知的「点击轨」。这样测的是算法本身，而不是某个素材。

覆盖：
* 包络/自相关等纯函数的行为与边界；
* `_grid_score` 的空间格率惩罚是否能把八度错误压下去；
* `estimate_tempo` 端到端在各种 BPM 下是否给出正确值（含非整十 BPM）；
* `best_grid_fit` 从已知重拍反推 BPM；
* `decode_mono` 在缺 ffmpeg 时的报错路径。
"""

import math
import os
import shutil
import struct
import tempfile
import wave

from run_tests import approx, raises, test

from rpechart import tempo as T
from rpechart.audio import ffmpeg_path

# ---------------------------------------------------------------- 合成音频


def _click_track(path, bpm, *, seconds=40.0, sample_rate=22050, tone=1000.0):
    """写一个 BPM 已知的点击轨：每拍一个 1000Hz 衰减正弦。

    衰减长度 400 个采样（约 18ms），远短于任何合理拍长，所以每个起始点
    在包络上是孤立的尖峰。
    """
    n = int(seconds * sample_rate)
    buf = [0.0] * n
    period = 60.0 / bpm * sample_rate
    i = 0
    while True:
        t = i * period
        if t >= n:
            break
        s = int(t)
        for k in range(400):
            if s + k < n:
                buf[s + k] += 0.9 * math.exp(-k / 60.0) * math.sin(
                    2.0 * math.pi * tone * k / sample_rate
                )
        i += 1
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(
            struct.pack("<%dh" % n, *[int(max(-1.0, min(1.0, v)) * 32767) for v in buf])
        )
    return path


class _Tmp:
    """临时目录（用例结束时清掉）。"""

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="rpechart_tempo_test_")
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)
        return False


def _peaks_of(path, **kw):
    samples, rate = T.decode_mono(path, **kw)
    env, env_rate = T.onset_envelope(samples, rate)
    return T._pick_peaks(env, env_rate), env, env_rate


# ---------------------------------------------------------------- 纯函数


@test
def onset_envelope_is_normalised_and_has_expected_rate():
    with _Tmp() as d:
        p = _click_track(os.path.join(d, "c120.wav"), 120, seconds=4.0)
        samples, rate = T.decode_mono(p)
        env, env_rate = T.onset_envelope(samples, rate)
        assert env, "包络不该为空"
        approx(env_rate, rate / 512.0)
        approx(max(env), 1.0)
        assert min(env) >= 0.0


@test
def onset_envelope_of_empty_input_is_empty():
    env, env_rate = T.onset_envelope([], 22050)
    assert env == []
    approx(env_rate, 0.0)


@test
def onset_envelope_accents_the_attacks():
    """包络的峰应该落在点击位置上，而不是衰减尾巴上。

    检测器报的是「谱差分最大的那一帧」，比真实起音晚约 1~2 帧
    （hop=512 @22050Hz ⇒ 约 0.023~0.06s），所以这里量的是**峰间距**
    ——那才是自相关要用的周期，且与相位无关。
    """
    with _Tmp() as d:
        p = _click_track(os.path.join(d, "c120.wav"), 120, seconds=6.0)
        peaks, _env, env_rate = _peaks_of(p)
        assert len(peaks) >= 10, f"起始点太少: {len(peaks)}"
        gaps = [b - a for a, b in zip(peaks, peaks[1:])]
        for g in gaps:
            approx(g, 0.5, tol=0.06, msg="峰间距应等于 120 BPM 的拍长")
        # 绝对位置：允许固定滞后（窗口居中 + 差分滞后）
        approx(peaks[0], 0.5, tol=0.08)


@test
def autocorrelate_is_one_at_zero_lag_and_symmetric_ish():
    ac = T.autocorrelate([1.0, -1.0, 1.0, -1.0, 1.0, -1.0])
    approx(ac[0], 1.0)
    # 周期 2 的信号在 lag 2/4 处应接近 1（去掉均值后正负交替会变成全零，
    # 所以这里只断言 lag 0 与长度）
    assert len(ac) == 6


@test
def autocorrelate_empty_is_empty():
    assert T.autocorrelate([]) == []


@test
def autocorrelate_shows_the_period_of_a_pulse_train():
    """纯粹的无噪声脉冲串：lag = 周期 处的自相关应显著高于邻域。"""
    period = 8
    vals = [0.0] * 200
    for i in range(0, 200, period):
        vals[i] = 1.0
    ac = T.autocorrelate(vals)
    approx(ac[0], 1.0)
    assert ac[period] > 0.5, ac[period]
    assert ac[period] > ac[period - 2]
    assert ac[period] > ac[period + 2]


@test
def interp_clamps_out_of_range():
    vals = [1.0, 2.0, 3.0]
    approx(T._interp(vals, 0.0), 1.0)
    approx(T._interp(vals, 0.5), 1.5)
    approx(T._interp(vals, 1.0), 2.0)
    # 刚好最后一项
    approx(T._interp(vals, 2.0), 3.0)
    # 越界
    approx(T._interp(vals, -1.0), 0.0)
    approx(T._interp(vals, 5.0), 0.0)
    assert T._interp([], 0.0) == 0.0


@test
def parabolic_peak_recovers_a_half_sample_shift():
    # 峰在 i=1 与 i=2 之间（i=1 的值和 i=2 的值同高）
    approx(T._parabolic_peak([0.0, 1.0, 1.0, 0.0], 1), 0.5)
    # 峰恰在整数位上
    approx(T._parabolic_peak([0.5, 1.0, 0.5], 1), 0.0)
    # 平顶 / 越界都退化为 0
    approx(T._parabolic_peak([1.0, 1.0, 1.0], 1), 0.0)
    approx(T._parabolic_peak([1.0, 2.0], 0), 0.0)


@test
def prior_prefers_120_over_its_octaves():
    assert T._prior(120.0) > T._prior(60.0)
    assert T._prior(120.0) > T._prior(240.0)
    # 对数对称：60 与 240 到 120 的距离相等
    approx(T._prior(60.0), T._prior(240.0))
    assert T._prior(0.0) == 0.0
    assert T._prior(-1.0) == 0.0


# ---------------------------------------------------------------- 网格打分


@test
def grid_score_is_near_zero_for_the_true_bpm():
    with _Tmp() as d:
        peaks, _env, _rate = _peaks_of(_click_track(os.path.join(d, "c120.wav"), 120))
        target, hits, empty, refined = T._grid_score(peaks, 120.0)
        approx(refined, 120.0, tol=0.05)
        assert empty == 0, "每个格子都该有起始点"
        assert hits == len(peaks)
        assert target < 0.2, target


@test
def grid_score_empty_input_is_infinite():
    target, hits, empty, refined = T._grid_score([], 120.0)
    assert target == float("inf")
    assert hits == 0


@test
def grid_score_prefers_the_true_bpm_over_the_octave_below():
    """120 的点击在 60 网格上「也能拟合」，但代价是残差暴增。

    注意：这里**不是**靠空格率区分的 —— 最小二乘会把周期拉到 60.03 去
    迁就所有起始点，于是格子反而不空（empty 仍是 0），但每一点离格点的
    距离都显著变大。定八度的主力是残差项。
    """
    with _Tmp() as d:
        peaks, _env, _rate = _peaks_of(_click_track(os.path.join(d, "c120.wav"), 120))
        t_true, _h1, _e1, r_true = T._grid_score(peaks, 120.0)
        t_low, _h2, _e2, r_low = T._grid_score(peaks, 60.0)
        approx(r_true, 120.0, tol=0.05)
        assert t_true < t_low, f"120 应优于 60: {t_true} vs {t_low}"
        # 低八度候选不该收敛到真值上
        assert abs(r_low - 120.0) > 1.0, f"60 的网格不该收到 120 上: {r_low}"


@test
def grid_score_penalises_the_octave_above_via_empty_slots():
    """240 的网格比真实事件密一倍，会出现大量空格 —— 这是空格率的用武之地。"""
    with _Tmp() as d:
        peaks, _env, _rate = _peaks_of(_click_track(os.path.join(d, "c120.wav"), 120))
        t_true, _h1, empty_true, _r1 = T._grid_score(peaks, 120.0)
        t_high, _h2, empty_high, _r2 = T._grid_score(peaks, 240.0)
        assert empty_true == 0, "真值的网格不该有空格"
        assert empty_high > 0, "高一倍的网格必须有空格"
        assert t_true < t_high, f"120 应优于 240: {t_true} vs {t_high}"


@test
def refine_bpm_recovers_the_exact_period_within_its_basin():
    """最小二乘精修：从真值 ±1 BPM 内出发能收到精确周期。

    收敛半径只有约 ±1 BPM（实测 174：173/174/175 → 174.000，176 → 177.3），
    这是把粗扫定在 1 BPM 步长的原因，也是本用例把起点限制在 ±1 的原因。
    """
    period = 60.0 / 174.0
    pts = [i * period for i in range(80)]
    for rough in (173.0, 174.0, 175.0):
        approx(T._refine_bpm(pts, rough), 174.0, tol=0.05, msg=f"from {rough}")


@test
def refine_bpm_basin_is_about_one_bpm():
    """把收敛半径钉住：超出 ±1 BPM 时不该假装收敛到真值。

    这条是为了防止将来有人偷偷把 ``iters`` 调大却以为半径也变大了。
    """
    period = 60.0 / 174.0
    pts = [i * period for i in range(80)]
    far = T._refine_bpm(pts, 168.0)
    assert abs(far - 174.0) > 1.0, f"168 不该收到 174 上（实测 {far}）"


@test
def refine_bpm_handles_degenerate_input():
    approx(T._refine_bpm([], 120.0), 120.0)
    approx(T._refine_bpm([0.0], 120.0), 120.0)
    approx(T._refine_bpm([1.0, 2.0], 0.0), 0.0)


@test
def pick_peaks_finds_the_click_count():
    with _Tmp() as d:
        # 4 秒 120 BPM = 8 个点击（0.5s 间隔，含 2.0s 处）
        peaks, _env, _rate = _peaks_of(_click_track(os.path.join(d, "c120.wav"), 120, seconds=4.0))
        assert 7 <= len(peaks) <= 9, f"期望约 8 个起始点，实际 {len(peaks)}"


@test
def pick_peaks_on_flat_signal_finds_nothing():
    assert T._pick_peaks([0.0] * 500, 100.0) == []
    assert T._pick_peaks([], 100.0) == []


# ---------------------------------------------------------------- 端到端


@test
def estimate_tempo_recovers_common_bpms():
    """整十以及 174 这类常用 BPM 都要准（吸附后应等于真值）。"""
    cases = (90, 120, 140, 150, 160, 174, 180, 200)
    with _Tmp() as d:
        for bpm in cases:
            p = _click_track(os.path.join(d, f"c{bpm}.wav"), bpm)
            est = T.estimate_tempo(p, snap=True)
            approx(est.bpm, float(bpm), tol=0.5, msg=f"{bpm} BPM")


@test
def estimate_tempo_handles_non_round_bpms():
    """非整十 BPM 不能被「吸附到最近的常见值」带偏。

    128/132 这些值本身就在 COMMON_BPMS 里，所以吸附是安全的；关键是
    自相关 + 网格拟合的先期估计已经足够接近。
    """
    with _Tmp() as d:
        for bpm in (128, 132):
            p = _click_track(os.path.join(d, f"c{bpm}.wav"), bpm)
            est = T.estimate_tempo(p, snap=True)
            approx(est.bpm, float(bpm), tol=0.5, msg=f"{bpm} BPM")


@test
def estimate_tempo_does_not_report_half_or_double():
    """回归保护：曾经的实现把 110/150/160/174 报成一半（75/108/86）。"""
    with _Tmp() as d:
        for bpm in (110, 150, 160, 174):
            p = _click_track(os.path.join(d, f"c{bpm}.wav"), bpm)
            est = T.estimate_tempo(p, snap=False)
            approx(est.bpm, float(bpm), tol=1.0, msg=f"{bpm} BPM")


@test
def estimate_tempo_reports_duration_and_alternatives():
    with _Tmp() as d:
        p = _click_track(os.path.join(d, "c120.wav"), 120, seconds=20.0)
        est = T.estimate_tempo(p)
        approx(est.duration, 20.0, tol=0.1)
        assert est.onset_rate > 0
        assert est.alternatives, "备选列表不该为空"
        assert all(a > 0 for a in est.alternatives)
        assert float(est) == est.bpm
        assert "120" in repr(est)


@test
def estimate_tempo_confidence_is_a_peak_prominence_not_a_share():
    """回归保护：置信度曾经是「峰占全部候选总分的比例」。

    那在真实音频上恒为 0.00 —— 自相关在非周期处大面积为负，3600 个候选
    的总和接近 0，比值没有意义；连一个完美的点击轨都只能拿到 0.0x。
    现在的定义是 (峰 - 中位数) / (1 - 中位数)，点击轨应当得到 0.5 以上。
    """
    with _Tmp() as d:
        for bpm in (120, 174):
            p = _click_track(os.path.join(d, f"c{bpm}.wav"), bpm)
            est = T.estimate_tempo(p)
            assert est.confidence > 0.5, f"{bpm} BPM 的置信度只有 {est.confidence}"
            assert est.confidence <= 1.0


@test
def estimate_tempo_silence_is_zero_bpm():
    with _Tmp() as d:
        p = os.path.join(d, "silence.wav")
        n = 22050 * 4
        with wave.open(p, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(struct.pack("<%dh" % n, *([0] * n)))
        est = T.estimate_tempo(p)
        approx(est.bpm, 0.0)
        # 静音不能给出「有节拍」的置信度
        approx(est.confidence, 0.0)
        assert est.alternatives == []


@test
def estimate_tempo_without_ffmpeg_raises(monkeypatched=None):
    """`decode_mono` 在找不到 ffmpeg 时应报 RuntimeError 而不是静默出错。"""
    real = T.ffmpeg_path

    def fake(what="ffmpeg"):
        return None

    T.ffmpeg_path = fake
    try:
        exc = raises(RuntimeError, T.decode_mono, "nonexistent.wav")
        assert "ffmpeg" in str(exc)
    finally:
        T.ffmpeg_path = real


# ---------------------------------------------------------------- snap / 网格拟合


@test
def snap_bpm_only_snaps_within_tolerance():
    approx(T._snap_bpm(119.5), 120.0)
    approx(T._snap_bpm(120.0), 120.0)
    approx(T._snap_bpm(174.3), 174.0)
    # 容差是相对的，所以 0.5 在这个区间（约 0.4%）内会被吸到 122
    approx(T._snap_bpm(122.5), 122.0)
    approx(T._snap_bpm(122.5, tolerance=0.0001), 122.5)
    # 附近没有常见值时保持原样
    approx(T._snap_bpm(68.0), 68.0)
    approx(T._snap_bpm(235.0), 235.0)
    approx(T._snap_bpm(0.0), 0.0)
    approx(T._snap_bpm(-5.0), -5.0)


@test
def best_grid_fit_recovers_bpm_from_beat_times():
    bpm = 174.0
    period = 60.0 / bpm
    beats = [i * period for i in range(1, 40)]
    found, err = T.best_grid_fit(beats)
    approx(found, bpm, tol=0.2)
    assert err < 0.01, err


@test
def best_grid_fit_ignores_skipped_beats():
    """漏掉一些重拍不该把结果带偏（偏差用「到最近整拍」的距离）。"""
    bpm = 140.0
    period = 60.0 / bpm
    beats = [i * period for i in range(1, 60) if i % 3 != 0]
    found, _err = T.best_grid_fit(beats)
    approx(found, bpm, tol=0.2)


@test
def best_grid_fit_respects_explicit_candidates():
    candidates = [100.0, 150.0, 200.0]
    period = 60.0 / 150.0
    beats = [i * period for i in range(1, 30)]
    found, _err = T.best_grid_fit(beats, candidates=candidates)
    approx(found, 150.0)


@test
def best_grid_fit_needs_two_points():
    exc = raises(ValueError, T.best_grid_fit, [1.0])
    assert "两个" in str(exc)
    raises(ValueError, T.best_grid_fit, [])


# ---------------------------------------------------------------- 与构建器接线


@test
def estimate_tempo_result_feeds_chart_builder():
    """端到端：估出来的 BPM 直接建成谱面，时间换算自洽。"""
    from rpechart import ChartBuilder

    with _Tmp() as d:
        p = _click_track(os.path.join(d, "c174.wav"), 174)
        bpm = T.estimate_tempo(p, snap=True).bpm
        cb = ChartBuilder(bpm=bpm, name="T")
        line = cb.line(name="L0")
        line.tap(0)
        line.tap(4)
        approx(cb.timing.beat_to_second(4), 4 * 60.0 / 174.0, tol=1e-6)
        report = cb.validate()
        assert report.ok, report.format()
