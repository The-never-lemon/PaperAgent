from __future__ import annotations

import asyncio
import time
from datetime import datetime
from xml.etree import ElementTree as ET

import httpx

from ..models import PaperDocument, SearchRequest
from .base import PaperSearchConnector


class ArxivPaperConnector(PaperSearchConnector):
    """arXiv connector。

    中文说明：
    负责把结构化检索意图（concept_groups + topic）拼成 arXiv Atom API 可接受的查询表达式。
    arXiv 的布尔语法：AND / OR / ANDNOT 必须大写，多词短语必须用双引号，空格是隐式 OR（反直觉！）。
    具体查询拼装规则不再暴露给上层 Agent。

    arXiv ToU 要求每次请求间隔 ≥3 秒且单连接。这里用类级锁 + 时间戳强制实现：
    - 同步 search：用 time.sleep
    - 异步 async_search：用 asyncio.sleep
    避免多源并发时触发 429 限流。
    """

    source_name = "arxiv"
    _endpoint = "https://export.arxiv.org/api/query"
    _atom_ns = {"atom": "http://www.w3.org/2005/Atom"}
    # 中文说明：arXiv ToU 要求每次请求间隔 ≥3 秒。类级时间戳用于强制串行化。
    _last_request_time: float = 0.0
    _sync_lock = __import__('threading').Lock()
    _async_lock: asyncio.Lock | None = None

    def __init__(self, client: httpx.Client | None = None):
        """初始化 HTTP 客户端。"""

        self.headers = {
            "User-Agent": "papers-agents/0.1 paper-retrieval",
            "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
        }
        self.client = client or httpx.Client(
            timeout=20.0,
            headers=self.headers,
        )

    def _enforce_sync_spacing(self) -> None:
        """同步版本：强制请求间隔 ≥3 秒（arXiv ToU）。"""

        with self._sync_lock:
            now = time.monotonic()
            elapsed = now - self.__class__._last_request_time
            if elapsed < 3.0:
                time.sleep(3.0 - elapsed)
            self.__class__._last_request_time = time.monotonic()

    async def _enforce_async_spacing(self) -> None:
        """异步版本：强制请求间隔 ≥3 秒（arXiv ToU）。"""

        # 中文说明：asyncio.Lock 必须在一个有事件循环的上下文里创建，
        # 所以这里用懒初始化（第一次调用时创建）。
        if self.__class__._async_lock is None:
            self.__class__._async_lock = asyncio.Lock()
        async with self.__class__._async_lock:
            now = time.monotonic()
            elapsed = now - self.__class__._last_request_time
            if elapsed < 3.0:
                await asyncio.sleep(3.0 - elapsed)
            self.__class__._last_request_time = time.monotonic()

    def search(self, request: SearchRequest) -> list[PaperDocument]:
        """执行 arXiv 检索，并在 connector 内完成查询拼装。"""

        # 中文说明：强制 3 秒串行门（arXiv ToU）。
        self._enforce_sync_spacing()
        query = self._render_concept_groups(request)
        response = self.client.get(
            self._endpoint,
            params={
                "search_query": query,
                "start": 0,
                # 中文说明：max_results 用 request.limit（service 层已经做了超量放大）。
                # arXiv 单次请求上限 2000，实测 >200 响应会很慢，这里加个软上限。
                "max_results": max(1, min(request.limit, 2000)),
                "sortBy": "relevance",
                "sortOrder": "descending",
            },
        )
        response.raise_for_status()
        return self._parse_response_text(response.text, request)

    async def async_search(
        self,
        request: SearchRequest,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[PaperDocument]:
        """异步执行 arXiv 检索，避免在异步编排里阻塞事件循环。"""

        # 中文说明：强制 3 秒串行门（arXiv ToU）。
        await self._enforce_async_spacing()
        query = self._render_concept_groups(request)
        resolved_client = client or httpx.AsyncClient(timeout=20.0)
        owns_client = client is None
        try:
            response = await resolved_client.get(
                self._endpoint,
                params={
                    "search_query": query,
                    "start": 0,
                    "max_results": max(1, min(request.limit, 2000)),
                    "sortBy": "relevance",
                    "sortOrder": "descending",
                },
                headers=self.headers,
                timeout=20.0,
            )
        finally:
            if owns_client:
                await resolved_client.aclose()
        response.raise_for_status()
        return self._parse_response_text(response.text, request)

    def _parse_response_text(self, text: str, request: SearchRequest) -> list[PaperDocument]:
        """把 arXiv XML 响应解析成论文列表，同步和异步入口共用。

        中文说明：
        年份已经在服务端通过 submittedDate:[...] 下推了，这里保留兜底校验。
        排除词在查询串里已经通过 ANDNOT 下推了，不再做客户端重复过滤。
        """

        root = ET.fromstring(text)
        papers: list[PaperDocument] = []
        for entry in root.findall("atom:entry", self._atom_ns):
            paper = self.normalize_paper(entry)
            if paper is None:
                continue
            if not self._within_year_range(paper, request):
                continue
            papers.append(paper)
        return papers[: request.limit]

    def _render_concept_groups(self, request: SearchRequest) -> str:
        """把结构化概念组渲染成 arXiv API 的查询串。

        中文说明：
        arXiv 的布尔语法（来自官方文档 + 实测）：
        - AND / OR / ANDNOT 必须大写，小写会被当普通词
        - 多词短语必须用双引号（%22 编码，httpx 的 params={} 会自动处理）
        - 空格是隐式 OR（反直觉！），所以多词短语不加引号会被拆成 OR
        - 默认字段是 all:（元数据范围，不是全文）
        - 字段前缀放在括号前会分配到组内每个项：all:(a OR b) = (all:a OR all:b)

        渲染规则：每个概念组是一个 parenthesized OR of quoted phrases，组间用 AND 连接。
        实测这种写法对同一意图返回 count=584，旧版自由布尔式返回 6540（precision 修复）。
        """

        groups = request.concept_groups
        if not groups:
            # 中文说明：没有任何概念组时，用 topic 兜底（虽然不精确，总比返回 0 篇好）。
            if request.topic.strip():
                words = [w.strip() for w in request.topic.split() if w.strip()]
                if words:
                    return " AND ".join(f'all:"{w}"' for w in words)
            return "all:*"

        # 中文说明：每个概念组渲染成 (all:"term1" OR all:"term2" OR ...)。
        # 所有项都加引号（单字词加不加引号行为一样，统一加可避免一类 bug）。
        rendered_groups: list[str] = []
        for group in groups:
            if not group:
                continue
            terms = " OR ".join(f'all:"{term}"' for term in group if str(term).strip())
            if terms:
                rendered_groups.append(f"({terms})")

        if not rendered_groups:
            return "all:*"

        # 中文说明：组间用 AND 连接（必须同时命中）。
        query = " AND ".join(rendered_groups)

        # 中文说明：追加排除词（ANDNOT）。
        if request.excluded_terms:
            exclusions = " OR ".join(f'all:"{t}"' for t in request.excluded_terms if str(t).strip())
            if exclusions:
                query = f"{query} ANDNOT ({exclusions})"

        # 中文说明：追加年份过滤（服务端下推）。
        # 格式：submittedDate:[YYYYMMDDHHMM TO YYYYMMDDHHMM]，GMT 时间，分钟精度，
        # TO 必须大写，方括号会被 httpx 自动 URL 编码。
        if request.year_from is not None or request.year_to is not None:
            year_from = request.year_from or 1990
            year_to = request.year_to or datetime.now().year
            query = f"{query} AND submittedDate:[{year_from:04d}01010000 TO {year_to:04d}12312359]"

        return query

    def normalize_paper(self, raw: object) -> PaperDocument | None:
        """把单个 Atom entry 解析成统一论文对象。"""

        if not isinstance(raw, ET.Element):
            return None
        entry = raw
        title = self._text(entry, "atom:title")
        if not title:
            return None
        paper_id = self._text(entry, "atom:id").rsplit("/", 1)[-1]
        authors = [author_name.text.strip() for author_name in entry.findall("atom:author/atom:name", self._atom_ns) if author_name.text]
        summary = self._text(entry, "atom:summary")
        published_text = self._text(entry, "atom:published")
        published_year = self._parse_year(published_text)
        pdf_url = ""
        doi = ""
        for link in entry.findall("atom:link", self._atom_ns):
            href = (link.attrib.get("href") or "").strip()
            title_attr = (link.attrib.get("title") or "").strip().lower()
            link_type = (link.attrib.get("type") or "").strip().lower()
            if link_type == "application/pdf" and href:
                pdf_url = href
            if title_attr == "doi" and href:
                doi = href.rsplit("/", 1)[-1]
        unique_id = doi or paper_id
        return PaperDocument(
            id=unique_id or title,
            paperId=unique_id,
            title=title,
            authors=authors,
            abstract=summary,
            year=published_year,
            venue="arXiv",
            url=self._text(entry, "atom:id"),
            pdf_url=pdf_url or None,
            doi=doi or None,
            source=self.source_name,
            publication_date=published_text,
            journal_conference="arXiv",
            language="en",
            metadata={"published": published_text, "arxiv_id": paper_id},
        )

    def _text(self, entry: ET.Element, path: str) -> str:
        """安全读取 XML 文本节点。"""

        node = entry.find(path, self._atom_ns)
        return node.text.strip() if node is not None and node.text else ""

    def _parse_year(self, value: str) -> int | None:
        """从发布时间中提取年份。"""

        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).year
        except ValueError:
            return None

    def _within_year_range(self, paper: PaperDocument, request: SearchRequest) -> bool:
        """按年份范围过滤结果（服务端已下推，这里是兜底校验）。"""

        if paper.year is None:
            return True
        if request.year_from is not None and paper.year < request.year_from:
            return False
        if request.year_to is not None and paper.year > request.year_to:
            return False
        return True
