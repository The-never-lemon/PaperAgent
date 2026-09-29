# 文件作用：汇总 arXiv、OpenAlex 和 Semantic Scholar 三个论文检索连接器。
from .arxiv import ArxivPaperConnector
from .base import PaperMetadataNormalizer, PaperSearchConnector
from .openalex import OpenAlexPaperConnector
from .semantic_scholar import SemanticScholarPaperConnector

__all__ = [
    "ArxivPaperConnector",
    "OpenAlexPaperConnector",
    "PaperMetadataNormalizer",
    "PaperSearchConnector",
    "SemanticScholarPaperConnector",
]
