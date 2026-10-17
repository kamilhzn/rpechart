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

## 多押高亮（双押/多押的外泛黄光）

口径来自 phira 的 \`prpr/src/parse.rs:20-71\` \`process_lines\`：把**所有判定线**的
音符起点汇到一起，**出现次数 ≥ 2** 的时间点算多押（跨线全局、不排除 fake），命中的
音符全部标记。渲染侧的开关是 \`Theme.multi_hint\`（默认 True，对齐 phira 的
\`config.double_hint\`）。

| 项 | 实现 |
| --- | --- |
| 多押起点集合 | \`RenderModel._multi_fracs\`（\`rpechart/render/timeline.py:200\`），Beat.frac 精确比较 |
| 音符标记 | \`NoteDraw.multi\` / \`NoteDraw.texture_mh\`（\`rpechart/render/scene.py\`） |
| 官方高亮贴图 | \`Texture/Tap2HL.png\` / \`Flick2HL.png\` / \`DragHL.png\`（\`rpechart/render/theme.py:105\`） |
| 高亮几何 | 与普通贴图**同宽**、只是更高，所以「宽度定、高度随比例」的既有规则即 phira \`draw_center\` 语义，无需额外缩放 |
| 长按 / 内置键形 | 官方**没有** Hold 的 HL 变体，按官方光带反解出的规则现场合成外泛光（\`rpechart/render/raster.py\` 的 \`_build_glow\`） |

合成规则是把贴图轮廓的 alpha 做高斯模糊再乘增益、染成 \`(255,255,102)\`：\`radius\`
取贴图短边的 0.24 倍，\`gain\` 取 2.0（用它与官方 \`Tap2HL\`/\`DragHL\` 的光带逐像素
比对，平均差 7/255）。两个必须守住的细节：模糊要在**带 padding 的画布**上做
（只模糊贴图本身则光晕一个像素都不外扩），长条的半径按**宽度**取而不是按长度
（按长度会让光晕退化成中间一团）；长条按宽度的 16% 取，直接拿整条宽度会得到一大团黄雾。

## 打击音（SE）

渲染出来的 MP4 默认**没有打击音** —— RPE 谱面数据里本来就没有音效，打击音是
播放时按音符触发的（两份官方样本 1000002 / 1000003 一个 `hitsound` 字段都没有）。
所以要出声，得由渲染器自己把音频素材按谱面时刻混进去。

素材对应关系（`.mp3` 由用户提供，仓库不含）：

| 音符 | 素材 | 说明 |
| --- | --- | --- |
| Tap（蓝） | `tap.mp3` | |
| Hold | `tap.mp3` | 与 phira 一致：`HitSound::default_from_kind` 里 `Hold{..} => Click` |
| Flick（红） | `flick.mp3` | |
| Drag（黄） | `drag.mp3` | 比 Tap 早 30ms（`SE_OFFSET_DRAG = -0.030`，即 `t = 音符时刻 + offset`） |

定位顺序：环境变量 `RPECHART_SE_DIR` → 与贴图同一套向上查找
（`PhiEdit/Resources/SE`）→ 从谱面目录逐级向上。默认音量 `SE_VOLUME = 0.532`、
拖音延迟 `SE_OFFSET_DRAG = -0.030` 都取自 PhiEdit 的 `Settings.json`。
方向按正负号字面理解（`t = 音符时刻 + offset`，所以 Drag 提前 30ms 落下去）：
RPE 手册对这个键只写「音符开始时间和产生音效与特效的时间差」，没有规定正方向，
而游戏里两者都设在命中反馈窗口内，听感差异极小；要翻成延后 30ms 只需把
`rpechart/audio.py` 的 `SE_OFFSET_DRAG` 取反。

入口有三个：

```bash
python -m rpechart.render video chart.json -o out.mp4 --se-dir PhiEdit/Resources/SE
# 也可 --se-volume 0.8
```

```python
from rpechart.audio import build_hitsound_track
from rpechart.video import render_chart_video

# 只混一条打击音轨（48kHz 立体声 WAV，调用方负责删除）
wav = build_hitsound_track('chart.json', se_dir='PhiEdit/Resources/SE')

# 或直接让视频带上：hitsounds= 目录 / {类型: 路径} 映射
render_chart_video('chart.json', 'out.mp4', audio='song.mp3',
                   hitsounds='PhiEdit/Resources/SE')
```

Studio 的导出面板里有「打击音效（SE）」下拉（不加 / 自动查找 / 指定目录）与音量输入，
后端的 `/api/export` 接同名参数 `hitsounds` / `se_volume`。

**降级是硬约定**：SE 目录找不到、素材缺失、ffmpeg 不给力 —— 任何一步失败都只往
stderr 打一行警告，然后退回不带音效的老路径，**绝不让导出失败**。
`tests/test_web_se.py::web_se_missing_dir_does_not_break_the_export` 钉的正是这条。

## 贴图与字体

- 贴图目录：`PhiEdit/Resources/Texture/`（含 `HE/` 31 帧判定环）
- 字体：`PhiEdit/Resources/fonts/cmdysj.ttf`（曲名/难度/文字卡）、`Exo-Regular.otf`（combo/分数）
- 定位：`rpechart/render/assets.py:29` 先看环境变量 `RPECHART_ASSETS`，再从包目录向上找 `PhiEdit/Resources`

没有官方素材时会自动兜底：`rpechart/render/builtin.py` 程序化生成标准蓝 Tap、黄 Drag、
红 Flick 和青蓝长条 Hold（白描边，比例对齐官方贴图），多押外泛光仍由 `_build_glow`
合成；字体退回 `ImageFont.load_default()`。所以库在别的机器上也能跑，只是画面不是实机观感。

## 性能

瓶颈全在 PIL 的 `rotate` / `alpha_composite`。已做的优化：

- 旋转精灵按 0.125° 量化后缓存（`rpechart/render/raster.py:113`）；
- Hold 长条先在 1/4 分辨率上旋转再放大（`rpechart/render/raster.py:222`）；
- 屏外音符/长按直接剔除（`rpechart/render/timeline.py:405`）。

1920×1080 单帧约 0.1s 量级；再快需要换 numpy / GPU 后端。

## 并行与可复现

`RenderModel.scene_at(秒) → Scene` 是**纯函数**（`rpechart/render/timeline.py:289`），
所以同一时刻任意多次调用结果一致、可以从任意帧续渲、也可以多进程分片。
