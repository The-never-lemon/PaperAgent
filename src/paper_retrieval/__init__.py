# 文件作用：导出论文检索服务和公共数据模型。
from .models import PaperDocument, SearchRequest, SearchResponse
from .service import PaperSearchService

__all__ = [
    "PaperDocument",
    "PaperSearchService",
    "SearchRequest",
    "SearchResponse",
]
