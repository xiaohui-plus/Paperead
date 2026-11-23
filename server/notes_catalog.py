"""在线笔记目录：从 paper-notes 仓库的文件树构建，本地缓存。

不把 2.4 万篇笔记抓进本地库：目录只保留路径与标题（约 24k 行），
正文在用户点开某一篇时才拉取并写入本地缓存。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from server.notes_http import http_get_text
from server.notes_terms import QueryPlan, expand_query, normalize, term_hit

TREE_URL = (
    "https://api.github.com/repos/M0rtzz/paper-notes/git/trees/main?recursive=1"
)
CACHE_TTL = 24 * 3600
_CONF_RE = re.compile(r"^[A-Z]{2,8}\d{4}$")

_THEME_STOP = {
    "with", "from", "into", "towards", "toward", "using", "based", "without",
    "under", "over", "through", "about", "their", "there", "these", "those",
    "that", "this", "what", "when", "where", "which", "while", "your", "have",
    "does", "more", "most", "less", "than", "then", "also", "such", "very",
    "multi", "large", "deep", "self", "cross", "high", "low", "new", "via",
}

# 领域大类：顺序固定，前端按此渲染会议卡片里的分组块。
FIELD_GROUPS: dict[str, tuple[str, ...]] = {
    "LLM 与大模型": (
        "llm_nlp",
        "llm_reasoning",
        "llm_agent",
        "llm_evaluation",
        "llm_efficiency",
        "llm_alignment",
        "llm_pretraining",
        "multi_agent",
        "hallucination",
        "knowledge_editing",
        "dialogue",
        "nlp_understanding",
        "nlp_generation",
        "multilingual_mt",
        "code_intelligence",
    ),
    "生成与多模态": (
        "image_generation",
        "video_generation",
        "multimodal_vlm",
        "vlm_reasoning",
        "vlm_efficiency",
        "image_restoration",
        "audio_speech",
    ),
    "视觉与 3D": (
        "3d_vision",
        "segmentation",
        "object_detection",
        "video_understanding",
        "human_understanding",
        "remote_sensing",
    ),
    "安全与可信": (
        "ai_safety",
        "llm_safety",
        "interpretability",
        "aigc_detection",
        "anomaly_detection",
    ),
    "强化学习与决策": ("reinforcement_learning", "robotics", "autonomous_driving"),
    "基础与理论": (
        "learning_theory",
        "optimization",
        "self_supervised",
        "graph_learning",
        "causal_inference",
        "federated_learning",
        "physics",
        "scientific_computing",
    ),
    "应用与交叉": (
        "medical_imaging",
        "medical_nlp",
        "computational_biology",
        "earth_science",
        "time_series",
        "signal_comm",
        "recommender",
        "social_computing",
        "information_retrieval",
        "model_compression",
        "others",
    ),
}

_ABBREV = {
    "llm": "LLM",
    "vlm": "VLM",
    "3d": "3D",
    "ai": "AI",
    "nlp": "NLP",
    "mt": "MT",
    "aigc": "AIGC",
    "rlhf": "RLHF",
    "vqa": "VQA",
}
_SPECIAL_LABELS = {
    "llm_alignment": "对齐 / RLHF",
    "self_supervised": "自监督",
    "model_compression": "模型压缩",
}

# 领域中文名；会议名保留英文缩写（CVPR2026 等不翻译）。
FIELD_LABELS_ZH = {
    "llm_nlp": "LLM 自然语言处理",
    "llm_reasoning": "LLM 推理",
    "llm_agent": "LLM 智能体",
    "llm_evaluation": "LLM 评测",
    "llm_efficiency": "LLM 效率",
    "llm_alignment": "对齐 / RLHF",
    "llm_pretraining": "预训练",
    "multi_agent": "多智能体",
    "hallucination": "幻觉检测",
    "knowledge_editing": "知识编辑",
    "dialogue": "对话",
    "nlp_understanding": "NLP 理解",
    "nlp_generation": "文本生成",
    "multilingual_mt": "多语言翻译",
    "code_intelligence": "代码智能",
    "image_generation": "图像生成",
    "video_generation": "视频生成",
    "multimodal_vlm": "多模态 VLM",
    "vlm_reasoning": "VLM 推理",
    "vlm_efficiency": "VLM 效率",
    "image_restoration": "图像修复",
    "audio_speech": "语音与音频",
    "3d_vision": "3D 视觉",
    "segmentation": "语义分割",
    "object_detection": "目标检测",
    "video_understanding": "视频理解",
    "human_understanding": "人体理解",
    "remote_sensing": "遥感",
    "ai_safety": "AI 安全",
    "llm_safety": "LLM 安全",
    "interpretability": "可解释性",
    "aigc_detection": "AIGC 检测",
    "anomaly_detection": "异常检测",
    "reinforcement_learning": "强化学习",
    "robotics": "机器人",
    "autonomous_driving": "自动驾驶",
    "learning_theory": "学习理论",
    "optimization": "优化",
    "self_supervised": "自监督",
    "graph_learning": "图学习",
    "causal_inference": "因果推断",
    "federated_learning": "联邦学习",
    "physics": "物理",
    "scientific_computing": "科学计算",
    "medical_imaging": "医学影像",
    "medical_nlp": "医学 NLP",
    "computational_biology": "计算生物学",
    "earth_science": "地球科学",
    "time_series": "时间序列",
    "signal_comm": "信号与通信",
    "recommender": "推荐系统",
    "social_computing": "社会计算",
    "information_retrieval": "信息检索 / RAG",
    "others": "其他",
}


def field_label(field: str) -> str:
    if field in FIELD_LABELS_ZH:
        return FIELD_LABELS_ZH[field]
    if field in _SPECIAL_LABELS:
        return _SPECIAL_LABELS[field]
    words = [word for word in re.split(r"[_\-]+", field) if word]
    return " ".join(_ABBREV.get(word, word[:1].upper() + word[1:]) for word in words)


def _slug_title(slug: str) -> str:
    text = re.sub(r"[_\-]+", " ", slug).strip()
    if not text:
        return slug
    return text[:1].upper() + text[1:]


class NotesCatalog:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "notes"
        self.path = self.root / "catalog.json"
        self.desc_path = self.root / "descriptions.json"
        self._lock = threading.Lock()
        self._desc: dict[str, str] = {}
        self._items: list[dict[str, Any]] = []
        self._updated_at = 0.0
        self._state = "idle"
        self._error = ""
        # 检索用的规范化文本与结果缓存，避免每次分页都重扫 2 万条目录。
        self._hay: dict[str, tuple[str, str, str]] = {}
        self._en_index: dict[str, set[str]] = {}
        self._index_ready = False
        self._index_generation = 0
        self._search_cache: dict[tuple[Any, ...], list[tuple[dict[str, Any], list[str], float]]] = {}
        self._warming = False
        self.load()

    # ── 缓存 ────────────────────────────────────────────

    def load(self) -> None:
        self._hay.clear()
        self._en_index.clear()
        self._index_ready = False
        self._index_generation += 1
        self._search_cache.clear()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        items = data.get("items")
        self._items = items if isinstance(items, list) else []
        self._updated_at = float(data.get("updated_at") or 0)
        self._state = "ready" if self._items else "idle"
        try:
            desc = json.loads(self.desc_path.read_text(encoding="utf-8"))
            if isinstance(desc, dict):
                self._desc = {str(key): str(value) for key, value in desc.items()}
        except (OSError, ValueError):
            self._desc = {}
        if self._items:
            # 首次搜索要扫 2 万条目录，启动后先预热规范化文本，避免第一次搜索卡 1-2 秒。
            self._warm_async()

    def _save(self, items: list[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "source": TREE_URL,
            "items": items,
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    @property
    def stale(self) -> bool:
        return not self._items or time.time() - self._updated_at > CACHE_TTL

    def status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "total": len(self._items),
            "updated_at": self._updated_at,
            "stale": self.stale,
            "error": self._error,
        }

    # ── 刷新 ────────────────────────────────────────────

    def ensure(self) -> dict[str, Any]:
        """目录为空或过期时在后台刷新，立即返回当前状态。"""
        if self._state == "loading":
            return self.status()
        if self._items and not self.stale:
            return self.status()
        self.refresh_async()
        return self.status()

    def refresh_async(self) -> None:
        if self._state == "loading":
            return
        self._state = "loading"
        threading.Thread(target=self._refresh, daemon=True).start()

    def _refresh(self) -> None:
        try:
            items = self._fetch()
            if not items:
                raise RuntimeError("目录为空")
            with self._lock:
                self._items = items
                self._updated_at = time.time()
                self._state = "ready"
                self._error = ""
                self._hay.clear()
                self._en_index.clear()
                self._index_ready = False
                self._index_generation += 1
                self._search_cache.clear()
            self._save(items)
            self._warm_async()
        except Exception as error:  # 网络/限流都只影响目录，不影响已缓存内容
            with self._lock:
                self._state = "error"
                self._error = str(error)

    def _fetch(self) -> list[dict[str, Any]]:
        payload = json.loads(http_get_text(TREE_URL, timeout=60))

        items: list[dict[str, Any]] = []
        for entry in payload.get("tree") or []:
            if entry.get("type") != "blob":
                continue
            path = str(entry.get("path") or "")
            if not path.startswith("docs/") or not path.endswith(".md"):
                continue
            if path.endswith("/index.md"):
                continue
            parts = path.split("/")
            if len(parts) != 4 or not _CONF_RE.fullmatch(parts[1]):
                continue
            slug = parts[3][: -len(".md")]
            items.append(
                {
                    "path": path,
                    "conference": parts[1],
                    "field": parts[2],
                    "slug": slug,
                    "title": _slug_title(slug),
                }
            )
        items.sort(key=lambda item: (item["conference"], item["field"], item["slug"]))
        return items

    # ── 查询 ────────────────────────────────────────────

    def conferences(self) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for item in self._items:
            counts[item["conference"]] = counts.get(item["conference"], 0) + 1
        return [
            {"name": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda pair: -pair[1])
        ]

    def fields(self, conference: str = "") -> list[dict[str, Any]]:
        counts: dict[tuple[str, str], int] = {}
        for item in self._items:
            if conference and item["conference"] != conference:
                continue
            key = (item["conference"], item["field"])
            counts[key] = counts.get(key, 0) + 1
        return [
            {"conference": conf, "name": field, "count": count}
            for (conf, field), count in sorted(
                counts.items(), key=lambda pair: (pair[0][0], -pair[1])
            )
        ]

    def overview(self) -> dict[str, Any]:
        """按「会议 → 大类 → 领域」聚合，供笔记广场的卡片布局使用。"""
        field_to_group = {
            field: group for group, fields in FIELD_GROUPS.items() for field in fields
        }
        group_order = list(FIELD_GROUPS)

        def groups_of(counts: dict[str, int]) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for group in group_order:
                entries = [
                    {"name": field, "label": field_label(field), "count": count}
                    for field, count in counts.items()
                    if field_to_group.get(field, "应用与交叉") == group
                ]
                entries.sort(key=lambda entry: -entry["count"])
                if entries:
                    out.append(
                        {
                            "name": group,
                            "count": sum(entry["count"] for entry in entries),
                            "fields": entries,
                        }
                    )
            return out

        conferences: dict[str, dict[str, Any]] = {}
        all_fields: dict[str, int] = {}
        for item in self._items:
            field = item["field"]
            all_fields[field] = all_fields.get(field, 0) + 1
            bucket = conferences.setdefault(
                item["conference"], {"name": item["conference"], "count": 0, "fields": {}}
            )
            bucket["count"] += 1
            bucket["fields"][field] = bucket["fields"].get(field, 0) + 1

        cards = [
            {
                "name": bucket["name"],
                "count": bucket["count"],
                "fieldCount": len(bucket["fields"]),
                "groups": groups_of(bucket["fields"]),
            }
            for bucket in conferences.values()
        ]
        cards.sort(key=lambda card: -card["count"])

        return {
            "total": len(self._items),
            "conferenceCount": len(conferences),
            "fieldCount": len(all_fields),
            "groups": groups_of(all_fields),
            "conferences": cards,
        }

    def description_of(self, path: str) -> str:
        return self._desc.get(path, "")

    def remember_descriptions(self, mapping: dict[str, str]) -> None:
        fresh = {
            path: text
            for path, text in mapping.items()
            if text and self._desc.get(path) != text
        }
        if not fresh:
            return
        with self._lock:
            self._desc.update(fresh)
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.desc_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._desc, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(self.desc_path)

    def field_across(self, field: str) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        for item in self._items:
            if item["field"] == field:
                counts[item["conference"]] = counts.get(item["conference"], 0) + 1
        return [
            {"name": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda pair: -pair[1])
        ]

    def themes(self, conference: str, field: str, limit: int = 6) -> list[dict[str, Any]]:
        """从标题 slug 里统计高频词，作为该领域的主题提示。"""
        counts: dict[str, int] = {}
        for item in self._items:
            if item["field"] != field:
                continue
            if conference and item["conference"] != conference:
                continue
            for word in re.split(r"[_\-]+", item["slug"]):
                word = word.strip().lower()
                if len(word) < 4 or word in _THEME_STOP:
                    continue
                counts[word] = counts.get(word, 0) + 1
        ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
        return [{"word": word, "count": count} for word, count in ranked[:limit]]

    def _build_index(self, generation: int | None = None) -> None:
        """英文词倒排：短语候选取各单词路径的交集，再由精确匹配过滤。"""
        if generation is None:
            generation = self._index_generation
        index: dict[str, set[str]] = {}
        for row in list(self._items):
            _title, _field, hay = self._hay_of(row)
            path = str(row.get("path") or "")
            for token in set(hay.split()):
                index.setdefault(token, set()).add(path)
        # 构建期间目录被刷新：丢弃旧索引，让新一轮预热接手。
        if generation != self._index_generation:
            return
        self._en_index = index
        self._index_ready = True

    def _warm_async(self) -> None:
        if self._warming or not self._items:
            return
        self._warming = True
        generation = self._index_generation

        def run() -> None:
            try:
                self._build_index(generation)
            finally:
                self._warming = False
                if generation != self._index_generation and self._items:
                    self._warm_async()

        threading.Thread(target=run, daemon=True).start()

    def _hay_of(self, row: dict[str, Any]) -> tuple[str, str, str]:
        """(标题, 领域, 全文) 的规范化文本；英文以空格分隔，可直接整词比较。"""
        path = str(row.get("path") or "")
        cached = self._hay.get(path)
        if cached is not None:
            return cached
        label = field_label(str(row.get("field") or ""))
        title_hay = normalize(f"{row.get('title') or ''} {row.get('slug') or ''}")
        field_hay = normalize(f"{row.get('field') or ''} {label}")
        hay = normalize(
            " ".join(
                [
                    str(row.get("title") or ""),
                    str(row.get("slug") or ""),
                    str(row.get("field") or ""),
                    label,
                    str(row.get("conference") or ""),
                ]
            )
        )
        cached = (title_hay, field_hay, hay)
        self._hay[path] = cached
        return cached

    def _score_row(
        self, plan: QueryPlan, row: dict[str, Any]
    ) -> tuple[str, list[str], float] | None:
        """任一概念/候选命中即返回；按概念覆盖度与命中字段排序。"""
        title_hay, field_hay, hay = self._hay_of(row)
        total = len(plan.concepts) or 1
        hits: list[str] = []
        concept_hits = 0
        title_hits = 0
        field_hits = 0

        for concept in plan.concepts:
            matched = False
            in_title = False
            in_field = False
            for term in concept.terms:
                if term_hit(term, hay):
                    matched = True
                    if term not in hits:
                        hits.append(term)
                if term_hit(term, title_hay):
                    in_title = True
                if term_hit(term, field_hay):
                    in_field = True
            if not matched:
                continue
            concept_hits += 1
            if in_title:
                title_hits += 1
            if in_field:
                field_hits += 1

        if not concept_hits:
            return None
        coverage = concept_hits / total
        score = round(coverage + (title_hits * 0.25 + field_hits * 0.12) / total, 4)
        return str(row.get("path") or ""), hits[:12], score

    def _candidate_rows(self, plan: QueryPlan) -> list[dict[str, Any]] | None:
        """用英文倒排先缩小范围；中文概念仍按目录逐条比较。"""
        if not self._index_ready:
            return None

        paths: set[str] = set()
        chinese = [concept for concept in plan.concepts if concept.chinese]
        for concept in plan.concepts:
            if concept.chinese:
                continue
            for term in concept.terms:
                tokens = term.split()
                if not tokens:
                    continue
                groups = [self._en_index.get(token, set()) for token in tokens]
                if any(not group for group in groups):
                    continue
                hit = set(groups[0])
                for group in groups[1:]:
                    hit &= group
                paths |= hit

        if chinese:
            for row in self._items:
                hay = self._hay_of(row)[2]
                if any(
                    any(term_hit(term, hay) for term in concept.terms)
                    for concept in chinese
                ):
                    paths.add(str(row.get("path") or ""))

        return [row for row in self._items if str(row.get("path") or "") in paths]

    def _search_rows(
        self, plan: QueryPlan, conference: str, field: str
    ) -> list[tuple[dict[str, Any], list[str], float]]:
        key = (
            plan.raw.lower(),
            conference,
            field,
            len(self._items),
            self._updated_at,
        )
        cached = self._search_cache.get(key)
        if cached is not None:
            return cached

        pool = self._candidate_rows(plan)
        if pool is None:
            pool = self._items

        scored: list[tuple[float, str, dict[str, Any], list[str]]] = []
        for row in pool:
            if conference and row["conference"] != conference:
                continue
            if field and row["field"] != field:
                continue
            result = self._score_row(plan, row)
            if result is None:
                continue
            path, hits, score = result
            scored.append((score, path, row, hits))
        scored.sort(key=lambda entry: (-entry[0], entry[1]))

        rows = [(row, hits, score) for score, _path, row, hits in scored]
        if len(self._search_cache) >= 32:
            self._search_cache.clear()
        self._search_cache[key] = rows
        return rows

    def list(
        self,
        conference: str = "",
        field: str = "",
        query: str = "",
        limit: int = 60,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        needle = query.strip()

        payload: dict[str, Any]
        if needle:
            plan = expand_query(needle)
            rows = self._search_rows(plan, conference, field)
            total = len(rows)
            page = rows[offset : offset + limit]
            items = [
                {
                    **row,
                    "label": field_label(row["field"]),
                    "hits": hits,
                    "score": score,
                }
                for row, hits, score in page
            ]
            payload = {"query": plan.to_dict()}
        else:
            rows = self._items
            if conference:
                rows = [row for row in rows if row["conference"] == conference]
            if field:
                rows = [row for row in rows if row["field"] == field]
            total = len(rows)
            page = rows[offset : offset + limit]
            items = [
                {
                    **row,
                    "label": field_label(row["field"]),
                }
                for row in page
            ]
            payload = {}

        return {
            **payload,
            "items": items,
            "matched": total,
            "offset": offset,
            "limit": limit,
        }