"""文献检索层。

只做「外部学术库检索」这件事，不依赖 FastAPI：
    sources.py  四个数据源的查询构造与结果解析
    http.py     TTL 缓存 / 在途合并 / 重试退避 / arXiv 节流
    search.py   统一检索、详情、开放获取全文、中文查询译英
    daily.py    每日推荐（热度 × 时间衰减 + 分类轮转）
    cite.py     引用格式与 BibTeX / RIS 导出
"""

from __future__ import annotations

from .models import Paper

__all__ = ["Paper"]
