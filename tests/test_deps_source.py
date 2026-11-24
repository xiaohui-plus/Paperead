from __future__ import annotations

import pytest


def test_release_urls_can_disable_deps_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式把仓库设为空串可禁用依赖渠道，只返回旧地址（内网/离线场景）。"""
    monkeypatch.setenv("PAPEREAD_DEPS_REPO", "")
    monkeypatch.setenv("PAPEREAD_DEPS_GH_REPO", "")
    from server import deps_source

    urls = deps_source.release_urls("x.zip", extra=["https://legacy/x.zip"])

    assert urls == ["https://legacy/x.zip"]


def test_default_repo_points_to_dependency_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不设环境变量时，依赖仓库是打包后的默认下载源。"""
    monkeypatch.delenv("PAPEREAD_DEPS_REPO", raising=False)
    monkeypatch.delenv("PAPEREAD_DEPS_GH_REPO", raising=False)
    from server import deps_source

    urls = deps_source.release_urls("x.zip", extra=["https://legacy/x.zip"])

    assert urls[0] == (
        "https://gitee.com/deng_xiaohui/paperead-dependency"
        "/releases/download/deps-latest/x.zip"
    )
    assert urls[-1] == "https://legacy/x.zip"


def test_release_urls_prefers_deps_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPEREAD_DEPS_REPO", "owner/deps")
    monkeypatch.setenv("PAPEREAD_DEPS_GH_REPO", "owner/deps-gh")
    from server import deps_source

    urls = deps_source.release_urls("x.zip", extra=["https://legacy/x.zip"])

    assert urls[0] == (
        "https://gitee.com/owner/deps/releases/download/deps-latest/x.zip"
    )
    assert any("ghproxy.net" in url for url in urls)
    assert any(url.startswith("https://github.com/owner/deps-gh") for url in urls)
    assert urls[-1] == "https://legacy/x.zip"


def test_node_and_pandoc_prefer_deps_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPEREAD_DEPS_REPO", "owner/deps")
    monkeypatch.delenv("PAPEREAD_NODE_URL", raising=False)
    monkeypatch.delenv("PAPEREAD_PANDOC_URL", raising=False)
    from server import node_setup
    from server.research import pandoc_setup

    assert node_setup._sources()[0].startswith(
        "https://gitee.com/owner/deps/releases/download/"
    )
    assert pandoc_setup._sources()[0].startswith(
        "https://gitee.com/owner/deps/releases/download/"
    )


def test_override_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式指定的单文件地址不受依赖仓库影响（内网/测试用）。"""
    monkeypatch.setenv("PAPEREAD_DEPS_REPO", "owner/deps")
    monkeypatch.setenv("PAPEREAD_PANDOC_URL", "https://mirror/pandoc.zip")
    from server.research import pandoc_setup

    assert pandoc_setup._sources() == ["https://mirror/pandoc.zip"]


def test_babeldoc_bases_put_deps_after_manifest_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分卷优先跟清单同目录，其次依赖仓库，最后主仓库旧目录。"""
    monkeypatch.setenv("PAPEREAD_DEPS_REPO", "owner/deps")
    monkeypatch.delenv("PAPEREAD_BABELDOC_ENGINE_URL", raising=False)
    from server import babeldoc_setup

    bases = babeldoc_setup._download_bases("https://primary/dir")

    assert bases[0] == "https://primary/dir"
    assert any("owner/deps" in base for base in bases)
    assert any("gitee.com/deng_xiaohui/paperead" in base for base in bases)


def test_babeldoc_manifest_sources_prefer_deps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAPEREAD_DEPS_REPO", "owner/deps")
    monkeypatch.delenv("PAPEREAD_BABELDOC_ENGINE_URL", raising=False)
    from server import babeldoc_setup

    sources = babeldoc_setup._manifest_sources()

    assert sources[0].startswith("https://gitee.com/owner/deps/releases/download/")