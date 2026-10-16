# 渲染：画面是怎么算出来的

权威依据是 **phira 的 `prpr/src/parse/rpe.rs`**（实际播放器语义）。下面每条口径都在源码里对过行号。

## 下落几何（最容易写错的地方）

**积分变量是秒，不是拍。** 这是本项目旧渲染器最严重的错误：旧版按拍积分，
导致下落速度整体快 `BPM / 106.5` 倍（BPM 153.6 → 1.442×）。

| 量 | 值 | 出处 |
| --- | --- | --- |
| `RPE_WIDTH` / `RPE_HEIGHT` | 1350 / 900 | `rpechart/render/geometry.py:22` |
| `SPEED_RATIO` | `10/45/0.83175 ≈ 0.267176` | `rpechart/render/geometry.py:26` |
| `Y_PER_SPEED_SECOND` | `450 × SPEED_RATIO ≈ 120.229` | `rpechart/render/geometry.py:29` |
| 下落积分 | `floor(t) = ∫ speed(τ)·Y_PER_SPEED_SECOND dτ`，`FloorCurve` 中点法按秒采样 | `rpechart/render/geometry.py:95` |
| 音符位移 | `(floor(note_t) − floor(t) + yOffset) × note.speed`，正数在上方 | `rpechart/render/geometry.py:154` |
| 屏幕映射 | `px_x = X × W / 1350`（`geometry.py:40`）、`px_y = Y × H / 900`（`geometry.py:45`，与 aspect_ratio 无关） |

## 音符

| 语义 | 实现 |
| --- | --- |
| 世界宽度 = `0.13175016 × W` | `rpechart/render/geometry.py:25`、`rpechart/render/geometry.py:60` |
| 高度 = 宽度 × 贴图高宽比 | `rpechart/render/raster.py:134` |
| 旋转 = 线旋转 `+ (above ? 0 : 180)` | `rpechart/render/timeline.py:405` |
| `visibleTime`：`>= 音符时刻` 时用常量透明度，否则在 `(t − visibleTime)` 秒内从 0 线性升到 `alpha/255` | `rpechart/render/geometry.py:164` |
| 判定后淡出 0.16s | `rpechart/render/geometry.py:185` |
| 越线后仍继续下落并淡出（**不要提前剔除**） | `rpechart/render/timeline.py:405` |

## 判定线

| 语义 | 实现 |
| --- | --- |
| 长度 = `line_length(6.0)` 世界单位 × `W/2` | `rpechart/render/timeline.py:364` |
| 粗细 = `0.0078 × H` | `rpechart/render/theme.py:63` |
| 编号画在**线的下方**（真机 label「2」y=396..411，线 y=383..389） | `rpechart/render/timeline.py:515` |
| `isCover != 1` → 背面音符也画 | `rpechart/render/timeline.py:112` |
| 父线：`rotateWithFather` 时累加父线旋转，位移按父线角度旋转后相加 | `rpechart/render/timeline.py:191`、`rpechart/render/timeline.py:201` |

## HUD（`rpechart/render/theme.py:10`）

坐标全部取自**真机截图 978×642 的实测 bbox**归一化，**不是** phira 的版式
（phira 把分数/曲名/难度放在垂直中部，与实机明显不同）。

| 元素 | 归一化坐标 |
| --- | --- |
| 暂停图标 | `nx 0.0082 / ny 0.0249 / h 0.0467` |
| combo 数字 | 居中 `nx 0.5 / ny 0.0187 / size 0.0783`，标签 `ny 0.0794 / size 0.0415` |
| 分数 | 右对齐 `nx 0.9826 / ny 0.0374 / size 0.0575`，7 位补零 |
| 曲名 | 左下 `nx 0.0082 / ny 1.0 / size 0.0491` |
| 难度 | 右下 `nx 0.9836 / ny 1.0 / size 0.0340` |
| 线编号 | `size 0.0234`，`dy +0.010`（线下方） |

「始终渲染」：`python -m rpechart.render video` 的 `--hud` 默认 **True**（要关用 `--no-hud`），
Web UI 上有一个 HUD 勾选框。

## 打击特效（判定环）

真机右下角的「旋转判定环」= `PhiEdit/Resources/Texture/HE/img-1..31.png` 序列
（模板匹配 mean abs err 最小 0.074）。**不是** `judgeball.png`（那是纯白圆环）。

| 项 | 值 | 实现 |
| --- | --- | --- |
| 序列定位 | `Texture/HE/img-N.png`，按 N 数值排序 | `rpechart/render/assets.py:89` |
| 时长 | 0.32s | `rpechart/render/theme.py:71` |
| 贴图边长 | `0.29 × H`（真机方框 132px，贴图里该框占 178/255） | `rpechart/render/theme.py:72` |
| 颜色 | 跟随判定线色 `(237,236,176)` | `rpechart/render/theme.py:91` |
| 位置 | 命中时刻的线起点 + 音符位移 | `rpechart/render/timeline.py:320` |

## 贴图与字体

- 贴图目录：`PhiEdit/Resources/Texture/`（含 `HE/` 31 帧判定环）
- 字体：`PhiEdit/Resources/fonts/cmdysj.ttf`（曲名/难度/文字卡）、`Exo-Regular.otf`（combo/分数）
- 定位：`rpechart/render/assets.py:29` 先看环境变量 `RPECHART_ASSETS`，再从包目录向上找 `PhiEdit/Resources`

没有官方素材时会自动兜底（画圆角色块 / 用 `ImageFont.load_default()`），
所以库在别的机器上也能跑，只是画面不是实机观感。

## 性能

瓶颈全在 PIL 的 `rotate` / `alpha_composite`。已做的优化：

- 旋转精灵按 0.125° 量化后缓存（`rpechart/render/raster.py:113`）；
- Hold 长条先在 1/4 分辨率上旋转再放大（`rpechart/render/raster.py:222`）；
- 屏外音符/长按直接剔除（`rpechart/render/timeline.py:405`）。

1920×1080 单帧约 0.1s 量级；再快需要换 numpy / GPU 后端。

## 并行与可复现

`RenderModel.scene_at(秒) → Scene` 是**纯函数**（`rpechart/render/timeline.py:289`），
所以同一时刻任意多次调用结果一致、可以从任意帧续渲、也可以多进程分片。
