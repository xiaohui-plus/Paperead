"""综述质量基线：把草稿和“目标综述”的差距变成 agent 可执行的补料清单。

引用审计只回答“引用是否可核对”，这里再回答“离一篇标准综述还差什么”：

- 骨架：摘要 / 引言 / 方法 / 讨论 / 结论 + 至少 3 个主题小节；
- 证据：本地全文来源 >= 5 篇、每个主题小节 >= 2 篇、页码级引用率 >= 60%；
- 引用：无未解析 paperId、无无引用论断；
- 规模：总引用 >= 15 处。

项目里放了 ``review-template.md``（或 ``综述模板.md``）时，用它的二级标题作为
必需骨架，覆盖默认 5 段式，方便按用户自己的模板（如 5A 模板）检查。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

TEMPLATE_NAMES = ("review-template.md", "综述模板.md", "综述模板.MD")

# (id, 展示名, 匹配关键词)。关键词按“标题包含”匹配，忽略大小写与编号前缀。
REQUIRED_SECTIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("abstract", "摘要", ("摘要", "abstract")),
    ("introduction", "引言/背景", ("引言", "背景", "introduction")),
    ("methods", "方法/检索策略", ("方法", "检索策略", "纳入与排除", "methods", "search strategy")),
    ("discussion", "讨论/局限", ("讨论", "局限", "争议", "discussion", "limitation")),
    ("conclusion", "结论/展望", ("结论", "展望", "conclusion", "future direction")),
)

MIN_TOPIC_SECTIONS = 3
MIN_SECTION_SOURCES = 2
MIN_FULLTEXT_SOURCES = 5
MIN_CITATIONS = 15
MIN_PAGE_RATIO = 0.6

_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
_NUMBER_PREFIX = re.compile(r"^[（(]?[0-9一二三四五六七八九十]+[、.．)）]\s*")
_MARKUP = re.compile(r"[*_`：:，,。.、\s]+")

# 系统内置的固定模板。用户在项目里选定后，会落成 files/review-template.md，
# 之后的引用审计与写作都按它检查；不选则用 DEFAULT 五段式基线。
def _section(
    title: str,
    words: str,
    points: list[str],
    checks: list[str],
) -> dict[str, Any]:
    return {
        "title": title,
        "required": True,
        "words": words,
        "points": points,
        "checks": checks,
    }


def _render_markdown(name: str, tagline: str, outline: list[dict[str, Any]]) -> str:
    """把结构化约束渲染成模板 Markdown。

    二级标题保持为章节名，供 ``sections_from_markdown`` 抽取骨架；标题下的
    “篇幅 / 必写 / 自检”列表是给写作者和 agent 看的详细约束。
    """
    lines = [f"# {name}", "", f"> {tagline}", ""]
    for item in outline:
        lines.append(f"## {item['title']}")
        lines.append("")
        if item.get("words"):
            lines.append(f"- 篇幅：{item['words']}")
        if item.get("points"):
            lines.append("- 必写：")
            lines.extend(f"  - {point}" for point in item["points"])
        if item.get("checks"):
            lines.append("- 自检：")
            lines.extend(f"  - {check}" for check in item["checks"])
        lines.append("")
    return "\n".join(lines)


# 通用综述：保留原来的五段式骨架，但补上每节该写什么、写多少、怎么检查。
_DEFAULT_OUTLINE = [
    _section(
        "摘要",
        "200–300 字",
        [
            "背景：一句话说明问题为何重要。",
            "目的：本文要回答的核心问题与范围。",
            "方法：检索来源、纳排标准与综合方式（叙述或定量）。",
            "结果：主要发现 2–3 条，尽量给出方向与强度，不只写“综述了”。",
            "结论：对研究或实践的含义，并点明证据边界。",
        ],
        ["不出现引用、图表和未定义缩写", "结果与结论能对应正文，不夸大证据"],
    ),
    _section(
        "引言与研究背景",
        "3–5 段",
        [
            "领域现状与重要性：这项问题影响谁、为什么现在需要综述。",
            "已有工作：按主题概括代表性研究，给出关键结论与分歧。",
            "研究空白：现有综述缺什么（范围、时效、方法、人群）。",
            "本文范围与问题：明确纳入边界、目标读者和要回答的问题。",
        ],
        ["引用来自真实文献并标注作者-年份或编号", "不把背景写成教科书章节"],
    ),
    _section(
        "方法与检索策略",
        "1–3 段 + 可选检索表",
        [
            "数据来源：数据库、预印本平台、灰色文献与手工检索。",
            "检索式：完整关键词、布尔逻辑、限定条件与检索日期。",
            "纳入与排除标准：研究对象、设计、时间、语言、结局。",
            "筛选与提取：几轮筛选、是否双人独立、分歧如何解决。",
            "质量与综合：偏倚评估工具、叙述综合或定量合并方法。",
        ],
        ["换一个研究者能按本节复现检索", "数量与日期前后一致"],
    ),
    _section(
        "主题章节",
        "至少 3 节；每节 2–5 段",
        [
            "每节只回答一个主题问题，标题直接写主题而不是“研究进展”。",
            "每节至少 2 篇不同来源互证；先给结论，再给证据与机制。",
            "区分“一致结论 / 存在争议 / 证据不足”三种状态。",
            "有图表时在正文先引用，再解释图表回答了什么。",
        ],
        ["每节来源不少于 2 篇", "没有证据的论断标【待补证据】或删除"],
    ),
    _section(
        "讨论与局限",
        "2–4 段",
        [
            "汇总证据：哪些结论稳定，哪些只在特定人群/场景成立。",
            "与既有综述比较：更新了什么、修正了什么、仍存争议什么。",
            "局限：检索范围、发表偏倚、异质性、证据等级。",
            "启示：对研究设计、临床/工程实践或政策的可操作建议。",
        ],
        ["局限具体到本研究，不写套话", "不引入前文没有的证据"],
    ),
    _section(
        "结论与展望",
        "1–2 段",
        [
            "回答引言提出的问题，给出 2–3 条主要结论。",
            "说明证据强度与适用边界。",
            "提出可执行的未来研究方向或实践建议。",
        ],
        ["不重复摘要原句", "不出现新数据或新引用"],
    ),
]

# 实证研究论文：IMRaD 扩展，面向有数据、有实验的原创论文。
_PAPER_OUTLINE = [
    _section(
        "标题与作者",
        "标题 ≤ 20 字；作者信息 1 段",
        [
            "标题写清研究对象、方法/干预与主要结论，避免“初探/浅析/思考”这类空泛词。",
            "作者按贡献排序，列出单位、通讯作者与基金信息。",
        ],
        ["标题能看出研究类型与核心变量", "作者单位与邮箱完整"],
    ),
    _section(
        "摘要",
        "250–300 字（结构化）",
        [
            "背景：1 句说明问题与缺口。",
            "目的：1 句写研究目标或假设。",
            "方法：数据来源、样本量、研究设计、主要结局与统计方法。",
            "结果：2–3 句给出关键数值（均值/比例、效应量、95% CI 或 p 值）。",
            "结论：1 句写主要发现与适用边界，不夸大因果。",
        ],
        ["不含引用、图表、未定义缩写", "结果数字与正文表格一致"],
    ),
    _section(
        "关键词",
        "3–6 个",
        [
            "覆盖研究对象、方法、核心变量与应用场景。",
            "避免“研究/分析/问题”等泛词。",
        ],
        ["与标题和摘要用词一致"],
    ),
    _section(
        "引言",
        "4–6 段",
        [
            "研究背景：领域现状与重要性，引用 5–10 篇近 5 年文献。",
            "问题与空白：已有研究哪里不足、矛盾或未覆盖，明确 gap。",
            "研究目标与假设：给出研究问题；有假设时写成可检验形式（H1/H2）。",
            "主要贡献：不超过 3 条，逐条对应方法与实验结果。",
            "论文结构：可选，用 1 段说明后续章节安排。",
        ],
        ["gap 有文献依据，不是主观判断", "贡献不写成“本文很重要”"],
    ),
    _section(
        "相关工作",
        "2–4 段",
        [
            "按主题或方法路线组织，不按时间流水账罗列。",
            "与最近 2–3 年的代表性工作对比，说明本文差异与改进点。",
            "每段至少 2 篇来源，避免只引 1 篇就下结论。",
        ],
        ["不是文献清单", "与引言重复的内容已合并"],
    ),
    _section(
        "方法",
        "按小标题分节，可含公式与框架图",
        [
            "数据/材料：来源、规模、纳入排除、训练/验证/测试划分、伦理审批或知情同意。",
            "任务与符号：形式化定义问题，列出关键符号表。",
            "模型/算法：整体框架图、核心公式、复杂度与实现细节。",
            "实验设置：基线、超参数、硬件、评价指标、统计检验方法。",
            "可复现性：代码/数据可用性、随机种子、依赖版本。",
        ],
        ["他人能按本节复现实验", "符号定义前后一致"],
    ),
    _section(
        "实验与结果",
        "按实验组组织；图表齐全",
        [
            "数据统计：样本量、类别分布、划分与缺失值处理。",
            "主实验：与至少 3 个基线对比；报告均值±方差、显著性检验与效应量。",
            "消融实验：逐模块验证贡献，固定其他条件。",
            "鲁棒性/泛化/效率：至少覆盖一项（跨数据集、噪声、少样本、耗时/显存）。",
            "误差分析：给出失败案例与原因，不只报最优结果。",
            "图表规范：正文先引用后出现；图注自明；坐标轴、单位、样本量齐全。",
        ],
        ["主结果有方差与显著性", "图表与正文数字一致", "不隐藏负面结果"],
    ),
    _section(
        "讨论",
        "3–5 段",
        [
            "解释主要发现：为什么会出现这个结果，机制是什么。",
            "与已有工作对比：哪些结论一致、哪些矛盾，可能原因。",
            "局限：数据偏差、方法假设、外部效度、因果推断边界。",
            "意义：对理论、方法、实践或政策的启示。",
        ],
        ["局限具体，不写“样本量有限”就结束", "不重复结果段落"],
    ),
    _section(
        "结论",
        "1–2 段",
        [
            "回答研究问题，列出 2–3 条主要结论。",
            "说明贡献与适用边界。",
            "给出未来工作方向，不引入新数据。",
        ],
        ["不出现新实验或新引用", "结论强度不超过证据"],
    ),
    _section(
        "附录",
        "按需",
        [
            "补充实验、超参数、数据字典、问卷或额外图表。",
            "附录图表同样编号，并在正文引用。",
        ],
        ["正文未展开的材料才放附录"],
    ),
]

# 叙述性综述：适合机制、临床、交叉学科主题，强调论证而不是穷举。
_NARRATIVE_OUTLINE = [
    _section(
        "摘要",
        "200–300 字",
        [
            "目的：说明综述范围与要回答的问题。",
            "方法：概述文献来源与综合方式（不必像系统综述那样列完整检索式）。",
            "主要观点：2–3 条核心结论。",
            "结论：对研究或实践的含义与证据边界。",
        ],
        ["不引用、不写未定义缩写", "结论与正文主题一致"],
    ),
    _section(
        "引言与研究背景",
        "3–5 段",
        [
            "领域重要性：问题影响谁、为什么值得现在综述。",
            "概念界定：核心概念、人群/对象、时间范围。",
            "已有观点与争议：代表性研究结论及分歧。",
            "综述目标与结构：本文按什么框架展开、目标读者是谁。",
        ],
        ["概念定义清晰", "范围边界明确"],
    ),
    _section(
        "概念框架与分类",
        "1–2 段 + 框架图/表",
        [
            "给出分析框架：按机制、场景、证据类型或时间阶段分类。",
            "说明分类维度为什么合理，避免“为了分类而分类”。",
            "图表要能独立读懂，正文先引用后解释。",
        ],
        ["分类维度互斥且覆盖主要文献", "图表有来源说明"],
    ),
    _section(
        "机制与证据",
        "按主题分 3–6 节",
        [
            "先解释机制或理论路径，再给支持与反对证据。",
            "每节至少 2 篇不同来源；动物/体外/人群证据分开表述。",
            "给出证据强度：一致、部分支持、争议、证据不足。",
            "避免把相关性写成因果。",
        ],
        ["每节来源 ≥ 2 篇", "不混用不同证据等级"],
    ),
    _section(
        "争议与局限",
        "2–4 段",
        [
            "列出关键争议与相反结果，分析方法学差异。",
            "指出发表偏倚、样本选择、测量方式、随访时长等局限。",
            "明确哪些问题现有证据无法回答。",
        ],
        ["争议双方都有代表文献", "不把“没有研究”写成“没有效果”"],
    ),
    _section(
        "结论与展望",
        "1–2 段",
        [
            "给出 2–3 条可操作结论，区分确定与不确定。",
            "提出未来研究问题与方法建议。",
        ],
        ["不重复摘要原句", "不引入新证据"],
    ),
]

# 系统综述：按 PRISMA 2020 组织，强调检索、筛选与偏倚评估可复现。
_SYSTEMATIC_OUTLINE = [
    _section(
        "摘要",
        "250–350 字（结构化）",
        [
            "背景：1 句说明临床/实践问题。",
            "目的：写清 PICO 问题与综述目标。",
            "方法：数据库、检索日期、纳排标准、数据提取、偏倚评估与综合方法。",
            "结果：纳入研究数、样本量、主要结局效应量及 95% CI。",
            "结论：证据确定性与实践含义；附注册号。",
        ],
        ["含注册号（PROSPERO/OSF）", "结果数字与 PRISMA 流程图、森林图一致"],
    ),
    _section(
        "引言",
        "3–5 段",
        [
            "背景：疾病/问题负担与现有实践的不确定性。",
            "既有综述：已有哪些综述、结论是否一致、缺什么。",
            "研究问题：用 PICO/PICOS 写清人群、干预、对照、结局、研究设计。",
            "目标：本综述要解决的具体问题。",
        ],
        ["研究问题可直接转成检索式", "不把背景写成教科书"],
    ),
    _section(
        "方法",
        "按 PRISMA 条目分小标题",
        [
            "方案与注册：注册平台、编号、与方案的偏离。",
            "纳排标准：PICOS、语言、时间、研究设计、样本量要求。",
            "检索策略：数据库/平台、完整检索式、检索日期、灰色文献与手工检索。",
            "筛选流程：双人独立筛选、分歧解决、全文获取。",
            "数据提取：提取表字段、双人核对、缺失数据处理。",
            "偏倚评估：RoB 2 / ROBINS-I / AMSTAR 2 等工具与评级规则。",
            "证据综合：叙述或 Meta 分析、异质性 I²、模型选择、亚组与敏感性分析、GRADE。",
        ],
        ["检索式可复现", "筛选与提取流程可审计", "偏离方案要说明"],
    ),
    _section(
        "结果",
        "按 PRISMA 结果条目组织",
        [
            "检索与筛选：报告识别、去重、标题摘要筛选、全文筛选、最终纳入数量，配 PRISMA 流程图。",
            "纳入研究特征：表格列出国家、设计、样本量、干预、对照、结局与随访。",
            "偏倚风险：逐研究评级，配汇总图/表。",
            "主要结局：效应量、95% CI、异质性与模型；配森林图。",
            "次要结局/亚组/敏感性：说明预设假设与探索性分析的区别。",
            "证据确定性：用 GRADE 给出每个结局的等级与降级理由。",
        ],
        ["PRISMA 各阶段数字对得上", "不选择性报告阳性结果"],
    ),
    _section(
        "讨论",
        "4–6 段",
        [
            "证据总结：主要发现与确定性，先回答研究问题。",
            "与既有综述比较：结论一致/矛盾及原因。",
            "机制与解释：结果为什么成立，适用哪些人群/场景。",
            "局限：检索限制、发表偏倚、异质性、研究质量、外部效度。",
            "意义：对指南、临床/工程实践与未来研究的建议。",
        ],
        ["不重复结果表", "局限与 GRADE 降级理由对应"],
    ),
    _section(
        "结论",
        "1–2 段",
        [
            "给出有证据边界的结论，不用“显著有效”替代效应量。",
            "写清对实践与研究的含义。",
        ],
        ["结论强度与 GRADE 一致"],
    ),
    _section(
        "附录",
        "按需",
        [
            "完整检索式、排除研究清单、数据提取表、偏倚评估明细。",
            "附录编号与正文引用一致。",
        ],
        ["主文无法容纳的材料才放附录"],
    ),
]


def _template(
    name: str,
    kind: str,
    description: str,
    tagline: str,
    outline: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "description": description,
        "outline": outline,
        "markdown": _render_markdown(name, tagline, outline),
    }


DEFAULT_TEMPLATE: dict[str, Any] = _template(
    "通用综述（五段式）",
    "review",
    "摘要 / 引言 / 方法 / 主题章节 / 讨论 / 结论的标准综述骨架，适合大多数选题。",
    "适用：还没有确定投稿格式，或需要一个稳妥的综述起点。",
    _DEFAULT_OUTLINE,
)

# 系统内置模板：论文 1 套、综述 2 套。id 保持与旧版兼容。
BUILTIN_TEMPLATES: dict[str, dict[str, Any]] = {
    "paper-empirical": _template(
        "实证研究论文（IMRaD）",
        "paper",
        "结构化摘要、引言、相关工作、方法、实验与结果、讨论、结论；面向有数据与实验的原创论文。",
        "适用：有明确研究问题、数据与实验结果的原创研究论文；章节按 IMRaD 扩展。",
        _PAPER_OUTLINE,
    ),
    "narrative": _template(
        "叙述性综述",
        "review",
        "按概念框架—机制—证据—争议—展望展开，强调论证与证据分级，适合机制、临床与交叉学科主题。",
        "适用：需要解释机制、梳理争议，而不是穷举全部文献的综述。",
        _NARRATIVE_OUTLINE,
    ),
    "systematic": _template(
        "系统综述（PRISMA 2020）",
        "review",
        "按 PRISMA 2020 组织检索、纳排、筛选、偏倚评估与证据综合，检索与筛选过程可复现。",
        "适用：需要交代完整检索策略、纳排标准与偏倚风险的系统综述或 Meta 分析。",
        _SYSTEMATIC_OUTLINE,
    ),
}

ALL_TEMPLATES: dict[str, dict[str, Any]] = {
    "default": DEFAULT_TEMPLATE,
    **BUILTIN_TEMPLATES,
}


def list_templates(files_dir: Path) -> list[dict[str, Any]]:
    """可选模板：系统默认、内置固定模板、用户上传到项目的模板。"""
    items: list[dict[str, Any]] = [
        {
            "id": "default",
            "name": DEFAULT_TEMPLATE["name"],
            "source": "default",
            "kind": DEFAULT_TEMPLATE["kind"],
            "description": DEFAULT_TEMPLATE["description"],
            "outline": DEFAULT_TEMPLATE["outline"],
            "sections": sections_from_markdown(DEFAULT_TEMPLATE["markdown"]),
        }
    ]
    for template_id, meta in BUILTIN_TEMPLATES.items():
        items.append(
            {
                "id": template_id,
                "name": meta["name"],
                "source": "builtin",
                "kind": meta["kind"],
                "description": meta["description"],
                "outline": meta["outline"],
                "sections": sections_from_markdown(meta["markdown"]),
            }
        )
    for path in _uploaded_templates(files_dir):
        items.append(
            {
                "id": f"file:{path.name}",
                "name": path.stem,
                "source": "upload",
                "kind": "",
                "description": "项目内上传的模板",
                "outline": [],
                "sections": template_sections(path),
            }
        )
    return items


def _uploaded_templates(files_dir: Path) -> list[Path]:
    found: list[Path] = []
    for name in TEMPLATE_NAMES:
        candidate = files_dir / name
        if candidate.is_file():
            found.append(candidate)
    if files_dir.is_dir():
        for candidate in files_dir.iterdir():
            if not candidate.is_file() or candidate.suffix.lower() != ".md":
                continue
            stem = candidate.stem.lower()
            if ("模板" in candidate.stem or "template" in stem) and candidate not in found:
                found.append(candidate)
    return found


def resolve_template(
    choice: str | None,
    files_dir: Path,
    external: "Callable[[str], tuple[list[str], str, str]] | None" = None,
) -> tuple[list[str] | None, str, str]:
    """按“显式选择 → 项目上传 → 系统默认”解析模板，返回 (小节, 名称, 错误)。

    ``external`` 用来解析应用级设置里的 ``upload:`` / ``paper:`` 选择，
    由服务端注入，避免本模块依赖文献库。
    """
    value = (choice or "").strip()
    if value and value not in {"default", "auto"}:
        if value in BUILTIN_TEMPLATES:
            meta = BUILTIN_TEMPLATES[value]
            return sections_from_markdown(meta["markdown"]), meta["name"], ""
        if external is not None and value.split(":", 1)[0] in {"upload", "paper"}:
            return external(value)
        name = value[5:] if value.startswith("file:") else value
        for path in _uploaded_templates(files_dir):
            if path.name == name or path.stem == name:
                return template_sections(path), path.name, ""
        return [], value, f"没有找到模板：{value}"
    uploaded = find_template(files_dir)
    if uploaded is not None:
        return template_sections(uploaded), uploaded.name, ""
    return None, "default-5-section", ""


def _clean_title(value: str) -> str:
    text = _NUMBER_PREFIX.sub("", value.strip())
    return _MARKUP.sub("", text).lower()


def find_template(files_dir: Path) -> Path | None:
    uploaded = _uploaded_templates(files_dir)
    return uploaded[0] if uploaded else None


def template_sections(path: Path) -> list[str]:
    """从模板 Markdown 里取二级标题，作为必需骨架。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return sections_from_markdown(text)


def sections_from_markdown(text: str) -> list[str]:
    titles: list[str] = []
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match and len(match.group(1)) == 2:
            title = match.group(2).strip()
            if title and "参考文献" not in title:
                titles.append(title)
    return titles


def _draft_headings(text: str) -> list[str]:
    titles: list[str] = []
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            titles.append(match.group(2).strip())
    return titles


def _contains(heading: str, keywords: tuple[str, ...]) -> bool:
    cleaned = _clean_title(heading)
    return any(_clean_title(keyword) in cleaned for keyword in keywords)


def evaluate(
    text: str,
    audit: dict[str, Any],
    template: Path | None = None,
    *,
    sections: list[str] | None = None,
    template_label: str = "",
    kind: str = "review",
) -> dict[str, Any]:
    """对照基线给草稿打分，返回检查项、差距与下一步动作。

    ``kind="paper"`` 时按论文模板检查：保留章节与引用完整性，
    不套用综述的“主题小节 / 页码级引用率”门槛。
    """
    is_paper = kind == "paper"
    headings = _draft_headings(text)
    metrics = audit.get("metrics") or {}
    section_reports = audit.get("sections") or []

    required: list[tuple[str, str, tuple[str, ...]]] = list(REQUIRED_SECTIONS)
    custom = sections if sections is not None else (template_sections(template) if template else [])
    if custom:
        mapped = []
        for title in custom:
            keywords = (title,)
            for _check_id, _label, known in REQUIRED_SECTIONS:
                if _contains(title, known):
                    keywords = tuple(dict.fromkeys((title, *known)))
                    break
            mapped.append((f"custom:{title}", title, keywords))
        required = mapped

    checks: list[dict[str, Any]] = []
    missing: list[str] = []
    for check_id, label, keywords in required:
        found = any(_contains(heading, keywords) for heading in headings)
        if not found:
            missing.append(label)
        checks.append(
            {
                "id": f"section:{check_id}",
                "label": f"章节「{label}」",
                "current": "有" if found else "缺失",
                "target": "必须存在",
                "passed": found,
                "hint": "" if found else f"补写「{label}」一节，先给结论再给证据。",
            }
        )

    topic_sections = [
        report
        for report in section_reports
        if report.get("title") not in (None, "", "正文")
        and not any(_contains(str(report.get("title")), known) for _, _, known in REQUIRED_SECTIONS)
    ]
    if not is_paper:
        checks.append(
            {
                "id": "topic-sections",
                "label": "主题小节数量",
                "current": len(topic_sections),
                "target": f">= {MIN_TOPIC_SECTIONS}",
                "passed": len(topic_sections) >= MIN_TOPIC_SECTIONS,
                "hint": ""
                if len(topic_sections) >= MIN_TOPIC_SECTIONS
                else "按“机制 / 临床证据 / 争议 / 落地条件”拆出至少 3 个主题小节。",
            }
        )

        weak = [str(r.get("title")) for r in topic_sections if int(r.get("sources") or 0) < MIN_SECTION_SOURCES]
        checks.append(
            {
                "id": "section-sources",
                "label": "每个主题小节来源数",
                "current": f"{len(topic_sections) - len(weak)}/{len(topic_sections)} 达标"
                if topic_sections
                else "无主题小节",
                "target": f">= {MIN_SECTION_SOURCES} 篇/节",
                "passed": bool(topic_sections) and not weak,
                "hint": "为 " + "、".join(weak[:4]) + " 补检索并写证据卡。" if weak else "",
            }
        )

    def metric_check(check_id: str, label: str, current: float | int, target: str, passed: bool, hint: str) -> None:
        checks.append(
            {
                "id": check_id,
                "label": label,
                "current": current,
                "target": target,
                "passed": passed,
                "hint": "" if passed else hint,
            }
        )

    fulltext = int(metrics.get("fulltextSources") or 0)
    metric_check(
        "fulltext",
        "本地全文来源",
        fulltext,
        f">= {MIN_FULLTEXT_SOURCES} 篇",
        fulltext >= MIN_FULLTEXT_SOURCES,
        f"用 academic_search 找 {MIN_FULLTEXT_SOURCES - fulltext} 篇以上，paper_import 导入后 paper_read 精读。",
    )

    citations_total = int(metrics.get("citationsTotal") or 0)
    metric_check(
        "citations",
        "引用总数",
        citations_total,
        f">= {MIN_CITATIONS} 处",
        citations_total >= MIN_CITATIONS,
        "关键论断逐条补引用：本地写（作者 年份, paperId, p页码），摘要来源写 DOI。",
    )

    page_ratio = float(metrics.get("pageRatio") or 0)
    if not is_paper:
        metric_check(
            "page-ratio",
            "页码级引用率",
            f"{round(page_ratio * 100)}%",
            f">= {round(MIN_PAGE_RATIO * 100)}%",
            page_ratio >= MIN_PAGE_RATIO,
            "对关键论断先 paper_read(paperId, page) 再标页码，不要凭摘要猜页码。",
        )

    unresolved = int(metrics.get("unresolved") or 0)
    metric_check(
        "unresolved",
        "未解析引用",
        unresolved,
        "0",
        unresolved == 0,
        "删除或替换本地库里不存在的 paperId。",
    )

    uncited = int(metrics.get("uncitedClaims") or 0)
    metric_check(
        "uncited",
        "无引用论断",
        uncited,
        "0",
        uncited == 0,
        "给这些论断补引用；确实无证据的改写成【待补证据】。",
    )

    passed_checks = sum(1 for check in checks if check["passed"])
    score = round(passed_checks / len(checks) * 100) if checks else 0
    gaps = [f"{check['label']}：{check['current']}（目标 {check['target']}）" for check in checks if not check["passed"]]
    actions = list(dict.fromkeys(check["hint"] for check in checks if check["hint"]))

    return {
        "template": template_label or (template.name if template else "default-5-section"),
        "kind": kind,
        "score": score,
        "passed": not gaps,
        "checks": checks,
        "missingSections": missing,
        "gaps": gaps,
        "actions": actions,
    }
