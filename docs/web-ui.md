# Web UI（Studio）：可编辑、可渲染的谱面工作台

`python -m rpechart.web --spec spec.json` 打开的这个界面不是"参数表单 + 预览图"，
而是一个**编辑器**：左边改参数或 JSON，中间出画面，下边是时间轴，右边看校验。
它和 `render/cli.py` 走同一个 `Renderer`，所以**预览看到的画面就是导出 MP4 的画面**
（同尺寸、同 HUD、同兜底素材）。

## 为什么是「静态三件套」而不是前端框架

`rpechart/web/static/` 下只有 `index.html` / `app.css` / `app.js` 三个文件，
**没有构建步骤**。这是刻意的：

- 库本身零第三方依赖，`pip install rpechart` 之后不应该还要 `npm install`；
- 单文件前端才能被 `package-data` 原样带进 wheel（见 `pyproject.toml` 的
  `"rpechart.web" = ["static/*"]`）；
- 全部同源、零外网请求（`tests/test_web_studio.py::web_studio_has_no_network_dependency`
  会扫描 `http://`、`cdnjs`、`fonts.googleapis` 等字样并失败）。

代价是没有模块系统：`app.js` 是一个 IIFE，靠 `$('id')` 取元素。因此有三条
**静态契约**由测试钉住，改前端时必须一起满足：

| 契约 | 用例 |
| --- | --- |
| app.js 里 `$('x')` 引用的 id 必须在 index.html 里存在 | `web_studio_dom_ids_match_the_javascript` |
| app.css 里 `var(--x)` 引用的令牌必须定义过 | `web_studio_css_variables_are_all_defined` |
| 只用同源 `/api/*` 路由 | `web_studio_uses_only_same_origin_api_routes` |

## 后端路由

`rpechart/web/server.py`（`WebApp` + `ThreadingHTTPServer`）。

| 路由 | 用途 |
| --- | --- |
| `GET /`、`GET /static/*` | 静态资源（`_serve_static` 有越界防护，穿越请求回 403） |
| `GET /api/health` | 存活探测 |
| `GET /api/spec`、`POST /api/spec` | 读/写 spec（**唯一写路由**） |
| `GET /api/stats` | 谱面统计 + 校验报告 |
| `GET /api/chart` | chart.json 的 RPE 风格原文 |
| `GET /api/capabilities` | 有没有 Pillow / ffmpeg / 官方素材 / SE 素材 |
| `GET /api/timeline` | 前端时间轴用的整条时间线 JSON |
| `GET /api/preview` | PNG 帧（Content-Type **恰好** `image/png`） |
| `GET /api/frame.jpg` | JPEG 帧（播放与胶片用，编码比 PNG 便宜约 8 倍） |
| `GET /api/filmstrip.jpg` | 一条胶片缩略图（可带 `start` / `end` 只取视野窗口） |
| `GET /api/se/<kind>` | 一个打击音素材（tap / flick / drag，供预览播放） |
| `POST /api/export`、`GET /api/export/status`、`POST /api/export/cancel` | 后台导出 MP4（`hitsounds` / `se_volume` 见下） |

**为什么要有 JPEG**：实测 960×540 下 PNG 编码 32.85ms/帧（8.3KB），JPEG q82
4.00ms/帧（17.8KB）。预览播放走每帧一次 HTTP，PNG 端到端只有约 17fps，不够看。

**导出是后台任务**：ffmpeg 写文件期间 `cancel` 必须先等本次新起的 ffmpeg 进程
退出、再删半成品，否则 mp4 被句柄钉住删不掉。进程探测走
`tasklist /FI "IMAGENAME eq <exe>"`（本机 0.40s），不是全量列举（全量 24.71s）。

## 前端结构

`app.js` 的骨架（按文件顺序）：

- `S`：唯一状态对象（spec/timeline/time/view/frameCache/export…）。
- `api(path, opts)`：统一 fetch，按 content-type 判 JSON 并检查 `res.ok`。
- 左栏 rail：`renderForm()` 造参数控件，`#spec` 文本域是**权威输入**，参数页写回
  的也是它；`mirrorSpecToJson()` 负责这一层的同步。
- 中间舞台：`showFrame(t)` 用 `S.frameSeq` 丢弃过期响应，
  `FRAME_CACHE_MAX` 是个 LRU；`prefetchAround()` 在播放时预取 10 帧。
- 时间轴：`paintTimeline()` → `paintTicks / paintFilm / paintTracks / paintPlayhead`；
  缩放按钮是 `.tl-ops` 里的 `−` / `+` / `适配`，滚轮/键盘仍然可用。
- 导出：`exportOpen / exportStart / pollExport / exportCancel` 对应上面三个路由。

### 时间轴的视野（zoom/pan）

`S.view = { zoom, start }` 是一个**时间窗口**，不是"显示比例"的修饰：

- `viewSpan() = tlDuration() / zoom`，`timeToX/xToTime` 都基于这个窗口；
- `clampView()` 保证窗口不越出 `[0, duration]`，`zoom = 1` 时就是整条铺满；
- 滚轮以**指针位置为锚点**缩放（`zoomTo(z, anchor)`），`Alt`+拖拽平移，
  双击或 `0` 复位；transport 里也提供了可见的 `−` / `+` / `适配` 按钮；
- 胶片不是整曲一张低清图：`filmWindow()` 只取当前视野，`loadFilmstrip()`
  以 180ms debounce 重新请求视野内的 n 张缩略图，所以放大后是「换一段胶片」，
  不是把同一张图拉长；
- **所有绘制都按视野剔除**：刻度从 `floor(start/step)*step` 起步、音符按
  `[t0, t1]` 跳过、速度事件轨同理、胶片按 `drawImage` 的源矩形裁剪。
  这不是微优化 —— 放大到 100 倍时可见区间只有全长的 1%，不剔除就要遍历整条谱面。

`tests/test_web_studio.py::web_studio_timeline_culls_offscreen_work` 钉的正是
这几行的**原文**（而不是"函数里有 continue"这种能被同名关键字骗过的弱断言）。

### 导出里的打击音（SE）

导出面板的「打击音效（SE）」下拉发的是 `hitsounds` 字段：`null`（不加）／
`"auto"`（让后端自己找）／一个 SE 目录路径。音量走 `se_volume`（默认 0.532，来自 PhiEdit 设置）。

默认值不是写死的，而是跟着 `/api/capabilities` 走：后端报出
`se: ["drag", "flick", "tap"]` 就把下拉默认到「自动查找官方 SE」，一个都没有就默认
「不加音效」——免得用户点一下导出才发现白等一场。同一条能力探测还给出 `se_dir`
与 `se_volume`。

**找不到素材不算错误**：后端的 `_parse_hitsounds` 只做「形状」归一化（空值→null、
`{dir: ...}`→路径、`{类型: 路径}` 映射），目录在不在、素材缺不缺一律留给渲染线程去判，
失败只打一行 stderr 警告并退回无音效。`tests/test_web_se.py` 钉住这套行为。

### 预览里的打击音（SE）

预览和导出用的是同一批素材，但触发方式不同：

- 能力探测报出 `se` 非空时，舞台右上角显示 `SE` 开关，默认开启；
- 音源走同源 `GET /api/se/<kind>`（只允许 tap / flick / drag），前端用
  WebAudio `decodeAudioData` 解码后缓存；
- 映射与导出一致：Tap / Hold → tap，Flick → flick，Drag → drag；音量取
  `capabilities.se_volume`，再乘 0.9 防多押叠加削波；
- 每次 `play()` 后由 `playSeBetween(seLast, time)` 只挑 `(t0, t1]` 内的音符，
  二分游标避免每帧全表扫描；`seek` / `pause` 会停掉正在响的 BufferSource；
- 预览是逐音符触发，**不是采样级混音**；导出仍由 ffmpeg 混一条 SE 轨，两者
  包络/30ms 拖音偏移不会逐样本一致。

### 设计令牌

颜色只有一个来源：`app.css` 的 `:root`。`<canvas>` 取不到级联，所以
`app.js` 用 `cssVar('--c-x', '#fallback')` 现查（带缓存），半透明色走
`_rgba(hex, alpha)`。测试禁止任何"裸写"的十六进制色值。

## 无头浏览器验收

仓库里没有 playwright/puppeteer，验收脚本用 **Node 内置 WebSocket 直连
headless Edge 的 CDP**（Windows 本机只有 `msedge.exe`）：

```bash
python -m rpechart.web --spec work/studio_spec.json --port 8755 --no-open &
rm -rf work/cdp_profile && node work/cdp_drive.mjs     # 16 项交互断言
rm -rf work/cdp_profile && node work/cdp_zoom.mjs      # 13 项时间轴断言
```

坑：每轮必须删 `work/cdp_profile`（否则端口占用/复用旧 target）；
`--screenshot` 必须给 **Windows 绝对路径**；CDP 的 `Input.dispatchKeyEvent`
对 `PageDown` 这类键**不能**传 `text`（会报 `Invalid 'text' parameter`）。

## 两个曾经的真实 bug（都有回归用例）

- **`hidden` 被类规则盖掉**：`.stage-empty { display: flex }` 的优先级高于
  浏览器默认的 `[hidden] { display: none }`，于是 `el.hidden = true` 完全无效，
  首屏的"还没有画面"遮罩永不消失。修法是样式表最前面加
  `[hidden] { display: none !important; }`（全表唯一允许 `!important` 的地方）。
- **时间轴画布键盘到不了**：`#tl` 有完整的 `keydown`（方向键逐帧），但没有
  `tabindex`，所以那段代码永远跑不到。同时它是 `role="slider"` 却从不写
  `aria-valuenow`，读屏永远念初始值。

## 本轮前端审计（2026-10）修了什么

这一轮不是重写，而是把「看起来能用、实际点下去不对」的地方逐条钉住。
新增的 21 个契约用例都在 `tests/test_web_studio.py`，与实现一一对应：

| 现象 | 根因 | 修法 / 用例 |
| --- | --- | --- |
| 播放后按钮还是「播放」图标 | SVG 没有 `hidden` IDL，`el.hidden = true` 只是挂了个 expando | 改 `setAttribute/removeAttribute('hidden')`；`play_icon_switches_by_attribute` |
| 画布聚焦时空格/方向键走两步 | `bindTimeline` 与 `document` 的 `globalKeys` 都处理，`preventDefault` 不阻止冒泡 | `globalKeys` 先看 `e.defaultPrevented`；`keyboard_is_not_handled_twice` |
| 1024 宽横向溢出、舞台被裁 | `.app` 的隐式 `auto` 列按 min-content 撑开 | `grid-template-columns: minmax(0, 1fr)` + topbar `min-width:0`；`layout_can_shrink_below_its_content` |
| 表单复选框被拉成 135px | `.fld input` 的 `width:100%` 同特异性覆盖了复选框宽度 | `.fld input:not([type=checkbox])`；`form_checkbox_keeps_its_size` |
| 帮助里的 J/L、+/-/0、PgUp/PgDn 不生效 | 全局只处理了方向键；缩放键只在画布聚焦时生效 | 全局补齐，画布与全局共用 `nudgeZoom/panView`；`global_timeline_keys` |
| 换帧率后画面停在旧时间点 | 帧键不含 fps，旧缓存被新 fps 复用 | 帧键尾追加 fps，`frameUrl` 从键里解析；`frame_cache_keys_include_the_frame_rate` |
| 慢请求覆盖新帧 | `force` 分支绕过了 `seq` 检查 | 时序检查无条件执行；`stale_frame_requests_never_win` |
| 校验错误排在警告后面 | `order[...] || 9` 把 ERROR 的 0 吃掉了 | `?? 9`；`issues_sort_errors_first` |
| 严重度配色/筛选全失效 | 后端发小写 `error`，前端类名与筛选键是大写 | `applyStats` 统一 `toUpperCase()`；`issue_severities_are_normalized` |
| 导出轮询起两条 setTimeout 链 | `exportOpen` 与 `pollExport` 各调度一次，`exportTimer` 只能取消后一条 | 只保留 running 分支的单次调度；`export_polling_has_one_loop` |
| 时间轴每帧重分配 canvas、hover 全量重绘 | 无尺寸判断、pointermove 直接画 | 尺寸不变跳过重设、hover 走 rAF 合并、`countNear` 二分；`timeline_does_not_reallocate_every_paint` / `hover_count_uses_binary_search` |
| 胶片失败永远显示「生成中…」 | 失败没写终态，`paintFilm` 还依赖 `S.caps` | `S.filmError` + 按 F 重试；`filmstrip_failure_has_a_terminal_state` |
| 帧缓存名义 LRU 实际 FIFO | 命中不刷新淘汰顺序 | `touchFrame(key)`；`frame_cache_is_lru_not_fifo` |
| `00:59.9996` 显示成 `00:60.000` | 先 floor 分钟再 toFixed 秒 | 先四舍五入到整毫秒再拆；`time_format_rounds_before_splitting` |
| favicon 404 | 没有 `<link rel="icon">` | 内联 base64 PNG，保持零外网；`ships_a_favicon` |
| 启动失败文案重复两遍 | catch 里既改文本又 append 同文案节点 | 清空后只 append 一个 `.boot-text`；`boot_failure_reports_once` |
| 模态框关闭后焦点丢到 body | 没记录触发元素 | `lastFocus` 记录/还原；`modals_restore_focus` |

另外修了 `/api/chart` 非 2xx 仍把错误正文当概览（`chart_text_checks_http_status`）、
模板不更新顶栏曲名（`template_updates_the_document_name`）、以及页签的
roving tabindex + 方向键切换（`tabs_are_keyboard_navigable`）。

### 本轮验收命令与结果

```bash
python tests/run_tests.py                 # 646 个用例全绿（前端契约 35 个）
node work/cdp_drive.mjs                   # 16/16
node work/cdp_zoom.mjs                    # 13/13
node work/cdp_export.mjs                  # 9/9（真实导出 960x540/30fps/0.4s，含 SE）
node work/_verify_finish.mjs              # 11/11：boot 单文案、焦点转移/还原、换 fps 重取帧、1024 布局
node work/_verify_se_zoom.mjs             # 20/20：视野胶片、缩放按钮、SE 开关/请求/播放
node work/_shots_layouts.mjs              # 720/1366/1920 截图，无横向溢出
```

截图在 `work/shots/_final_1024x768.png`、`_layout_720x900.png`、
`_layout_1366x768.png`、`_layout_1920x1080.png`；本轮胶片/缩放/SE 的截图是
`_se_zoom_1600.png`、`_se_zoom_1600_zoom.png`、`_se_zoom_1024.png`。

### 有意没做的

- 1180px 以下直接隐藏整个检查器（校验/分线/概览），720px 以下左栏与舞台改为上下堆叠：
  这是现有的响应式取舍，不是 bug；真要做移动端编辑器需要另一套布局。
- 模态框只做了「焦点进入 + 关闭还原」，没有做 Tab 焦点陷阱；屏幕阅读器用户仍可能
  Tab 到背景里的顶栏。
- 导出验收依赖本机 ffmpeg 与 SE 素材；没有 SE 时面板默认「不加音效」是刻意行为，
  不是能力探测失败。
- **音符级编辑没有做**：目前 Studio 是「参数 / JSON 编辑 + 时间轴定位」，不是
  RPE 式打谱器。时间轴上没有新增 / 删除 / 拖动 / 框选音符的入口，也没有撤销栈；
  这不是入口忘了写，是还没有实现。需要的话要单独立项（命中测试、吸附、批量选择、
  撤销模型）。
- 预览 SE 是 WebAudio 逐音符触发，不做采样级混音；导出走 ffmpeg 混音，两者听感
  接近但不保证逐样本一致，也没有单独的预览音量滑杆。

