"""LaTeX 导出：把项目里的 Markdown 稿转成可编译的 ``.tex``，并按需编译 PDF。

Pandoc 只负责 Markdown → LaTeX 片段；中文/英文模板、参考文献与编译流程都在
本模块里完成，保证用户说“用 LaTeX 写”时项目自身就有可用的能力：

- 中文稿默认 ``ctexart``（xelatex 编译），英文稿默认 ``article``；
- 已有完整 ``.tex``（含 ``\\documentclass``）时原样保留，只负责编译；
- 本机没有 pandoc 或 TeX 引擎时返回可读的错误，由调用方决定是降级还是提示。

这里只做本地转换与编译，不联网、不启用 ``--shell-escape``。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

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


def find_pandoc() -> str | None:
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
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", f"pandoc 执行失败：{exc}"
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace")[:400]
        return "", f"pandoc 转换失败：{detail}"
    return proc.stdout.decode("utf-8", "replace"), ""


def _wrap_document(
    body: str,
    *,
    title: str,
    author: str,
    abstract: str,
    style: str,
    bib_name: str | None,
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
        bibliography = (
            f"\n\\bibliographystyle{{plainnat}}"
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
            r"\usepackage{natbib}",
            r"\providecommand{\tightlist}{\setlength{\itemsep}{0pt}\setlength{\parskip}{0pt}}",
            r"\providecommand{\pandocbounded}[1]{#1}",
            r"\providecommand{\real}[1]{#1}",
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


def build_tex(
    markdown: str,
    *,
    title: str = "",
    author: str = "",
    abstract: str = "",
    template: str = "auto",
    bib_name: str | None = None,
) -> tuple[str, str, str]:
    """把 Markdown 或完整 .tex 转成可编译的 LaTeX 文档。

    返回 ``(tex, style, error)``；``style`` 是实际使用的模板名。
    """
    text = (markdown or "").strip()
    if not text:
        return "", "", "没有可导出的正文：请传 content，或 source 指向项目里的文稿。"

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

    tex = _wrap_document(
        fragment,
        title=derived_title,
        author=author,
        abstract=abstract,
        style=style,
        bib_name=bib_name,
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
        return subprocess.run(command, cwd=workdir, capture_output=True, timeout=timeout)

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
