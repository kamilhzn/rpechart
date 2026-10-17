# 测试

**不用 pytest** —— 本机没装，而且这个仓库讨厌额外依赖。
`tests/run_tests.py` 是一个 106 行的自建 runner。

## 用法

```bash
python tests/run_tests.py            # 跑全部
python tests/run_tests.py render     # 只跑名字含 render 的
python tests/run_tests.py web        # 只跑 web
```

全过时打印 `全部通过: N 个用例 (T s)` 并返回 0；有失败则逐条打印断言与栈并返回 1。

## 写法

```python
from run_tests import test, approx, raises

@test
def my_case():
    assert something == other
```

| 工具 | 作用 | 位置 |
| --- | --- | --- |
| `@test` | 把函数注册进 `_REGISTRY` | `tests/run_tests.py:32` |
| `approx(a, b, tol=1e-6)` | 浮点近似 | `tests/run_tests.py:38` |
| `raises(exc, fn, *a)` | 断言抛异常 | `tests/run_tests.py:43` |
| `main(argv)` | 支持 pattern 过滤 | `tests/run_tests.py:66` |

runner 用 `importlib` **按路径**加载 `test_*.py`（不是 import 包），
所以测试文件不需要 `__init__.py`，互相也不会串味。

## 测试文件管什么

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_model.py` | 模型对象、`_present` / `_extra` 无损往返 |
| `tests/test_beat.py` | `Beat` 的 Fraction 精度与 `raw` 保留 |
| `tests/test_timing.py` | 拍 ↔ 秒、非恒定 BPM |
| `tests/test_easing.py` | 29 条缓动与越界回退 |
| `tests/test_interp.py` | `value_at` 的乱序/重叠/窗口行为（P0 修复的守门测试） |
| `tests/test_validate.py` | 三级校验规则，含 `_validate_out_of_screen` 的线速口径 |
| `tests/test_builders.py` | 写谱 API、`_splice` 裁剪 |
| `tests/test_io.py` | 读写、原子写、`info.txt` 键序、install / export / import |
| `tests/test_rpedump.py` | **RPE 风格序列化的逐字节复刻** |
| `tests/test_real_charts.py` | 样本谱面的往返保真（动态注册，读 `tests/fixtures/`） |
| `tests/test_audio.py` | 音频嗅探 / 时长 / LRC |
| `tests/test_tempo.py` | BPM 估计与网格拟合 |
| `tests/test_video.py` | 帧 → MP4、渲染器定位、流式解码 |
| `tests/test_render.py` | 渲染几何（按秒积分）、场景纯函数、HUD |
| `tests/test_gen.py` | 生成层（spec 校验、确定性、三道合法化、四条硬约束） |
| `tests/test_render_cycles.py` | 渲染侧的 `father` 链（含 `father=0` 与成环/自环/深链） |
| `tests/test_render_hl.py` | 多押高亮：口径、HL 贴图、现场合成外泛光（自造迷你素材） |
| `tests/test_render_builtin.py` | 无官方素材时的内置键形（四键标准色/白边、多押黄光、官方优先） |
| `tests/test_web.py` | Web UI 的 HTTP 契约（真起服务、真打请求） |
| `tests/test_web_api.py` | 新增 API：能力探测 / 时间轴 / JPEG / 胶片 / 导出任务 |
| `tests/test_web_studio.py` | 前端静态契约：DOM id、CSS 令牌、可达性、时间轴缩放、SE 接线 |
| `tests/test_web_se.py` | 导出打击音在 Web 层的参数归一化与降级 |
| `tests/test_bugfixes.py` | 一轮只读审查揪出的缺陷的守门测试（崩溃 / 静默丢数据 / 字节保真） |
| `tests/test_se_audio.py` | 打击音混音与 `--se-dir` 命令行接线 |
| `tests/test_example.py` | `examples/make_demo_chart.py` 能跑通 |

## 约定

- 用例只在**临时目录**里造数据，不联网、不需要 pytest；
- 需要样本谱面时从 `tests/fixtures/` **只读**取（缺样本则跳过而不是失败）；
- 当前规模：`python tests/run_tests.py` → **622 个用例**（约 63s）。
