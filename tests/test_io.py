"""io.py —— 谱面文件、谱面文件夹、.pez 包、Chartlist 的读写。

这一层直接动磁盘，所以测试全部在临时目录里做，不碰用户的 RPE 安装。
重点验证：原子写、BOM 容忍、info.txt 解析、安装谱面的副作用是否完整。
"""

import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from run_tests import approx, raises, test

from rpechart.io import (
    ChartFolder,
    ChartIOError,
    export_pez,
    find_rpe_root,
    import_pez,
    install_chart,
    load_chart,
    load_json,
    read_chartlist,
    save_chart,
    save_json,
    scan_chart_ids,
    write_chartlist,
)
from rpechart.model import BPMItem, Chart, EventLayer, JudgeLine, Meta, Note, SpeedEvent
from rpechart.beat import Beat


class _Tmp:
    """给每个用例一个干净临时目录，退出时自动删。"""

    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="rpechart_test_")
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)
        # 允许 os.path.join(self.path, ...) 这类写法在 with 体内用
        if hasattr(self, "path"):
            self.path = None
        return False


def _sample_chart():
    return Chart(
        BPMList=[BPMItem(bpm=174.0, startTime=Beat(0))],
        META=Meta(name="测试", charter="我", id="", song="song.mp3", background="bg.png"),
        judgeLineList=[
            JudgeLine(
                Name="L0",
                notes=[Note(type=1, startTime=Beat(0), positionX=50.0)],
            )
        ],
    )


# ------------------------------------------------------------------ JSON 基础
@test
def save_and_load_json_roundtrip():
    with _Tmp() as d:
        p = os.path.join(d, "x.json")
        save_json(p, {"a": 1, "中文": "值"})
        assert load_json(p) == {"a": 1, "中文": "值"}


@test
def save_json_writes_utf8_without_bom():
    """必须写无 BOM 的 UTF-8，否则 RPE 的解析器可能读不动。"""
    with _Tmp() as d:
        p = os.path.join(d, "x.json")
        save_json(p, {"name": "曲名"})
        raw = open(p, "rb").read()
        assert not raw.startswith(b"\xef\xbb\xbf")
        assert "曲名" in raw.decode("utf-8")


@test
def load_json_tolerates_bom():
    """别人用记事本另存过的谱面可能带 BOM —— 要能吃下去。"""
    with _Tmp() as d:
        p = os.path.join(d, "bom.json")
        open(p, "wb").write(b"\xef\xbb\xbf" + json.dumps({"a": 1}).encode("utf-8"))
        assert load_json(p) == {"a": 1}


@test
def load_json_reports_missing_file_and_bad_json():
    with _Tmp() as d:
        raises(ChartIOError, load_json, os.path.join(d, "nope.json"))
        p = os.path.join(d, "bad.json")
        open(p, "w", encoding="utf-8").write("{ not json ")
        raises(ChartIOError, load_json, p)


@test
def save_json_does_not_leave_a_tmp_file_on_success():
    with _Tmp() as d:
        p = os.path.join(d, "x.json")
        save_json(p, {"a": 1})
        assert sorted(os.listdir(d)) == ["x.json"]


@test
def save_json_is_atomic_and_overwrites():
    """写第二次必须干净覆盖，不能残留旧内容尾部。"""
    with _Tmp() as d:
        p = os.path.join(d, "x.json")
        save_json(p, {"long_key": "x" * 500})
        save_json(p, {"a": 1})
        assert load_json(p) == {"a": 1}


# ------------------------------------------------------------------ Chart 读写
@test
def save_and_load_chart_roundtrip():
    with _Tmp() as d:
        p = os.path.join(d, "chart.json")
        chart = _sample_chart()
        save_chart(chart, p)
        back = load_chart(p)
        assert back.META.name == "测试"
        assert back.judgeLineList[0].notes[0].positionX == 50.0
        assert back.to_json() == chart.to_json()


@test
def save_chart_requires_a_chart_object():
    with _Tmp() as d:
        raises(TypeError, save_chart, {"not": "a chart"}, os.path.join(d, "c.json"))


def _unsorted_chart():
    """一张事件顺序被打乱的谱面（模拟直接 append 事件的调用方）。"""
    line = JudgeLine(
        Name="L",
        notes=[Note(type=1, startTime=Beat(0), positionX=0.0)],
        eventLayers=[
            EventLayer(
                speedEvents=[
                    SpeedEvent(Beat(0), Beat(8), 1.0, 1.0),
                    SpeedEvent(Beat(16), Beat(24), 4.0, 4.0),
                    SpeedEvent(Beat(8), Beat(16), 2.0, 2.0),
                ]
            )
        ],
    )
    return Chart(
        BPMList=[BPMItem(bpm=174.0, startTime=Beat(0))],
        META=Meta(name="排序", charter="我", id=""),
        judgeLineList=[line],
    )


@test
def save_chart_sorts_events_before_writing():
    """写盘前必须就地排序，否则乱序事件会原样落盘（P0 回归）。

    真实谱面 9805 个事件层无一例乱序，RPE 自己写出的文件恒有序。
    """
    with _Tmp() as d:
        p = os.path.join(d, "chart.json")
        save_chart(_unsorted_chart(), p)
        raw = json.load(open(p, encoding="utf-8"))
        starts = [e["startTime"] for e in raw["judgeLineList"][0]["eventLayers"][0]["speedEvents"]]
        assert starts == [[0, 0, 1], [8, 0, 1], [16, 0, 1]], starts


@test
def export_pez_writes_a_sorted_chart():
    """打包前同样要排序，保证包里的谱面是 RPE 的标准顺序。"""
    with _Tmp() as d:
        pez = export_pez(_unsorted_chart(), os.path.join(d, "o.pez"))
        with zipfile.ZipFile(pez) as zf:
            raw = json.loads(zf.read("chart.json").decode("utf-8"))
        starts = [e["startTime"] for e in raw["judgeLineList"][0]["eventLayers"][0]["speedEvents"]]
        assert starts == [[0, 0, 1], [8, 0, 1], [16, 0, 1]], starts


@test
def load_chart_accepts_dict_directly():
    c = load_chart({"BPMList": [{"bpm": 150.0, "startTime": [0, 0, 1]}], "judgeLineList": []})
    approx(c.BPMList[0].bpm, 150.0)


@test
def load_chart_accepts_a_chart_object_idempotently():
    chart = _sample_chart()
    assert load_chart(chart) is chart


@test
def load_chart_from_pez_picks_the_shallowest_json():
    """.pez 里可能有多个 json（如 extra.json），要挑最浅最短的那个。"""
    with _Tmp() as d:
        p = os.path.join(d, "in.pez")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("shaders/extra.json", json.dumps({"junk": 1}))
            zf.writestr("chart.json", json.dumps(_sample_chart().to_json()))
        c = load_chart(p)
        assert c.META.name == "测试"


@test
def load_chart_from_pez_without_json_fails_clearly():
    with _Tmp() as d:
        p = os.path.join(d, "empty.pez")
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("song.mp3", b"xx")
        raises(ChartIOError, load_chart, p)


# ------------------------------------------------------------------ ChartFolder
@test
def chart_folder_id_is_the_directory_name():
    with _Tmp() as d:
        f = ChartFolder(os.path.join(d, "Resources", "7"))
        assert f.id == "7"
        assert f.chart_json == f.path / "chart.json"
        assert f.info_txt == f.path / "info.txt"
        assert not f.exists


@test
def chart_folder_load_falls_back_to_info_txt_name():
    """谱面文件名不叫 chart.json 时，要靠 info.txt 的 Chart 键找到它。"""
    with _Tmp() as d:
        folder = ChartFolder(os.path.join(d, "Resources", "3"))
        folder.path.mkdir(parents=True)
        (folder.path / "my_chart.json").write_text(
            json.dumps(_sample_chart().to_json()), encoding="utf-8"
        )
        folder.write_info(chart_file="my_chart.json", song="a.mp3", background="b.png")
        c = folder.load()
        assert c.META.name == "测试"


@test
def chart_folder_load_raises_when_nothing_found():
    with _Tmp() as d:
        folder = ChartFolder(os.path.join(d, "Resources", "9"))
        folder.path.mkdir(parents=True)
        raises(ChartIOError, folder.load)


@test
def chart_folder_write_and_read_info_roundtrip():
    with _Tmp() as d:
        folder = ChartFolder(os.path.join(d, "Resources", "1"))
        folder.write_info(chart_file="chart.json", song="s.mp3", background="b.png")
        info = folder.read_info()
        assert info["chart"] == "chart.json"
        assert info["song"] == "s.mp3"
        # RPE 用的键名是 Picture；read_info 额外给出 background 别名
        assert info["picture"] == "b.png"
        assert info["background"] == "b.png"


@test
def write_info_matches_the_real_rpe_layout():
    """键名与键序照抄 23 份真实 .pez：首行 #，曲绘键叫 Picture 不叫 Background。"""
    with _Tmp() as d:
        folder = ChartFolder(os.path.join(d, "Resources", "1"))
        folder.write_info(
            chart_file="c.json",
            song="s.ogg",
            background="p.png",
            name="曲名",
            path="1",
            level="IN Lv.14",
            composer="曲师",
            charter="谱师",
        )
        text = folder.info_txt.read_text(encoding="utf-8")
        first = text.splitlines()[0]
        assert first == "#"
        assert "Picture: p.png" in text
        assert "Background:" not in text
        keys = [ln.split(":")[0] for ln in text.splitlines() if ":" in ln]
        assert keys == [
            "Name",
            "Path",
            "Song",
            "Picture",
            "Chart",
            "Level",
            "Composer",
            "Charter",
        ]


@test
def read_info_maps_picture_to_background_alias():
    with _Tmp() as d:
        folder = ChartFolder(os.path.join(d, "Resources", "1"))
        folder.path.mkdir(parents=True)
        folder.info_txt.write_text("#\nChart: a.json\nPicture: p.png\n", encoding="utf-8")
        info = folder.read_info()
        assert info["picture"] == "p.png"
        assert info["background"] == "p.png"


@test
def read_info_is_case_insensitive_and_handles_fullwidth_colon():
    """键名大小写不敏感；全角冒号也要认（中文输入法常见）。"""
    with _Tmp() as d:
        folder = ChartFolder(os.path.join(d, "Resources", "1"))
        folder.path.mkdir(parents=True)
        folder.info_txt.write_text(
            "# 注释行\nChart: a.json\nSONG： b.mp3\n\n", encoding="utf-8"
        )
        info = folder.read_info()
        assert info["chart"] == "a.json"
        assert info["song"] == "b.mp3"


@test
def read_info_ignores_lines_without_a_colon():
    with _Tmp() as d:
        folder = ChartFolder(os.path.join(d, "Resources", "1"))
        folder.path.mkdir(parents=True)
        folder.info_txt.write_text("garbage line\nChart: a.json\n", encoding="utf-8")
        assert folder.read_info() == {"chart": "a.json"}


@test
def chart_folder_save_creates_directories():
    with _Tmp() as d:
        folder = ChartFolder(os.path.join(d, "Resources", "5"))
        folder.save(_sample_chart())
        assert folder.chart_json.exists()
        assert folder.load().META.name == "测试"


# ------------------------------------------------------------------ Chartlist
@test
def chartlist_read_write_roundtrip():
    with _Tmp() as d:
        write_chartlist(d, ["1", "2", "10"])
        assert read_chartlist(d) == ["1", "2", "10"]


@test
def chartlist_missing_returns_empty():
    with _Tmp() as d:
        assert read_chartlist(d) == []


@test
def chartlist_skips_comments_and_blank_lines():
    with _Tmp() as d:
        open(os.path.join(d, "Chartlist.txt"), "w", encoding="utf-8").write(
            "# header\n\n1\n  2  \n"
        )
        assert read_chartlist(d) == ["1", "2"]


@test
def scan_chart_ids_only_takes_numeric_folders():
    """1.5.0+ 会自动扫描，但只认纯数字文件夹名。"""
    with _Tmp() as d:
        res = os.path.join(d, "Resources")
        for name in ("1", "2", "10", "abc", "3x", "01"):
            os.makedirs(os.path.join(res, name), exist_ok=True)
        open(os.path.join(res, "not_a_dir.txt"), "w").close()
        got = scan_chart_ids(d)
        assert set(got) == {"1", "2", "10", "01"}
        # 按数值排序
        assert got.index("2") < got.index("10")


@test
def scan_chart_ids_without_resources_returns_empty():
    with _Tmp() as d:
        assert scan_chart_ids(d) == []


# ------------------------------------------------------------------ find_rpe_root
@test
def find_rpe_root_walks_up_to_phi_edit():
    with _Tmp() as d:
        os.makedirs(os.path.join(d, "Resources", "1"))
        open(os.path.join(d, "PhiEdit.exe"), "w").close()
        deep = os.path.join(d, "Resources", "1", "sub", "deeper")
        os.makedirs(deep)
        assert find_rpe_root(deep) == Path(d)


@test
def find_rpe_root_accepts_a_file_path():
    with _Tmp() as d:
        os.makedirs(os.path.join(d, "Resources"))
        open(os.path.join(d, "Chartlist.txt"), "w").close()
        f = os.path.join(d, "somefile.txt")
        open(f, "w").close()
        assert find_rpe_root(f) == Path(d)


@test
def find_rpe_root_falls_back_to_any_resources_dir():
    """没有 PhiEdit.exe 也没有 Chartlist.txt 时，只要有 Resources 就认。"""
    with _Tmp() as d:
        os.makedirs(os.path.join(d, "Resources"))
        assert find_rpe_root(d) == Path(d)


@test
def find_rpe_root_returns_none_when_nothing_matches():
    with _Tmp() as d:
        assert find_rpe_root(d) is None


# ------------------------------------------------------------------ install_chart
@test
def install_chart_creates_a_complete_chart_folder():
    with _Tmp() as d:
        os.makedirs(os.path.join(d, "Resources"))
        song = os.path.join(d, "_song.mp3")
        bg = os.path.join(d, "_bg.png")
        open(song, "wb").write(b"ID3fake")
        open(bg, "wb").write(b"\x89PNGfake")

        folder = install_chart(d, 42, _sample_chart(), song_src=song, background_src=bg)

        assert folder.path == os.path.join(d, "Resources", "42") or str(folder.path).endswith(
            os.path.join("Resources", "42")
        )
        assert folder.chart_json.exists()
        assert folder.info_txt.exists()

        chart = folder.load()
        # 音频/曲绘被复制进来，且 META 被回填成实际文件名
        assert (folder.path / chart.META.song).exists()
        assert (folder.path / chart.META.background).exists()
        assert chart.META.song.endswith(".mp3")
        assert chart.META.background.endswith(".png")
        assert chart.META.id == "42"

        info = folder.read_info()
        assert info["chart"] == "chart.json"
        assert info["song"] == chart.META.song


def _tiny_wav(path, seconds=2.0, rate=8000):
    """写一个真能被 audio.py 解析的 16-bit 单声道 WAV，用来验证 duration 回填。"""
    import struct

    frames = int(rate * seconds)
    data = b"\x00\x00" * frames
    riff = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    body = b"data" + struct.pack("<I", len(data)) + data
    with open(path, "wb") as fh:
        fh.write(riff + fmt + body)


@test
def install_chart_backfills_meta_duration_from_the_song():
    """RPE 1.5.0+ 的 META 有 ``duration``，安装时按真实歌曲填一次。"""
    with _Tmp() as d:
        os.makedirs(os.path.join(d, "Resources"))
        song = os.path.join(d, "_song.wav")
        _tiny_wav(song, seconds=2.0)

        chart = _sample_chart()
        assert chart.META.duration is None
        folder = install_chart(d, 7, chart, song_src=song, song_name="music.wav")

        loaded = folder.load()
        assert loaded.META.duration is not None
        approx(loaded.META.duration, 2.0, tol=1e-6)
        assert "duration" in loaded.META.to_json()


@test
def install_chart_does_not_invent_duration_without_a_song():
    with _Tmp() as d:
        os.makedirs(os.path.join(d, "Resources"))
        folder = install_chart(d, 8, _sample_chart())
        assert folder.load().META.duration is None


@test
def install_chart_writes_the_group_line_like_rpe_does():
    """``info.txt`` 要有 ``Group:`` 行 —— RPE 自己保存时就会写（实测 RPE 重写
    1000001 后 info.txt 才出现 ``Group: Default``），位置在该文件最后。"""
    with _Tmp() as d:
        os.makedirs(os.path.join(d, "Resources"))
        folder = install_chart(d, 9, _sample_chart())

        text = folder.info_txt.read_text(encoding="utf-8")
        lines = [l for l in text.splitlines() if l.strip()]
        assert text.splitlines()[0] == "#"
        assert "Group: Default" in lines
        # 与真实 RPE 一致：Group 是最后一行的键
        assert lines[-1] == "Group: Default"
        assert folder.read_info()["group"] == "Default"


@test
def install_chart_group_line_follows_the_chart_group_list():
    with _Tmp() as d:
        os.makedirs(os.path.join(d, "Resources"))
        chart = _sample_chart()
        chart.judgeLineGroup = ["Main"]
        folder = install_chart(d, 10, chart)
        assert "Group: Main" in folder.info_txt.read_text(encoding="utf-8")


@test
def install_chart_respects_explicit_names():
    with _Tmp() as d:
        os.makedirs(os.path.join(d, "Resources"))
        song = os.path.join(d, "_s.mp3")
        open(song, "wb").write(b"x")
        folder = install_chart(
            d, 1, _sample_chart(), song_src=song, song_name="music.mp3"
        )
        assert (folder.path / "music.mp3").exists()
        assert folder.load().META.song == "music.mp3"


@test
def install_chart_honours_overwrite_false():
    with _Tmp() as d:
        os.makedirs(os.path.join(d, "Resources", "1"))
        raises(ChartIOError, install_chart, d, 1, _sample_chart(), overwrite=False)


@test
def install_chart_updates_chartlist_only_when_it_exists():
    """RPE >= 1.5.0 没有 Chartlist.txt；不能凭空造一个出来。"""
    with _Tmp() as d:
        os.makedirs(os.path.join(d, "Resources"))
        install_chart(d, 1, _sample_chart())
        assert not os.path.exists(os.path.join(d, "Chartlist.txt"))

        # 老版本：文件存在则追加
        write_chartlist(d, ["7"])
        install_chart(d, 8, _sample_chart())
        assert read_chartlist(d) == ["7", "8"]
        # 重复安装同一个 id 不会写重
        install_chart(d, 8, _sample_chart())
        assert read_chartlist(d) == ["7", "8"]


@test
def install_chart_sets_meta_id_when_placeholder():
    with _Tmp() as d:
        os.makedirs(os.path.join(d, "Resources"))
        chart = _sample_chart()
        chart.META.id = "0"
        folder = install_chart(d, 99, chart)
        assert folder.load().META.id == "99"


@test
def install_chart_keeps_a_meaningful_meta_id():
    """用户自己写过的 META.id 不要被覆盖掉。"""
    with _Tmp() as d:
        os.makedirs(os.path.join(d, "Resources"))
        chart = _sample_chart()
        chart.META.id = "my-custom-id"
        folder = install_chart(d, 99, chart)
        assert folder.load().META.id == "my-custom-id"


@test
def install_chart_leaves_chinese_paths_alone_but_documents_them():
    """库本身要能处理中文路径（用 tmp 目录做代理）；能不能开是 RPE 的事。"""
    with _Tmp() as d:
        root = os.path.join(d, "中文目录")
        os.makedirs(os.path.join(root, "Resources"))
        folder = install_chart(root, 5, _sample_chart())
        assert folder.chart_json.exists()


# ------------------------------------------------------------------ .pez
@test
def export_and_import_pez_roundtrip():
    with _Tmp() as d:
        folder = ChartFolder(os.path.join(d, "Resources", "1"))
        folder.save(_sample_chart())
        (folder.path / "song.mp3").write_bytes(b"audio")
        (folder.path / "bg.png").write_bytes(b"image")
        folder.write_info("chart.json", "song.mp3", "bg.png")

        pez = export_pez(folder.load(), os.path.join(d, "out.pez"), folder=folder.path)
        assert pez.suffix == ".pez"
        assert zipfile.is_zipfile(pez)

        with zipfile.ZipFile(pez) as zf:
            names = set(zf.namelist())
        assert "chart.json" in names
        assert "song.mp3" in names
        assert "bg.png" in names
        # 真实 .pez 23/23 都带 info.txt，导出必须带上
        assert "info.txt" in names
        with zipfile.ZipFile(pez) as zf:
            info = zf.read("info.txt").decode("utf-8")
        assert info.splitlines()[0] == "#"
        assert "Chart: chart.json" in info
        assert "Song: song.mp3" in info
        # Chartlist.txt 不是 .pez 的成员
        assert "Chartlist.txt" not in names

        dest = os.path.join(d, "extracted")
        import_pez(pez, dest)
        assert os.path.exists(os.path.join(dest, "chart.json"))
        assert os.path.exists(os.path.join(dest, "song.mp3"))
        assert load_chart(os.path.join(dest, "chart.json")).META.name == "测试"


@test
def export_pez_adds_the_extension_when_missing():
    with _Tmp() as d:
        pez = export_pez(_sample_chart(), os.path.join(d, "noext"))
        assert str(pez).endswith(".pez")


@test
def export_pez_includes_extra_files():
    with _Tmp() as d:
        extra = os.path.join(d, "extra.json")
        open(extra, "w").write("{}")
        pez = export_pez(_sample_chart(), os.path.join(d, "x.pez"), extra_files=[extra])
        with zipfile.ZipFile(pez) as zf:
            assert "extra.json" in zf.namelist()


@test
def export_pez_retargets_info_chart_key():
    """真实谱面文件常叫 <id>.json；导出后 info.txt 的 Chart: 必须指向新文件。

    否则 RPE 会按那份旧 info.txt 去加载包里剩下的旧 <id>.json，本次修改被静默丢弃。
    """
    with _Tmp() as d:
        folder = ChartFolder(os.path.join(d, "Resources", "1"))
        folder.save(_sample_chart(), chart_name="0904298276430233.json")
        (folder.path / "s.ogg").write_bytes(b"audio")
        (folder.path / "p.png").write_bytes(b"image")
        folder.write_info(chart_file="0904298276430233.json", song="s.ogg", background="p.png")

        pez = export_pez(folder.load(), os.path.join(d, "out.pez"), folder=folder.path)
        with zipfile.ZipFile(pez) as zf:
            names = set(zf.namelist())
            info = zf.read("info.txt").decode("utf-8")
        assert "Chart: chart.json" in info
        assert "Picture: p.png" in info  # 其余行原样保留
        # 和 chart_name 同名的文件由 writestr 写，不该再被复制一遍
        assert names == {"chart.json", "info.txt", "s.ogg", "p.png"}


@test
def export_pez_does_not_pack_the_stale_chart_file():
    """包里不能同时有新旧两份谱面 —— 那会让 Chart: 键决定加载谁，同样是静默错误。"""
    with _Tmp() as d:
        folder = ChartFolder(os.path.join(d, "Resources", "1"))
        folder.save(_sample_chart(), chart_name="old.json")
        folder.write_info(chart_file="old.json")

        pez = export_pez(folder.load(), os.path.join(d, "out.pez"), folder=folder.path)
        with zipfile.ZipFile(pez) as zf:
            names = set(zf.namelist())
        assert "old.json" not in names
        assert "chart.json" in names


@test
def export_pez_custom_chart_name_shows_up_in_info():
    with _Tmp() as d:
        pez = export_pez(
            _sample_chart(), os.path.join(d, "x.pez"), chart_name="9000000000.json"
        )
        with zipfile.ZipFile(pez) as zf:
            assert "9000000000.json" in zf.namelist()
            info = zf.read("info.txt").decode("utf-8")
        assert "Chart: 9000000000.json" in info


@test
def export_pez_without_folder_synthesises_info_txt_with_meta():
    """没有文件夹可抄时，info.txt 从 META 合成（真实 .pez 23/23 都带这个文件）。"""
    with _Tmp() as d:
        chart = _sample_chart()
        chart.META.charter = "谱师甲"
        chart.META.level = "IN Lv.14"
        chart.META.composer = "曲师乙"
        pez = export_pez(chart, os.path.join(d, "x.pez"))
        with zipfile.ZipFile(pez) as zf:
            info = zf.read("info.txt").decode("utf-8")
        lines = info.splitlines()
        assert lines[0] == "#"
        assert "Name: 测试" in info
        assert "Charter: 谱师甲" in info
        assert "Level: IN Lv.14" in info
        assert "Composer: 曲师乙" in info
        assert "Song: song.mp3" in info
        assert "Picture: bg.png" in info


@test
def import_pez_blocks_zip_slip():
    """恶意/损坏的包不能写到目标目录外面去。"""
    with _Tmp() as d:
        pez = os.path.join(d, "evil.pez")
        with zipfile.ZipFile(pez, "w") as zf:
            zf.writestr("chart.json", "{}")
            zf.writestr("../../escaped.txt", b"boom")
            zf.writestr("/absolute.txt", b"boom")
        dest = os.path.join(d, "safe")
        import_pez(pez, dest)
        assert os.path.exists(os.path.join(dest, "chart.json"))
        assert not os.path.exists(os.path.join(d, "escaped.txt"))
        assert not os.path.exists(os.path.join(dest, "..", "..", "escaped.txt"))
        assert not os.path.exists("/absolute.txt")


@test
def import_pez_honours_overwrite_false():
    with _Tmp() as d:
        pez = export_pez(_sample_chart(), os.path.join(d, "x.pez"))
        dest = os.path.join(d, "dest")
        os.makedirs(dest)
        raises(ChartIOError, import_pez, pez, dest, overwrite=False)
