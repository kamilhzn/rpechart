# 生成：只给参数就出一张谱面

入口只有一个 —— **`ChartSpec`**（`rpechart/gen/spec.py:191`）。写一个 JSON，
交给 `generate()`，得到 `chart.json` + 校验报告。

## 一分钟上手

```bash
python -m rpechart.gen demo.spec.json -o build/demo.json
python -m rpechart.gen demo.spec.json --dry          # 只统计与校验
python -m rpechart.gen demo.spec.json -o out.json --video --video-out out.mp4
```

```python
from rpechart.gen import ChartSpec, generate
result = generate(ChartSpec.from_json("demo.spec.json"))
print(result["notes"], result["report"].format())
```

## 最小 spec

```json
{
  "name": "示例曲", "bpm": 150.0, "last_beat": 128.0, "duration": 51.2,
  "lines": [{"name": "主角", "y": -200.0}, {"name": "对手", "y": 180.0}],
  "sections": [
    {"start": 0,  "end": 32,  "role": "N", "density": 0.5, "tone": "dark"},
    {"start": 32, "end": 96,  "role": "B", "density": 2.0, "tone": "blue"},
    {"start": 96, "end": 128, "role": "R", "density": 2.5, "tone": "red"}
  ],
  "out": "build/demo.json"
}
```

## 流水线

```
spec.json
  ├─ features：逐拍特征（beats.json，或没给就按段落角色合成）
  ├─ structure：段落表（手写 or 按 8 拍自动切段）
  ├─ motion：判定线摆动 / 线速 ramp（与音符轨道**解耦**）
  ├─ notes.plan：节奏型 + 角色分派 → [(拍, 类型, x, 线, 时长)]
  │    └─ lanes：tame_lanes → free_holds → dedupe_same_line（顺序不可换）
  └─ assemble：ChartBuilder → Chart → validate → save_chart（RPE 风格）
```

## 字段总览

| 字段 | 含义 |
| --- | --- |
| `name` / `charter` / `composer` / `level` | 写进 `META` |
| `song` / `background` / `illustrator` | 资源文件名 |
| `offset` | `META.offset`（毫秒，负值 = 谱面提前） |
| `rpe_version` | RPE 版本（默认 160） |
| `chart_id` | 装进 PhiEdit 时用的谱面 id |
| `bpm` / `phase` | 主 BPM；`beats.json` 的相位（秒） |
| `duration` | 歌曲时长（秒），写进 `META.duration` |
| `last_beat` | 谱面末拍（末段必须盖到这里） |
| `beats` / `features` | `beats.json` 路径 / 内联内容（`features` 优先） |
| `lanes` | 轨道网格 |
| `sections` / `auto_sections` / `auto_min_len` / `auto_target_len` | 段表与自动切段 |
| `lines` | 判定线定义（`LineSpec`：名称 / 初始 y / 纹理 / 是否可放音符…） |
| `motion` | 摆动 / 门限 / 线速（`MotionSpec`） |
| `style` | 音符编排参数（`NotePlanner`） |
| `palette` | 色调 → `[起始RGB, 结束RGB]`（线的 `color_fade`） |
| `captions` | 秒 → 文字卡点 |
| `special` | 缩放 / 倾斜 / 闪烁等演出 |
| `out` / `install` / `video` | 输出路径 / 是否装进 PhiEdit / 视频参数 |
| `role_thresholds` | 角色判定阈值（默认值在 `rpechart/gen/features.py:129`） |

## 四条硬约束（都来自真实事故，不要改）

1. **半正弦端点必须用 `cos` 采样**（`rpechart/gen/motion.py:74`）。
   用 `sin` 会让 `phase=0` 的轴恒等于 0，判定线「不动」。
2. **后处理顺序不可换**：`plan_notes → tame_lanes → free_holds → dedupe_same_line`
   （`rpechart/gen/assemble.py:298`）。
3. **Hold 与 Tap 叠加、不替换**；中性段（`role == "N"`）**不能整段跳过**
   （历史上曾把一整段 Tap 删光只留 Hold，谱面后半截空白）。
4. **`line_speed` 不过 `_splice`**（`rpechart/builders.py:261` 是唯一做裁剪的地方），
   所以区间重叠必须由生成层自证不重叠（`rpechart/gen/motion.py:168` `assert_disjoint`）。

## 确定性

**完全没有随机数**：同一份 spec 生成两次逐字节相同（`rpechart/gen/notes.py:83` 的
`stable_jitter` 是哈希派生的纯函数，不是 `random`）。
`ChartSpec.to_json()` 的输出是稳定排序的普通 dict。

## 特征从哪来

`beats.json` 是逐拍特征表，由音频/视频分析得到（本仓库只负责读取，
附带的 `rpechart.tempo` 能从音频里估 BPM）。
不给 `beats` 时，`synth_features`（`rpechart/gen/assemble.py:79`）会按段落的
角色与密度合成一份确定性特征，所以**没有音频也能出谱**。
