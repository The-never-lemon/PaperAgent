from __future__ import annotations

import httpx

from ..models import PaperDocument, SearchRequest
from .base import PaperSearchConnector


class OpenAlexPaperConnector(PaperSearchConnector):
    """OpenAlex connector。

    这里负责把结构化输入拼成 OpenAlex 可接受的搜索参数，
    上层只需要关心 topic 和 keywords，而不需要知道具体参数格式。
    """

    source_name = "openalex"
    _endpoint = "https://api.openalex.org/works"
    # 中文说明：单次 HTTP 请求的超时秒数。async_related 里创建临时 httpx.AsyncClient 时复用。
    _timeout_seconds = 20.0

    def __init__(self, client: httpx.Client | None = None, api_key: str | None = None):
        """初始化 HTTP 客户端，并保存可选的 OpenAlex API Key。"""

        self.headers = {
            "User-Agent": "papers-agents/0.1 paper-retrieval",
            "Accept": "application/json",
        }
        # 中文说明：OpenAlex 把密钥放在请求参数 api_key 中；密钥为空时不发送该参数，
        # 因此 config/system.yaml 保持 null 也能继续使用匿名检索。
        self.api_key = (api_key or "").strip()
        self.client = client or httpx.Client(
            timeout=self._timeout_seconds,
            headers=self.headers,
        )

    def search(self, request: SearchRequest) -> list[PaperDocument]:
        """执行 OpenAlex 检索，并在 connector 内完成查询拼装。"""

        response = self.client.get(self._endpoint, params=self._params(request))
        response.raise_for_status()
        return self._parse_payload(response.json(), request)

    async def async_search(
        self,
        request: SearchRequest,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[PaperDocument]:
        """异步执行 OpenAlex 检索，避免在异步编排里阻塞事件循环。"""

        resolved_client = client or httpx.AsyncClient(timeout=20.0)
        owns_client = client is None
        try:
            response = await resolved_client.get(
                self._endpoint,
                params=self._params(request),
                headers=self.headers,
                timeout=20.0,
            )
        finally:
            if owns_client:
                await resolved_client.aclose()
        response.raise_for_status()
        return self._parse_payload(response.json(), request)

    def _params(self, request: SearchRequest) -> dict[str, str | int]:
        """构造 OpenAlex 请求参数，同步和异步入口共用。

        中文说明：
        OpenAlex 的查询可以走两个路径：
        - 顶层 search= 参数：等于 fulltext.search，会命中正文（噪声大）
        - filter=title_and_abstract.search: 字段级检索：只命中标题+摘要（精确度高）
        本项目选后者。

        用「管道+重复子句」形式表示概念组（无括号、不 500、不受 1500 字符限制）：
        - 组内同义词用 | 连（OR）
        - 组间重复同一 title_and_abstract.search: 键（逗号分隔，= AND）
        实测这种写法和标准布尔式产生完全一致的 x_query.oql，但更安全。

        同时必加 type:article|review|preprint + has_abstract:true 提精度。
        """

        filters: list[str] = []

        # 中文说明：渲染概念组（核心改动）。
        concept_filter = self._render_concept_groups(request)
        if concept_filter:
            filters.append(concept_filter)

        # 中文说明：排除词（OpenAlex 没有专门的排除 filter，只能客户端过滤，
        # 这里用 ! 前缀加到 title_and_abstract.search 里）。
        if request.excluded_terms:
            for term in request.excluded_terms:
                term = str(term).strip()
                if term:
                    # 中文说明：! 前缀 = NOT（在 OpenAlex filter 里）。
                    filters.append(f'title_and_abstract.search:!{term}')

        # 中文说明：年份过滤（服务端下推）。
        if request.year_from is not None:
            filters.append(f"from_publication_date:{request.year_from}-01-01")
        if request.year_to is not None:
            filters.append(f"to_publication_date:{request.year_to}-12-31")

        # 中文说明：过滤掉非文章类型（社论、书信等），且要求有摘要（便于后续评分）。
        filters.append("type:article|review|preprint")
        filters.append("has_abstract:true")

        params: dict[str, str | int] = {
            # 中文说明：filter 形式检索时，顶层 search= 不再需要（传空或不传）。
            # 但 OpenAlex 要求至少有一个检索入口，所以把 filter 也算作检索。
            "filter": ",".join(filters),
            "per_page": max(1, min(request.limit, 100)),  # OpenAlex per_page 上限 100。
            "sort": "relevance_score:desc",
        }
        if self.api_key:
            # 中文说明：OpenAlex 的 api_key 走 Authorization: Bearer header 更安全
            # （不进 URL 和日志），但 URL 参数形式也能工作。为最小改动这里先用 URL 参数。
            params["api_key"] = self.api_key
        return params

    def _render_concept_groups(self, request: SearchRequest) -> str:
        """把结构化概念组渲染成 OpenAlex 的 filter 子句。

        中文说明：
        渲染规则：每个概念组用 | 连（OR），组间用逗号重复同一 title_and_abstract.search: 键。
        例如 [["large language model","LLM"],["kv cache","key-value cache"]] 渲染成：
            title_and_abstract.search:"large language model"|LLM,title_and_abstract.search:"kv cache"|"key-value cache"
        这种写法：
        - 完全没有括号 → 不可能触发「括号不配对 → HTTP 500」
        - 绕过 search= 的 1500 字符上限
        - 实测同一查询 count=1414，旧版自由布尔式 count=3573（recall 修复）

        多词短语要加双引号（OpenAlex 把引号当短语匹配，每个词仍然词干化）。
        """

        groups = request.concept_groups
        if not groups:
            # 中文说明：没有概念组时用 topic 兜底。
            if request.topic.strip():
                # 中文说明：topic 是自然语言，整串当短语匹配。
                return f'title_and_abstract.search:"{request.topic.strip()}"'
            return ""

        clauses: list[str] = []
        for group in groups:
            if not group:
                continue
            # 中文说明：每个同义词，含空格则加双引号，不含空格则裸写（裸写走词干化，召回更好）。
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
                # 中文说明：一个概念组渲染成一个 title_and_abstract.search: 子句。
                clauses.append(f'title_and_abstract.search:{"|".join(terms)}')

        # 中文说明：多个子句用逗号连接（= AND）。
        return ",".join(clauses)

    def _parse_payload(self, payload: dict[str, object], request: SearchRequest) -> list[PaperDocument]:
        """把 OpenAlex JSON 响应解析成论文列表，同步和异步入口共用。"""

        papers: list[PaperDocument] = []
        for item in payload.get("results", []) or []:
            paper = self.normalize_paper(item)
            if paper is None:
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

        中文注释：
        OpenAlex 的引用 API：
        - references：GET /works/{id} 返回 referenced_works 字段（OpenAlex ID 列表）
        - citations：GET /works?filter=cites:{id}
        external_ref 可以是 DOI:xxx 或 OpenAlex 内部 id（W 开头）。
        """

        # 第一步：把 external_ref 转成 OpenAlex 能认识的 work id。
        work_id = await self._resolve_work_id(external_ref, client)
        if not work_id:
            return []

        # 第二步：按 direction 查引用关系。
        if direction == "references":
            # 查这篇论文引用了哪些论文。
            related_ids = await self._fetch_referenced_works(work_id, client)
        elif direction == "citations":
            # 查哪些论文引用了这篇论文。
            related_ids = await self._fetch_citing_works(work_id, client, limit)
        else:
            return []

        if not related_ids:
            return []

        # 第三步：批量查这些论文的元数据。
        return await self._fetch_works_by_ids(related_ids[:limit], client)

    async def _resolve_work_id(
        self, external_ref: str, client: httpx.AsyncClient | None
    ) -> str | None:
        """把 external_ref 转成 OpenAlex work id。"""

        own_client = client is None
        c = client or httpx.AsyncClient(timeout=self._timeout_seconds)
        try:
            if external_ref.startswith("DOI:"):
                # DOI 查询：GET /works/https://doi.org/xxx
                doi = external_ref[4:]
                url = f"{self._endpoint}/https://doi.org/{doi}"
                resp = await c.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("id")
            elif external_ref.startswith("W"):
                # 已经是 OpenAlex id。
                return external_ref
            return None
        finally:
            if own_client:
                await c.aclose()

    async def _fetch_referenced_works(
        self, work_id: str, client: httpx.AsyncClient | None
    ) -> list[str]:
        """查这篇论文引用了哪些论文，返回 OpenAlex id 列表。"""

        own_client = client is None
        c = client or httpx.AsyncClient(timeout=self._timeout_seconds)
        try:
            url = f"{self._endpoint}/{work_id}"
            resp = await c.get(url)
            if resp.status_code != 200:
                return []
            data = resp.json()
            return data.get("referenced_works") or []
        finally:
            if own_client:
                await c.aclose()

    async def _fetch_citing_works(
        self, work_id: str, client: httpx.AsyncClient | None, limit: int
    ) -> list[str]:
        """查哪些论文引用了这篇论文，返回 OpenAlex id 列表。"""

        own_client = client is None
        c = client or httpx.AsyncClient(timeout=self._timeout_seconds)
        try:
            params = {
                "filter": f"cites:{work_id}",
                "per-page": min(limit, 200),
                "select": "id",
            }
            resp = await c.get(self._endpoint, params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
            results = data.get("results") or []
            return [r.get("id") for r in results if r.get("id")]
        finally:
            if own_client:
                await c.aclose()

    async def _fetch_works_by_ids(
        self, work_ids: list[str], client: httpx.AsyncClient | None
    ) -> list[PaperDocument]:
        """批量查一组 OpenAlex work id 的元数据，转成 PaperDocument。"""

        if not work_ids:
            return []
        own_client = client is None
        c = client or httpx.AsyncClient(timeout=self._timeout_seconds)
        try:
            # OpenAlex 支持 filter=openalex:W1|W2|W3 批量查询。
            ids_str = "|".join(work_ids)
            params = {
                "filter": f"openalex:{ids_str}",
                "per-page": len(work_ids),
            }
            resp = await c.get(self._endpoint, params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
            results = data.get("results") or []
            papers: list[PaperDocument] = []
            for raw in results:
                paper = self.normalize_paper(raw)
                if paper is not None:
                    papers.append(paper)
            return papers
        finally:
            if own_client:
                await c.aclose()


    def normalize_paper(self, raw: object) -> PaperDocument | None:
        """把单条 OpenAlex work 记录解析成统一论文对象。"""

        if not isinstance(raw, dict):
            return None
        item = raw
        title = str(item.get("title") or "").strip()
        if not title:
            return None
        authorships = item.get("authorships") or []
        authors: list[str] = []
        if isinstance(authorships, list):
            for authorship in authorships:
                if not isinstance(authorship, dict):
                    continue
                author = authorship.get("author") or {}
                if isinstance(author, dict):
                    display_name = str(author.get("display_name") or "").strip()
                    if display_name:
                        authors.append(display_name)
        primary_location = item.get("primary_location") or {}
        source = primary_location.get("source") if isinstance(primary_location, dict) else {}
        pdf_url = self._pick_pdf_url(item)
        venue = ""
        if isinstance(source, dict):
            venue = str(source.get("display_name") or "").strip()
        doi = str(item.get("doi") or "").strip()
        if doi.startswith("https://doi.org/"):
            doi = doi.removeprefix("https://doi.org/")
        openalex_id = str(item.get("id") or "").strip()
        paper_id = doi or openalex_id
        publication_date = str(item.get("publication_date") or "").strip()
        biblio = item.get("biblio") or {}
        volume = ""
        issue = ""
        if isinstance(biblio, dict):
            volume = str(biblio.get("volume") or "").strip()
            issue = str(biblio.get("issue") or "").strip()
        return PaperDocument(
            id=paper_id or title,
            paperId=paper_id,
            title=title,
            authors=authors,
            abstract=self._abstract_from_inverted_index(item.get("abstract_inverted_index")),
            year=self._maybe_int(item.get("publication_year")),
            venue=venue or None,
            url=openalex_id or None,
            pdf_url=pdf_url or None,
            doi=doi or None,
            source=self.source_name,
            publication_date=publication_date,
            journal_conference=venue,
            volume=volume,
            issue=issue,
            language=str(item.get("language") or "").strip(),
            metadata={
                "cited_by_count": item.get("cited_by_count"),
                "type": item.get("type"),
            },
        )

    def _pick_pdf_url(self, item: dict[str, object]) -> str:
        """从一篇 OpenAlex 论文记录里挑出真正能下载的全文直链。

        中文注释：
        OpenAlex 一条记录里有好几个跟全文有关的地址，可靠性差别很大，必须按顺序挑：

        1) best_oa_location.pdf_url —— OpenAlex 认为最好的那处开放获取位置里的 PDF
        2) primary_location.pdf_url —— 论文主发布位置的 PDF
        3) locations[] 里第一条非空的 pdf_url —— 各个仓储/镜像位置挨个看
        4) open_access.oa_url —— 兜底

        为什么不能像以前那样直接用 oa_url？因为 oa_url 的含义只是"最好的开放获取位置"，
        很多出版社（尤其 ACM、Elsevier）在这个字段里给的是 DOI 落地页，不是 PDF 文件本身。
        拿落地页当全文地址去下载，轻则被出版社的门禁挡回来（Cloudflare 403），
        重则下回来一个介绍页 HTML，被后面的精读流程当成论文正文读。

        实测（DOI 10.1145/3620666.3651380，NeuPIMs 那篇）：
        - open_access.oa_url       = https://doi.org/10.1145/3620666.3651380（落地页，403）
        - best_oa_location.pdf_url = null
        - locations[1].pdf_url     = https://arxiv.org/pdf/2403.00579（能正常下载）
        旧代码只取了 oa_url，等于把这条可下载的直链白白扔掉，结果就是下载失败、
        精读被迫降级成摘要。注意这篇的 best_oa_location 也是 null，所以只改成读
        best_oa_location 并不够，必须一路扫到 locations 列表。
        """

        # 第一步：先看两个"位置"字段里自带的 pdf_url。
        for key in ("best_oa_location", "primary_location"):
            location = item.get(key) or {}
            if isinstance(location, dict):
                candidate = str(location.get("pdf_url") or "").strip()
                if candidate:
                    return candidate

        # 第二步：挨个看 locations 列表，取第一条非空的 pdf_url。
        locations = item.get("locations") or []
        if isinstance(locations, list):
            for location in locations:
                if not isinstance(location, dict):
                    continue
                candidate = str(location.get("pdf_url") or "").strip()
                if candidate:
                    return candidate

        # 第三步：兜底用 oa_url。它可能并不是 PDF，但总比一条全文地址都没有强。
        open_access = item.get("open_access") or {}
        if isinstance(open_access, dict):
            return str(open_access.get("oa_url") or "").strip()
        return ""

    def _maybe_int(self, value: object) -> int | None:
        """安全转换可选年份字段。"""

        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _abstract_from_inverted_index(self, value: object) -> str | None:
        """把 OpenAlex 的摘要词表还原成普通摘要文本。"""

        if not isinstance(value, dict):
            return None
        positions: dict[int, str] = {}
        for word, raw_indexes in value.items():
            if not isinstance(raw_indexes, list):
                continue
            for raw_index in raw_indexes:
                try:
                    positions[int(raw_index)] = str(word)
                except (TypeError, ValueError):
                    continue
        if not positions:
            return None
        return " ".join(positions[index] for index in sorted(positions))

    def _contains_excluded_terms(self, paper: PaperDocument, excluded_terms: list[str]) -> bool:
        """对标题和摘要做排除词过滤。"""

        haystack = f"{paper.title} {paper.abstract or ''}".lower()
        return any(term.strip().lower() in haystack for term in excluded_terms if term.strip())
