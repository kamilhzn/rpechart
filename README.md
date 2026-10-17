# rpechart

程序化读写 **Re:PhiEdit (RPE) / Phigros 谱面**（`chart.json`）的 Python 库：

- **无损往返** —— 载入再写出与 RPE 自己导出的 chart.json **逐字节相同**；
- **标准 pez 导出** —— `.pez`（zip）的成员、`info.txt` 键序与换行都照抄真实文件；
- **离线渲染** —— 用官方贴图复刻实机画面（下落几何按秒积分、HUD、判定环、
  双押/多押的外泛黄光），可渲成单帧/接触表/MP4，并可叠上 PhiEdit 的打击音效；
- **参数化生成** —— 只写一个 `spec.json`（BPM / 段落 / 判定线 / 风格）就得到一张谱面；
- **本地 Studio** —— 浏览器里的谱面编辑器：左栏改参数或 JSON、中间出画面
  （与导出 MP4 **同一个渲染器**）、下边是可缩放的时间轴与胶片、右边实时看校验；
  一键后台导出 MP4（可取消）。纯静态三件套，零构建、零外网。

## 安装

```bash
pip install -e .            # 核心库：零第三方依赖
pip install -e ".[render]"  # 加渲染（Pillow）
pip install -e ".[dev]"     # 加 numpy + imageio-ffmpeg（特征提取与音视频）
```

## 五步上手

```python
import rpechart as R

# 1. 读一张现成的谱面
chart = R.load_chart("chart.json")
print(chart.META.name, len(chart.judgeLineList), "条判定线")

# 2. 校验（ERROR 必须改 / WARNING 仔细看 / CAUTION 作参考）
report = R.validate(chart)
print(report.format())

# 3. 导出成标准 pez
R.export_pez(chart, "out.pez", chart_name="chart.json")

# 4. 渲染成视频（同一份 chart.json）
R.render_chart_video("chart.json", "out.mp4", fps=60, size=(1920, 1080))

# 5. 想连打击音一起出（素材来自 PhiEdit/Resources/SE，缺了也不报错）
R.render_chart_video("chart.json", "out.mp4", audio="song.mp3",
                   hitsounds="PhiEdit/Resources/SE")
```

想看画面而不写代码：`python -m rpechart.web --spec examples/demo.spec.json`（打开 Studio，
见 [docs/web-ui.md](docs/web-ui.md)）。

## 文档

详细文档在 [`docs/`](docs/)，**[AGENTS.md](AGENTS.md) 是路由表**。

## 测试

```bash
python tests/run_tests.py          # 全部
python tests/run_tests.py render   # 只跑名字含 render 的
```

自建 runner，**不需要 pytest**，全部用例在临时目录里造数据。
测试样本在 `tests/fixtures/`，由 `python tests/fixtures/make_fixtures.py` 生成，
**不含任何第三方素材**（真实谱面与官方贴图都不进版本库）。
