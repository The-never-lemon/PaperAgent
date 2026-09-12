from __future__ import annotations

import asyncio
import time

import httpx

from ..models import PaperDocument, SearchRequest
from .base import PaperSearchConnector


class SemanticScholarPaperConnector(PaperSearchConnector):
    """Semantic Scholar connector。

    该 connector 负责把结构化检索意图拼接成 Graph API 查询参数，
    并保留来源内部支持的过滤逻辑。

    中文说明：
    S2 官方建议单 key 1 RPS 限速（实测匿名池更紧）。这里加类级串行门强制 ≥1 秒间隔，
    避免多源并发时把匿名池打爆触发长时间 429。
    """

    source_name = "semantic_scholar"
    # 中文说明：类级串行门，跨实例共享。
    _last_request_time: float = 0.0
    _sync_lock = __import__('threading').Lock()
    _async_lock: asyncio.Lock | None = None
    # 中文说明：Semantic Scholar 有三个搜索端点，行为差异很大，必须选对：
    # - /paper/search         有相关度排序，但完全不支持布尔语法（and/or/括号会被当普通词或停用词，
    #                         实测召回损失 40%~90%），且匿名配额下频繁 429 不可用。
    # - /paper/search/bulk    支持完整布尔语法（+ AND / | OR / - NOT / "短语" / * 前缀 / () 分组），
    #                         没有相关度排序（sort=relevance:desc → HTTP 400），但匿名配额宽松且稳定，
    #                         支持服务端过滤（year / minCitationCount / fieldsOfStudy / openAccessPdf）
    #                         和按 citationCount 排序，作为高精确度检索的主端点最合适。
    # - /paper/search/match   单条标题精确匹配，不适合批量检索。
    # 因此这里切到 bulk 端点；缺失的相关度排序在 Phase 3 用客户端 embedding 重排补。
    _endpoint = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
    # 中文说明：bulk 端点请求的字段清单。相比旧版新增了 5 个关键字段：
    # - citationCount / influentialCitationCount / referenceCount：引用相关数据，
    #   既是重排打分的重要信号（"高引论文优先"），也是服务端 minCitationCount 下推的前提。
    # - corpusId：S2 内部数字 id，构造 /paper/{id}/references 等引用扩展 URL 时要用 CorpusId: 前缀。
    # - s2FieldsOfStudy：论文的研究领域分类，可用于后续按领域过滤（本阶段仅取回备用）。
    _fields = ",".join(
        [
            "title",
            "abstract",
            "year",
            "authors",
            "venue",
            "url",
            "externalIds",
            "openAccessPdf",
            "publicationDate",
            "journal",
            "publicationVenue",
            "citationCount",
            "influentialCitationCount",
            "referenceCount",
            "corpusId",
            "s2FieldsOfStudy",
        ]
    )
    # 中文说明：单次 HTTP 请求的超时秒数。async_related 创建临时 httpx.AsyncClient 时复用。
    _timeout_seconds = 20.0

    def __init__(self, client: httpx.Client | None = None, api_key: str | None = None):
        """初始化 HTTP 客户端，并在配置了密钥时注入 API Key。

        中文说明：
        Semantic Scholar 的密钥通过 x-api-key 请求头传递（注意 header 名大小写敏感，
        官方 swagger.json 原文明确强调过）。配置为 null 时不发送该头，请求按匿名规则执行：
        - /paper/search/bulk 端点的匿名配额明显宽松，实测可稳定使用。
        - /paper/search 端点的匿名池在高峰期基本不可用（实测连续 429 数分钟）。
        """

        headers = {
            "User-Agent": "papers-agents/0.1 paper-retrieval",
            "Accept": "application/json",
        }
        resolved_key = (api_key or "").strip()
        if resolved_key:
            headers["x-api-key"] = resolved_key
        self.headers = headers
        self.client = client or httpx.Client(timeout=self._timeout_seconds, headers=headers)

    def _enforce_sync_spacing(self) -> None:
        """同步版本：强制请求间隔 ≥1 秒（S2 官方建议 1 RPS）。"""

        with self._sync_lock:
            now = time.monotonic()
            elapsed = now - self.__class__._last_request_time
            if elapsed < 1.0:
                time.sleep(1.0 - elapsed)
            self.__class__._last_request_time = time.monotonic()

    async def _enforce_async_spacing(self) -> None:
        """异步版本：强制请求间隔 ≥1 秒（S2 官方建议 1 RPS）。"""

        if self.__class__._async_lock is None:
            self.__class__._async_lock = asyncio.Lock()
        async with self.__class__._async_lock:
            now = time.monotonic()
            elapsed = now - self.__class__._last_request_time
            if elapsed < 1.0:
                await asyncio.sleep(1.0 - elapsed)
            self.__class__._last_request_time = time.monotonic()

    def search(self, request: SearchRequest) -> list[PaperDocument]:
        """执行 Semantic Scholar 检索，并在 connector 内完成查询拼装。"""

        # 中文说明：强制 1 秒串行门（S2 官方建议 1 RPS）。
        self._enforce_sync_spacing()
        response = self.client.get(self._endpoint, params=self._params(request))
        response.raise_for_status()
        return self._parse_payload(response.json(), request)

    async def async_search(
        self,
        request: SearchRequest,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[PaperDocument]:
        """异步执行 Semantic Scholar 检索，避免在异步编排里阻塞事件循环。

        中文说明：
        bulk 端点的响应结构是 {total, data, token}，其中 token 仅在还有后续页时出现。
        本阶段只取第一页（bulk 默认返回 1000 条，对于 limit ≤ 15 的场景已经完全够用）。
        """

        # 中文说明：强制 1 秒串行门（S2 官方建议 1 RPS）。
        await self._enforce_async_spacing()
        resolved_client = client or httpx.AsyncClient(timeout=self._timeout_seconds)
        owns_client = client is None
        try:
            response = await resolved_client.get(
                self._endpoint,
                params=self._params(request),
                headers=self.headers,
                timeout=self._timeout_seconds,
            )
        finally:
            if owns_client:
                await resolved_client.aclose()
        response.raise_for_status()
        return self._parse_payload(response.json(), request)

    def _params(self, request: SearchRequest) -> dict[str, str | int]:
        """构造 Semantic Scholar bulk 端点的请求参数，同步和异步入口共用。

        中文说明：
        bulk 端点的关键参数：
        - query：用概念组渲染的布尔串（+ AND / | OR / - NOT / "短语" / () 分组）。
        - fields：必填，指定返回哪些字段。
        - sort：bulk 只支持 paperId / publicationDate / citationCount（sort=relevance:desc 直接 400）。
          这里默认用 citationCount:desc，把高引论文拉到前面，作为缺失的相关度排序的替代。
          真正的相关度排序在 Phase 3 用客户端 embedding 重排补。
        - year：服务端下推年份过滤（连字符区间，如 2020-2024）。
        - limit：bulk 端点会完全忽略这个参数（实测传 limit=5 仍返回 1000 条），所以不发送。
        """

        params: dict[str, str | int] = {
            "query": self._render_concept_groups(request),
            "fields": self._fields,
            "sort": "citationCount:desc",
        }
        # 中文说明：年份用服务端下推，避免客户端过滤导致少给结果。
        if request.year_from is not None and request.year_to is not None:
            params["year"] = f"{request.year_from}-{request.year_to}"
        elif request.year_from is not None:
            params["year"] = f"{request.year_from}-"
        elif request.year_to is not None:
            params["year"] = f"-{request.year_to}"
        return params

    def _parse_payload(self, payload: dict[str, object], request: SearchRequest) -> list[PaperDocument]:
        """把 Semantic Scholar bulk 响应解析成论文列表，同步和异步入口共用。

        中文说明：
        bulk 端点的响应结构是 {total, data, token}。注意：
        - total 是不可信的估算值（实测同一条查询两次跑出不同数字），绝不能用来判断分页。
        - data 是当前页的论文数组。
        - token 仅在还有后续页时出现（即使本页不满 1000 条也可能有 token）。
        本阶段只取第一页，token 直接忽略；Phase 2 引入超量拉取时再考虑翻页。
        """

        papers: list[PaperDocument] = []
        for item in payload.get("data", []) or []:
            paper = self.normalize_paper(item)
            if paper is None:
                continue
            # 中文说明：年份已经在服务端下推了，但客户端再校验一遍更稳（防止服务端返回越界数据）。
            if not self._within_year_range(paper, request):
                continue
            if self._contains_excluded_terms(paper, request.excluded_terms):
                continue
            papers.append(paper)
        return papers[: request.limit]

    async def async_related(
        self,
        external_ref: str,
        direction: str,
        limit: int,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[PaperDocument]:
        """以某个外部标识为种子，顺着引用关系扩展检索。

        中文说明：
        Semantic Scholar 的引用 API：
        - references：GET /paper/{id}/references（本文引用了哪些论文）
        - citations：GET /paper/{id}/citations（哪些论文引用了本文）
        external_ref 支持多种前缀形式：DOI:xxx / ARXIV:xxx / CorpusId:xxx / PMID:xxx 等。
        """

        # 第一步：把 external_ref 转成 S2 能认识的 paper id。
        paper_id = self._to_s2_id(external_ref)
        if not paper_id:
            return []

        # 第二步：查引用或被引用。
        if direction == "references":
            endpoint = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}/references"
        elif direction == "citations":
            endpoint = f"https://api.semanticscholar.org/graph/v1/paper/{paper_id}/citations"
        else:
            return []

        own_client = client is None
        # 中文说明：修复旧代码里 self._timeout_seconds 未定义的问题——现在类属性已经声明。
        # 同时把旧的 self._headers()（从未定义的方法）改成 self.headers（实例属性）。
        c = client or httpx.AsyncClient(timeout=self._timeout_seconds)
        try:
            # 中文说明：引用接口每页上限 1000 条；fields 用不加前缀的写法，
            # 论文字段会自动嵌套到 citedPaper / citingPaper 下（官方实测两种写法都有效）。
            params = {
                "limit": min(limit, 1000),
                "fields": "paperId,title,abstract,authors,year,venue,externalIds,url,openAccessPdf,citationCount,corpusId",
            }
            resp = await c.get(endpoint, params=params, headers=self.headers)
            if resp.status_code != 200:
                return []
            data = resp.json()
            items = data.get("data") or []
            papers: list[PaperDocument] = []
            for item in items:
                # references 返回 {"citedPaper": {...}}，citations 返回 {"citingPaper": {...}}
                paper_data = item.get("citedPaper") or item.get("citingPaper") or item
                paper = self.normalize_paper(paper_data)
                if paper is not None:
                    papers.append(paper)
            return papers
        finally:
            if own_client:
                await c.aclose()

    def _to_s2_id(self, external_ref: str) -> str:
        """把 external_ref 转成 Semantic Scholar 能认识的 paper id。

        中文说明：
        Semantic Scholar 支持 9 种论文 ID 形式（来自官方文档）：
        - 裸 40 位 sha（S2 内部 id，如 649def34f8be52c8b66281af98ae884c09aef38b）
        - CorpusId:xxx（S2 内部数字 id，注意：纯数字必须带 CorpusId: 前缀，
          实测 /paper/215416146 → 404，/paper/CorpusId:215416146 → 200）
        - DOI:xxx、ARXIV:xxx、MAG:xxx、ACL:xxx、PMID:xxx、PMCID:xxx
        - URL:xxx（仅识别 semanticscholar.org / arxiv.org / aclweb.org / acm.org / biorxiv.org）

        旧版只处理了 DOI:/ARXIV:/纯数字/40 位 sha 4 种，且对纯数字直接原样返回
        （触发 404 bug）。这里补全前缀处理，并把"纯数字"识别成 CorpusId 形式。

        注意：CorpusId 是大小写混合形式（不是全大写 CORPUSID），S2 严格区分，必须精确写。
        """

        ref = (external_ref or "").strip()
        if not ref:
            return ""

        # 中文说明：用户可能写 "corpusid:123" 或 "CORPUSID:123"，统一映射到 S2 接受的标准形式。
        # CorpusId 是 S2 官方唯一的大小写混合前缀，其他前缀都是全大写。
        canonical_prefixes = {
            "DOI": "DOI",
            "ARXIV": "ARXIV",
            "CORPUSID": "CorpusId",
            "MAG": "MAG",
            "ACL": "ACL",
            "PMID": "PMID",
            "PMCID": "PMCID",
            "URL": "URL",
        }
        colon_idx = ref.find(":")
        if colon_idx > 0:
            head = ref[:colon_idx].upper()
            if head in canonical_prefixes:
                value = ref[colon_idx + 1:].strip()
                return f"{canonical_prefixes[head]}:{value}" if value else ""

        # 中文说明：40 位字母数字 → S2 内部 sha id，直接使用。
        if len(ref) == 40 and ref.isalnum():
            return ref

        # 中文说明：纯数字 → S2 内部数字 id，必须补 CorpusId: 前缀（关键修复）。
        if ref.isdigit():
            return f"CorpusId:{ref}"

        # 中文说明：无法识别的形式，返回空串让调用方短路返回 []。
        return ""


    def _render_concept_groups(self, request: SearchRequest) -> str:
        """把结构化概念组渲染成 Semantic Scholar bulk 端点的 query 串。

        中文说明：
        Semantic Scholar bulk 端点的布尔语法（实测验证）：
        - + 表示 AND，| 表示 OR，- 表示 NOT（都是符号，不是英文单词）
        - 写成 AND/OR/NOT 英文单词会被当停用词静默丢弃，不报错
        - 多词短语用双引号
        - () 分组
        - 所有词做英文词干化（quantization 已覆盖 quantize/quantized/quantizations）

        渲染规则：每个概念组用 | 连（OR），组间用 + 连（AND），整组用括号包裹。
        实测同一意图：
        - 旧版布尔式（and/or 当停用词丢弃）：total=186
        - 正确渲染：total=1902（recall 修复 10 倍）

        URL 编码时 httpx 会自动把 + 编成 %2B，不需要我们处理。
        """

        groups = request.concept_groups
        if not groups:
            # 中文说明：没有概念组时用 topic 兜底（当作单概念组的单同义词）。
            topic = request.topic.strip()
            if topic:
                # 中文说明：topic 是自然语言，整串当短语处理。
                if " " in topic:
                    return f'"{topic}"'
                return topic
            return ""

        rendered_groups: list[str] = []
        for group in groups:
            if not group:
                continue
            # 中文说明：每个同义词，含空格或连字符加引号，单词裸写（裸写走词干化，召回更好）。
            terms = []
            for term in group:
                term = str(term).strip()
                if not term:
                    continue
                if " " in term or "-" in term:
                    terms.append(f'"{term}"')
                else:
                    terms.append(term)
            if terms:
                # 中文说明：组内用 | 连（OR），整组加括号。
                rendered_groups.append(f'({" | ".join(terms)})')

        if not rendered_groups:
            return ""

        # 中文说明：组间用 + 连（AND）。
        query = " + ".join(rendered_groups)

        # 中文说明：追加排除词（- 前缀）。
        if request.excluded_terms:
            for term in request.excluded_terms:
                term = str(term).strip()
                if term:
                    query = f"{query} -{term}"

        return query

    def normalize_paper(self, raw: object) -> PaperDocument | None:
        """把单条 Semantic Scholar 记录解析成统一论文对象。"""

        if not isinstance(raw, dict):
            return None
        item = raw
        title = str(item.get("title") or "").strip()
        if not title:
            return None
        authors: list[str] = []
        authors_raw = item.get("authors") or []
        if isinstance(authors_raw, list):
            for author in authors_raw:
                if not isinstance(author, dict):
                    continue
                name = str(author.get("name") or "").strip()
                if name:
                    authors.append(name)
        external_ids = item.get("externalIds") or {}
        doi = ""
        arxiv_id = ""
        if isinstance(external_ids, dict):
            doi = str(external_ids.get("DOI") or "").strip()
            arxiv_id = str(external_ids.get("ArXiv") or external_ids.get("Arxiv") or "").strip()
        open_access_pdf = item.get("openAccessPdf") or {}
        pdf_url = self._pick_pdf_url(open_access_pdf, arxiv_id)
        semantic_id = str(item.get("paperId") or "").strip()
        paper_id = doi or arxiv_id or semantic_id
        venue = self._venue(item)
        journal = item.get("journal") or {}
        volume = ""
        issue = ""
        if isinstance(journal, dict):
            volume = str(journal.get("volume") or "").strip()
            issue = str(journal.get("issue") or "").strip()
        return PaperDocument(
            id=paper_id or title,
            paperId=paper_id,
            title=title,
            authors=authors,
            abstract=str(item.get("abstract") or "").strip() or None,
            year=self._maybe_int(item.get("year")),
            venue=venue or None,
            url=str(item.get("url") or "").strip() or None,
            pdf_url=pdf_url or None,
            doi=doi or None,
            source=self.source_name,
            publication_date=str(item.get("publicationDate") or "").strip(),
            journal_conference=venue,
            volume=volume,
            issue=issue,
            # 中文说明：metadata 里保留 S2 提供的引用数，供 service._rank_merged_papers
            # 的多源合并排序使用（旧版只存了 semantic_scholar_id 和 arxiv_id，导致
            # 重排维度"引用数"只有 OpenAlex 有值、S2 结果全部按 0 处理，严重破坏排序质量）。
            metadata={
                "semantic_scholar_id": semantic_id,
                "arxiv_id": arxiv_id,
                "cited_by_count": self._maybe_int(item.get("citationCount")),
                "influential_citation_count": self._maybe_int(item.get("influentialCitationCount")),
                "reference_count": self._maybe_int(item.get("referenceCount")),
                "corpus_id": self._maybe_int(item.get("corpusId")),
            },
        )

    def _pick_pdf_url(self, open_access_pdf: object, arxiv_id: str) -> str:
        """从一条 Semantic Scholar 记录里挑出真正能下载的全文直链。

        中文注释：
        S2 的 openAccessPdf.url 名义上是"开放获取 PDF 地址"，但实测并不保证真是 PDF。
        例如 DOI 10.1145/3620666.3651380 返回的是：
            {"url": "https://doi.org/10.1145/3620666.3651380", "status": "GOLD", "license": "CCBY"}
        看着很正规（金开放获取、CC-BY 许可），但 url 其实是出版社的 DOI 落地页，
        拿去下载会撞门禁，或被下回来一堆介绍页 HTML。

        好在 S2 同时给出了 externalIds.ArXiv。只要知道 arXiv 编号，就能拼出
        https://arxiv.org/pdf/{编号}，这条几乎总是能直接下载。所以挑选顺序是：

        1) openAccessPdf.url 看着确实像 PDF 文件 → 直接用它
        2) 有 arXiv 编号 → 用 arXiv 的 PDF 直链
        3) openAccessPdf.url 兜底（可能不是 PDF，但总比一条全文地址都没有强）
        """

        candidate = ""
        if isinstance(open_access_pdf, dict):
            candidate = str(open_access_pdf.get("url") or "").strip()

        if candidate and self._looks_like_pdf_url(candidate):
            return candidate

        if arxiv_id:
            return f"https://arxiv.org/pdf/{arxiv_id}"

        return candidate

    def _looks_like_pdf_url(self, url: str) -> bool:
        """粗略判断一个地址是不是 PDF 文件本身，而不是论文介绍页。

        中文注释：
        只做很保守的判断：以 .pdf 结尾，或者路径里带 /pdf/。
        判断不出来时返回 False 而不是 True——因为一旦把介绍页当成 PDF 交给下载器，
        要么被对方门禁拒绝，要么下回来一堆网页内容冒充论文正文，两种都比"承认拿不到"
        更糟。宁可走 arXiv 兜底或诚实返回"没有全文地址"。
        """

        # 去掉查询串再判断，避免 ?version=xxx 之类的后缀干扰。
        without_query = url.lower().split("?", 1)[0]
        return without_query.endswith(".pdf") or "/pdf/" in without_query

    def _venue(self, item: dict[str, object]) -> str:
        """从 Semantic Scholar 的多个可能字段里取期刊或会议名称。"""

        venue = str(item.get("venue") or "").strip()
        if venue:
            return venue
        publication_venue = item.get("publicationVenue") or {}
        if isinstance(publication_venue, dict):
            name = str(publication_venue.get("name") or "").strip()
            if name:
                return name
        journal = item.get("journal") or {}
        if isinstance(journal, dict):
            return str(journal.get("name") or "").strip()
        return ""

    def _maybe_int(self, value: object) -> int | None:
        """安全转换可选年份字段。"""

        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _within_year_range(self, paper: PaperDocument, request: SearchRequest) -> bool:
        """按年份范围过滤结果。"""

        if paper.year is None:
            return True
        if request.year_from is not None and paper.year < request.year_from:
            return False
        if request.year_to is not None and paper.year > request.year_to:
            return False
        return True

    def _contains_excluded_terms(self, paper: PaperDocument, excluded_terms: list[str]) -> bool:
        """对标题和摘要做排除词过滤。"""

        haystack = f"{paper.title} {paper.abstract or ''}".lower()
        return any(term.strip().lower() in haystack for term in excluded_terms if term.strip())
