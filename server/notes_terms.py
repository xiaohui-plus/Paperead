"""笔记检索的查询扩展：中文自动展开成相关英文短语/单词。

笔记目录只有英文标题与英文领域 slug，中文查询直接匹配命中率很低。这里沿用
论文检索的思路（多个概念、任一命中、英文按词边界比较），先把中文查询按领域
词典切成概念，再给每个概念配一组英文候选。命中判定规则：

- 任一概念或候选命中即算命中（OR）；
- 英文按完整单词比较，不做字母级子串匹配（``gan`` 不会命中 ``organ``）；
- 单字符英文候选直接忽略，避免把字母级噪声混进结果。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CJK_RE = re.compile(r"[\u3400-\u9fff]")
_EN_RE = re.compile(r"[a-z0-9][a-z0-9\-+_.]*")
_PUNCT_RE = re.compile(r"[\s,，、;；:：/|()（）\[\]【】<>《》\"'“”‘’!！?？。.、·]+")

# 英文停用词：参与匹配会把几乎所有标题都带进来。
_EN_STOP = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "into", "is", "it", "of", "on", "or", "the", "to", "via", "with",
    "without", "under", "over", "towards", "toward", "using", "based",
    "what", "when", "where", "which", "who", "how", "why", "this", "that",
    "these", "those", "its", "their", "our", "your", "we", "you", "they",
}

# 中文里只起连接作用、不表达检索意图的字词。
_ZH_FILLER = {
    "的", "了", "在", "与", "和", "及", "中", "等", "之", "对", "从",
    "为", "把", "被", "是", "有", "个", "篇", "里", "上", "下",
    "关于", "如何", "什么", "怎么", "怎样", "哪些", "最新", "进展",
    "方法", "研究", "论文", "笔记", "工作", "技术", "系统", "相关",
}
_ZH_FILLER_PREFIX = ("的", "了", "在", "与", "和", "及", "中", "等", "之", "对", "从")

# 中文概念 → 英文候选短语/单词。key 支持被更长的 key 覆盖（最长匹配优先）。
ZH_TERMS: dict[str, tuple[str, ...]] = {
    # 大模型与语言
    "大语言模型": ("llm", "llms", "large language model", "large language models", "language model"),
    "大模型": ("llm", "large model", "large language model", "foundation model"),
    "语言模型": ("language model", "language models", "lm"),
    "自然语言处理": ("nlp", "natural language processing", "natural language"),
    "智能体": ("agent", "agents", "agentic"),
    "多智能体": ("multi agent", "multiagent", "multi agent system", "multi agent systems"),
    "推理": ("reasoning", "reason", "inference", "infer"),
    "评测": ("evaluation", "evaluate", "benchmark", "assessment", "assess"),
    "评估": ("evaluation", "evaluate", "benchmark", "assessment"),
    "预训练": ("pretraining", "pre training", "pretrain", "pretrained"),
    "幻觉": ("hallucination", "hallucinate", "hallucinated", "factual"),
    "知识编辑": ("knowledge editing", "model editing"),
    "对话": ("dialogue", "dialog", "conversational", "chat"),
    "文本生成": ("text generation", "generation", "generative"),
    "机器翻译": ("machine translation", "translation", "mt"),
    "多语言": ("multilingual", "multi lingual", "cross lingual", "cross-lingual"),
    "代码智能": ("code", "coding", "program", "programming"),
    "对齐": ("alignment", "align", "aligned", "preference"),
    "偏好优化": ("preference optimization", "dpo", "ppo", "rlhf", "preference"),
    "强化学习": ("reinforcement learning", "rl", "policy gradient"),
    "模仿学习": ("imitation learning", "imitation", "behavior cloning"),
    "世界模型": ("world model", "world models"),
    "具身智能": ("embodied", "embodiment", "embodied ai"),
    "规划": ("planning", "plan", "planner"),
    "工具调用": ("tool use", "tool", "tools", "function calling"),
    "记忆": ("memory", "memorization", "memorize"),
    "长上下文": ("long context", "context length", "context window"),
    "上下文学习": ("in context learning", "in-context learning", "icl"),
    "提示": ("prompt", "prompts", "prompting"),
    "提示工程": ("prompt engineering", "prompt", "prompting"),
    "提示注入": ("prompt injection", "injection"),
    "思维链": ("chain of thought", "chain-of-thought", "cot"),
    "少样本": ("few shot", "few-shot", "fewshot", "low resource"),
    "小样本": ("few shot", "few-shot", "fewshot", "low resource"),
    "零样本": ("zero shot", "zero-shot", "zeroshot"),
    "微调": ("fine tuning", "fine-tuning", "finetune", "sft"),
    "参数高效微调": ("peft", "lora", "adapter", "efficient tuning"),
    "越狱": ("jailbreak", "jailbreaking"),
    "红队": ("red team", "red teaming", "red-teaming"),
    "拒答": ("refusal", "refuse", "refusal"),
    "位置编码": ("position encoding", "positional encoding", "rope", "position embedding"),
    "混合专家": ("mixture of experts", "moe", "sparse"),
    # 生成与多模态
    "多模态": ("multimodal", "multi modal", "vision language", "vlm"),
    "多模态大模型": ("multimodal large language model", "mllm", "vlm", "lmms"),
    "视觉语言模型": ("vision language model", "vlm", "vlms", "vision-language"),
    "图像生成": ("image generation", "image synthesis", "text to image", "t2i"),
    "视频生成": ("video generation", "video synthesis", "text to video", "t2v"),
    "图像编辑": ("image editing", "editing", "edit"),
    "可控生成": ("controllable generation", "controllable", "controlnet", "control"),
    "个性化": ("personalization", "personalized", "customization", "customized"),
    "风格迁移": ("style transfer", "style"),
    "扩散模型": ("diffusion", "diffusion model", "diffusion models", "ddpm", "stable diffusion"),
    "扩散": ("diffusion", "denoising diffusion", "generative"),
    "生成对抗网络": ("gan", "gans", "generative adversarial", "adversarial"),
    "变分自编码器": ("vae", "autoencoder", "auto encoder", "variational"),
    "自编码器": ("autoencoder", "auto encoder", "vae"),
    "超分辨率": ("super resolution", "super-resolution", "upscale", "upsampling"),
    "图像修复": ("image restoration", "restoration", "inpainting", "denoising"),
    "去噪": ("denoising", "denoise", "noise"),
    "去雾": ("dehazing", "haze"),
    "去雨": ("deraining", "rain"),
    "低光": ("low light", "low-light", "lowlight"),
    "语音": ("speech", "audio", "voice"),
    "音频": ("audio", "speech", "acoustic"),
    "语音识别": ("speech recognition", "asr"),
    "语音合成": ("text to speech", "tts", "speech synthesis"),
    "说话人": ("speaker", "speakers", "speaker diarization"),
    "音乐": ("music", "musical"),
    "数字人": ("digital human", "avatar", "talking head"),
    "视频理解": ("video understanding", "video"),
    # 视觉与 3D
    "三维视觉": ("3d vision", "3d", "three dimensional"),
    "三维": ("3d", "three dimensional", "3d reconstruction"),
    "三维重建": ("3d reconstruction", "reconstruction", "nerf", "gaussian splatting"),
    "神经辐射场": ("nerf", "neural radiance field", "radiance field"),
    "高斯泼溅": ("gaussian splatting", "gaussian", "splatting"),
    "点云": ("point cloud", "point clouds"),
    "网格": ("mesh", "meshes"),
    "语义分割": ("segmentation", "semantic segmentation", "segment"),
    "实例分割": ("instance segmentation", "segmentation", "segment"),
    "目标检测": ("object detection", "detection", "detector", "detect"),
    "人体理解": ("human understanding", "human pose", "pose estimation", "human"),
    "姿态估计": ("pose estimation", "pose", "keypoint"),
    "动作识别": ("action recognition", "action", "activity recognition"),
    "跟踪": ("tracking", "track", "mot"),
    "光流": ("optical flow", "flow"),
    "深度估计": ("depth estimation", "depth"),
    "立体视觉": ("stereo", "stereo vision", "binocular"),
    "人脸": ("face", "facial", "faces"),
    "手势": ("gesture", "hand"),
    "遥感": ("remote sensing", "satellite", "aerial"),
    "自动驾驶": ("autonomous driving", "self driving", "driving", "vehicle", "bev"),
    # 安全与可信
    "人工智能安全": ("ai safety", "safety", "safe"),
    "安全": ("safety", "safe", "secure", "security"),
    "可解释性": ("interpretability", "explainability", "explainable", "interpretable", "xai"),
    "可解释": ("interpretability", "explainability", "explainable", "interpretable", "xai"),
    "生成内容检测": ("aigc detection", "ai generated", "generated image detection", "deepfake"),
    "伪造检测": ("forgery detection", "forgery", "deepfake", "manipulation detection"),
    "异常检测": ("anomaly detection", "anomaly", "outlier", "out of distribution", "ood"),
    "对抗攻击": ("adversarial attack", "adversarial", "attack"),
    "对抗样本": ("adversarial example", "adversarial examples", "adversarial"),
    "鲁棒性": ("robustness", "robust"),
    "后门": ("backdoor", "backdoors", "trojan"),
    "投毒": ("poisoning", "poison", "data poisoning"),
    "水印": ("watermark", "watermarking", "watermarked"),
    "隐私": ("privacy", "private", "differential privacy"),
    "公平性": ("fairness", "fair", "bias", "debiasing"),
    "偏见": ("bias", "biased", "debiasing"),
    "不确定性": ("uncertainty", "uncertain", "calibration"),
    "校准": ("calibration", "calibrated", "calibrate"),
    # 基础与理论
    "强化学习与决策": ("reinforcement learning", "rl", "decision"),
    "决策": ("decision", "decision making", "policy"),
    "奖励": ("reward", "rewards"),
    "策略": ("policy", "policies"),
    "离线强化学习": ("offline reinforcement learning", "offline rl", "offline"),
    "学习理论": ("learning theory", "theory", "theoretical", "generalization"),
    "优化": ("optimization", "optimizer", "convergence"),
    "自监督": ("self supervised", "self-supervised", "ssl", "contrastive"),
    "对比学习": ("contrastive learning", "contrastive", "simclr", "moco"),
    "表示学习": ("representation learning", "representation", "representation"),
    "嵌入": ("embedding", "embeddings", "embed"),
    "图学习": ("graph learning", "graph", "gnn"),
    "图神经网络": ("graph neural network", "gnn", "graph neural networks"),
    "因果推断": ("causal inference", "causal", "causality"),
    "联邦学习": ("federated learning", "federated", "privacy"),
    "物理": ("physics", "physical", "simulation"),
    "科学计算": ("scientific computing", "scientific", "simulation", "numerical"),
    "迁移学习": ("transfer learning", "transfer"),
    "持续学习": ("continual learning", "continual", "lifelong learning"),
    "元学习": ("meta learning", "meta-learning", "few shot"),
    "主动学习": ("active learning", "active"),
    "半监督": ("semi supervised", "semi-supervised", "semi supervised learning"),
    "无监督": ("unsupervised", "unsupervised learning"),
    "弱监督": ("weakly supervised", "weakly-supervised", "weak supervision"),
    "域适应": ("domain adaptation", "domain shift"),
    "域泛化": ("domain generalization", "domain shift"),
    "长尾": ("long tail", "long-tailed", "longtail", "imbalanced"),
    "不平衡": ("imbalanced", "imbalance", "class imbalance"),
    "数据增强": ("data augmentation", "augmentation", "augment"),
    "数据合成": ("synthetic data", "data synthesis", "synthetic"),
    "标注": ("annotation", "annotations", "labeling", "labelling"),
    # 应用与交叉
    "医学影像": ("medical imaging", "medical image", "radiology", "mri", "ct"),
    "医学自然语言处理": ("medical nlp", "clinical", "biomedical", "healthcare"),
    "计算生物学": ("computational biology", "bioinformatics", "protein", "molecule", "drug"),
    "药物": ("drug", "drugs", "molecule", "molecular"),
    "分子": ("molecule", "molecular", "molecules"),
    "蛋白质": ("protein", "proteins", "protein structure"),
    "基因": ("gene", "genes", "genomic", "genomics"),
    "单细胞": ("single cell", "single-cell", "scrna"),
    "地球科学": ("earth science", "climate", "weather", "geoscience"),
    "天气": ("weather", "climate", "forecasting"),
    "气候": ("climate", "climate change", "weather"),
    "时间序列": ("time series", "temporal", "forecasting", "forecast"),
    "信号与通信": ("signal", "communication", "wireless", "channel"),
    "推荐系统": ("recommender", "recommendation", "recsys", "collaborative filtering"),
    "推荐": ("recommendation", "recommender", "recsys"),
    "社会计算": ("social computing", "social"),
    "信息检索": ("information retrieval", "retrieval", "rag", "search"),
    "检索增强": ("retrieval augmented", "retrieval-augmented", "rag", "retrieval augmentation"),
    "检索": ("retrieval", "retrieve", "search"),
    "排序": ("ranking", "rank", "reranking", "re-ranking"),
    "广告": ("advertising", "ads", "ctr", "click through rate"),
    "用户建模": ("user modeling", "user model", "user"),
    "模型压缩": ("model compression", "compression", "pruning", "quantization", "distillation"),
    "知识蒸馏": ("knowledge distillation", "distillation", "distill"),
    "蒸馏": ("distillation", "distill", "distilled"),
    "量化": ("quantization", "quantize", "quantized"),
    "剪枝": ("pruning", "prune", "pruned"),
    # NLP 任务与模型结构
    "情感分析": ("sentiment analysis", "sentiment", "emotion"),
    "情感": ("emotion", "sentiment", "emotional"),
    "命名实体识别": ("named entity recognition", "ner", "named entity"),
    "关系抽取": ("relation extraction", "relation", "relationship"),
    "问答": ("question answering", "qa", "question answer"),
    "摘要": ("summarization", "summary", "summarize"),
    "文本分类": ("text classification", "classification", "classifier"),
    "纠错": ("error correction", "correction", "grammatical error"),
    "Transformer": ("transformer", "transformers"),
    "注意力": ("attention", "attentions"),
    "自注意力": ("self attention", "self-attention", "attention"),
    "效率": ("efficiency", "efficient", "acceleration", "accelerate", "latency", "throughput"),
    "加速": ("acceleration", "accelerate", "efficient", "speedup"),
    "稀疏": ("sparse", "sparsity", "sparsification"),
    "数据集": ("dataset", "datasets", "data"),
    "基准": ("benchmark", "benchmarks", "evaluation"),
    "自回归": ("autoregressive", "autoregression", "autoregressive model"),
    "世界知识": ("world knowledge", "knowledge"),
    "文生图": ("text to image", "t2i", "image generation"),
    "文生视频": ("text to video", "t2v", "video generation"),
    "视觉问答": ("visual question answering", "vqa", "visual question answer"),
    "视频问答": ("video question answering", "videoqa", "video qa"),
    "图像理解": ("image understanding", "image comprehension", "visual understanding"),
    "图像分类": ("image classification", "classification", "classifier"),
    "图像分割": ("image segmentation", "segmentation", "segment"),
    "点云分割": ("point cloud segmentation", "point cloud"),
    "跨模态": ("cross modal", "cross-modal", "crossmodal", "multimodal"),
    "图文检索": ("image text retrieval", "text image retrieval", "cross modal retrieval"),
    "知识图谱": ("knowledge graph", "knowledge graphs", "kg"),
    "机器遗忘": ("machine unlearning", "unlearning"),
    "指令微调": ("instruction tuning", "instruction fine tuning", "instruction following", "sft"),
    "人类反馈": ("human feedback", "rlhf", "preference"),
    "泛化": ("generalization", "generalize", "generalisation"),
    "多标签": ("multi label", "multi-label", "multilabel"),
    "开放词汇": ("open vocabulary", "open-vocabulary", "openvocab"),
    "长文本": ("long text", "long document", "long context"),
    "模型窃取": ("model stealing", "model extraction"),
    "版权": ("copyright", "copyrighted"),
    "对抗训练": ("adversarial training", "adversarial"),
    "提示学习": ("prompt learning", "prompt tuning", "prompt"),
    "语音分离": ("speech separation", "source separation", "speech enhancement"),
}

# 按首字分组，避免每个字符都遍历整本词典。
_BY_FIRST: dict[str, list[str]] = {}
for _key in ZH_TERMS:
    _BY_FIRST.setdefault(_key[0], []).append(_key)
for _keys in _BY_FIRST.values():
    _keys.sort(key=len, reverse=True)


@dataclass(frozen=True)
class Concept:
    """一个检索概念：中文原词，或英文单词/短语。"""

    source: str
    terms: tuple[str, ...]

    @property
    def chinese(self) -> bool:
        return bool(CJK_RE.search(self.source))


@dataclass(frozen=True)
class QueryPlan:
    raw: str
    concepts: tuple[Concept, ...]

    @property
    def empty(self) -> bool:
        return not self.concepts

    def to_dict(self) -> dict[str, object]:
        terms: list[dict[str, str]] = []
        seen: set[str] = set()
        for concept in self.concepts:
            for term in concept.terms:
                if term in seen:
                    continue
                seen.add(term)
                terms.append({"term": term, "source": concept.source})
        return {
            "raw": self.raw,
            "concepts": [concept.source for concept in self.concepts],
            "terms": terms,
        }


def normalize(text: str) -> str:
    """英文转小写、把标点/下划线/连字符统一成空格，并保留中文。

    返回带首尾空格的字符串，英文候选可以直接用 ``" term " in hay`` 做
    整词判定；中英相邻处补空格（“LLM推理” → “llm 推理”）。
    """
    text = str(text or "").lower()
    text = re.sub(r"([a-z0-9])([\u3400-\u9fff])", r"\1 \2", text)
    text = re.sub(r"([\u3400-\u9fff])([a-z0-9])", r"\1 \2", text)
    text = re.sub(r"[^0-9a-z\u3400-\u9fff]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return f" {text} " if text else " "


def normalized_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    """把候选统一成小写、标点转空格的形式；中文候选随之保持不变。"""
    out: list[str] = []
    for term in terms:
        value = normalize(term).strip()
        if value and value not in out:
            out.append(value)
    return tuple(out)


def term_hit(term: str, hay: str) -> bool:
    """词边界命中：中文按子串，英文按完整单词/短语。

    ``term`` 须是 ``normalized_terms`` 处理过的结果，查询时就不必反复
    规范化同一个候选（2 万条目录下这是主要耗时点）。
    """
    probe = str(term).strip()
    if not probe:
        return False
    if CJK_RE.search(probe):
        return probe in hay
    if len(probe) < 2:
        return False
    return f" {probe} " in hay


def _strip_filler(text: str) -> str:
    changed = True
    while changed and text:
        changed = False
        for filler in _ZH_FILLER_PREFIX:
            if text.startswith(filler) and len(text) > len(filler):
                text = text[len(filler) :]
                changed = True
    return text


def _zh_concepts(segment: str) -> list[Concept]:
    concepts: list[Concept] = []
    buffer = ""

    def flush() -> None:
        nonlocal buffer
        piece = _strip_filler(buffer.strip())
        buffer = ""
        if not piece:
            return
        if piece in _ZH_FILLER or len(piece) < 1:
            return
        concepts.append(Concept(source=piece, terms=normalized_terms((piece,))))

    index = 0
    while index < len(segment):
        chunk = segment[index]
        if _PUNCT_RE.match(chunk):
            flush()
            index += 1
            continue
        matched = ""
        for key in _BY_FIRST.get(chunk, ()):
            if segment.startswith(key, index):
                matched = key
                break
        if matched:
            flush()
            concepts.append(
                Concept(source=matched, terms=normalized_terms(ZH_TERMS[matched]))
            )
            index += len(matched)
        else:
            buffer += chunk
            index += 1
    flush()
    return concepts


def _en_concepts(text: str) -> list[Concept]:
    concepts: list[Concept] = []
    seen: set[str] = set()
    for token in _EN_RE.findall(text.lower()):
        token = token.strip("-_.")
        if len(token) < 2 or token in _EN_STOP or token in seen:
            continue
        seen.add(token)
        concepts.append(Concept(source=token, terms=normalized_terms((token,))))
    return concepts


def expand_query(query: str) -> QueryPlan:
    """把查询拆成概念；中文部分补齐英文候选，英文部分保留原词。"""
    raw = str(query or "").strip()
    if not raw:
        return QueryPlan(raw=raw, concepts=())

    pieces = [piece for piece in _PUNCT_RE.split(raw) if piece]
    concepts: list[Concept] = []
    seen: set[str] = set()

    def add(items: list[Concept]) -> None:
        for item in items:
            if item.source in seen:
                continue
            seen.add(item.source)
            concepts.append(item)

    for piece in pieces:
        if CJK_RE.search(piece):
            add(_zh_concepts(piece))
            # 中文串里夹的英文同样纳入（“RAG 检索增强”）。
            add(_en_concepts(piece))
        else:
            add(_en_concepts(piece))

    if not concepts:
        # 全是填充词或单字符英文时，退回原查询，避免“搜了但没搜”。
        concepts.append(Concept(source=raw, terms=normalized_terms((raw,))))
    return QueryPlan(raw=raw, concepts=tuple(concepts))