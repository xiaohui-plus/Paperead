"""LaTeX 导出：把项目里的 Markdown 稿转成可编译的 ``.tex``，并按需编译 PDF。

Pandoc 只负责 Markdown → LaTeX 片段；中文/英文模板、参考文献与编译流程都在
本模块里完成，保证用户说“用 LaTeX 写”时项目自身就有可用的能力：

- 中文稿默认 ``ctexart``（xelatex 编译），英文稿默认 ``article``；
- 已有完整 ``.tex``（含 ``\\documentclass``）时原样保留，只负责编译；
- 本机没有 pandoc 或 TeX 引擎时返回可读的错误，由调用方决定是降级还是提示。

这里只做本地转换与编译，不联网、不启用 ``--shell-escape``。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from server.winproc import hidden_kwargs

MAX_TEXT = 2_000_000
COMPILE_TIMEOUT = 180
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
AUX_SUFFIXES = {
    ".aux",
    ".log",
    ".out",
    ".toc",
    ".bbl",
    ".blg",
    ".fls",
    ".fdb_latexmk",
    ".synctex.gz",
    ".xdv",
    ".nav",
    ".snm",
    ".vrb",
}
STYLES = {"ctexart", "article"}


def has_cjk(text: str) -> bool:
    """判断文本里是否出现中日韩字符，用来挑中文模板。"""
    return bool(CJK_RE.search(text or ""))


def read_text(path: Path, limit: int = MAX_TEXT) -> str:
    """读取纯文本；导出需要完整正文，所以上限比工具读取宽得多。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text if len(text) <= limit else text[:limit]


def escape_latex(value: str) -> str:
    """转义标题/作者里的 LaTeX 特殊字符。"""
    mapping = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(mapping.get(char, char) for char in value or "")


def bundled_tools_dir() -> Path | None:
    """安装包随附的 tools/ 目录（目前放 pandoc）。

    sidecar 的布局是 ``<安装目录>/resources/paperead-server/paperead-server.exe``，
    tools 在 ``<安装目录>/resources/tools``；源码运行时回退到
    ``src-tauri/resources/tools``，方便本地不装 pandoc 也能测导出。
    """
    override = os.environ.get("PAPEREAD_TOOLS_DIR")
    if override:
        path = Path(override)
        if path.is_dir():
            return path
    exe_dir = Path(sys.executable).resolve().parent
    candidates = [
        exe_dir.parent / "tools",
        Path(__file__).resolve().parents[2] / "src-tauri" / "resources" / "tools",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def find_pandoc() -> str | None:
    """优先用安装包内置的 pandoc，其次是首次启动自动下载的那份，最后才是 PATH。"""
    override = os.environ.get("PAPEREAD_PANDOC")
    if override and Path(override).is_file():
        return override
    tools = bundled_tools_dir()
    if tools:
        bundled = tools / "pandoc" / "pandoc.exe"
        if bundled.is_file():
            return str(bundled)
    try:
        from server.research.pandoc_setup import installed_exe

        downloaded = installed_exe()
    except Exception:  # noqa: BLE001 - 下载模块不可用时不拖累导出
        downloaded = None
    if downloaded is not None:
        return str(downloaded)
    return shutil.which("pandoc") or shutil.which("pandoc.exe")


def find_engines() -> list[str]:
    """按优先级返回可用的 TeX 引擎。

    xelatex 对中文最稳；某些 Windows 环境下 xdvipdfmx 会失败，因此保留
    pdflatex（ctexart 也能用）作为回退，lualatex 放最后。
    """
    engines: list[str] = []
    for name in (
        "xelatex",
        "xelatex.exe",
        "pdflatex",
        "pdflatex.exe",
        "lualatex",
        "lualatex.exe",
    ):
        found = shutil.which(name)
        if found and found not in engines:
            engines.append(found)
    return engines


def find_engine() -> str | None:
    engines = find_engines()
    return engines[0] if engines else None


def find_bibtex() -> str | None:
    return shutil.which("bibtex") or shutil.which("bibtex.exe")


def _split_title(markdown: str, title: str) -> tuple[str, str]:
    """去掉正文开头的一级标题，避免和模板标题重复。"""
    lines = markdown.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].lstrip().startswith("# "):
        heading = lines[0].lstrip()[2:].strip()
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
        return "\n".join(lines), title or heading
    return markdown, title


def _markdown_to_fragment(markdown: str, natbib: bool) -> tuple[str, str]:
    exe = find_pandoc()
    if not exe:
        return "", (
            "服务器未找到 pandoc，无法把 Markdown 转成 LaTeX。"
            "可以直接写完整的 .tex（含 \\documentclass）再导出 PDF。"
        )
    command = [
        exe,
        "-f",
        "markdown+tex_math_dollars+pipe_tables+strikeout+task_lists",
        "-t",
        "latex",
        "--wrap=none",
    ]
    if natbib:
        command.append("--natbib")
    try:
        proc = subprocess.run(
            command,
            input=markdown.encode("utf-8"),
            capture_output=True,
            timeout=120,
            **hidden_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"pandoc 执行失败：{exc}"
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace")[:400]
        return "", f"pandoc 转换失败：{detail}"
    # pandoc 在 Windows 下输出 CRLF；若原样写进 .tex，再经文本模式写入会变成
    # ``\r\r\n``，在 ``{%`` 行后凭空多出一个空行，导致 \hypertarget 报错。
    output = proc.stdout.decode("utf-8", "replace")
    return output.replace("\r\n", "\n").replace("\r", "\n"), ""


def _wrap_document(
    body: str,
    *,
    title: str,
    author: str,
    abstract: str,
    style: str,
    bib_name: str | None,
    nocite: bool = False,
) -> str:
    if style == "ctexart":
        document_class = r"\documentclass[UTF8,a4paper,11pt]{ctexart}"
    else:
        document_class = r"\documentclass[a4paper,11pt]{article}"

    abstract_block = ""
    if abstract.strip():
        abstract_block = (
            "\n\\begin{abstract}\n" + abstract.strip() + "\n\\end{abstract}\n"
        )
    bibliography = ""
    if bib_name:
        # 正文里的引用是“作者 年份, paperId, pN”这样的自然语言标注，pandoc
        # 不会把它转成 \cite；用 \nocite{*} 把自动生成的文献条目全部列出，
        # 至少保证 PDF 有一份完整、可核对的参考文献表。
        # 参考文献单独起页，避免从正文末页底部挤进来又跨页。
        cite_all = r"\nocite{*}" + "\n" if nocite else ""
        bibliography = (
            f"\n\\clearpage"
            f"\n\\bibliographystyle{{plainnat}}"
            f"\n{cite_all}"
            f"\n\\bibliography{{{bib_name}}}\n"
        )

    return "\n".join(
        [
            document_class,
            r"\usepackage[margin=2.5cm]{geometry}",
            r"\usepackage{amsmath,amssymb,amsfonts}",
            r"\usepackage{graphicx}",
            r"\usepackage{booktabs,longtable,array,multirow}",
            r"\usepackage{float}",
            r"\usepackage{xcolor}",
            r"\usepackage{url}",
            r"\usepackage{calc}",  # pandoc 的表格列宽用 \real{0.25}，需要 calc
            r"\usepackage[hidelinks]{hyperref}",
            # 部分 TeX Live 精简安装缺 pzdr.tfm，hyperref 在 XeTeX 下会因此在
            # \begin{document} 报错；链接本身不受影响，直接跳过这段字体设置。
            r"\makeatletter",
            r"\renewcommand\Hy@setup@XeTeXLinkBox@dvipdfmx{}",
            r"\makeatother",
            r"\usepackage{natbib}",
            r"\usepackage{newunicodechar}",
            # 正文里的 μ/α 等希腊字母在 Latin Modern 里没有字形，转成数学符号。
            r"\newunicodechar{α}{\ensuremath{\alpha}}",
            r"\newunicodechar{β}{\ensuremath{\beta}}",
            r"\newunicodechar{γ}{\ensuremath{\gamma}}",
            r"\newunicodechar{δ}{\ensuremath{\delta}}",
            r"\newunicodechar{ε}{\ensuremath{\varepsilon}}",
            r"\newunicodechar{θ}{\ensuremath{\theta}}",
            r"\newunicodechar{λ}{\ensuremath{\lambda}}",
            r"\newunicodechar{μ}{\ensuremath{\mu}}",
            r"\newunicodechar{σ}{\ensuremath{\sigma}}",
            r"\newunicodechar{τ}{\ensuremath{\tau}}",
            r"\newunicodechar{φ}{\ensuremath{\varphi}}",
            r"\newunicodechar{χ}{\ensuremath{\chi}}",
            r"\newunicodechar{ω}{\ensuremath{\omega}}",
            r"\newunicodechar{≥}{\ensuremath{\geq}}",
            r"\newunicodechar{≤}{\ensuremath{\leq}}",
            r"\setcounter{secnumdepth}{0}",
            # 综述正文不需要运行页眉，只保留页码，避免参考文献页顶部出现章节名。
            r"\pagestyle{plain}",
            r"\providecommand{\tightlist}{\setlength{\itemsep}{0pt}\setlength{\parskip}{0pt}}",
            r"\providecommand{\pandocbounded}[1]{#1}",
            f"\\title{{{escape_latex(title or 'Untitled')}}}",
            f"\\author{{{escape_latex(author)}}}",
            r"\date{\today}",
            "",
            r"\begin{document}",
            r"\maketitle",
            abstract_block.rstrip(),
            "",
            body.strip(),
            bibliography.rstrip(),
            "",
            r"\end{document}",
            "",
        ]
    )


_PAPER_ID_INLINE = re.compile(r"\bp_[0-9a-f]{10,16}\b")
_CJK_QUOTED = re.compile(r'"([^"\n]{1,200})"')
_LATEX_CJK_QUOTE = re.compile(r"``([^`\n]{1,200})''")


def _normalize_cjk_quotes(text: str) -> str:
    """导出前把中文语境里的成对 ASCII 引号换成中文引号。

    pandoc 的 smart 对紧贴中文的 ``"x"`` 有时不触发，原样留下去在 PDF 里会变成
    两个右引号，所以在 Markdown 阶段先自己配对。
    """
    if '"' not in text or not has_cjk(text):
        return text

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        if CJK_RE.search(inner):
            return "“" + inner + "”"
        return match.group(0)

    return _CJK_QUOTED.sub(replace, text)


def _restore_cjk_quotes(fragment: str) -> str:
    """把 pandoc 输出的 ```x''`` 还原成中文引号。

    pandoc 的 LaTeX writer 会把 Unicode 引号统一写成 ```x''``；在 CJK 语境里
    渲染出来是两个右引号。这里只还原内含中文的引号，英文原样保留。
    """
    if "``" not in fragment:
        return fragment

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        if CJK_RE.search(inner):
            return "“" + inner + "”"
        return match.group(0)

    return _LATEX_CJK_QUOTE.sub(replace, fragment)


def _strip_paper_ids(
    text: str, labels: dict[str, str] | None = None
) -> str:
    """去掉正文引用里的内部 paperId，并清理多余标点。

    给了 ``labels``（paperId → “作者 年份”）时，把裸 paperId 换成可读的
    作者-年份标注；若同一处已经写了作者或年份，则只删掉 paperId。
    """

    if "p_" not in text:
        return text

    def replace(match: re.Match[str]) -> str:
        label = (labels or {}).get(match.group(0), "").strip()
        if not label:
            return ""
        prefix = text[max(0, match.start() - 48) : match.start()]
        parts = label.split()
        surname = parts[0] if parts else ""
        year = parts[-1] if parts else ""
        if (surname and surname in prefix) or (
            year.isdigit() and year in prefix
        ):
            return ""
        return label

    cleaned = _PAPER_ID_INLINE.sub(replace, text)
    cleaned = re.sub(r"[,，]\s*[,，]", "，", cleaned)
    cleaned = re.sub(r"([（(])\s*[,，]\s*", r"\1", cleaned)
    return cleaned


def build_tex(
    markdown: str,
    *,
    title: str = "",
    author: str = "",
    abstract: str = "",
    template: str = "auto",
    bib_name: str | None = None,
    nocite: bool = False,
    citation_labels: dict[str, str] | None = None,
) -> tuple[str, str, str]:
    """把 Markdown 或完整 .tex 转成可编译的 LaTeX 文档。

    返回 ``(tex, style, error)``；``style`` 是实际使用的模板名。
    """
    text = (markdown or "").strip()
    if not text:
        return "", "", "没有可导出的正文：请传 content，或 source 指向项目里的文稿。"

    # 正文里的“（Li 2026, p_5c0ab2865c69, p1）”只是内部审计标注，
    # 导出时去掉 paperId，避免读者在 PDF 里看到内部主键。
    text = _strip_paper_ids(text, citation_labels)
    text = _normalize_cjk_quotes(text)
    if bib_name and "\\cite" not in text:
        # 自然语言引用不会变成 \cite，靠 \nocite{*} 把 bib 里的条目列出来。
        nocite = True

    style = (template or "auto").strip().lower()
    if style not in STYLES:
        style = "ctexart" if has_cjk(text) else "article"

    # 模型直接写好的完整 .tex 原样保留，只补编译所需的样式判断。
    if "\\documentclass" in text[:4000]:
        declared = "ctexart" if "{ctexart}" in text[:4000] else "article"
        return text + ("" if text.endswith("\n") else "\n"), declared, ""

    body_md, derived_title = _split_title(text, title)
    fragment, error = _markdown_to_fragment(body_md, natbib=bool(bib_name))
    if error:
        return "", style, error
    fragment = _restore_cjk_quotes(fragment)

    tex = _wrap_document(
        fragment,
        title=derived_title,
        author=author,
        abstract=abstract,
        style=style,
        bib_name=bib_name,
        nocite=nocite,
    )
    return tex, style, ""


def _error_excerpt(log: str, limit: int = 1200) -> str:
    lines = [
        line
        for line in log.splitlines()
        if line.startswith("!") or line.startswith("l.") or "Error" in line
    ]
    excerpt = "\n".join(lines[-12:]) if lines else log[-limit:]
    return excerpt[-limit:].strip() or "（无日志输出）"


def _cleanup_aux(tex_path: Path) -> None:
    for suffix in AUX_SUFFIXES:
        (tex_path.parent / f"{tex_path.stem}{suffix}").unlink(missing_ok=True)


def _compile_with(engine: str, tex_path: Path) -> tuple[Path | None, str]:
    """用单个引擎编译；失败时清理半成品，返回 ``(pdf, error)``。"""
    workdir = tex_path.parent
    pdf = tex_path.with_suffix(".pdf")
    pdf.unlink(missing_ok=True)
    _cleanup_aux(tex_path)
    timeout = 90 if "lualatex" in Path(engine).stem.lower() else COMPILE_TIMEOUT
    logs: list[str] = []

    def run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            command,
            cwd=workdir,
            capture_output=True,
            timeout=timeout,
            **hidden_kwargs(),
        )

    try:
        first = run(
            [engine, "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", tex_path.name]
        )
        logs.append((first.stdout + first.stderr).decode("utf-8", "replace"))
        if first.returncode != 0:
            pdf.unlink(missing_ok=True)
            _cleanup_aux(tex_path)
            return None, _error_excerpt(logs[-1])

        # 有参考文献时跑 bibtex，再补两遍编译让交叉引用稳定下来。
        if any(workdir.glob("*.bib")):
            bibtex = find_bibtex()
            if bibtex:
                bib = run([bibtex, tex_path.stem])
                logs.append((bib.stdout + bib.stderr).decode("utf-8", "replace"))
        for _ in range(2):
            last = run(
                [engine, "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", tex_path.name]
            )
            logs.append((last.stdout + last.stderr).decode("utf-8", "replace"))
            if last.returncode != 0:
                break
    except (OSError, subprocess.TimeoutExpired) as exc:
        pdf.unlink(missing_ok=True)
        _cleanup_aux(tex_path)
        return None, str(exc)

    if pdf.is_file() and pdf.stat().st_size > 0:
        _cleanup_aux(tex_path)
        return pdf, ""
    pdf.unlink(missing_ok=True)
    _cleanup_aux(tex_path)
    return None, _error_excerpt(logs[-1] if logs else "")


def compile_tex(tex_path: Path) -> tuple[Path | None, str, str]:
    """在 ``.tex`` 所在目录编译 PDF，返回 ``(pdf, engine, error)``。

    按 xelatex → pdflatex → lualatex 顺序尝试：某些 Windows 环境下
    xelatex 的 xdvipdfmx 会崩溃，pdflatex 能编译 ctexart 作为回退。
    """
    engines = find_engines()
    if not engines:
        return (
            None,
            "",
            "未检测到 LaTeX 引擎（xelatex / pdflatex / lualatex）。"
            "安装 TeX Live 或 MiKTeX 后即可自动编译 PDF。",
        )
    if not tex_path.is_file():
        return None, engines[0], f"找不到要编译的 .tex：{tex_path.name}"

    errors: list[str] = []
    for engine in engines:
        pdf, error = _compile_with(engine, tex_path)
        if pdf is not None:
            return pdf, engine, ""
        errors.append(f"{Path(engine).stem}: {error}")
    return None, "", "LaTeX 编译失败：" + "；".join(errors)[:1200]


def bib_stem(directory: Path) -> str | None:
    """目录里有 .bib 时返回第一个文件名（不含扩展名），供模板生成参考文献段。"""
    for path in sorted(directory.glob("*.bib")):
        return path.stem
    return None


def _probe_version(command: list[str], timeout: int = 8) -> str:
    try:
        proc = subprocess.run(
            command, capture_output=True, timeout=timeout, **hidden_kwargs()
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    raw = (proc.stdout or proc.stderr).decode("utf-8", "replace").strip()
    return raw.splitlines()[0] if raw else ""


def find_winget() -> str | None:
    found = shutil.which("winget") or shutil.which("winget.exe")
    if found:
        return found
    # WindowsApps 下的应用执行别名不在部分进程的 PATH 里，单独找一次。
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "Microsoft" / "WindowsApps" / "winget.exe"
        if candidate.exists():
            return str(candidate)
    return None


def toolchain_status() -> dict[str, Any]:
    """设置页用的写作工具自检：pandoc + TeX 引擎 + bibtex。"""
    pandoc = find_pandoc()
    engines = []
    seen: set[str] = set()
    for path in find_engines():
        name = Path(path).stem.lower()
        if name in seen:
            continue
        seen.add(name)
        engines.append(
            {"name": name, "path": path, "version": _probe_version([path, "--version"])}
        )
    bibtex = find_bibtex()
    winget = find_winget()
    missing = []
    if not pandoc:
        missing.append("pandoc")
    if not engines:
        missing.append("latex")
    return {
        "pandoc": {
            "found": bool(pandoc),
            "path": pandoc or "",
            "version": _probe_version([pandoc, "--version"]) if pandoc else "",
        },
        "engines": engines,
        "bibtex": {
            "found": bool(bibtex),
            "path": bibtex or "",
            "version": _probe_version([bibtex, "--version"]) if bibtex else "",
        },
        "installer": {"winget": bool(winget), "package": "MiKTeX.MiKTeX"},
        "ready": bool(pandoc and engines),
        "missing": missing,
    }
