# 序列化：为什么写出的 chart.json 与 RPE 逐字节相同

RPE 用的是 RapidJSON 的 `PrettyWriter` + `kFormatSingleLineArray` + `std::map` 键序。
`rpechart/rpedump.py` 把这套规则在纯 Python 下复刻了一遍。

## 七条规则

| # | 规则 | 实现 |
| --- | --- | --- |
| 1 | 缩进 **3 个空格**/层；键值分隔是 `"key" : value`（冒号**两侧都有空格**） | `rpedump.py:32` |
| 2 | 对象键按**字节序排序**（不是插入序） | `rpedump.py:62` |
| 3 | 纯标量数组内联成 `[ 0, 0, 1 ]`，含容器元素的数组才逐行展开 | `rpedump.py:62` |
| 4 | 空对象 `{}`、空数组 `[]` | `rpedump.py:62` |
| 5 | 浮点用 `%.17g`（`0.8` → `0.80000000000000004`、`153.6` → `153.59999999999999`），无 `.`/`e` 时补 `.0`；整数按整数写；**`-0.0` 保留负号** | `rpedump.py:35` |
| 6 | 文件 **LF**、无 BOM、末尾恰好一个 `\n` | `io.py:104`（显式 `newline=""`） |
| 7 | 模型侧：`alphaEvents` / `colorEvents` 的 `start`/`end` 整数化；`Event.from_json` 不用 `or` 兜底（否则吞掉 `-0.0`） | `model.py:158` |

## 验证方式

`tests/test_rpedump.py` 拿两份**真实 `.pez`** 做全链路比对：

```
解出 zip 里的 chart.json → json.loads → Chart.from_json → to_json → dumps
```

期望结果：`程序化示例.pez` **82046 字节**、`IN-v1.1.pez` **1260458 字节**，全部相等。

```bash
python tests/run_tests.py rpedump
```

## 为什么不"顺手规范化"

RPE 各版本对同一字段写法差别很大（1.4 写整数 `135`、1.5+ 写 `135.0`；有的版本
总写 `bezier`/`linkgroup` 默认值、有的整键省略）。所以库**不替 RPE 决定「写不写这个键」**：

- 载入时见过的键（`_present`）原样写回；
- 未建模的未知键（`_extra`）无损透传；
- `color` / `tint` 记住原名；
- `eventLayers` 末尾的 `null` 占位（`_null_layers`）原样保留；
- `numOfNotes` 保留文件里的原值（`_raw_numOfNotes`）。

保真回测有两个层次：`tests/test_real_charts.py` 对样本谱面做 `from_json(raw).to_json()`
的**语义**往返比对（int/float 归一，键增删必须为 0）；`tests/test_rpedump.py` 再做
**逐字节**比对 —— 样本谱面 `dumps(json.loads(raw)) + "\n" == raw` 必须成立。
