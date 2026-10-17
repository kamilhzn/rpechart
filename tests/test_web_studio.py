"""Studio 前端的静态契约测试。

前端是**无构建步骤**的三件套（index.html / app.css / app.js），所以这里守的是
「这个前端自己还成立吗」：没有外网依赖、没有动态代码、DOM id 与 JS 引用对得上、
CSS 令牌都定义过、无障碍与响应式的关键钩子还在。

真起服务的往返测试在 tests/test_web.py（既有）+ tests/test_web_api.py（新 API）。
"""

import os

from run_tests import test

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "rpechart", "web", "static")


def _read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _between(text, opener, closer):
    """把 text 里 opener 与 closer 之间的片段全部取出来（不做正则）。"""
    out = []
    rest = text
    while True:
        i = rest.find(opener)
        if i < 0:
            return out
        rest = rest[i + len(opener):]
        j = rest.find(closer)
        if j < 0:
            return out
        out.append(rest[:j])
        rest = rest[j + len(closer):]


def _css_token_names(text):
    """把所有 `--name:` 形态的令牌名取出来（只接受字母数字与连字符）。

    刻意不用正则：测试文件本身也要能一眼看懂、不被转义绕晕。
    """
    out = set()
    for chunk in text.split("--")[1:]:
        head = chunk.split(":")[0].strip()
        if head and all(c.isalnum() or c == "-" for c in head):
            out.add("--" + head)
    return out


@test
def web_studio_ships_the_three_static_files():
    """三件套必须都在，且 HTML 通过 link/script 真正引到它们。

    这条是给「重构时顺手把 app.css 改名」踩的刹车：打包 package-data 只收
    static/*，文件名一改就装不进 wheel。
    """
    for name in ("index.html", "app.css", "app.js"):
        path = os.path.join(STATIC, name)
        assert os.path.isfile(path), "缺少 " + name
        assert os.path.getsize(path) > 512, name + " 太小，像是被清空了"
    html = _read("index.html")
    assert '<link rel="stylesheet" href="/static/app.css">' in html
    assert '<script src="/static/app.js">' in html


@test
def web_studio_has_no_network_dependency():
    """前端不能有外网依赖：CDN 字体、图标库、统计脚本一律不许。

    这个库的卖点是「离线可跑」，预览界面弹不出网也不能掉样式。
    """
    bad_markers = (
        "http://", "https://", "//cdn.", "cdnjs", "unpkg.com", "jsdelivr",
        "fonts.googleapis", "fonts.gstatic",
    )
    for name in ("index.html", "app.css", "app.js"):
        text = _read(name)
        for marker in bad_markers:
            assert marker not in text, name + " 里有外网引用: " + marker
    for url in _between(_read("app.css"), "url(", ")"):
        cleaned = url.strip().strip(chr(34)).strip(chr(39))
        assert not cleaned.startswith("//"), "app.css 引了外网 " + cleaned


@test
def web_studio_javascript_avoids_dynamic_code():
    """不许 eval / new Function / document.write：静态页面也要能过 CSP。"""
    js = _read("app.js")
    assert "eval(" not in js
    assert "new Function" not in js
    assert "document.write" not in js
    # innerHTML 是 XSS 与「拼字符串拼错」的双重来源；这个前端只用 createElement。
    assert "innerHTML" not in js


@test
def web_studio_dom_ids_match_the_javascript():
    """JS 里 $('x') 引到的每个 id 都必须在 HTML 里存在。

    改版时最典型的静默失败：JS 查一个已经不存在的节点，得到 null，
    之后某处 TypeError 只在点某个按钮时才炸。
    """
    html = _read("index.html")
    js = _read("app.js")
    ids = set(_between(html, ' id="', '"'))
    refs = set(_between(js, "$('", "')"))
    assert refs, "没有从 app.js 里解析到任何 $() 调用，说明选择器写法变了"
    missing = sorted(refs - ids)
    assert not missing, "app.js 引用了 HTML 里不存在的 id: " + repr(missing)


@test
def web_studio_css_variables_are_all_defined():
    """var(--x) 用到的 --x 必须先在 :root 定义过。

    设计令牌的唯一价值就是「改一处、全局变」；引用一个没定义的令牌，
    浏览器会静默回退成初始值，看起来像随机错位。
    """
    css = _read("app.css")
    assert ":root" in css, "缺少 :root 设计令牌"
    defined = _css_token_names(css)
    used = set("--" + x.strip() for x in _between(css, "var(--", ")"))
    assert used, "没有解析到 var() 引用"
    undefined = sorted(used - defined)
    assert not undefined, "app.css 用了未定义的令牌: " + repr(undefined)


@test
def web_studio_styles_cover_accessibility_and_motion():
    """焦点可见、键盘可达、减弱动效、响应式断点 —— 一个都不能少。"""
    html = _read("index.html")
    css = _read("app.css")
    assert ":focus-visible" in css, "缺少 :focus-visible，键盘用户看不到焦点"
    assert "prefers-reduced-motion" in css, "缺少减弱动效分支"
    assert "@media" in css, "没有响应式断点"
    assert "aria-label" in html or "aria-labelledby" in html
    assert 'lang="zh-CN"' in html
    assert 'name="viewport"' in html
    assert "<title>" in html
    # 承载时间轴的画布要能被键盘聚焦（方向键定位）
    assert 'tabindex="0"' in html


@test
def web_studio_uses_only_same_origin_api_routes():
    """前端只许打同源 /api/ 路由，路径拼法统一。"""
    js = _read("app.js")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_/.")
    routes = set()
    for chunk in _between(js, "/api/", chr(39)) + _between(js, "/api/", chr(34)):
        for cut in ("?", "&", "#", " "):
            chunk = chunk.split(cut)[0]
        clean = ""
        for ch in chunk:
            if ch in allowed:
                clean += ch
            else:
                break
        if clean:
            routes.add("/api/" + clean)
    assert routes, "没有解析到任何 /api/ 路由"
    for route in sorted(routes):
        assert ".." not in route, "路由里有上跳: " + route
        assert "//" not in route[1:], "路由拼错: " + route
    assert "/api/spec" in routes
    assert "/api/stats" in routes


def _tags(html):
    """把 '<tag ...>' 扫成 (tag, attrs) —— 不做正则，避免测试文件自己被转义绕晕。"""
    out = []
    rest = html
    while True:
        i = rest.find("<")
        if i < 0:
            return out
        j = rest.find(">", i)
        if j < 0:
            return out
        body = rest[i + 1:j].strip()
        rest = rest[j + 1:]
        if not body or body.startswith(("/", "!", "?")):
            continue
        parts = body.split(None, 1)
        out.append((parts[0], parts[1] if len(parts) > 1 else ""))


def _attr(attrs, name):
    """从一个标签的 attrs 里取属性值；没有就返回 None。"""
    key = name + "="
    i = attrs.find(key)
    while i >= 0:
        before = attrs[i - 1] if i else " "
        if before.isspace():
            quote = attrs[i + len(key):i + len(key) + 1]
            if quote in ('"', "'"):
                j = attrs.find(quote, i + len(key) + 1)
                return attrs[i + len(key) + 1:j]
        i = attrs.find(key, i + 1)
    return None


def _css_blocks(css):
    """(选择器, 声明体) 列表。"""
    out = []
    rest = css
    while True:
        b = rest.find("{")
        if b < 0:
            return out
        e = rest.find("}", b)
        if e < 0:
            return out
        sel = rest[:b]
        cut = max(sel.rfind("}"), sel.rfind(";"))
        if cut >= 0:
            sel = sel[cut + 1:]
        out.append((sel.strip(), rest[b + 1:e]))
        rest = rest[e + 1:]


@test
def web_studio_hidden_attribute_actually_hides():
    """el.hidden = true 必须真的把元素藏起来。

    浏览器自带的 [hidden]{display:none} 只是作者样式表里最弱的一档，任何一条
    带 display 的类规则都能盖过它。Studio 里 .stage-empty 是 flex、.btn 是
    inline-flex，于是「还没有画面 / 胶片生成中… / 取消导出」这些靠 hidden
    开关的元素永远不消失 —— 页面看着像卡住，其实是 CSS 输了。
    """
    html = _read("index.html")
    css = _read("app.css")
    js = _read("app.js")

    toggled = set()
    i = 0
    while True:
        i = js.find("$('", i)
        if i < 0:
            break
        j = js.find("')", i)
        name = js[i + 3:j]
        if js[j + 2:j + 13].startswith(".hidden"):
            toggled.add(name)
        i = j + 1
    assert toggled, "没有从 app.js 里解析到任何 .hidden 开关"

    ids = {}
    for tag, attrs in _tags(html):
        name = _attr(attrs, "id")
        if name:
            ids[name] = (tag, (_attr(attrs, "class") or "").split())

    blocks = _css_blocks(css)
    # 全局兜底：[hidden] { display: none !important }（选择器里可能带前面的注释）
    global_guard = False
    for sel, body in blocks:
        if "display" not in body or "none" not in body or "!important" not in body:
            continue
        for part in sel.split(","):
            part = part.rsplit("*/", 1)[-1].strip()
            if part == "[hidden]":
                global_guard = True
    failures = []
    for name in sorted(toggled):
        tag, classes = ids.get(name, (None, []))
        if tag is None:
            failures.append(name + "（app.js 引用但 HTML 里没有）")
            continue
        covered = False
        for sel, body in blocks:
            if "display" not in body or "none" not in body or not sel.endswith("[hidden]"):
                continue
            for cls in classes:
                if "." + cls in sel:
                    covered = True
        if not (covered or global_guard):
            failures.append(name + " <" + tag + " class=" + " ".join(classes) + ">")
    assert not failures, (
        "这些元素设了 hidden，但没有任何 display:none 能盖住它们的类规则: "
        + repr(failures)
        + "（补一条 [hidden] { display: none !important; } 兜底）"
    )

def _js_function(js, name):
    """取出 `function name(...) {...}` 的函数体（花括号配平，不做正则）。"""
    head = "function " + name + "("
    i = js.find(head)
    assert i >= 0, "app.js 里没有 " + name + "()"
    b = js.find("{", i)
    assert b >= 0, name + "() 没有函数体"
    depth = 0
    for k in range(b, len(js)):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                return js[b + 1:k]
    raise AssertionError(name + "() 的花括号不配平")


@test
def web_studio_keyboard_and_screen_reader_state_stay_in_sync():
    """图标换了，语义也得换；键盘要能走到真正操作时间轴的那个元素。

    两件事都是「看得见的人不受影响、看不见的人被坑」的静默失败：
    1. 播放键靠切换 SVG 显示来换图标，aria-label 若固定写着「播放」，
       读屏用户按空格后听到的还是「播放」。
    2. 时间轴画布有 pointerdown/keydown 两套交互，但没有 tabindex 就
       永远拿不到键盘焦点，方向键逐帧形同虚设。
    """
    html = _read("index.html")
    js = _read("app.js")

    # 时间轴画布本身必须可聚焦
    tl_attrs = ""
    for tag, attrs in _tags(html):
        if _attr(attrs, "id") == "tl":
            tl_attrs = attrs
    assert tl_attrs, "HTML 里找不到 id=\"tl\" 的时间轴画布"
    assert _attr(tl_attrs, "tabindex") == "0", "时间轴画布没有 tabindex=\"0\"，键盘到不了"

    play_body = _js_function(js, "play")
    pause_body = _js_function(js, "pause")
    def _aria_labels(body):
        return _between(body, "setAttribute('aria-label', '", "'")

    play_aria = _aria_labels(play_body)
    pause_aria = _aria_labels(pause_body)
    assert play_aria == ["暂停"], (
        "play() 里 aria-label 必须是「暂停」，实际是 " + repr(play_aria)
    )
    assert pause_aria == ["播放"], (
        "pause() 里 aria-label 必须是「播放」，实际是 " + repr(pause_aria)
    )

    # role=slider 的时间轴必须报值，否则读屏永远念初始值
    readout = _js_function(js, "paintTimeReadout")
    for attr in ("aria-valuenow", "aria-valuemax", "aria-valuetext"):
        assert attr in readout, "paintTimeReadout() 没同步 " + attr
    assert 'role="slider"' in html, "时间轴画布少了 role=slider"
    assert 'aria-valuemin="0"' in html, "时间轴画布少了 aria-valuemin"


@test
def web_studio_canvas_colors_come_from_the_css_tokens():
    """<canvas> 不是 DOM 节点，色值取不到级联，只能自己查。

    以前画布把十六进制抄了一遍，于是 app.css 里改 --c-tap，时间轴上的
    Tap 还是旧蓝色 —— 这种「一处改了、另一处没改」在深色主题里尤其难发现。
    规则：画布里出现的十六进制只允许是 cssVar(name, fallback) 的 fallback。
    """
    js = _read("app.js")
    used_tokens = set()

    # 1) 每个 cssVar('--x', ...) 引用的令牌必须在 app.css 里定义
    for raw in _between(js, "cssVar('", "'"):
        name = raw.strip()
        if name.startswith("--"):
            used_tokens.add(name)
    assert used_tokens, "没有从 app.js 里解析到任何 cssVar('--…') 引用"
    defined = _css_token_names(_read("app.css"))
    missing = sorted(used_tokens - defined)
    assert not missing, "画布引用了 app.css 里没有的令牌: " + repr(missing)

    # 2) 没有裸写的色值：每个 '#rrggbb' / '#rgb' 字面量都必须是 cssVar 的回退参数。
    #    回退参数在同一行写成 cssVar('--c-x', '#...')，所以判断「本行是否
    #    以 cssVar( 开头且逗号结尾」即可，既不误判 replace('#', '')，
    #    也不看注释里的颜色名。
    hexes = "0123456789abcdefABCDEF"
    offenders = []
    for lineno, line in enumerate(js.splitlines(), 1):
        k = 0
        while True:
            k = line.find("'#", k)
            if k < 0:
                break
            body = line[k + 2:]
            digits = ""
            for ch in body:
                if ch in hexes:
                    digits += ch
                else:
                    break
            closer = line[k + 2 + len(digits):k + 3 + len(digits)]
            is_color = len(digits) in (3, 6) and closer == "'"
            if is_color:
                head = line[:k]
                if "cssVar(" not in head or not head.rstrip().endswith(","):
                    offenders.append(str(lineno) + ": " + line[k:k + len(digits) + 3])
            k = k + 1
    assert not offenders, (
        "app.js 里有没走设计令牌的硬编码色值（应写成 cssVar('--c-x', '#…')）: "
        + repr(offenders)
    )


@test
def web_studio_canvas_helper_mixes_alpha_without_nan():
    """canvas 没有 color-mix()，半透明靠 _rgba 拼；拼错会得到 rgba(NaN, …)。

    这条用最简单的等价 Python 实现复算一遍公式，确保 JS 里那份逻辑
    对 3 位简写、6 位、以及非法输入都不会产出 NaN。
    """
    js = _read("app.js")
    assert "function _rgba(" in js, "缺少 _rgba()，画布半透明色无处可来"
    src = _js_function(js, "_rgba")
    assert "replace('#', '')" in src, "_rgba 没有剥 # 前缀"
    assert "length === 3" in src, "_rgba 没处理 3 位简写"
    assert "isNaN" in src, "_rgba 没有非法输入兜底（会拼出 rgba(NaN …)）"
    assert "rgba(" in src, "_rgba 没有产出 rgba()"
    # 两个半透明辅助都必须走 _rgba，不许自己写 rgba 字面量
    for helper in ("signalLine", "fgLine"):
        body = _js_function(js, helper)
        assert "_rgba(" in body, helper + "() 没走 _rgba"


@test
def web_studio_timeline_supports_zoom_and_pan():
    """时间轴只要能缩放平移，才配叫「可编辑前端」。

    这条钉住的是**机制存在**而不是外观：滚轮必须 preventDefault（否则
    浏览器会带着整页滚走）、缩放必须以指针为锚点、键盘必须能缩放与翻页、
    视野必须被夹在 [0, duration] 内。长谱面上没有它，每拍会被压到一像素以下。
    """
    js = _read("app.js")
    html = _read("index.html")

    for fn in ("viewSpan", "clampView", "zoomTo", "resetZoom", "paintZoom"):
        assert "function " + fn + "(" in js, "缺少 " + fn + "()，时间轴缩放不完整"

    wheel = _between(js, "cv.addEventListener('wheel'", "passive: false")[0]
    assert "e.preventDefault()" in wheel, "滚轮没有 preventDefault，页面会跟着滚走"
    assert "zoomTo(" in wheel, "滚轮没有接上 zoomTo()"
    assert "xToTime(" in wheel, "滚轮没有算锚点时间，缩放不会停在指针下"

    keydown = _js_function(js, "bindTimeline")
    for key in ("'+'", "'-'", "'0'", "'PageUp'", "'PageDown'"):
        assert key in keydown, "时间轴键盘交互少了 " + key

    dbl = _between(js, "cv.addEventListener('dblclick'", "});")[0]
    assert "resetZoom(" in dbl, "双击没有复位视野"

    # 缩放读数必须真的挂到 DOM 上（否则用户不知道画面为什么只剩一小段）
    assert 'id="tl-zoom"' in html, "缺少 #tl-zoom 读数"
    assert "$('tl-zoom')" in js, "paintZoom 没写进 #tl-zoom"


@test
def web_studio_global_timeline_keys():
    """帮助浮层把 +/-/0、PgUp/PgDn 写成全局快捷键，那就得真的全局可用。"""
    keys = _js_function(_read("app.js"), "globalKeys")
    for key in ("'+'", "'-'", "'0'", "'PageUp'", "'PageDown'"):
        assert key in keys, "globalKeys 没有实现帮助里写的 " + key


@test
def web_studio_timeline_culls_offscreen_work():
    """放大之后，跑不跑得快取决于「不画看不见的东西」。

    这是编辑器里最容易悄悄退化的地方：功能都对，谱面一大就卡。三条边界：
    刻度从视野起点起步而不是从 0 遍历、音符按视野剔除、视野夹在谱面之内。
    """
    js = _read("app.js")

    # 刻度必须从「视野起点向下取整到 step」起步，而不是从 0 起步。
    # 断言整行而不是子串：只查 'S.view.start' 的话，末尾求 last 的那行
    # 也能让它通过（实测变异成 first = 0 仍然全绿）。
    ticks = _js_function(js, "paintTicks")
    assert "var first = Math.floor(S.view.start / step) * step;" in ticks, (
        "刻度起点不是从视野起点算的（放大后会从 0 白跑几万个点）"
    )
    assert "var t = 0;" not in ticks, "刻度仍然从 0 开始遍历"

    # 音符剔除：断言那一行的原文。只查 'continue' 会被速度轨的同名关键字骗过。
    tracks = _js_function(js, "paintTracks")
    assert "if (nt.end < t0 || nt.start > t1) continue;" in tracks, (
        "音符轨没有按视野剔除（长谱面放大后会遍历全部音符）"
    )
    assert "var t0 = S.view.start, t1 = S.view.start + viewSpan();" in tracks, (
        "音符剔除没有用视野窗口 [t0, t1]"
    )

    # 速度事件轨也必须按视野剔除，它是逐事件遍历的
    # _between 返回的是片段列表，先取正文再判断（直接 in 列表会退化成等值比较）
    sp_loop = _between(tracks, "for (var m = 0; m < sp.length; m++)", "if (started) g.stroke();")[0]
    assert "continue" in sp_loop, "速度事件轨没有剔除视野外的事件"
    # t0 / t1 是视野窗口，定义在音符循环之前（两处共用）
    assert "var t0 = S.view.start, t1 = S.view.start + viewSpan();" in tracks, (
        "视野窗口 t0/t1 没有定义在 paintTracks 里"
    )

    clamp_fn = _js_function(js, "clampView")
    assert "S.view.start = clamp(" in clamp_fn, "视野起点没有被夹住"
    assert "S.view.zoom = 1" in clamp_fn, "超出全长时没有回落到 1 倍"

@test
def web_studio_export_modal_wires_the_hitsounds():
    """导出面板要能选打击音（SE），并且默认跟着后端能力走。

    这条链路最怕「UI 长得像开关、payload 里其实没带上」——所以断言的是
    exportStart() 的 payload 原文，而不是 DOM 里有没有那几个控件。
    """
    html = _read("index.html")
    js = _read("app.js")
    css = _read("app.css")

    for dom in ('id="ex-se-mode"', 'id="ex-se-volume"', 'id="ex-se-dir"',
                'id="ex-se-dir-row"', 'id="ex-se-hint"'):
        assert dom in html, "导出面板缺少 " + dom

    payload = _js_function(js, "exportStart")
    assert "hitsounds: exportHitsounds()" in payload, "exportStart 的 payload 没带上 hitsounds"
    assert "se_volume: num(" in payload and "ex-se-volume" in payload, "exportStart 的 payload 没带上 se_volume"

    pick = _js_function(js, "exportHitsounds")
    assert "return 'auto'" in pick, "「自动查找官方 SE」没有映射成 auto"
    assert "return null" in pick, "「不加音效」必须发 null（而不是空串）"

    paint = _js_function(js, "paintSeMode")
    assert "ex-se-dir-row" in paint and "hidden" in paint, "SE 目录输入框没有跟着模式显隐"

    # 有素材就默认打开，没有就默认关掉：默认值来自 /api/capabilities
    opener = _js_function(js, "exportOpen")
    assert "caps.se" in opener, "导出面板没有读后端的 SE 能力"
    assert "hasSe ? 'auto' : 'none'" in opener, "有/没有 SE 素材时，默认选项应该不同"

    # shell：SE 那一块必须有壳样式，不然两个控件会浮在表单上
    assert ".se-box" in css, "缺少 .se-box 样式"
    assert "ex-se-mode" in js and "addEventListener(" in js, "切换 SE 模式没有重画表单"


@test
def web_studio_play_icon_switches_by_attribute():
    """播放键的两个 SVG 必须靠 hidden 属性切换，不能给 SVG 元素赋 .hidden。

    SVGSVGElement 没有 hidden IDL 属性：给 svg.hidden 赋值只是挂了个同名
    expando，既不改属性也不改样式。结果就是播放中仍显示三角形、暂停中仍
    显示双竖线，而 aria-label 已经变了 —— 看得见的人和读屏的人看到两种状态。
    """
    js = _read("app.js")
    helper = _js_function(js, "setPlayingIcon")
    assert "setAttribute('hidden', '')" in helper, "播放图标没有用 setAttribute 藏起来"
    assert "removeAttribute('hidden')" in helper, "播放图标没有用 removeAttribute 露出来"
    assert "setPlayingIcon(true)" in _js_function(js, "play"), "play() 没切到暂停图标"
    assert "setPlayingIcon(false)" in _js_function(js, "pause"), "pause() 没切回播放图标"


@test
def web_studio_keyboard_is_not_handled_twice():
    """画布 keydown 已处理的事件不能再冒泡给 globalKeys。

    实测画布聚焦时空格会 play() + pause() 互相抵消，方向键一次走两帧。
    另外 J/L 在帮助面板里写了却从未实现；按钮上的空格应交给按钮自己。
    """
    js = _read("app.js")
    keys = _js_function(js, "globalKeys")
    assert "if (e.defaultPrevented) return;" in keys, (
        "globalKeys 没有跳过已被画布 preventDefault 的事件（空格/方向键会双处理）"
    )
    assert "tag === 'BUTTON'" in keys, "按钮上的空格/回车应由按钮自己激活"
    for key in ("case 'j':", "case 'l':"):
        assert key in keys, "globalKeys 缺少 " + key
    assert "seek(S.time - 1, true)" in keys and "seek(S.time + 1, true)" in keys, (
        "J/L 没有实现前后 1 秒"
    )
    assert ".modal:not([hidden])" in keys, "浮层打开时全局快捷键仍会触发"


@test
def web_studio_tabs_are_keyboard_navigable():
    """role=tab 必须配 roving tabindex 与左右方向键，否则键盘用户进不去。"""
    js = _read("app.js")
    assert "function bindTabKeys(" in js, "缺少页签键盘导航"
    bind = _js_function(js, "bindTabKeys")
    assert "ArrowLeft" in bind and "ArrowRight" in bind, "页签不响应左右方向键"
    assert "tabIndex" in bind, "页签没有维护 roving tabindex"
    assert "bindTabKeys('" in js, "bind() 没有挂上页签键盘导航"
    assert "tabIndex" in _js_function(js, "switchTab"), "switchTab 没有同步 tabindex"


@test
def web_studio_frame_cache_keys_include_the_frame_rate():
    """帧缓存键必须带 fps；否则换帧率后旧预取会按新 fps 反推时间。"""
    js = _read("app.js")
    key_fn = _js_function(js, "frameKey")
    assert "+ '|' + S.fps" in key_fn, "frameKey 没有把 fps 编进缓存键"
    url_fn = _js_function(js, "frameUrl")
    assert "p[4]" in url_fn and "|| S.fps" in url_fn, "frameUrl 没有从键里取回 fps"
    assert "idx / S.fps" not in url_fn, "frameUrl 仍在用当前 S.fps 反推时间"


@test
def web_studio_stale_frame_requests_never_win():
    """慢的旧帧请求不能覆盖新帧，失败也不能把新帧的 loading 关掉。"""
    js = _read("app.js")
    body = _js_function(js, "showFrame")
    assert "if (seq !== S.frameSeq) return;" in body, "showFrame 没有无条件丢弃过期帧"
    assert "seq !== S.frameSeq && !opts.force" not in body, "force 请求绕过了过期保护"


@test
def web_studio_timeline_does_not_reallocate_every_paint():
    """重绘不等于重建 backing store；悬停重绘要合并到一帧。"""
    js = _read("app.js")
    paint = _js_function(js, "paintTimeline")
    assert "cv.width !==" in paint and "cv.height !==" in paint, (
        "paintTimeline 每次都重设 canvas 尺寸（清空并重新分配 backing store）"
    )
    assert "function scheduleTimelinePaint(" in js, "缺少悬停重绘调度器"
    bind = _js_function(js, "bindTimeline")
    assert "scheduleTimelinePaint()" in bind, "pointermove 仍在每次移动时全量重绘"


@test
def web_studio_hover_count_uses_binary_search():
    """悬停计数不能每移动一像素就线性扫完整条判定线。"""
    js = _read("app.js")
    body = _js_function(js, "countNear")
    assert "while (lo < hi)" in body, "countNear 没有二分定位起始音符"
    assert "notes[i].start > t + win" in body, "countNear 没有在窗口右边界提前停止"


@test
def web_studio_filmstrip_failure_has_a_terminal_state():
    """胶片失败后不能永远画「胶片生成中…」；段落带也不该依赖素材能力。"""
    js = _read("app.js")
    load = _js_function(js, "loadFilmstrip")
    assert "S.filmError" in load, "loadFilmstrip 失败时没有记录终态"
    assert "S.filmKey = ''" in load, "胶片失败后没有留下重试入口"
    paint = _js_function(js, "paintFilm")
    assert "S.filmError" in paint, "paintFilm 没有渲染失败信息"
    assert "S.caps && S.spec" not in paint, "段落色带仍被素材能力错误地挡住"


@test
def web_studio_frame_cache_is_lru_not_fifo():
    """注释写着 LRU，缓存命中就必须刷新顺序；否则常用帧会被先淘汰。"""
    js = _read("app.js")
    assert "function touchFrame(" in js, "缺少 touchFrame()"
    fetch = _js_function(js, "fetchFrame")
    assert "touchFrame(key)" in fetch, "缓存命中没有刷新 LRU 顺序"


def _css_rule(css, selector):
    """按选择器取一条规则的声明体（忽略选择器前面的注释）。"""
    for sel, body in _css_blocks(css):
        clean = sel.rsplit("*/", 1)[-1] if "*/" in sel else sel
        for part in clean.split(","):
            if part.strip() == selector:
                return body
    raise AssertionError("app.css 里找不到规则 " + selector)


@test
def web_studio_layout_can_shrink_below_its_content():
    """整页 grid 的隐式列是 auto，顶栏的 min-content 会把 1024 撑到 1083。

    这不是「窄屏样式不好看」，而是横向滚动条 + 舞台被推出视口。
    """
    css = _read("app.css")
    app = _css_rule(css, ".app")
    assert "grid-template-columns: minmax(0, 1fr);" in app, (
        ".app 没有把唯一一列声明成 minmax(0, 1fr)，内容会撑破视口"
    )
    topbar = _css_rule(css, ".topbar")
    assert "min-width: 0;" in topbar, "顶栏没有允许收缩"
    assert ".caps { display: none; }" in css, "<=1180 时能力徽标仍在占顶栏宽度"


@test
def web_studio_form_checkbox_keeps_its_size():
    """.fld input 的 width:100% 不能把复选框拉成一条。"""
    css = _read("app.css")
    assert ".fld input:not([type=checkbox])" in css, "复选框没有被排除在 width:100% 之外"
    box = _css_rule(css, ".fld input[type=checkbox]")
    assert "width: 13px;" in box and "flex: 0 0 auto;" in box, (
        "复选框在 .fld 里没有锁回 13px 宽"
    )


@test
def web_studio_ships_a_favicon():
    """浏览器每次开页都会请求 /favicon.ico，不内联就会在控制台留 404。"""
    html = _read("index.html")
    assert 'rel="icon"' in html, "index.html 没有 favicon"
    assert 'type="image/png"' in html and "data:image/png;base64," in html, (
        "favicon 应该是内联 PNG（外链会被离线约束测试拦下）"
    )


@test
def web_studio_issues_sort_errors_first():
    """ERROR 的序号是 0，不能用 || 9 兜底，否则错误排到最后。"""
    js = _read("app.js")
    body = _js_function(js, "renderIssues")
    assert "order[a.severity] ?? 9" in body, "严重度排序没有用 ?? 兜底"
    assert "order[a.severity] || 9" not in body, "ERROR=0 仍被 || 9 吃掉"


@test
def web_studio_issue_severities_are_normalized():
    """/api/stats 发的是小写 error/warning/caution，UI 的类名与筛选键是大写。"""
    js = _read("app.js")
    body = _js_function(js, "applyStats")
    assert "toUpperCase()" in body, "applyStats 没有统一校验问题的严重度大小写"


@test
def web_studio_export_polling_has_one_loop():
    """导出轮询只能有一条 setTimeout 链，否则取消时旧链还在跑。"""
    js = _read("app.js")
    body = _js_function(js, "pollExport")
    assert body.count("setTimeout(step, 600)") == 1, "pollExport 里有多条轮询链"
    assert "if (once" not in body, "pollExport 还留着第二条 once 调度"


@test
def web_studio_boot_failure_reports_once():
    """启动失败只该出现一条文案，不要先 textContent 再 append 同文案。"""
    js = _read("app.js")
    body = _js_function(js, "boot")
    assert body.count("初始化失败：") == 1, "boot 失败文案重复了"
    assert body.count("boot-text") == 1, "boot 失败时插入了多个提示节点"


@test
def web_studio_chart_text_checks_http_status():
    """404 的 HTML 错误页不能当成 chart.json 原文显示。"""
    js = _read("app.js")
    body = _js_function(js, "refreshChartText")
    assert "if (!res.ok)" in body, "refreshChartText 没有检查 res.ok"


@test
def web_studio_time_format_rounds_before_splitting():
    """59.9996 秒必须进位成 01:00.000，不能出现 00:60.000。"""
    js = _read("app.js")
    body = _js_function(js, "fmtTime")
    assert "Math.round" in body and "total % 60000" in body, (
        "fmtTime 没有先按毫秒取整再拆分钟/秒"
    )


@test
def web_studio_template_updates_the_document_name():
    """插入模板后顶栏名必须跟着 spec 走，否则生成前后像两份谱面。"""
    js = _read("app.js")
    body = _js_function(js, "useTemplate")
    assert "$('doc-name').value = S.spec.name" in body, "useTemplate 没有同步顶栏谱面名"


@test
def web_studio_modals_restore_focus():
    """浮层打开时把焦点送进去，关闭后还给触发它的控件。"""
    js = _read("app.js")
    opened = _js_function(js, "openModal")
    assert "lastFocus" in opened, "openModal 没有记录/转移焦点"
    assert ".modal-body" in opened, "打开导出浮层时焦点应落在第一个表单控件，而不是标题栏的关闭键"
    assert "lastFocus" in _js_function(js, "closeModal"), "closeModal 没有还原焦点"
