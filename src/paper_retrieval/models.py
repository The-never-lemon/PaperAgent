from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.utils.latex_text import clean_latex_text


JsonObject = dict[str, Any]


@dataclass(slots=True)
class PaperDocument:
    """统一的论文领域模型。

    这一层负责把不同数据源返回的字段差异压平，避免上游流程直接依赖某个站点的私有字段。
    """

    id: str
    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str | None = None
    year: int | None = None
    venue: str | None = None
    url: str | None = None
    pdf_url: str | None = None
    doi: str | None = None
    source: str | None = None
    paperId: str | None = None
    publication_date: str = ""
    journal_conference: str = ""
    volume: str = ""
    issue: str = ""
    language: str = ""
    metadata: JsonObject = field(default_factory=dict)

    def __post_init__(self) -> None:
        """补齐统一字段的默认值，并清洗元数据里的 LaTeX 标记。

        中文注释：旧代码通常只传 `id` 和 `venue`；新检索链路会使用 `paperId` 和
        `journal_conference`。这里把两个体系轻轻对齐，避免一次改动牵连太多下游代码。
        如果 connector 明确传入 `paperId=""`，表示这个来源没有可靠唯一编号，不会被这里覆盖。

        中文注释（清洗）：检索源返回的标题、摘要、作者、期刊名经常自带 LaTeX 标记，
        而且不带 $ 定界符（例如摘要里写「The \\textit{PIMAEX} reward」）。这些字符会一路
        污染四个地方：前端展示、喂给模型的提示词、综述的参考文献、导出的 bibtex/csv。
        放在这里洗一次，下面全部六个构造论文对象的地方就都干净了——包括三个检索源、
        手动上传、以及从工作区还原论文对象的两处。

        注意顺序：清洗放在前面，因为下面 journal_conference 是从 venue 派生的，
        先洗再派生，派生出来的值也就是干净的。
        """

        self.title = clean_latex_text(self.title)
        # 摘要允许是 None（有些来源不提供），只在真有时才洗。
        if self.abstract:
            self.abstract = clean_latex_text(self.abstract)
        if self.venue:
            self.venue = clean_latex_text(self.venue)
        if self.journal_conference:
            self.journal_conference = clean_latex_text(self.journal_conference)
        self.authors = [clean_latex_text(author) for author in self.authors]

        if self.paperId is None:
            self.paperId = self.doi or self.id
        if not self.journal_conference and self.venue:
            self.journal_conference = self.venue

    def to_dict(self) -> JsonObject:
        """把领域对象转成普通字典，便于工具层、调试和接口输出直接消费。"""

        return {
            "id": self.id,
            "paperId": self.paperId or "",
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year or "",
            "abstract": self.abstract or "",
            "source": self.source or "",
            "url": self.url or "",
            "publication_date": self.publication_date,
            "journal/conference": self.journal_conference,
            "journal_conference": self.journal_conference,
            "volume": self.volume,
            "issue": self.issue,
            "language": self.language,
            "venue": self.venue,
            "pdf_url": self.pdf_url,
            "doi": self.doi,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class SearchRequest:
    """统一检索请求。

    中文说明：
    旧版用自由文本布尔式（query）作为检索意图，但三个学术 API（arXiv / OpenAlex / Semantic Scholar）
    各自的布尔语法完全不同（大小写 AND/OR/ANDNOT vs 大写 AND/OR/NOT vs 符号 +/|/-），
    让 LLM 写一个自由布尔式然后原样广播给三个 API，必然出错（实测召回损失 40%~11 倍）。

    新版改用结构化概念组：
    - concept_groups：组之间是 AND（必须同时命中），组内是同义/近义写法的 OR（命中任一即可）。
      例如 [["large language model", "LLM"], ["kv cache", "key-value cache"]] 表示
      必须同时命中 LLM 相关 AND kv cache 相关。每个 connector 把它渲染成自己源的原生语法。
    - topic：用一句自然语言描述当前研究主题。它**不参与检索语法**，也不参与排序，
      只用于日志和调试时说明"这次查询想找什么"。
    - title：已经知道论文完整标题时使用。有 title 时各检索源按标题字段检索，
      不再把标题拆成概念组。title 由主 Agent 自己判断后填写，不是用户选的模式。

    这样就把"模型怎么表达意图"与"每个源怎么执行检索"彻底隔离了。
    """

    topic: str = ""
    concept_groups: list[list[str]] = field(default_factory=list)
    # 中文说明：用户要找「已经知道标题的那一篇」时，主 Agent 把完整标题放这里。
    # 有标题时各检索源按标题字段搜，不再把标题拆成概念组去做布尔匹配。
    title: str = ""
    source: str | None = None
    sources: list[str] = field(default_factory=list)
    limit: int = 10
    year_from: int | None = None
    year_to: int | None = None
    excluded_terms: list[str] = field(default_factory=list)

    def normalized_title(self) -> str:
        """整理按标题检索用的字符串：去掉首尾空白，并把标题里的双引号换成空格。

        中文说明：三个检索源都用引号把标题当成一整句去搜。标题本身再带双引号，
        查询串会被提前截断。这里只做这一处清理，不改大小写、也不拆词。
        """

        return " ".join(str(self.title or "").replace('"', " ").split())


@dataclass(slots=True)
class SearchResponse:
    """统一检索响应。

    除了论文列表以外，还保留来源统计和错误信息，便于上层做诊断和展示。
    """

    query: str
    sources_used: list[str] = field(default_factory=list)
    source_results: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    papers: list[PaperDocument] = field(default_factory=list)

    @property
    def total(self) -> int:
        """返回去重后的论文数量。"""

        return len(self.papers)

    def to_dict(self) -> JsonObject:
        """把检索响应转成普通字典，便于调试和接口透传。"""

        return {
            "query": self.query,
            "sources_used": list(self.sources_used),
            "source_results": dict(self.source_results),
            "errors": dict(self.errors),
            "papers": [paper.to_dict() for paper in self.papers],
            "total": self.total,
        }
