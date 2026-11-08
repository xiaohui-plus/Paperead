"""论文理解层：导读、前置知识、脉络、溯源。

从「读完」到「搞懂」——见 docs/design-tutor.md。
"""

from .concepts import Concept, extract
from .guide import build_guide

__all__ = ["Concept", "extract", "build_guide"]
