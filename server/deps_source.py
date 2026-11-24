"""第三方依赖（BabelDOC 引擎 / node / pandoc）的下载源。

为什么单独一个仓库
------------------
这三件合计约 275MB，且与 App 版本解耦（引擎 0.7.1 不会因 App 从 0.2.32 升到
0.2.33 而重传）。此前把它们挂在主仓库的固定 ``latest`` 发行版上，app 发版覆盖
``latest`` 时曾把引擎分卷覆盖丢了一半（只剩 part003），客户端因此永远下载失败，
而发布脚本仍显示成功。

现在依赖走独立仓库的固定 tag：主仓库只放安装包与更新清单，依赖不再随版本发布
被覆盖；客户端下载失败时仍会回退主仓库旧地址，保证迁移期可用。

配置
----
- ``PAPEREAD_DEPS_REPO``：Gitee 依赖仓库，形如 ``owner/name``。
- ``PAPEREAD_DEPS_GH_REPO``：GitHub 镜像仓库，形如 ``owner/name``。
- ``PAPEREAD_DEPS_TAG``：依赖发行版 tag，默认 ``deps-latest``。

两个仓库都为空时，只返回调用方传入的旧地址（行为与改造前一致）。
"""

from __future__ import annotations

import os

DEFAULT_TAG = "deps-latest"
# 依赖专用仓库（public）。仓库名按 Gitee 上实际拼写，不要自行"纠正"。
DEFAULT_GITEE_REPO = "deng_xiaohui/paperead-dependency"
DEFAULT_GITHUB_REPO = ""


def _env_or_default(name: str, default: str) -> str:
    """环境变量显式存在时以它为准；显式设为空串表示禁用该渠道。"""
    raw = os.environ.get(name)
    if raw is not None:
        return raw.strip()
    return default


def gitee_repo() -> str:
    return _env_or_default("PAPEREAD_DEPS_REPO", DEFAULT_GITEE_REPO)


def github_repo() -> str:
    return _env_or_default("PAPEREAD_DEPS_GH_REPO", DEFAULT_GITHUB_REPO)


def tag() -> str:
    return os.environ.get("PAPEREAD_DEPS_TAG", "").strip() or DEFAULT_TAG


def release_bases(*, release_tag: str | None = None) -> list[str]:
    """依赖下载目录（按优先级）：Gitee 依赖仓库 → ghproxy → GitHub 镜像。"""
    release_tag = release_tag or tag()
    bases: list[str] = []
    repo = gitee_repo()
    if repo:
        bases.append(f"https://gitee.com/{repo}/releases/download/{release_tag}")
    gh = github_repo()
    if gh:
        bases.append(
            f"https://ghproxy.net/https://github.com/{gh}/releases/download/{release_tag}"
        )
        bases.append(f"https://github.com/{gh}/releases/download/{release_tag}")
    return bases


def release_urls(
    name: str,
    *,
    release_tag: str | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """某依赖文件的完整候选地址：依赖仓库优先，``extra``（旧地址）兜底。"""
    urls = [f"{base}/{name}" for base in release_bases(release_tag=release_tag)]
    for url in extra or []:
        if url and url not in urls:
            urls.append(url)
    return urls


def merged_bases(primary: str, *, extra: list[str] | None = None) -> list[str]:
    """分卷下载用的目录候选：清单所在目录 + 依赖仓库 + 旧目录，去重保序。"""
    bases: list[str] = [primary] if primary else []
    for base in release_bases():
        if base not in bases:
            bases.append(base)
    for base in extra or []:
        if base and base not in bases:
            bases.append(base)
    return bases