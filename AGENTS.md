# rpechart — 程序化制谱

用 Python 程序化地**读 / 写 / 生成 / 校验 / 渲染** Re:PhiEdit (RPE) / Phigros 谱面
（`chart.json`），让一份谱面既能导出成标准 `.pez`，也能离线渲染成视频（双输出）。

## 技术栈

Python ≥ 3.10（库本身**零第三方依赖**）；渲染与 Web UI 预览需要 Pillow，
特征提取需要 numpy，音频/视频走 imageio-ffmpeg 自带的 ffmpeg。
测试是自建 runner（`tests/run_tests.py`），不是 pytest。

## 目录速查

| 目录 | 是什么 |
| --- | --- |
| `rpechart/` | 库本体（模型 / 序列化 / 校验 / 渲染 / 生成 / Web UI） |
| `tests/` | 自建 runner 的用例 |
| `tests/fixtures/` | 自合成的测试样本（谱面 / pez / 媒体容器），`make_fixtures.py` 可重建 |
| `docs/` | 详细文档（本文件只做路由，细节都在这里） |
| `work/` 里的 `cdp_*.mjs` | 无头 Edge 的 CDP 验收脚本（改前端后跑一遍） |
| `build/`、`work/` | 生成物与一次性产物（**不入版本库**） |

## 数据安全（改之前先看）

- 测试样本在 `tests/fixtures/`，是保真回测**唯一**的依据，**不可删除、不可改写**。
- 仓库里**不含**官方贴图与字体（第三方素材）。渲染时用 `RPECHART_ASSETS` 或把
  `PhiEdit/Resources/` 放在包目录之上；找不到就退回内置键形兜底，不会崩。
- 跑测试不需要联网，用例都在临时目录里造数据。

## 想了解什么

| 想知道 | 看 |
| --- | --- |
| 库分成哪几层、每层的不变量 | [docs/architecture.md](docs/architecture.md) |
| 写出的 chart.json 为什么和 RPE 逐字节一样 | [docs/serialization.md](docs/serialization.md) |
| 画面怎么算出来的（下落几何 / HUD / 判定环） | [docs/rendering.md](docs/rendering.md) |
| 只给参数怎么生成一张谱面 | [docs/generation.md](docs/generation.md) |
| 怎么跑测试、每个测试文件管什么 | [docs/testing.md](docs/testing.md) |
| 哪些能改、哪些数据不能删、素材从哪来 | [docs/data-safety.md](docs/data-safety.md) |
| Web UI 怎么改成编辑器、路由与验收脚本 | [docs/web-ui.md](docs/web-ui.md) |

## 常用命令

```bash
# 跑全部测试
python tests/run_tests.py

# 从参数文件生成谱面
python -m rpechart.gen spec.json -o build/chart.json

# 打开 Studio（改参数/JSON → 立刻看画面 → 导出 MP4）
python -m rpechart.web --spec spec.json
# 前端改完之后：无头浏览器交互验收（需要 127.0.0.1 上已起服务）
rm -rf work/cdp_profile && node work/cdp_drive.mjs

# 导出标准 pez / 渲染视频
python -c "import rpechart as R; R.export_pez(R.load_chart('chart.json'), 'out.pez')"
python -m rpechart.render video chart.json -o out.mp4
```
