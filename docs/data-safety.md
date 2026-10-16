# 数据安全：哪些能改、哪些不能碰

这个仓库里混着**代码**、**测试样本**和**外部素材引用**。改之前先看这张表。

## 进版本库、不能随手删的

| 路径 | 为什么 |
| --- | --- |
| `rpechart/` | 库本体（模型 / 序列化 / 校验 / 渲染 / 生成 / Web UI）。这是仓库存在的理由。 |
| `tests/` | 用例。自建 runner，不需要 pytest。 |
| `tests/fixtures/` | **测试样本**：三份谱面（现代 / 老版本 / 多判定线）+ 一个 `.pez` + 两个合成媒体容器。全部由 `tests/fixtures/make_fixtures.py` **用本库自己生成**，不含任何第三方素材。保真回测（逐字节相同）唯一的依据 —— 删了就只剩跳过。 |
| `docs/`、`AGENTS.md`、`README.md` | 文档。`AGENTS.md` 是路由表，细节都在 `docs/`。 |

## 外部素材：本仓库不包含，需要你自己指路

原始项目里有一批**没有版权的第三方素材**（编辑器本体、官方贴图、真实谱面、手册），
它们**故意不在这个仓库里**，`.gitignore` 只放行代码、文档与自合成的测试样本。

| 你想要的 | 去哪里弄 |
| --- | --- |
| 官方贴图与字体 | Re:PhiEdit 安装目录下的 `PhiEdit/Resources/Texture/` 与 `fonts/` |
| 判定环（打击特效） | 同上的 `Texture/HE/`，31 帧 `img-1..31.png` |
| 真实谱面语料 | 自己收集，或用 `tests/fixtures/charts/*.json` 起步 |

渲染器通过 `rpechart/render/assets.py` 定位贴图：先看环境变量 `RPECHART_ASSETS`，
再从包目录逐级向上找 `PhiEdit/Resources`。**找不到就退回内置的色块兜底**，
不会崩、也不会去找任何硬编码盘符。

## 可以随便改

| 路径 | 说明 |
| --- | --- |
| `rpechart/` | 改完必须跑 `python tests/run_tests.py`。 |
| `tests/` | 用例（`tests/fixtures/` 改完要重跑 `make_fixtures.py` 并确认字节保真）。 |
| `docs/`、`AGENTS.md`、`README.md` | 改文档要同步 `tests/test_packaging.py` 里的断言。 |
| `build/`、`work/` | 生成物与一次性产物，随便写、随便删（都不入版本库）。 |

## 机器相关的路径

`rpechart/_bootstrap.py` 是唯一的「找仓库根」实现，**fail-closed**：
找不到就抛 `RuntimeError`，绝不回退到硬编码路径（回退比失败更糟 —— 它会让脚本
在错误的目录上「成功」运行，然后写出一堆看起来正常、其实放错地方的文件）。

`tests/test_packaging.py::no_module_hardcodes_an_absolute_repo_path` 用 AST 扫
全仓源码，任何**非 docstring** 的字符串常量里出现写死的仓库路径都会让测试变红。

## 外部依赖的边界

- 核心库（读写、校验、打包）**零第三方依赖**；
- 渲染需要 `Pillow`，特征提取需要 `numpy`，音视频需要 `imageio-ffmpeg`；
- 测试不需要联网、不需要 pytest。
