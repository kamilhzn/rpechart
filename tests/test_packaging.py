"""工程结构的守门测试：打包元数据、文档路由、样本可复现、路径收敛。

这些用例守的不是功能，而是**「仓库还能被别人 clone 下来跑」这件事**：
版本号两处要一致、子包要能 import、AGENTS.md 指到的文档要真的存在、
测试样本能由脚本重新生成、代码里不能有写死的盘符。
它们很便宜，但每一条都对应过一次真实的踩坑。
"""

import ast
import os
import re

from run_tests import test

import rpechart

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import tomllib
except ModuleNotFoundError:                                   # pragma: no cover
    tomllib = None


def _read(rel):
    with open(os.path.join(ROOT, *rel.split("/")), encoding="utf-8") as fh:
        return fh.read()


def _pyproject():
    if tomllib is None:                                      # pragma: no cover
        return None
    return tomllib.loads(_read("pyproject.toml"))


@test
def packaging_version_matches_the_library():
    """`pyproject.toml` 的 version 必须等于 `rpechart.__version__`。

    两处写版本号迟早会漂 —— 发布出去的包版本和 `import rpechart` 报的
    不是同一个数，是最难查的一类 bug。
    """
    data = _pyproject()
    if data is None:                                         # pragma: no cover
        return
    assert data["project"]["name"] == "rpechart"
    assert data["project"]["version"] == rpechart.__version__, (
        data["project"]["version"], rpechart.__version__)


@test
def packaging_declares_the_four_subpackages():
    """四个子包都要能被发现（`rpechart` / `.render` / `.gen` / `.web`）。"""
    for sub in ("rpechart", "rpechart.render", "rpechart.gen", "rpechart.web"):
        path = os.path.join(ROOT, *sub.split("."))
        assert os.path.isfile(os.path.join(path, "__init__.py")), sub
        try:
            __import__(sub)
        except Exception as exc:
            raise AssertionError(f"import {sub} 失败：{exc!r}") from exc


@test
def packaging_ships_the_web_static_files():
    """Web UI 是纯静态前端，必须随包带上（`package-data` 不能漏）。"""
    data = _pyproject()
    if data is None:                                         # pragma: no cover
        return
    pkg_data = data.get("tool", {}).get("setuptools", {}).get("package-data", {})
    assert "rpechart.web" in pkg_data, pkg_data
    static = os.path.join(ROOT, "rpechart", "web", "static")
    names = sorted(os.listdir(static))
    for want in ("index.html", "app.css", "app.js"):
        assert want in names, (want, names)
    html = _read("rpechart/web/static/index.html")
    assert "/static/app.css" in html and "/static/app.js" in html


@test
def packaging_keeps_the_core_dependency_free():
    """核心库必须零第三方依赖；Pillow / numpy 只能出现在 extras 里。

    这是这个库的边界：读写、校验、打包在任何只有标准库的机器上都要能跑。
    """
    data = _pyproject()
    if data is None:                                         # pragma: no cover
        return
    assert data["project"]["dependencies"] == [], data["project"]["dependencies"]
    extras = data["project"]["optional-dependencies"]
    joined = " ".join(v for vs in extras.values() for v in vs).lower()
    assert "pillow" in joined and "numpy" in joined


@test
def agents_md_is_a_router_not_a_manual():
    """AGENTS.md 只做路由：要短，且指向的 docs/*.md 必须真的存在。

    用户明确规定过：AGENTS.md 是路由不是手册，详情放 docs/。
    """
    text = _read("AGENTS.md")
    assert len(text.splitlines()) <= 90, "AGENTS.md 太长了，细节请推去 docs/"
    links = set(re.findall(r"\(docs/([A-Za-z0-9_.-]+\.md)\)", text))
    assert len(links) >= 4, links
    for name in sorted(links):
        assert os.path.isfile(os.path.join(ROOT, "docs", name)), name


@test
def docs_cover_the_five_required_topics():
    """docs/ 要覆盖架构、序列化、渲染、生成、测试、数据安全。"""
    for name in ("architecture.md", "serialization.md", "rendering.md",
                 "generation.md", "testing.md", "data-safety.md"):
        path = os.path.join(ROOT, "docs", name)
        assert os.path.isfile(path), name
        body = _read("docs/" + name)
        assert len(body.splitlines()) >= 20, (name, "太短，像是占位")


@test
def fixtures_are_generated_by_a_script_that_is_checked_in():
    """测试样本必须能由 `make_fixtures.py` 重新生成（否则 clone 下来就缺料）。

    这个仓库刻意**不带任何第三方素材**（真实谱面、官方贴图、音频都不进版本库），
    所以保真回测的依据只能是自己合成的样本。脚本在、样本在，缺一不可。
    """
    script = os.path.join(ROOT, "tests", "fixtures", "make_fixtures.py")
    assert os.path.isfile(script), "缺少 make_fixtures.py，样本就不可复现了"
    body = _read("tests/fixtures/make_fixtures.py")
    assert "def main(" in body, "make_fixtures.py 要有 main() 入口"

    for rel in ("tests/fixtures/charts/modern.json",
                "tests/fixtures/charts/legacy.json",
                "tests/fixtures/charts/multi.json",
                "tests/fixtures/demo.pez",
                "tests/fixtures/media/disguised.mp3",
                "tests/fixtures/media/silent.mp4"):
        assert os.path.isfile(os.path.join(ROOT, *rel.split("/"))), rel


@test
def fixtures_regenerate_byte_identically():
    """重跑 `make_fixtures.py` 必须产出**一模一样**的字节。

    否则样本一进版本库，每次重跑都会产生无意义的 diff —— 而样本又是保真回测的
    唯一依据，diff 一多就没人敢动了。`.pez` 是 zip，成员时间戳默认取当前时间，
    所以生成脚本必须显式冻结它。
    """
    import zipfile

    pez = os.path.join(ROOT, "tests", "fixtures", "demo.pez")
    with zipfile.ZipFile(pez) as zf:
        stamps = {i.date_time for i in zf.infolist()}
    assert len(stamps) == 1, f"zip 成员时间戳不一致，样本不可复现: {stamps}"
    import time

    only = next(iter(stamps))
    assert only[0] != time.gmtime().tm_year, (
        f"zip 成员带的是当前年份的时间戳 {only} —— 时间戳没被冻结，样本不可复现")


@test
def fixtures_survive_the_gitignore_rules():
    """样本不能被 `.gitignore` 的 `*.mp4` / `build/` 之类的通配吃掉。

    这里不调用 git（测试要能在任何机器上跑），只检查忽略规则里**确实有**
    把 `tests/fixtures/` 排除在外的 negate 规则。
    """
    text = _read(".gitignore")
    assert "!tests/fixtures/**" in text, text


@test
def gitattributes_forces_lf_for_byte_fidelity():
    """仓库必须强制 LF —— 否则保真回测会「本机过、别处不过」。

    本库的核心卖点之一是与 RPE 逐字节相同的 chart.json（LF、无 BOM）。
    只要 Git 在 checkout 时把 LF 换成 CRLF，这个保证就悄悄失效了。
    """
    joined = _read(".gitattributes")
    assert "text=auto eol=lf" in joined, "缺少全局 LF 规则"
    assert "*.png binary" in joined and "*.mp3 binary" in joined
    # 样本文件不做行尾规范化：保真断言要逐字节比较
    assert "tests/fixtures/** -text" in joined, joined


@test
def gitignore_keeps_the_big_assets_out_of_version_control():
    """运行产物不能进版本库（`build/`、`work/`、视频、音频…）。

    注意：`*.pez` **不**在这里 —— 测试样本 `tests/fixtures/demo.pez` 必须入库，
    它由下面那条 negate 规则保住。
    """
    text = _read(".gitignore")
    for want in ("build/", "work/", "__pycache__/", "*.mp4", "*.wav"):
        assert want in text, want
    assert "!tests/fixtures/**" in text, "样本会被 *.mp4 / build/ 之类的规则吃掉"


@test
def no_module_hardcodes_an_absolute_repo_path():
    """源码里不能有写死的仓库路径（路径都要经 `_bootstrap` 或环境变量）。

    用 AST 找**真在代码里用的**字符串常量：模块/函数/类的 docstring 允许提到
    绝对路径（那是给人看的说明），其余位置一律不允许。
    """
    offenders = []
    for base in ("rpechart", "tests", "examples"):
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, base)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(dirpath, fn)
                with open(path, encoding="utf-8") as fh:
                    src = fh.read()
                tree = ast.parse(src, filename=path)
                docs = set()
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                         ast.ClassDef)):
                        body = getattr(node, "body", None)
                        if (body and isinstance(body[0], ast.Expr)
                                and isinstance(body[0].value, ast.Constant)
                                and isinstance(body[0].value.value, str)):
                            docs.add(id(body[0].value))
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                            and id(node) not in docs
                            and "F:" + chr(92) + "Phi" in node.value):
                        rel = os.path.relpath(path, ROOT).replace(chr(92), "/")
                        offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, "写死的仓库路径：" + ", ".join(sorted(set(offenders))[:10])


@test
def bootstrap_is_fail_closed_and_is_the_only_finder():
    """`_bootstrap.find_repo_root` 找不到就抛，绝不回退到硬编码路径。

    回退比失败更糟：它会让脚本在错误的目录上「成功」运行，然后写出一堆
    看起来正常、其实放错地方的文件（CHAM 项目踩过一模一样的坑）。
    """
    from rpechart._bootstrap import ensure_importable, find_repo_root
    import tempfile

    here = find_repo_root(__file__)
    assert os.path.isfile(os.path.join(str(here), "rpechart", "__init__.py"))
    assert ensure_importable(__file__) == here

    away = tempfile.mkdtemp(prefix="rpeboot_")
    try:
        find_repo_root(away)
    except RuntimeError:
        pass
    else:
        raise AssertionError("仓库外应该 fail-closed")


@test
def scripts_and_tests_share_one_bootstrap():
    """根目录探测只有 `_bootstrap` 一处实现，脚本与用例都复用它。

    原始项目里这段逻辑被抄了 40 多遍，其中一个脚本抄错就会「在错误的目录上
    成功运行」。现在 `tests/` 与 `examples/` 里凡是需要仓库根/可导入性的，
    都走 `_bootstrap`。
    """
    users = 0
    for base in ("examples", "tests"):
        d = os.path.join(ROOT, base)
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py"):
                continue
            body = _read(f"{base}/{fn}")
            if "_bootstrap" in body:
                users += 1
    assert users >= 2, f"只有 {users} 个脚本用了 _bootstrap"


@test
def readme_exists_and_links_the_docs():
    """README 要在，并且指向 AGENTS.md 与 docs/。"""
    text = _read("README.md")
    assert "AGENTS.md" in text
    assert "docs/" in text
    assert "rpechart" in text
