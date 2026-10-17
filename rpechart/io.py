"""读写 RPE 谱面：JSON 文件、谱面文件夹、``.pez`` 包、以及 RPE 的 ``Chartlist``。

RPE 的磁盘布局（见说明书 031 与 pgrfm wiki）：

* ``RPE根目录/Resources/<谱面ID>/chart.json`` —— 谱面本体
* ``RPE根目录/Resources/<谱面ID>/info.txt`` —— 该谱面用到的文件名
* ``RPE根目录/Resources/<谱面ID>/song.mp3``、``background.png``、……
* ``RPE根目录/Chartlist.txt`` —— 1.5.0 **之前**用它列出有哪些谱面；1.5.0 起改为
  自动扫描 ``Resources`` 下的纯数字文件夹。

本模块的 :func:`install_chart` 会一次性把这些都写对，并处理 Chartlist 兼容。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import typing as _t
import zipfile
from pathlib import Path

from .model import Chart
from .rpedump import dumps as rpe_dumps
from .timing import Timing

__all__ = [
    "load_json",
    "save_json",
    "load_chart",
    "save_chart",
    "load_chart_file",
    "save_chart_file",
    "Timing",
    "ChartFolder",
    "install_chart",
    "read_chartlist",
    "write_chartlist",
    "export_pez",
    "import_pez",
    "find_rpe_root",
    "ChartIOError",
]


class ChartIOError(RuntimeError):
    """谱面读写相关的错误。"""


# ------------------------------------------------------------------ JSON 基础
def load_json(path: _t.Union[str, os.PathLike]) -> dict:
    p = Path(path)
    if not p.exists():
        raise ChartIOError(f"文件不存在: {p}")
    try:
        text = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = p.read_text(encoding="utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChartIOError(f"{p} 不是合法 JSON: {exc}") from exc


def save_json(
    path: _t.Union[str, os.PathLike],
    data: _t.Any,
    *,
    indent: int = 2,
    ensure_ascii: bool = False,
) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=ensure_ascii, indent=indent),
        encoding="utf-8",
    )
    tmp.replace(p)  # 原子替换：避免 RPE 恰好读到半个文件
    return p


# ------------------------------------------------------------------ Chart 读写
def load_chart(
    path: _t.Union[str, os.PathLike, dict],
) -> Chart:
    """从文件、JSON 字符串或 dict 载入 :class:`~rpechart.model.Chart`。"""
    if isinstance(path, Chart):
        return path
    if isinstance(path, dict):
        return Chart.from_json(path)
    p = Path(path)
    if p.suffix.lower() == ".pez":
        with zipfile.ZipFile(p) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".json")]
            if not names:
                raise ChartIOError(f"{p} 里找不到 json 谱面文件")
            names.sort(key=lambda n: (n.count("/"), len(n)))
            return Chart.from_json(json.loads(zf.read(names[0]).decode("utf-8-sig")))
    return Chart.from_json(load_json(p))


def save_chart(
    chart: Chart,
    path: _t.Union[str, os.PathLike],
    *,
    indent: int = 2,
) -> Path:
    """写出一张谱面为 JSON 文件（RPE 自己的文本风格，LF 换行）。

    写出前会对**副本**调用 :meth:`~rpechart.model.Chart.sort`，保证落盘的音符与
    事件按 RPE 自己的标准顺序排列。此前只有 :meth:`ChartBuilder.save` 会排序，
    走 ``load_chart`` → 改 → ``save_chart`` 的路径会把乱序（例如
    ``JudgeLineBuilder.line_speed`` 直接 append 造成的）原样写进文件。

    排序只发生在副本上：旧实现对调用方**就地**排序，写盘之后调用方手里的
    Chart 已经被改了顺序（调用方要是拿它继续编辑，看到的是自己没做过的重排）。

    ``indent`` 参数保留只为兼容旧调用：格式由 :mod:`rpechart.rpedump` 决定，
    缩进恒为 RPE 的 3 个空格。
    """
    if not isinstance(chart, Chart):
        raise TypeError("save_chart 需要 Chart 对象")
    chart = chart.copy()
    chart.sort()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    # 用 RPE 自己的写出风格（3 空格缩进、"key" : value、键按字节序、纯标量数组
    # 内联），并把换行固定成 LF —— RPE 的 chart.json 是 LF，Windows 上默认的
    # newline=None 会把它翻成 CRLF。
    tmp.write_text(rpe_dumps(chart.to_json()) + "\n", encoding="utf-8", newline="")
    tmp.replace(p)  # 原子替换：避免 RPE 恰好读到半个文件
    return p


load_chart_file = load_chart
save_chart_file = save_chart


# ------------------------------------------------------------------ 谱面文件夹
class ChartFolder:
    """一个谱面文件夹（``Resources/<id>/``）的封装。"""

    def __init__(self, path: _t.Union[str, os.PathLike]) -> None:
        self.path = Path(path)

    # 让 ChartFolder 能直接喂给 os.path.join / open / Path 等
    def __fspath__(self) -> str:
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)

    def __repr__(self) -> str:
        return f"ChartFolder({str(self.path)!r})"

    # ---------------------------------------------------------- 基本路径
    @property
    def id(self) -> str:
        return self.path.name

    @property
    def chart_json(self) -> Path:
        return self.path / "chart.json"

    @property
    def info_txt(self) -> Path:
        return self.path / "info.txt"

    @property
    def exists(self) -> bool:
        return self.path.is_dir()

    def file(self, name: str) -> Path:
        return self.path / name

    # ---------------------------------------------------------- info.txt
    def read_info(self) -> dict[str, str]:
        """读 info.txt。

        实测格式：**第一行是一个 ``#``**，其后每行一条 ``键: 值``，值不加引号。
        键名大小写不敏感；全角冒号也认（中文输入法常见）。

        曲绘在 RPE 写出的文件里叫 ``Picture``，本函数额外把它复制成 ``background``
        别名，这样调用方不必关心是哪个版本写的。
        """
        info: dict[str, str] = {}
        if not self.info_txt.exists():
            return info
        text = self.info_txt.read_text(encoding="utf-8-sig", errors="replace")
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                key, _, value = line.partition(":")
            elif "：" in line:
                key, _, value = line.partition("：")
            else:
                continue
            info[key.strip().lower()] = value.strip()
        if "picture" in info and "background" not in info:
            info["background"] = info["picture"]
        return info

    def write_info(
        self,
        chart_file: str = "chart.json",
        song: _t.Optional[str] = None,
        background: _t.Optional[str] = None,
        extra: _t.Optional[dict[str, str]] = None,
        *,
        name: _t.Optional[str] = None,
        path: _t.Optional[str] = None,
        level: _t.Optional[str] = None,
        composer: _t.Optional[str] = None,
        charter: _t.Optional[str] = None,
        illustrator: _t.Optional[str] = None,
        group: _t.Optional[str] = None,
    ) -> Path:
        """按 RPE 自己的键序写 info.txt。

        键序与键名照抄 23 份真实 ``.pez`` 的实测结果：``#``、``Name``、``Path``、
        ``Song``、``Picture``、``Chart``、``Level``、``Composer``、``Charter``，然后才是
        ``extra``。**曲绘的键是 ``Picture`` 而不是 ``Background``** —— 写错键名 RPE 不报错，
        只是曲绘静默不显示，所以这里必须写对。

        ``Name``/``Level``/``Composer``/``Charter`` 是 RPE 选曲列表里直接显示的内容，
        给了就写。``extra`` 用来放 ``LastEditTime``/``Length`` 之类的编辑器记账字段
        —— 本库不伪造这些值，需要时由调用方显式传入。

        ``Group`` 是选曲界面里的谱面分组：实测 RPE 自己保存谱面时总会写这一行，
        且它出现在 ``info.txt`` 的**最后**（``IN-v1.1.pez`` 的实测键序里它在
        ``EditTime`` 之后）。RPE 的 ``group_display.txt`` 就是按这个键统计组内
        谱面数的，缺了它该谱面在分组视图里可能归不进任何组。
        """
        lines: list[str] = ["#"]
        if name:
            lines.append(f"Name: {name}")
        if path:
            lines.append(f"Path: {path}")
        if song:
            lines.append(f"Song: {song}")
        if background:
            lines.append(f"Picture: {background}")
        lines.append(f"Chart: {chart_file}")
        if level:
            lines.append(f"Level: {level}")
        if composer:
            lines.append(f"Composer: {composer}")
        if charter:
            lines.append(f"Charter: {charter}")
        if illustrator:
            lines.append(f"Illustrator: {illustrator}")
        for key, value in (extra or {}).items():
            lines.append(f"{key}: {value}")
        if group:
            lines.append(f"Group: {group}")
        self.path.mkdir(parents=True, exist_ok=True)
        # RPE 写 info.txt 用 CRLF（实测两份真实 .pez 与磁盘上的 info.txt 都是
        # CRLF、末尾带换行、无 BOM）；chart.json 反而是 LF。两者不能混。
        self.info_txt.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        return self.info_txt

    # ---------------------------------------------------------- 谱面本体
    def _sync_current_song(self, song: _t.Optional[str]) -> None:
        """把 info.txt 里的 ``Song:`` 同步成真实的音频文件名。

        RPE 自己（以及本库的 :func:`install_chart`）都会在改名/重装后更新这一行，
        否则选曲界面按 info.txt 去找 ``song.mp3`` 而文件其实叫 ``xxx.ogg``。
        """
        if not song or not self.info_txt.exists():
            return
        try:
            text = self.info_txt.read_text(encoding="utf-8-sig")
        except OSError:
            return
        out: list[str] = []
        replaced = False
        for raw in text.splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or ":" not in stripped:
                out.append(raw)
                continue
            key, _, value = stripped.partition(":")
            if key.strip().lower() == "song":
                out.append(f"Song: {song}")
                replaced = True
            else:
                out.append(raw)
        if not replaced:
            out.append(f"Song: {song}")
        self.info_txt.write_text("\r\n".join(out) + "\r\n", encoding="utf-8")

    def load(self) -> Chart:
        for candidate in (self.chart_json,):
            if candidate.exists():
                return load_chart(candidate)
        # 兼容：info.txt 里写了别的文件名
        info = self.read_info()
        name = info.get("chart") or info.get("chartfile") or info.get("谱面")
        if name and (self.path / name).exists():
            return load_chart(self.path / name)
        raise ChartIOError(f"{self.path} 下找不到谱面 JSON")

    def save(self, chart: Chart, chart_name: str = "chart.json") -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        return save_chart(chart, self.path / chart_name)


# ------------------------------------------------------------------ Chartlist
_NUMBER_RE = re.compile(r"^\d+$")


def read_chartlist(rpe_root: _t.Union[str, os.PathLike]) -> list[str]:
    """读 ``Chartlist.txt``，返回谱面 ID 列表。"""
    path = Path(rpe_root) / "Chartlist.txt"
    if not path.exists():
        return []
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def write_chartlist(rpe_root: _t.Union[str, os.PathLike], ids: _t.Sequence[str]) -> Path:
    path = Path(rpe_root) / "Chartlist.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(ids) + "\n" if ids else "", encoding="utf-8")
    return path


def scan_chart_ids(rpe_root: _t.Union[str, os.PathLike]) -> list[str]:
    """扫描 ``Resources`` 下的纯数字文件夹（1.5.0+ 的行为）。"""
    resources = Path(rpe_root) / "Resources"
    if not resources.is_dir():
        return []
    ids = [d.name for d in resources.iterdir() if d.is_dir() and _NUMBER_RE.match(d.name)]
    return sorted(ids, key=lambda x: int(x))


def find_rpe_root(start: _t.Union[str, os.PathLike]) -> _t.Optional[Path]:
    """从 ``start`` 往上找 RPE 根目录（含 ``Resources`` 与 ``PhiEdit.exe`` 的目录）。"""
    p = Path(start).resolve()
    if p.is_file():
        p = p.parent
    for candidate in [p, *p.parents]:
        if (candidate / "Resources").is_dir() and (
            (candidate / "PhiEdit.exe").exists() or (candidate / "Chartlist.txt").exists()
        ):
            return candidate
    for candidate in [p, *p.parents]:
        if (candidate / "Resources").is_dir():
            return candidate
    return None


def install_chart(
    rpe_root: _t.Union[str, os.PathLike],
    chart_id: _t.Union[str, int],
    chart: Chart,
    *,
    song_src: _t.Optional[_t.Union[str, os.PathLike]] = None,
    background_src: _t.Optional[_t.Union[str, os.PathLike]] = None,
    song_name: _t.Optional[str] = None,
    background_name: _t.Optional[str] = None,
    chart_name: str = "chart.json",
    update_chartlist: bool = True,
    overwrite: bool = True,
) -> ChartFolder:
    """把一张谱面（含音频与曲绘）安装进 RPE 的 ``Resources/<id>/``。

    会自动：

    1. 建好文件夹、复制音频/曲绘并**回填 META 里的文件名**；
    2. 写 ``info.txt``；
    3. 若存在 ``Chartlist.txt``（RPE < 1.5.0）则把该 id 追加进去。
    """
    root = Path(rpe_root)
    cid = str(chart_id)
    folder_path = root / "Resources" / cid
    folder = ChartFolder(folder_path)
    if folder_path.exists() and not overwrite:
        raise ChartIOError(f"{folder_path} 已存在（overwrite=False）")
    folder_path.mkdir(parents=True, exist_ok=True)

    # ---- 音频/曲绘
    if song_src is not None:
        src = Path(song_src)
        target_name = song_name or "song" + src.suffix.lower()
        shutil.copy2(src, folder_path / target_name)
        chart.META.song = target_name
    if background_src is not None:
        src = Path(background_src)
        target_name = background_name or "background" + src.suffix.lower()
        shutil.copy2(src, folder_path / target_name)
        chart.META.background = target_name
    if chart.META.id in ("", "0", None):
        chart.META.id = cid

    # 若音频/曲绘已在文件夹里但 META 没指向它们，自动挑一个
    if not chart.META.song:
        for ext in (".mp3", ".ogg", ".wav", ".m4a", ".flac"):
            found = sorted(folder_path.glob(f"*{ext}"))
            if found:
                chart.META.song = found[0].name
                break
    if not chart.META.background:
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            found = sorted(folder_path.glob(f"*{ext}"))
            if found:
                chart.META.background = found[0].name
                break

    # RPE 1.5.0+ 会在 META 里记一个 ``duration``（秒），编辑器自己保存时也会写。
    # 我们按实际的歌曲文件填一次，让谱面和 RPE 自己产出的文件更一致。
    if chart.META.duration is None and chart.META.song:
        song_path = folder_path / chart.META.song
        if song_path.is_file():
            try:
                from .audio import duration_of

                chart.META.duration = float(duration_of(song_path))
            except Exception:
                pass

    # ---- 写出
    folder.save(chart, chart_name)
    meta = chart.META
    folder.write_info(
        chart_file=chart_name,
        song=meta.song or None,
        background=meta.background or None,
        name=getattr(meta, "name", None) or None,
        path=cid,
        level=getattr(meta, "level", None) or None,
        composer=getattr(meta, "composer", None) or None,
        charter=getattr(meta, "charter", None) or None,
        illustrator=getattr(meta, "illustrator", None) or None,
        group=(chart.judgeLineGroup[0] if chart.judgeLineGroup else "Default"),
    )

    if update_chartlist:
        list_path = root / "Chartlist.txt"
        if list_path.exists():
            ids = read_chartlist(root)
            if cid not in ids:
                ids.append(cid)
                write_chartlist(root, ids)
    return folder


# ------------------------------------------------------------------ .pez
def export_pez(
    chart: Chart,
    out_path: _t.Union[str, os.PathLike],
    *,
    folder: _t.Optional[_t.Union[str, os.PathLike]] = None,
    chart_name: str = "chart.json",
    extra_files: _t.Optional[_t.Sequence[_t.Union[str, os.PathLike]]] = None,
) -> Path:
    """导出 ``.pez``（其实是 zip，RPE 可直接导入）。

    实测 23 份真实 ``.pez`` **每一份都含 ``info.txt``**，且 RPE 的「导入谱面」就是按它
    的 ``Chart:`` 键决定加载哪个文件。所以本函数一定会写出 ``info.txt``：``folder`` 里有
    现成的就带上（保留 ``LastEditTime``/``EditTime`` 这类编辑器记账字段），否则按
    ``chart.META`` 合成一份。

    两个容易踩的坑，本函数已处理：

    1. **必须把 ``Chart:`` 指向本次真正写出的 ``chart_name``。** 真实谱面文件夹里的谱面
       文件常叫 ``<id>.json`` 而不是 ``chart.json``；若原样照抄那份 info.txt，RPE 会去加载
       文件夹里那份**旧的** <id>.json，这次的修改就被静默丢弃了。
    2. **不能把旧谱面也打进包。** 否则包里同时存在新旧两份谱面，靠 ``Chart:`` 键决定用
       哪份，同样是静默错误的来源。

    ``Chartlist.txt`` 不是 ``.pez`` 的成员，不会被打包。
    """
    out = Path(out_path)
    if out.suffix.lower() != ".pez":
        out = out.with_suffix(".pez")
    out.parent.mkdir(parents=True, exist_ok=True)

    # 与 save_chart 一致：在副本上排序，保证包里的谱面是 RPE 的标准顺序，
    # 同时不改动调用方手里的 Chart。
    chart = chart.copy()
    chart.sort()

    meta = chart.META
    song_name = getattr(meta, "song", None) or None
    picture_name = getattr(meta, "background", None) or None

    # 文件夹里原本的谱面文件名（以及它自己的 info.txt）
    stale_chart_name: _t.Optional[str] = None
    folder_info_text: _t.Optional[str] = None
    if folder is not None:
        base = Path(folder)
        if base.is_dir():
            folder_info = ChartFolder(base)
            info = folder_info.read_info()
            stale_chart_name = info.get("chart") or None
            candidate = base / "info.txt"
            if candidate.is_file():
                folder_info_text = candidate.read_text(encoding="utf-8-sig", errors="replace")

    skip = {chart_name, "info.txt", "Chartlist.txt"}
    if stale_chart_name and stale_chart_name != chart_name:
        skip.add(stale_chart_name)

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        # chart.json 用 RPE 自己的写出风格（3 空格缩进、"key" : value、键按字节序、
        # 纯标量数组内联、LF 换行）。RPE 打开、改、再保存出来的就是这种文本，
        # 用 json.dumps(indent=2) 写出的是另一种风格，diff 起来全是噪声。
        zf.writestr(chart_name, rpe_dumps(chart.to_json()) + "\n")
        if folder_info_text is not None:
            zf.writestr(
                "info.txt",
                _retarget_info_chart(folder_info_text, chart_name, song_name),
            )
        else:
            zf.writestr(
                "info.txt",
                _synth_info_txt(
                    chart_name=chart_name,
                    song=song_name,
                    picture=picture_name,
                    meta=meta,
                ),
            )
        files: list[Path] = []
        if folder is not None:
            base = Path(folder)
            files.extend(sorted(p for p in base.rglob("*") if p.is_file()))
        for extra in extra_files or []:
            p = Path(extra)
            if p.is_file():
                files.append(p)
        for p in files:
            if p.name in skip:
                continue
            if folder is not None and stale_chart_name and p.name == stale_chart_name:
                continue
            rel = p.name if folder is None else str(p.relative_to(Path(folder)))
            zf.write(p, rel)
    return out


def _retarget_info_chart(
    text: str,
    chart_name: str,
    song: _t.Optional[str] = None,
) -> str:
    """改写 info.txt 的 ``Chart:``（必要时还有 ``Song:``），其余原样保留。

    真实谱面文件夹的谱面文件常叫 ``<id>.json``。导出时必须把这个键改成本次真正写出的
    文件名，否则 RPE 会加载包里那份旧谱面，本次修改被静默丢弃。``Song:`` 同理：
    若谱面换过音频（例如把 MP4 容器改名转码成 ``song.mp3``），旧 info.txt 会继续指向
    那个读不出时长的文件。

    换行统一成 CRLF —— RPE 自己写 info.txt 就是 CRLF（实测两份真实 ``.pez``）。
    """
    lines = text.splitlines()
    out: list[str] = []
    replaced_chart = False
    replaced_song = False
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            out.append(raw)
            continue
        if ":" in raw:
            key, _, _value = raw.partition(":")
        elif "：" in raw:
            key, _, _value = raw.partition("：")
        else:
            out.append(raw)
            continue
        name = key.strip().lower()
        if name == "chart":
            out.append(f"Chart: {chart_name}")
            replaced_chart = True
        elif name == "song" and song:
            out.append(f"Song: {song}")
            replaced_song = True
        else:
            out.append(raw)
    if not replaced_chart:
        out.append(f"Chart: {chart_name}")
    if song and not replaced_song:
        out.append(f"Song: {song}")
    return "\r\n".join(out) + "\r\n"


def _synth_info_txt(
    *,
    chart_name: str,
    song: _t.Optional[str],
    picture: _t.Optional[str],
    meta: _t.Any,
) -> str:
    """按 RPE 的键序合成一份 info.txt（缺省字段就不写这一行）。"""
    lines = ["#"]
    for key, value in (
        ("Name", getattr(meta, "name", None)),
        ("Path", getattr(meta, "id", None)),
        ("Song", song),
        ("Picture", picture),
        ("Chart", chart_name),
        ("Level", getattr(meta, "level", None)),
        ("Composer", getattr(meta, "composer", None)),
        ("Charter", getattr(meta, "charter", None)),
        ("Illustrator", getattr(meta, "illustrator", None)),
    ):
        if value:
            lines.append(f"{key}: {value}")
    # 与 RPE 一致：info.txt 用 CRLF（chart.json 才是 LF）。
    return "\r\n".join(lines) + "\r\n"


def import_pez(
    pez_path: _t.Union[str, os.PathLike],
    dest: _t.Union[str, os.PathLike],
    *,
    overwrite: bool = True,
) -> Path:
    """把 ``.pez`` 解到目标文件夹（RPE 也是这么导的）。"""
    src = Path(pez_path)
    target = Path(dest)
    if target.exists() and not overwrite:
        raise ChartIOError(f"{target} 已存在（overwrite=False）")
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src) as zf:
        for member in zf.namelist():
            # 防止 zip slip
            name = member.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                continue
            zf.extract(member, target)
    return target
