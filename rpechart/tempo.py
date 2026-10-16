"""音频节拍/BPM 估计 —— 为「速度安排」提供一个起点。

制谱时最先要确定的就是基准 BPM。手工听感估 BPM 既慢又容易差个一两 BPM，
而 RPE 的 BPMList 一旦错位，整张谱的音符落点都会跟着漂。这个模块用
「能量包络 + 自相关」的经典做法给出一个可用的估计值，再吸附到常见拍号
网格上，直接可以喂给 :class:`~rpechart.builders.ChartBuilder`。

算法（纯 numpy，不引入 librosa/aubio）：

1. 把音频解码成单声道 float 波形（解码交给 ffmpeg，见 :func:`decode_mono`）；
2. 计算**起始强度包络**（onset strength）：对幅度谱做一阶差分，只保留
   能量上升的半波，再按帧求和 —— 这比直接用 RMS 能量对鼓点敏感得多；
3. 对包络做**自相关**，在 60~200 BPM 对应的 lag 范围内找峰值；
4. 把峰值做**抛物线插值**取亚帧精度，再输出 BPM 与其倍/半关系。

得到的 BPM 不是绝对真理：自相关天然分不清 70 和 140。所以
:func:`estimate_tempo` 会把候选的 2 倍/1.5 倍关系一并给出，由人（或后续
的落点验证）来定夺。:func:`best_grid_fit` 则反过来 —— 给定一组已知的
重拍时刻，挑出最贴合的那个 BPM。
"""

from __future__ import annotations

import math
import os
import struct
import subprocess
import tempfile
import typing as _t
import wave
from dataclasses import dataclass, field

from .audio import ffmpeg_path

__all__ = [
    "TempoEstimate",
    "decode_mono",
    "onset_envelope",
    "autocorrelate",
    "estimate_tempo",
    "best_grid_fit",
    "COMMON_BPMS",
]

#: 制谱里最常见的基准 BPM。估计值会优先吸附到这些数附近，因为绝大多数曲子
#: 都是整数或 .5 结尾的 BPM（实测真实 RPE 谱面的 BPMList 也是这个分布）。
COMMON_BPMS: tuple[float, ...] = (
    70, 72, 75, 76, 80, 84, 85, 87, 88, 90, 92, 95, 96, 98, 100,
    102, 104, 105, 106, 108, 110, 112, 114, 115, 116, 118, 120, 122,
    124, 125, 126, 128, 130, 132, 134, 135, 136, 138, 140, 142, 144,
    145, 146, 148, 150, 152, 155, 156, 158, 160, 162, 164, 165, 168,
    170, 172, 174, 175, 176, 178, 180, 184, 185, 186, 188, 190, 192,
    195, 196, 198, 200, 204, 205, 208, 210, 215, 220, 225, 230, 240,
)

#: 自相关搜索的 BPM 范围。低于 60 的太慢（基本是半拍关系），高于 240 的
#: 会被帧率限制住精度。
_MIN_BPM = 60.0
_MAX_BPM = 240.0


@dataclass
class TempoEstimate:
    """一次 BPM 估计的结果。

    ``bpm`` 是主估计值；``confidence`` 是 0~1 的自相关峰锐度（不是概率，
    只用来横向比较同一个文件的不同候选）；``alternatives`` 给出倍半关系
    与吸附后的整数 BPM，按可信度从高到低。

    ``confidence`` 的定义是 ``(峰值 - 中位数) / (1 - 中位数)``：自相关已经
    归一化到 ``ac[0] == 1``，所以「峰有多接近 1」本身就是个天然尺度。
    用中位数而不是均值做基线，是因为自相关在非周期处会大面积为负，
    均值会被拉到 0 附近、让静音和真有节拍的文件看起来一样。
    """

    bpm: float
    confidence: float = 0.0
    alternatives: list[float] = field(default_factory=list)
    onset_rate: float = 0.0
    duration: float = 0.0
    _extras: dict = field(default_factory=dict, repr=False)

    def __float__(self) -> float:
        return float(self.bpm)

    def __repr__(self) -> str:
        alts = ", ".join(f"{b:g}" for b in self.alternatives[:4])
        return (
            f"<TempoEstimate {self.bpm:.3f} BPM "
            f"(conf {self.confidence:.2f}, 备选 {alts})>"
        )


# ------------------------------------------------------------------ 解码
def decode_mono(
    path: _t.Union[str, os.PathLike],
    *,
    sample_rate: int = 22050,
    max_seconds: _t.Optional[float] = None,
) -> tuple[list[float], int]:
    """用 ffmpeg 把任意音频解码成单声道 float 样本。

    返回 ``(samples, sample_rate)``，``samples`` 是 ``[-1, 1]`` 的 float 列表。

    挑 22050 Hz 是因为 BPM 估计只关心低频包络，更高采样率纯属浪费；
    音乐文件动辄两三分钟，44.1kHz 立体声解出来是几十兆的临时数据。

    没有可用的 ffmpeg 时抛 :class:`RuntimeError`（调用方可以退回自己的波形源）。
    """
    exe = ffmpeg_path("ffmpeg")
    if not exe:
        raise RuntimeError("找不到 ffmpeg，无法解码音频（可 pip install imageio-ffmpeg）")

    src = str(path)
    with tempfile.TemporaryDirectory(prefix="rpechart_tempo_") as tmp:
        out = os.path.join(tmp, "mono.wav")
        cmd = [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", src]
        if max_seconds:
            cmd += ["-t", f"{float(max_seconds):.6f}"]
        cmd += ["-ac", "1", "-ar", str(int(sample_rate)), "-f", "wav", out]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0 or not os.path.exists(out):
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"ffmpeg 解码失败: {err or proc.returncode}")
        return _read_wav_mono(out)


def _read_wav_mono(path: str) -> tuple[list[float], int]:
    """读 16-bit PCM 单声道 WAV（ffmpeg 的输出格式由我们指定，所以只处理这一种）。

    不直接用 :mod:`wave` 的 ``readframes`` 是因为要一次性拿到整个数组做
    自相关，逐帧读没有意义。
    """
    with wave.open(path, "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())

    if width != 2:
        raise RuntimeError(f"期望 16-bit PCM，实际 {width * 8}-bit")

    count = len(raw) // 2
    ints = struct.unpack(f"<{count}h", raw[: count * 2])
    if channels <= 1:
        samples = [v / 32768.0 for v in ints]
    else:
        # ffmpeg 已经 -ac 1 了，这里只是兜底
        frames = count // channels
        samples = [
            sum(ints[i * channels : (i + 1) * channels]) / (32768.0 * channels)
            for i in range(frames)
        ]
    return samples, rate


# ------------------------------------------------------------------ 包络
def onset_envelope(
    samples: _t.Sequence[float],
    sample_rate: int,
    *,
    hop: int = 512,
    window: int = 1024,
) -> tuple[list[float], float]:
    """算起始强度包络，返回 ``(strengths, 每秒帧数)``。

    做法：分帧 → 加 Hann 窗 → 幅度谱 → 相邻帧的**正半波差分**求和。
    只取正差分（``max(0, |X_t| - |X_{t-1}|)``）能滤掉衰减尾巴，让包络在
    鼓点/拨弦的起音处出现尖峰，而这正是自相关要找的周期。
    """
    if not samples:
        return [], 0.0
    hop = max(1, int(hop))
    window = max(hop, int(window))
    n = len(samples)

    try:
        import numpy as np
    except ImportError:  # pragma: no cover - numpy 是本仓的既有依赖
        return _onset_envelope_pure(samples, sample_rate, hop, window), sample_rate / hop

    arr = np.asarray(samples, dtype=np.float64)

    # 帧数：保证最后一帧的 window 不越界
    if n < window:
        arr = np.pad(arr, (0, window - n))
        n = window
    frames = 1 + (n - window) // hop
    idx = np.arange(window)[None, :] + hop * np.arange(frames)[:, None]
    framed = arr[idx]
    framed = framed * np.hanning(window)[None, :]

    spec = np.abs(np.fft.rfft(framed, axis=1))
    # 一阶差分，只留上升沿
    diff = np.diff(spec, axis=0)
    np.maximum(diff, 0.0, out=diff)
    strength = diff.sum(axis=1)

    if strength.size:
        peak = float(strength.max())
        if peak > 0:
            strength = strength / peak
    return strength.tolist(), sample_rate / hop


def _onset_envelope_pure(
    samples: _t.Sequence[float], sample_rate: int, hop: int, window: int
) -> list[float]:
    """没有 numpy 时的退路：用能量差分代替谱差分。

    对鼓点足够，对纯旋律弱一些。留着是为了让这个模块在没有 numpy 的
    环境里也能跑（本仓的其它模块都不强依赖 numpy）。
    """
    strengths: list[float] = []
    prev = 0.0
    for start in range(0, max(1, len(samples) - window + 1), hop):
        chunk = samples[start : start + window]
        energy = sum(v * v for v in chunk) / max(1, len(chunk))
        diff = energy - prev
        strengths.append(diff if diff > 0 else 0.0)
        prev = energy
    peak = max(strengths) if strengths else 0.0
    if peak > 0:
        strengths = [s / peak for s in strengths]
    return strengths


# ------------------------------------------------------------------ 自相关
def autocorrelate(values: _t.Sequence[float]) -> list[float]:
    """归一化自相关（只用非负 lag）。"""
    n = len(values)
    if n == 0:
        return []
    try:
        import numpy as np

        arr = np.asarray(values, dtype=np.float64)
        arr = arr - arr.mean()
        full = np.correlate(arr, arr, mode="full")[n - 1 :]
        if full[0] != 0:
            full = full / full[0]
        return full.tolist()
    except ImportError:  # pragma: no cover
        mean = sum(values) / n
        centered = [v - mean for v in values]
        denom = sum(v * v for v in centered) or 1.0
        out = []
        for lag in range(n):
            s = 0.0
            limit = n - lag
            for i in range(limit):
                s += centered[i] * centered[i + lag]
            out.append(s / denom)
        return out


def _parabolic_peak(ac: _t.Sequence[float], i: int) -> float:
    """对自相关峰做抛物线插值，返回亚采样精度的 lag 偏移（-0.5~0.5）。"""
    if i <= 0 or i >= len(ac) - 1:
        return 0.0
    a, b, c = ac[i - 1], ac[i], ac[i + 1]
    denom = a - 2.0 * b + c
    if abs(denom) < 1e-12:
        return 0.0
    offset = 0.5 * (a - c) / denom
    return offset if -0.5 <= offset <= 0.5 else 0.0


def _interp(values: _t.Sequence[float], x: float) -> float:
    """在 ``values`` 上按浮点下标做线性插值（越界返回 0）。"""
    if not values or x < 0:
        return 0.0
    i = int(math.floor(x))
    if i >= len(values) - 1:
        return float(values[-1]) if i == len(values) - 1 else 0.0
    frac = x - i
    return float(values[i]) * (1.0 - frac) + float(values[i + 1]) * frac


#: 节奏先验：BPM 空间上的对数正态权重，中心 120。
#:
#: 没有它，自相关只会挑「最强周期」，而强周期常常是真实拍的两倍长
#: （实测合成 120 BPM 点击被报成 60）。加上先验后，同样强度的 60 与
#: 120 会明显偏向 120。对数正态比线性窗好，因为人对节奏的感知也是
#: 对数尺度的（60↔120 与 120↔240 的「距离」相等）。
_PRIOR_CENTER = 120.0
_PRIOR_SIGMA = 0.9


def _prior(bpm: float) -> float:
    if bpm <= 0:
        return 0.0
    z = math.log(bpm / _PRIOR_CENTER) / _PRIOR_SIGMA
    return math.exp(-0.5 * z * z)


def _pick_peaks(
    env: _t.Sequence[float], env_rate: float, *, max_peaks: int = 600
) -> list[float]:
    """从起始包络里挑出显著峰的**时刻（秒）**。

    阈值取全局均值 + 一倍标准差，再要求是局部极大且与上一个峰至少隔
    80ms（避免一个起音被相邻两帧重复计入）。
    """
    if not env:
        return []
    n = len(env)
    mean = sum(env) / n
    var = sum((v - mean) ** 2 for v in env) / n
    thresh = mean + math.sqrt(var)
    min_gap = max(1, int(env_rate * 0.08))
    peaks: list[float] = []
    last = -min_gap
    for i in range(1, n - 1):
        v = env[i]
        if v < thresh or v <= env[i - 1] or v < env[i + 1]:
            continue
        if i - last < min_gap:
            # 同一簇里保留更强者
            if peaks and v > env[int(round(peaks[-1] * env_rate))]:
                peaks[-1] = i / env_rate
                last = i
            continue
        peaks.append(i / env_rate)
        last = i
        if len(peaks) >= max_peaks:
            break
    return peaks


def _refine_bpm(
    peaks: _t.Sequence[float],
    rough: float,
    *,
    hi_bpm: float = _MAX_BPM,
    iters: int = 5,
) -> float:
    """用最小二乘把 ``rough`` 精修到亚帧精度。

    模型是 ``t_i = t0 + n_i * P``：给定期望周期 ``P`` 把每个起始点分配
    到最近的整拍 ``n_i``，再对 ``(t0, P)`` 做线性最小二乘，然后重新分配
    ``n_i`` 迭代几轮。相位 ``t0`` 是自由参数（第 0 拍落在哪由谱师自己定）。

    比「扫一遍候选值数命中率」好的地方：它一次用上所有起始点，精度不再
    受包络帧长（22050/512 ≈ 43 帧每秒）限制。合成点击轨实测能到 ±0.01 BPM。

    **收敛半径只有正负一个 BPM 左右**（实测 174：173/174/175 都收到
    174.000，176 却收到 177.3）。所以必须先把粗估做到 ±1 BPM 以内 ——
    这也是 :func:`estimate_tempo` 里要先用 1 BPM 步长粗扫、再用 0.02 步长
    精扫的原因。作为粗略的比例精修器（比如 170 → 174）它不可靠。
    """
    if not peaks or rough <= 0:
        return rough
    pts = [float(p) for p in peaks]
    p = 60.0 / rough
    if p <= 0:
        return rough
    t0 = pts[0]
    k = len(pts)
    if k < 2:
        return rough

    for _ in range(max(1, iters)):
        ns = [round((t - t0) / p) for t in pts]
        sn = sum(ns)
        snn = sum(n * n for n in ns)
        st = sum(pts)
        snt = sum(n * t for n, t in zip(ns, pts))
        denom = k * snn - sn * sn
        if abs(denom) < 1e-12:
            break
        new_p = (k * snt - sn * st) / denom
        if new_p <= 0:
            break
        p = new_p
        t0 = (st - p * sn) / k
        if 60.0 / p > hi_bpm:
            break

    if p <= 0:
        return rough
    out = 60.0 / p
    return out if _MIN_BPM <= out <= hi_bpm else rough


def _grid_score(
    peaks: _t.Sequence[float], bpm: float
) -> tuple[float, int, int, float]:
    """给一个候选 BPM 打分，返回 ``(目标值, 命中数, 空格数, 精修后 BPM)``。

    目标值越小越好，由三项组成：

    * **相对残差** —— 起始点到最近整拍的距离 / 周期。这是主导项，用来
      判断「这组起始点能不能整齐地落在等距网格上」；
    * **空格率** —— 首末拍之间没有任何起始点落上的格子占比。仅靠残差
      无法区分 240 与 120（120 的点击也能落在 240 网格的一半格子上），
      空格率把这种「网格太密」的情形压下去；
    * **节奏先验** —— 只用来打破平局（见 :func:`_prior`）。

    注意：**定八度靠的主要是残差而不是空格率**。把 120 BPM 的点击拿去
    拟 60 BPM 时，最小二乘会把周期拉成 61.56 这种值去迁就所有点，于是
    格子反而不空、但残差明显变大。所以不要指望「低八度一定会出现空格」。
    """
    if not peaks or bpm <= 0:
        return float("inf"), 0, 0, bpm
    pts = [float(p) for p in peaks]
    refined = _refine_bpm(pts, bpm)
    period = 60.0 / refined
    if period <= 0:
        return float("inf"), 0, 0, refined

    t0 = pts[0]
    ns = [round((t - t0) / period) for t in pts]
    resid = 0.0
    for t, n in zip(pts, ns):
        resid += abs(t - (t0 + n * period))
    mean_resid = resid / len(pts) / period  # 归一化到「拍」

    slots = max(ns) - min(ns) + 1
    occupied = len(set(ns))
    empty = max(0, slots - occupied)
    empty_ratio = empty / slots if slots > 0 else 0.0

    # 命中判定：落在 1/8 拍以内的起始点算「命中」
    hits = sum(1 for t, n in zip(pts, ns) if abs(t - (t0 + n * period)) < period / 8.0)
    target = mean_resid * 4.0 + empty_ratio * 2.0
    # 先验只用来打破平局：网格目标本身已经很强，先验不该盖过它。但对
    # 「重音只落在每隔一拍」的曲子，60/120 的空格率同为 0，此时先验决定取谁。
    target -= 0.6 * math.log(_prior(refined) + 1e-9)
    return target, hits, empty, refined


def estimate_tempo(
    path: _t.Union[str, os.PathLike],
    *,
    sample_rate: int = 22050,
    min_bpm: float = _MIN_BPM,
    max_bpm: float = _MAX_BPM,
    max_seconds: _t.Optional[float] = 120.0,
    snap: bool = True,
    refine: bool = True,
) -> TempoEstimate:
    """估计一个音频文件的 BPM。

    ``max_seconds`` 默认只看前两分钟：BPM 在整曲里通常不变，而自相关是
    O(n log n) 的，看完整首歌对结果没帮助却明显更慢。

    ``snap=True`` 时会把结果吸附到 :data:`COMMON_BPMS` 里最接近的值
    （相对误差 1% 以内才吸附）—— 因为自相关的结果常常是 173.98 这种
    带着零点几误差的数，而谱师要的是 174。

    ``refine=True`` 时会在自相关结果的基础上，用「起始点落在整拍上」的目标
    重新定八度并精修周期。自相关分不清 70/140 这类倍半关系，必须靠这一步
    的网格空格率惩罚才能定下来。
    """
    samples, rate = decode_mono(path, sample_rate=sample_rate, max_seconds=max_seconds)
    env, env_rate = onset_envelope(samples, rate)
    duration = len(samples) / float(rate) if rate else 0.0
    if not env or env_rate <= 0:
        return TempoEstimate(0.0, 0.0, [], 0.0, duration)

    ac = autocorrelate(env)
    if len(ac) < 4:
        return TempoEstimate(0.0, 0.0, [], env_rate, duration)

    # 在 BPM 空间（而不是整数 lag 空间）上细扫。lag 的整数分辨率太粗：
    # 22050Hz/hop512 只有 43 帧每秒，174 BPM 附近相邻两帧要差 6 BPM。
    # 分数 = 节奏先验 × 该周期处的自相关，自相关做线性插值。
    bpm_lo = max(_MIN_BPM, float(min_bpm))
    bpm_hi = min(_MAX_BPM, float(max_bpm))
    scored: list[tuple[float, float]] = []
    steps = int((bpm_hi - bpm_lo) / 0.05) + 1
    for i in range(max(1, steps)):
        b = bpm_lo + i * 0.05
        lag = env_rate * 60.0 / b
        if lag < 1.0 or lag >= len(ac) - 1:
            continue
        scored.append((b, _prior(b) * _interp(ac, lag), _interp(ac, lag)))
    if not scored:
        return TempoEstimate(0.0, 0.0, [], env_rate, duration)

    scored.sort(key=lambda p: p[1], reverse=True)
    best_b, best_score, _best_raw = scored[0]
    # 全曲没有可辨的周期性（静音、纯噪音、极度平坦的素材）时自相关处处接近 0。
    # 此时不能报「最像的那个 BPM」—— 那只会是搜索区间的下界（60）。直接给 0，
    # 调用方看到 0 就知道该手工填 BPM，而不是拿到一个看似合理的错值。
    if best_score <= 1e-6:
        return TempoEstimate(0.0, 0.0, [], env_rate, duration)

    # 置信度取「峰高出背景多少」。不能用「峰占所有候选总分的比例」——
    # 实测那会在真实音频上给出 0.00（3600 个候选里大量为负、总和接近 0，
    # 比值毫无意义），连完美点击轨也拿不到高分。改用中位数做基线：自相关
    # 已归一化到 ac[0] == 1，「峰有多接近 1」本身就是天然尺度。用中位数而
    # 不是均值，是因为自相关在非周期处大面积为负，均值会被拉到 0 附近，
    # 让静音和真有节拍的文件看起来一样。
    raws = sorted(r[2] for r in scored)
    median_raw = raws[len(raws) // 2]
    if median_raw >= 1.0:
        conf = 1.0
    else:
        conf = (_best_raw - median_raw) / (1.0 - median_raw)
    conf = max(0.0, min(1.0, conf))

    alts: list[float] = []
    bpm = best_b

    # 自相关分不清倍/半（70 与 140 的自相关峰一样高），而且在 110/160/174
    # 这类非整十 BPM 上，前几名候选根本轮不到真值。所以不依赖自相关的
    # 短名单，直接在整个 BPM 区间上粗扫一遍「起始点落在整拍网格上」的
    # 目标，再在最优附近精扫。120 BPM 的点击轨在 60 BPM 网格上也能完全
    # 吻合，但 60 BPM 网格会空掉一半格子，空格率惩罚项会把它压下去。
    if refine:
        pts = _pick_peaks(env, env_rate)
        if len(pts) >= 8:

            def _scan(lo: float, hi: float, step: float) -> tuple[float, float]:
                best_t, best_b = float("inf"), 0.0
                n = int((hi - lo) / step) + 1
                for i in range(max(1, n)):
                    c = lo + i * step
                    if c < bpm_lo or c > bpm_hi:
                        continue
                    target, hits, _empty, fixed = _grid_score(pts, c)
                    if hits < 4:
                        continue
                    if target < best_t:
                        best_t, best_b = target, fixed
                return best_t, best_b

            target, fixed = _scan(bpm_lo, bpm_hi - 0.5 * 1.0, 1.0)
            if fixed > 0:
                t2, f2 = _scan(max(bpm_lo, fixed - 1.0), min(bpm_hi, fixed + 1.0), 0.02)
                if t2 < target:
                    fixed = f2
                bpm = fixed

    if snap:
        snapped = _snap_bpm(bpm)
        if snapped != bpm:
            alts.append(round(bpm, 3))
        bpm = snapped

    # 倍/半与三连关系始终列出来供人挑：定八度靠的是网格空格率，那是个
    # 启发式，用户可能不同意（比如一首 70 BPM 的慢歌会被推成 140）。
    for mult in (2.0, 0.5, 1.5, 1.0 / 1.5, 3.0, 1.0 / 3.0):
        cand = bpm * mult
        if bpm_lo <= cand <= bpm_hi:
            alts.append(round(cand, 3))

    alts.sort(key=lambda b: abs(b - bpm))
    # 去重且去掉与主值相同的项
    seen: list[float] = []
    for b in alts:
        if abs(b - bpm) < 1e-9:
            continue
        if any(abs(b - s) < 1e-9 for s in seen):
            continue
        seen.append(b)

    return TempoEstimate(round(bpm, 6), round(conf, 6), seen, env_rate, duration)


def _snap_bpm(bpm: float, tolerance: float = 0.01) -> float:
    """把 BPM 吸附到最近的常见值；偏差超过 ``tolerance``（相对）就原样返回。"""
    if bpm <= 0:
        return bpm
    best = min(COMMON_BPMS, key=lambda c: abs(c - bpm))
    if abs(best - bpm) / bpm <= tolerance:
        return float(best)
    return bpm


# ------------------------------------------------------------------ 网格拟合
def best_grid_fit(
    beats: _t.Sequence[float],
    *,
    candidates: _t.Optional[_t.Sequence[float]] = None,
    lo: float = _MIN_BPM,
    hi: float = _MAX_BPM,
    step: float = 0.1,
) -> tuple[float, float]:
    """给定一组已知的重拍时刻（秒），挑出最贴合的 BPM。

    返回 ``(bpm, 平均偏差秒)``。

    这是比纯自相关更可靠的路子：如果能从波形里抽出可信的重拍，或者手头
    已经有别人标的卡点时间，直接问「哪个 BPM 能让这些点都落在整拍上」
    比猜周期稳得多。偏差用「到最近整拍的距离」的均值，所以对漏掉的重拍
    不敏感。
    """
    pts = sorted(float(b) for b in beats)
    if len(pts) < 2:
        raise ValueError("至少需要两个重拍时刻才能拟合 BPM")

    if candidates is not None:
        grid = [float(c) for c in candidates]
    else:
        n = int(round((hi - lo) / step)) + 1
        grid = [lo + i * step for i in range(max(1, n))]

    span = pts[-1] - pts[0]
    best_bpm, best_err = grid[0], float("inf")
    for bpm in grid:
        if bpm <= 0:
            continue
        period = 60.0 / bpm
        # 相位取第一个点，只优化周期：对制谱来说首个重拍就是第 0 拍
        total = 0.0
        for t in pts:
            k = (t - pts[0]) / period
            total += abs(k - round(k)) * period
        err = total / len(pts)
        # 极短跨度下高 BPM 会「碰巧」拟合得很好，用跨度加权惩罚
        if span < 4 * period:
            err += 0.005 * (1.0 - span / (4 * period))
        if err < best_err:
            best_err, best_bpm = err, bpm
    return round(best_bpm, 6), round(best_err, 6)
