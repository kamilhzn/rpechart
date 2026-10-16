# 架构

一句话：**一份 `chart.json` 是唯一真值**。库把它读成对象树（模型层）、按 RPE 的
字节风格写回去（序列化层）、按实机语义算出画面（渲染层）、按参数造出来（生成层），
外面套一层本地 Web UI 让人能看见并调整（观察层）。

```
        spec.json ──► gen（参数 → 谱面）
                          │
                          ▼
  chart.json ──► io / model（无损往返）──► validate（三级问题）
                          │
                          ├──► rpedump + io.save_chart / export_pez ──► chart.json / .pez
                          └──► render（几何 → 场景 → 光栅）──► video（ffmpeg）──► .mp4
                          └──► web（本地 UI：改参数 → 重生成 → 看画面）
```

## 分层

| 模块 | 职责 | 关键不变量 | 看哪 |
| --- | --- | --- | --- |
| `beat.py` | 时间单位 = **拍** | 用 `Fraction` 精确保存；`raw` 保留 RPE 不约分的写法（`[2,0,4]`） | `beat.py:76`、`beat.py:115` |
| `timing.py` | 拍 ↔ 秒 | 非恒定 BPM 逐段累加；`effective = bpm / bpmfactor` | `timing.py:26`、`timing.py:76`、`timing.py:94` |
| `easing.py` | 29 条缓动 | `ease(1,t)==t`；越界一律回退 1 | `easing.py:326`、`easing.py:290` |
| `model.py` | 对象树 | **无损往返**：`_present` 记住「载入时出现过哪些键」，`_extra` 透传未知键 | `model.py:235`、`model.py:1105` |
| `interp.py` | 事件求值 | `value_at` 入口按 `(startTime, endTime)` 排序，乱序/重叠都不静默出错 | `interp.py:90`、`interp.py:137` |
| `rpedump.py` | RPE 风格序列化 | 3 空格缩进、`"k" : v`、键字节序、`%.17g`、LF、`-0.0` 保号 | `rpedump.py:32`、`rpedump.py:86` |
| `io.py` | 读写 / 安装 / pez | `save_chart` 先 `chart.sort()` 再按 RPE 风格写 | `io.py:104`、`io.py:454` |
| `validate.py` | 三级校验 | ERROR 必须改 / WARNING 仔细看 / CAUTION 作参考 | `validate.py:42`、`validate.py:165` |
| `builders.py` | 写谱 API | `_splice` 负责裁剪重叠；**`line_speed` 是唯一不过 `_splice` 的方法** | `builders.py:261`、`builders.py:884` |
| `audio.py` | 音频探测 | **按魔数嗅探**，不按后缀（`.mp3` 可能是 MP4/AAC） | `audio.py:550` |
| `tempo.py` | BPM 估计 | 与相位联合估计；返回 `alternatives` 供人工判 2× / 0.5× | `tempo.py:433` |
| `video.py` | 帧 → MP4 | 流式喂 ffmpeg stdin；默认用**库自带**渲染器 | `video.py:386`、`video.py:329` |
| `render/` | 离线渲染 | 见 [rendering.md](rendering.md) | `render/renderer.py:49` |
| `gen/` | 参数 → 谱面 | 见 [generation.md](generation.md) | `gen/spec.py:191` |
| `web/` | 本地 UI | 每次 POST 都走同一条 `ChartSpec → build → validate → save_chart` 流水线 | `web/server.py:86` |

## 渲染子系统（`rpechart/render/`）

| 文件 | 职责 |
| --- | --- |
| `geometry.py` | 世界坐标 ↔ 屏幕、下落积分（`FloorCurve`）、音符位移/透明度 |
| `scene.py` | **纯数据**：`Scene` / `NoteDraw` / `HoldDraw` / `LineDraw` / `HitDraw` / `TextDraw` / `HudState` |
| `theme.py` | 颜色、尺寸、HUD 归一化坐标、贴图名 —— 全部可替换 |
| `assets.py` | 定位 `PhiEdit/Resources/`（贴图、字体），带缓存 |
| `timeline.py` | `RenderModel.scene_at(秒) → Scene`，**无状态的纯函数** |
| `raster.py` | 唯一依赖 PIL 的一层：`Scene → PIL.Image` |
| `renderer.py` | 门面：`Renderer(path).frame(t, size=…)` |
| `videostream.py` | 背景视频的流式解码（不整片进内存） |
| `cli.py` | `stats` / `frame` / `range` / `sheet` / `video` 子命令 |

**为什么求值（`timeline`）与绘制（`raster`）分开**：`scene_at` 是纯函数，
所以它可测、可并行、可从任意时刻续渲，也只有它需要懂 RPE 语义；
换后端（numpy / GPU / 浏览器）时上面一层不用动。

## 生成子系统（`rpechart/gen/`）

| 文件 | 职责 |
| --- | --- |
| `features.py` | 逐拍特征（平滑 / 填充 / 限幅 / 角色判定 / beats.json 读写） |
| `structure.py` | 段落模型（角色枚举 + 自动切段 + 手动段表） |
| `lanes.py` | 轨道网格 + 三道合法化 |
| `notes.py` | 节奏型、角色分派、Hold 生成 |
| `motion.py` | 判定线运动（cos 半正弦 / 门限插值 / 线速 ramp） |
| `assemble.py` | 组装成 `ChartBuilder` → `Chart` |
| `spec.py` | 声明式参数 `ChartSpec`（**唯一的对外入口**） |
| `cli.py` | `python -m rpechart.gen` |

## 观察子系统（`rpechart/web/`）

零依赖（标准库 `http.server`）：一个页面 + 一个「spec ↔ 谱面 ↔ 画面」的会话对象。
路由表见 `web/server.py:232`（GET）与 `web/server.py:266`（POST），
起服务见 `web/cli.py:69`。
