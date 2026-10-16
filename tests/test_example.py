"""示例脚本必须真的能跑通 —— 它是给别人照着抄的骨架，坏了比库坏了更误导。

这里跑的是 ``examples/make_demo_chart.py`` 本身（用子进程，避免它的
``sys.path`` 插入污染测试进程），断言它能生成 JSON、能被读回、并且安装到
一个假的 RPE 根目录后结构正确（``Resources/<id>/`` 下四个文件齐备）。

**不碰用户机器上的 RPE 安装**：假根目录建在临时目录里。
"""

import json
import os
import subprocess
import sys
import tempfile
import wave

from run_tests import test

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXAMPLE = os.path.join(ROOT, "examples", "make_demo_chart.py")


def _silence_wav(path, seconds=3.0, rate=22050):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


class _Tmp:
    def __enter__(self):
        self.d = tempfile.mkdtemp(prefix="rpechart_example_")
        return self.d

    def __exit__(self, *exc):
        import shutil

        shutil.rmtree(self.d, ignore_errors=True)
        return False


def _run(args, cwd, timeout=180):
    return subprocess.run(
        [sys.executable, EXAMPLE, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )


@test
def example_script_builds_a_chart_file():
    with _Tmp() as d:
        out = os.path.join(d, "out", "chart.json")
        song = _silence_wav(os.path.join(d, "s.wav"))
        proc = _run(
            ["--out", out, "--song", song, "--background", ""],
            cwd=ROOT,
        )
        assert proc.returncode == 0, f"退出码 {proc.returncode}\n{proc.stderr[-2000:]}"
        assert os.path.exists(out), "没有生成 chart.json"
        data = json.load(open(out, encoding="utf-8"))
        for key in ("BPMList", "META", "judgeLineList"):
            assert key in data, f"顶层缺 {key}"
        assert data["judgeLineList"], "没有任何判定线"


@test
def example_script_installs_into_a_fake_rpe_root():
    with _Tmp() as d:
        root = os.path.join(d, "rpe")
        os.makedirs(os.path.join(root, "Resources"))
        open(os.path.join(root, "PhiEdit.exe"), "w").close()
        song = _silence_wav(os.path.join(d, "s.wav"))
        bg = os.path.join(d, "bg.png")
        open(bg, "wb").write(b"\x89PNG\r\n\x1a\n")  # 只需存在，不解析

        proc = _run(
            [
                "--install",
                "--rpe-root",
                root,
                "--id",
                "123456",
                "--out",
                os.path.join(d, "out", "chart.json"),
                "--song",
                song,
                "--background",
                bg,
            ],
            cwd=ROOT,
        )
        assert proc.returncode == 0, f"退出码 {proc.returncode}\n{proc.stderr[-2000:]}"

        folder = os.path.join(root, "Resources", "123456")
        assert os.path.isdir(folder), "没有建出谱面文件夹"
        names = set(os.listdir(folder))
        assert "chart.json" in names, names
        assert "info.txt" in names, names

        # info.txt 的 Chart 行必须指向真实存在的文件
        info = {}
        for line in open(os.path.join(folder, "info.txt"), encoding="utf-8"):
            line = line.strip()
            if ":" in line:
                k, _, v = line.partition(":")
                info[k.strip().lower()] = v.strip()
        assert info.get("chart") == "chart.json", info
        assert os.path.exists(os.path.join(folder, info["chart"])), info

        chart = json.load(open(os.path.join(folder, "chart.json"), encoding="utf-8"))
        # META 的 song/background 必须与实际落盘的文件名一致
        for key in ("song", "background"):
            value = chart["META"][key]
            assert os.path.exists(os.path.join(folder, value)), f"{key}={value} 不存在"


@test
def example_script_is_importable_without_running():
    """有 ``if __name__ == '__main__'`` 守卫，import 不该产生副作用。"""
    code = (
        "import importlib.util,sys,os;"
        f"spec=importlib.util.spec_from_file_location('demo',r'{EXAMPLE}');"
        "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
        "print('imported', callable(m.build), callable(m.main))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "imported True True" in proc.stdout
