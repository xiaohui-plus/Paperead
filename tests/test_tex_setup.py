"""LaTeX 引擎自动安装（MiKTeX 便携版）的状态机与路径探测回归。

不跑真实网络：下载/安装步骤都被替换成内存桩，只验证
「阶段推进 → 提升目录 → 状态落地」与「固定路径优先探测」两件事。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server import tex_setup


@pytest.fixture()
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("PAPEREAD_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("PAPEREAD_MOCK", raising=False)
    monkeypatch.delenv("PAPEREAD_TEX_AUTODOWNLOAD", raising=False)
    monkeypatch.delenv("PAPEREAD_TEX_AUTODOWNLOAD_DELAY", raising=False)
    tex_setup.reset_for_tests()
    yield tmp_path
    tex_setup.reset_for_tests()


def test_status_starts_idle_and_not_installed(isolated: Path) -> None:
    state = tex_setup.status()
    assert state["status"] == "idle"
    assert state["installed"] is False
    assert state["version"] == ""


def test_clean_env_points_temp_to_writable_data_dir(isolated: Path) -> None:
    """系统 TEMP 可能不可写；安装子进程必须改用自己的临时目录。"""
    env = tex_setup._clean_env()
    assert env["TEMP"] == str(tex_setup._tmp_dir())
    assert env["TMP"] == str(tex_setup._tmp_dir())
    # PATH 只保留系统目录与引擎目录，不继承用户环境里的非法条目。
    parts = env["PATH"].split(";")
    assert len(parts) == 4
    assert parts[0].endswith("system32")
    assert str(tex_setup.bin_dir()) in parts


def test_mirrors_are_domestic_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAPEREAD_TEX_MIRROR", raising=False)
    bases = tex_setup._mirror_bases()
    assert bases[0].startswith("https://mirrors.tuna.tsinghua.edu.cn")
    assert any("ustc" in base for base in bases)
    assert any("aliyun" in base for base in bases)


def test_setup_urls_use_mirror_setup_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAPEREAD_TEX_SETUP_URL", raising=False)
    monkeypatch.setenv("PAPEREAD_TEX_MIRROR", "https://mirror.example/CTAN/miktex")
    urls = tex_setup._setup_urls()
    assert urls == [f"https://mirror.example/CTAN/miktex/setup/windows-x64/{tex_setup.SETUP_ZIP}".replace("+", "%2B")]


def test_install_chain_advances_phases_and_promotes(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    """下载 → 安装 → 配置 → 中文支持 全部走通后，便携目录被提升且状态为 done。"""
    phases: list[str] = []

    monkeypatch.setattr(tex_setup, "_download_setup", lambda target: target.write_bytes(b"zip"))
    monkeypatch.setattr(tex_setup, "_extract_setup", lambda archive, target: target.write_bytes(b"exe"))

    def run_setup(setup, phase, timeout, on_tick=None):
        phases.append(phase)
        if phase == "install":
            staging_bin = (
                tex_setup._staging_dir() / "texmfs" / "install" / "miktex" / "bin" / "x64"
            )
            staging_bin.mkdir(parents=True, exist_ok=True)
            (staging_bin / "xelatex.exe").write_bytes(b"")
            (staging_bin / "bibtex.exe").write_bytes(b"")
        return 0

    monkeypatch.setattr(tex_setup, "_run_setup", run_setup)
    monkeypatch.setattr(tex_setup, "_configure", lambda repository: None)
    monkeypatch.setattr(tex_setup, "_install_extras", lambda: "")
    monkeypatch.setattr(tex_setup, "_register", lambda: None)
    monkeypatch.setattr(tex_setup, "_invalidate_toolchain_cache", lambda: None)
    monkeypatch.setattr(tex_setup, "_probe_version", lambda exe: "26.5")

    tex_setup._run()

    state = tex_setup.status()
    assert phases == ["packages", "install"]
    assert state["status"] == "done"
    assert state["percent"] == 100
    assert tex_setup.installed_exe() is not None


def test_install_failure_keeps_retryable_state(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    monkeypatch.setattr(tex_setup, "_download_setup", lambda target: target.write_bytes(b"zip"))
    monkeypatch.setattr(tex_setup, "_extract_setup", lambda archive, target: target.write_bytes(b"exe"))
    monkeypatch.setattr(tex_setup, "_run_setup", lambda *a, **k: 1)
    monkeypatch.setattr(tex_setup.shutil, "rmtree", lambda path, ignore_errors=False: None)

    tex_setup._run()

    state = tex_setup.status()
    assert state["status"] == "error"
    assert "退出码 1" in state["error"]
    assert state["installed"] is False


def test_install_fails_fast_when_disk_is_full(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        tex_setup.shutil, "disk_usage", lambda path: SimpleNamespace(free=32 * 1024 * 1024)
    )
    tex_setup._run()

    state = tex_setup.status()
    assert state["status"] == "error"
    assert "磁盘空间不足" in state["error"]


def test_start_if_missing_skips_in_mock(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    monkeypatch.setenv("PAPEREAD_MOCK", "1")
    tex_setup.start_if_missing()
    assert tex_setup.status()["status"] == "idle"


def test_start_if_missing_skips_when_engine_exists(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    monkeypatch.setattr(
        "server.research.latex.find_engines", lambda: ["C:/fake/xelatex.exe"], raising=True
    )
    started: list[str] = []
    monkeypatch.setattr(tex_setup, "start", lambda: started.append("hit") or tex_setup.status())

    tex_setup.start_if_missing()

    assert started == []
    assert tex_setup.status()["status"] == "idle"


def test_find_engines_prefers_tex_bin_dir(
    monkeypatch: pytest.MonkeyPatch, isolated: Path
) -> None:
    from server.research import latex

    bin_dir = tex_setup.bin_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("xelatex.exe", "pdflatex.exe", "bibtex.exe"):
        (bin_dir / name).write_bytes(b"")

    monkeypatch.setattr(latex, "_search_bin_dirs", lambda: [bin_dir])
    monkeypatch.setattr(latex.shutil, "which", lambda name: None)

    engines = latex.find_engines()
    assert engines == [str(bin_dir / "xelatex.exe"), str(bin_dir / "pdflatex.exe")]
    assert latex.find_bibtex() == str(bin_dir / "bibtex.exe")