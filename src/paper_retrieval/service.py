from __future__ import annotations

import asyncio
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

import httpx

from src.llm.config import SystemConfig
from src.utils import get_logger, logging_context

from .connectors import ArxivPaperConnector, OpenAlexPaperConnector, PaperSearchConnector, SemanticScholarPaperConnector
from .download import _ARXIV_DOI_PREFIX
from .models import PaperDocument, SearchRequest, SearchResponse

if TYPE_CHECKING:
    from src.graph.runtime_resources import WorkflowRuntimeResources
    from src.llm.config import ModelConfig


logger = get_logger(__name__)


# 中文说明：允许重试的 HTTP 状态码。429 是「请求太频繁，等会儿再来」，
# 5xx 是「服务端自己出问题了」，这两种都值得再试一次。
_RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)


# 中文说明：RRF（Reciprocal Rank Fusion，排名融合）里的平滑常数。
# 多源检索时用它在各源的排名上算融合分：某篇论文在某个源里排第 rank 名，
# 就贡献 1/(K + rank)，把它在各个源里的贡献加起来就是总分。
# K 取 60 是这类融合的常用默认值。它的效果是"名次之间的差距被压得比较平缓"，
# 于是"被多个源同时命中"依然比"只在单个源里排得靠前"更有分量，
# 与改造前"先看命中几个源"的排序意图一致，但不再是一刀切的阶跃比较。
RRF_K = 60

# 中文说明：单次 embedding 请求最多发几条文本。各平台的上限差别很大
# （实测 DashScope 是 20 条，OpenAI 是 2048 条），所以正确做法是在档位里配；
# 这个常量只在配置缺失或配得不合法时兜底，取一个各家都安全的保守值。
EMBEDDING_BATCH_SIZE_FALLBACK = 16


def _is_retryable_search_error(exc: Exception) -> bool:
    """判断一次检索失败值不值得重试。

    中文说明：只有两种情况值得重试——
    1. 服务端明确回了「稍后再来」的状态码（429 限流、500/502/503/504 服务端故障）；
    2. 网络层面的问题：连不上、连接被中途掐断、读数据读超时。

    这里必须用 httpx.TransportError 来判断网络问题，**不能**用 Python 自带的 TimeoutError。
    原因：connector 都是直接用 httpx 发请求的，httpx 的各种超时
    （ReadTimeout / ConnectTimeout）继承的是 TimeoutException → TransportError，
    跟内置的 TimeoutError 没有半点继承关系。写成 isinstance(exc, TimeoutError) 的话，
    真实网络超时永远匹配不上，重试逻辑等于白写。
    （download.py 里用的就是 httpx.TimeoutException，两边口径保持一致。）

    其他异常不重试——比如参数拼错、返回内容解析不了，重试也不会让错误参数变对。
    """

    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in _RETRYABLE_STATUS_CODES:
        return True
    # 中文说明：TransportError 是 httpx 所有网络层异常的父类，各种超时和连接失败都归在它下面。
    return isinstance(exc, httpx.TransportError)


def _backfill_fulltext_fields(representative: PaperDocument, group: list[PaperDocument]) -> None:
    """把同组其它记录里可用的全文下载信息补给代表记录。

    中文注释：
    选代表看的是引用数，而 arXiv 记录拿不到引用数（它的数据源不提供这个字段），
    于是同一篇论文同时被 arXiv 和 OpenAlex 收录时，代表必然是 OpenAlex 那条，
    arXiv 记录里那个稳定好用的 PDF 直链就跟着被丢掉了。
    结果就是：明明有能下载的链接，下载却失败，精读被迫降级成摘要。

    这里在保留代表（引用数信息最全的那条）的前提下，把两样东西补回来：
    1. arxiv_id —— 下载层靠它拼 arXiv 的 PDF 直链；
    2. pdf_url —— 代表自己没有可下载链接时，用组里别的记录的。

    代表自己已经有值的字段一律不动，避免把更好的信息覆盖掉。
    """

    if representative.metadata is None:
        representative.metadata = {}
    metadata = representative.metadata

    # 中文注释：先补 arXiv 编号。它是最有用的一样——下载层拿到编号就能拼出
    # arXiv 的 PDF 直链，而 arXiv 的链接比出版社的开放获取链接稳定得多。
    if not str(metadata.get("arxiv_id") or "").strip():
        for candidate in group:
            arxiv_id = str((candidate.metadata or {}).get("arxiv_id") or "").strip()
            if arxiv_id:
                metadata["arxiv_id"] = arxiv_id
                break

    # 中文注释：代表自己没有 PDF 直链时，借组里别的记录的用。
    if not str(representative.pdf_url or "").strip():
        for candidate in group:
            if candidate is representative:
                continue
            if str(candidate.pdf_url or "").strip():
                representative.pdf_url = candidate.pdf_url
                break


class PaperSearchService:
    """论文检索编排层。

    这一层只负责三件事：
    1. 管理 connector 注册表；
    2. 把结构化检索意图转换成各 connector 需要的请求；
    3. 聚合、去重和截断结果。
    具体查询语句如何拼接，由各个 connector 自己决定。
    """

    def __init__(self, connectors: dict[str, PaperSearchConnector] | None = None):
        """初始化服务，并注入默认 connector 集合。"""

        resolved = connectors or self._build_default_connectors()
        self._connectors = dict(resolved)
        logger.info("论文检索服务初始化完成", extra={"sources": sorted(self._connectors.keys())})

    # 中文说明：OVER_FETCH_FACTOR 控制每源超量拉取的倍数。
    # 实测客户端过滤（年份、排除词）后通常剩 60-80%，取 3 保证最终能拿到用户期望的数量。
    # 超量拉取后再用 RRF 重排截断到用户期望的数量，能显著提升排序质量。
    OVER_FETCH_FACTOR = 3

    @staticmethod
    def _resolve_embedding_batch_size(config: "ModelConfig") -> int:
        """取当前嵌入档位配置的单次请求条数；没配或配得不合法时退回保守值。

        中文说明：这个值应该按平台能力配（实测 DashScope 20 条、OpenAI 2048 条），
        所以优先读档位里的 batch_size；读不到、或读到的不是个合法条数，
        就用 EMBEDDING_BATCH_SIZE_FALLBACK 兜底，保证一定有个能用的值。
        """

        try:
            profile = config.resolve_embedding_profile(config.default_embedding_profile)
        except Exception:
            return EMBEDDING_BATCH_SIZE_FALLBACK
        size = profile.batch_size
        if isinstance(size, int) and size > 0:
            return size
        return EMBEDDING_BATCH_SIZE_FALLBACK

    async def _embedding_rerank(
        self,
        candidates: list[PaperDocument],
        topic: str,
    ) -> dict[str, float]:
        """对候选论文做 embedding 语义重排。

        中文说明：
        把 topic（用户研究主题）和每篇候选论文的 title + abstract 一起 embed，
        算 topic 向量与每个候选向量的余弦相似度。调用方传进来的 candidates
        应当是"合并去重后的代表论文"，这样每个候选对应唯一一个分数，
        不会出现同一篇论文的兄弟记录互相覆盖分数的情况。

        关键设计：
        - 前置检查：没配嵌入档位、或档位引用的 provider 没配 API Key，直接跳过并
          返回空 dict，不发那次注定失败的请求。
        - 失败降级：provider 报错、返回数量不匹配、numpy 没装，都返回空 dict，
          让 _rank_merged_papers 这一维给 0 分，不影响主流程。
        - 分批请求：按档位配置的 batch_size 切开并发发送，避开各平台的单次条数上限。
        - 不建全库向量索引，只对当次检索的候选（几十篇）做一次 embed。

        返回：{paper_id 或 id: 余弦相似度} 的 dict。失败时返回空 dict。
        """

        if not topic or not candidates:
            return {}

        # 中文说明：provider 会持有网络连接池，无论成功还是失败，最后都要把它关掉。
        snapshot = None
        try:
            # 中文说明：动态 import 避免循环依赖（factory → base → config）。
            import json as _json
            from pathlib import Path as _Path
            from src.llm.config import ModelConfig, SystemConfig
            from src.llm.factory import make_provider

            # 中文说明：从 config/model.json 加载模型配置（default_embedding_profile 在这里）。
            model_config_path = _Path("config/model.json")
            if not model_config_path.exists():
                logger.info("embedding 重排跳过：config/model.json 不存在")
                return {}

            model_data = _json.loads(model_config_path.read_text(encoding="utf-8"))
            system_config = SystemConfig.load()
            config = ModelConfig.from_dict(model_data, system_config)

            # 中文说明：一个嵌入模型档位都没配时直接降级。先在这里判断清楚，
            # 免得后面取不到 provider 才抛错，日志里反而看不出真正原因。
            if not config.embedding_profiles:
                logger.info("embedding 重排跳过：未配置任何嵌入模型档位")
                return {}

            # 中文说明：再看这个档位引用的 provider 到底有没有可用的 API Key。
            # 密钥不在档位里，而在档位指向的 provider 上（明文 api_key，或
            # api_key_env 指向的环境变量），所以要问 resolve_embedding_provider_config
            # 才拿得到"最终真正生效的那把钥匙"。没有钥匙就直接跳过，
            # 免得白发一次注定被拒的请求——省的不只是时间，还有日志噪音。
            _, provider_config = config.resolve_embedding_provider_config(config.default_embedding_profile)
            if not provider_config.api_key:
                logger.info("embedding 重排跳过：嵌入档位引用的 provider 未配置 API Key")
                return {}

            snapshot = make_provider(config, embedding_profile_name=config.default_embedding_profile)
            provider = snapshot.provider

            # 中文说明：构造输入文本。第一个是 topic，后面是每篇候选的 title + abstract。
            # 空白候选用 title 兜底，保证向量化输入不为空。
            texts = [topic]
            for p in candidates:
                text = f"{p.title or ''} {p.abstract or ''}".strip()
                texts.append(text or p.title or 'unknown')

            # 中文说明：分批 embed。各平台对"单次请求最多几条"有硬上限
            # （实测 DashScope 超过 20 条就直接返回 400），把几十篇候选一次性
            # 发过去会被整批打回。这里按档位配置的 batch_size 切开、并发请求，
            # 再把各批的向量按原顺序拼回来。
            batch_size = self._resolve_embedding_batch_size(config)
            batches = [texts[start : start + batch_size] for start in range(0, len(texts), batch_size)]
            batch_responses = await asyncio.gather(*(provider.embed(batch) for batch in batches))
            embeddings: list[list[float]] = []
            for batch, batch_response in zip(batches, batch_responses):
                if not batch_response.ok or len(batch_response.embeddings) != len(batch):
                    logger.warning(
                        "embedding 重排跳过：响应异常",
                        extra={
                            "ok": batch_response.ok,
                            "expected": len(batch),
                            "got": len(batch_response.embeddings),
                        },
                    )
                    return {}
                embeddings.extend(batch_response.embeddings)

            # 中文说明：用 numpy 算余弦相似度，numpy 是项目直接依赖（见 pyproject.toml）。
            import numpy as np

            topic_vec = np.asarray(embeddings[0], dtype=np.float32)
            topic_norm = float(np.linalg.norm(topic_vec))
            if topic_norm == 0:
                return {}

            scores: dict[str, float] = {}
            for i, p in enumerate(candidates):
                cand_vec = np.asarray(embeddings[i + 1], dtype=np.float32)
                cand_norm = float(np.linalg.norm(cand_vec))
                if cand_norm == 0:
                    sim = 0.0
                else:
                    sim = float(np.dot(topic_vec, cand_vec) / (topic_norm * cand_norm))
                # 中文说明：用 paperId 作 key。调用方保证传进来的候选已经是
                # 合并去重后的代表，所以键和值一一对应（不需要用去重键 _paper_key：
                # 代表之间本来就不会重复，而 _paper_key 是另一套带归一化的规则）。
                key = p.paperId or p.id
                scores[key] = sim

            logger.info(
                "embedding 重排完成",
                extra={
                    "candidate_count": len(candidates),
                    "avg_similarity": sum(scores.values()) / len(scores) if scores else 0,
                },
            )
            return scores

        except Exception as exc:
            # 中文说明：任何异常都降级为纯词面排序，不阻塞主流程。
            logger.warning(
                "embedding 重排失败，降级为纯词面排序",
                extra={"error": str(exc)},
                exc_info=True,
            )
            return {}
        finally:
            # 中文说明：把本轮临时创建的 provider 关掉，否则每检索一次就漏一个连接池。
            if snapshot is not None:
                try:
                    await snapshot.aclose()
                except Exception:
                    logger.warning("关闭 embedding provider 失败", exc_info=True)

    def search(
        self,
        *,
        topic: str = "",
        concept_groups: list[list[str]] | None = None,
        source: str | None = None,
        sources: list[str] | None = None,
        limit: int = 10,
        year_from: int | None = None,
        year_to: int | None = None,
        excluded_terms: list[str] | None = None,
        truncate: bool = True,
    ) -> SearchResponse:
        """执行统一检索入口。

        中文说明：
        新版用结构化概念组表达检索意图（组间 AND、组内 OR 同义词），每个 connector
        把它渲染成自己源的原生语法。limit 是用户期望的数量，内部会乘以 OVER_FETCH_FACTOR
        超量拉取，合并后截断到用户期望的数量。
        """

        # 中文说明：超量拉取，保证过滤后仍能拿到用户期望的数量。
        effective_limit = max(1, limit) * self.OVER_FETCH_FACTOR
        request = SearchRequest(
            topic=topic,
            concept_groups=[list(g) for g in (concept_groups or [])],
            source=source,
            sources=list(sources or []),
            limit=effective_limit,
            year_from=year_from,
            year_to=year_to,
            excluded_terms=list(excluded_terms or []),
        )
        selected = self._select_connectors(request.source, request.sources)
        response = SearchResponse(query=self._request_summary(request))
        with logging_context(
            search_query=response.query,
            search_source=self._log_search_source(request),
            limit=request.limit,
            year_from=request.year_from,
            year_to=request.year_to,
        ):
            logger.info(
                "开始执行论文检索",
                extra={
                    "group_count": len(request.concept_groups),
                    "excluded_term_count": len(request.excluded_terms),
                    "effective_limit": effective_limit,
                },
            )
            if not selected:
                response.errors["sources"] = "No valid paper retrieval source selected."
                logger.warning("未找到可用的论文数据源")
                return response
            if len(selected) == 1:
                source_name, connector = next(iter(selected.items()))
                # 中文说明：单源路径也加重试（D6）。和 _async_search_many 一致的策略。
                papers = None
                for attempt in range(3):
                    try:
                        papers = connector.search(self._single_source_request(request, source_name))
                        break
                    except Exception as exc:
                        # 中文说明：判断这次失败值不值得重试，具体规则见 _is_retryable_search_error。
                        if not _is_retryable_search_error(exc) or attempt == 2:
                            logger.exception(
                                "单源论文检索失败",
                                extra={"source": source_name, "attempt": attempt + 1},
                            )
                            raise
                        # 中文说明：退避刻意做得比常规慢（5s/15s/45s），避免短退避正好撞上 429 的持续期限。
                        delay = min(60, 5 * (3 ** attempt)) + random.uniform(0, 1.0)
                        logger.warning(
                            "单源检索遇到可重试错误，%0.1f 秒后重试",
                            delay,
                            extra={"source": source_name, "attempt": attempt + 1, "error": str(exc)},
                        )
                        time.sleep(delay)
                if papers is None:
                    raise RuntimeError("unreachable: retry loop should have raised or returned")
                response.sources_used = [source_name]
                response.source_results[source_name] = len(papers)
                # 中文说明：截断到用户期望的 limit（不是 effective_limit）。
                response.papers = self._deduplicate_papers(
                    papers, limit if truncate else None, concept_groups=request.concept_groups
                )
                logger.info(
                    "单源论文检索完成",
                    extra={"source": source_name, "raw_count": len(papers), "deduped_count": len(response.papers)},
                )
                return response
            gathered = self._search_many(selected, request)
            response.sources_used = list(selected.keys())
            merged: list[PaperDocument] = []
            rrf_scores: dict[str, float] = {}
            for source_name, outcome in gathered.items():
                if isinstance(outcome, Exception):
                    response.errors[source_name] = str(outcome)
                    response.source_results[source_name] = 0
                    continue
                response.source_results[source_name] = len(outcome)
                merged.extend(outcome)
                # 中文说明：与异步路径同一套排名融合规则，详细说明见 async_search。
                for rank, paper in enumerate(outcome):
                    key = self._paper_key(paper)
                    rrf_scores[key] = rrf_scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
            response.papers = self._deduplicate_papers(
                merged, limit if truncate else None,
                concept_groups=request.concept_groups,
                rrf_scores=rrf_scores,
            )
            logger.info(
                "多源论文检索完成",
                extra={
                    "source_count": len(selected),
                    "merged_count": len(merged),
                    "deduped_count": len(response.papers),
                    "error_sources": sorted(response.errors.keys()),
                },
            )
            return response

    async def async_search(
        self,
        *,
        topic: str = "",
        concept_groups: list[list[str]] | None = None,
        source: str | None = None,
        sources: list[str] | None = None,
        limit: int = 10,
        year_from: int | None = None,
        year_to: int | None = None,
        excluded_terms: list[str] | None = None,
        truncate: bool = True,
        runtime_resources: WorkflowRuntimeResources | None = None,
    ) -> SearchResponse:
        """异步执行统一检索入口，并限制多来源并发数量。"""

        # 中文说明：超量拉取，保证过滤后仍能拿到用户期望的数量。
        effective_limit = max(1, limit) * self.OVER_FETCH_FACTOR
        request = SearchRequest(
            topic=topic,
            concept_groups=[list(g) for g in (concept_groups or [])],
            source=source,
            sources=list(sources or []),
            limit=effective_limit,
            year_from=year_from,
            year_to=year_to,
            excluded_terms=list(excluded_terms or []),
        )
        selected = self._select_connectors(request.source, request.sources)
        response = SearchResponse(query=self._request_summary(request))
        with logging_context(
            search_query=response.query,
            search_source=self._log_search_source(request),
            limit=request.limit,
            year_from=request.year_from,
            year_to=request.year_to,
        ):
            logger.info(
                "开始执行异步论文检索",
                extra={
                    "group_count": len(request.concept_groups),
                    "excluded_term_count": len(request.excluded_terms),
                    "effective_limit": effective_limit,
                },
            )
            if not selected:
                response.errors["sources"] = "No valid paper retrieval source selected."
                logger.warning("未找到可用的论文数据源")
                return response
            if len(selected) == 1:
                source_name, connector = next(iter(selected.items()))
                # 中文说明：单源路径也走带重试的统一入口，
                # 避免「只配了一个数据源时网络超时不重试」和「多源检索会重试」的行为不一致。
                papers = await self._async_search_with_retry(
                    source_name,
                    connector,
                    request,
                    runtime_resources.http_client if runtime_resources is not None else None,
                )
                response.sources_used = [source_name]
                response.source_results[source_name] = len(papers)
                # 中文说明：先把重复记录合并成"每篇只留一条代表"，再只对代表做
                # embedding 重排。这样每个代表对应唯一一个分数，不会和自己的兄弟
                # 记录互相覆盖，也省掉了对重复内容的重复向量化。
                # 单源没有可融合的排名，所以不传 rrf_scores。
                representatives = self._merge_duplicates(papers)
                # 中文说明：没配嵌入密钥时这一步内部直接返回空 dict，排序退化成纯词面。
                embedding_scores = await self._embedding_rerank(representatives, request.topic)
                response.papers = self._rank_and_truncate(
                    representatives,
                    concept_groups=request.concept_groups,
                    embedding_scores=embedding_scores,
                    limit=limit if truncate else None,
                )
                logger.info(
                    "异步单源论文检索完成",
                    extra={"source": source_name, "raw_count": len(papers), "deduped_count": len(response.papers)},
                )
                return response
            gathered = await self._async_search_many(selected, request, runtime_resources=runtime_resources)
            response.sources_used = list(selected.keys())
            merged: list[PaperDocument] = []
            rrf_scores: dict[str, float] = {}
            for source_name, outcome in gathered.items():
                if isinstance(outcome, Exception):
                    response.errors[source_name] = str(outcome)
                    response.source_results[source_name] = 0
                    continue
                response.source_results[source_name] = len(outcome)
                merged.extend(outcome)
                # 中文说明：累加每个源的排名融合分。outcome 里论文的先后顺序就是
                # 这个源自己给出的名次（arXiv 按相关度、OpenAlex 按相关度、S2 按引用数），
                # 排第 rank 名就贡献 1/(RRF_K + rank + 1)。同一篇论文被多个源命中时，
                # 这几个分数会加在一起，所以"多个源都收录、而且名次靠前"的论文分最高。
                for rank, paper in enumerate(outcome):
                    key = self._paper_key(paper)
                    rrf_scores[key] = rrf_scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
            # 中文说明：先合并重复记录拿到代表列表，再只对代表做 embedding 重排（理由同单源路径）。
            representatives = self._merge_duplicates(merged, rrf_scores)
            embedding_scores = await self._embedding_rerank(representatives, request.topic)
            # 中文说明：截断到用户期望的 limit（不是 effective_limit）。
            response.papers = self._rank_and_truncate(
                representatives,
                concept_groups=request.concept_groups,
                embedding_scores=embedding_scores,
                limit=limit if truncate else None,
            )
            logger.info(
                "异步多源论文检索完成",
                extra={
                    "source_count": len(selected),
                    "merged_count": len(merged),
                    "deduped_count": len(response.papers),
                    "error_sources": sorted(response.errors.keys()),
                },
            )
            return response

    async def async_related(
        self,
        *,
        external_ref: str,
        direction: str,
        limit: int = 10,
        sources: list[str] | None = None,
        runtime_resources: "WorkflowRuntimeResources | None" = None,
    ) -> SearchResponse:
        """以某个外部标识为种子，顺着引用关系扩展检索。

        中文注释：
        这个方法调每个 connector 的 async_related（如果实现了的话），
        合并结果并去重。direction 是 "references" 或 "citations"。
        并发控制和 HTTP 客户端复用由 runtime_resources 提供，和 async_search 保持一致。
        """

        connectors = self._select_connectors(None, sources)
        if not connectors:
            return SearchResponse(query=external_ref, papers=[], errors={"sources": "no available sources"})

        # 并发控制：优先用 run 级共享信号量，没有时自建一个（和 _async_search_many 对齐）。
        semaphore = (
            runtime_resources.search_source_semaphore
            if runtime_resources is not None
            else asyncio.Semaphore(min(len(connectors), 4))
        )
        # HTTP 客户端复用：优先用 run 级共享客户端，没有时各 connector 自建。
        shared_client = runtime_resources.http_client if runtime_resources is not None else None

        async def _fetch_one(connector):
            async with semaphore:
                try:
                    return await connector.async_related(
                        external_ref=external_ref,
                        direction=direction,
                        limit=limit,
                        client=shared_client,
                    )
                except NotImplementedError:
                    # connector 没实现 async_related，返回空列表。
                    return []
                except Exception as exc:
                    logger.error(
                        "引文扩展单源失败",
                        extra={"source": connector.source_name, "error": str(exc)},
                    )
                    return []

        results = await asyncio.gather(*[_fetch_one(c) for c in connectors.values()])
        all_papers: list[PaperDocument] = []
        errors: dict[str, str] = {}
        for papers in results:
            all_papers.extend(papers)
        # 去重并截断到 limit。
        deduped = self._deduplicate_papers(all_papers, limit, concept_groups=None)
        return SearchResponse(query=external_ref, papers=deduped, errors=errors)

    def available_sources(self) -> list[str]:
        """返回当前可用来源名称。"""

        return sorted(self._connectors.keys())

    def _build_default_connectors(self) -> dict[str, PaperSearchConnector]:
        """构建默认 connector 注册表。"""

        # 中文说明：论文检索密钥与模型密钥分开保存在 system.yaml 中。
        # 每次新建检索服务都会重新读取配置，修改密钥后无需改业务代码。
        retrieval_config = SystemConfig.load().paper_retrieval
        openalex = OpenAlexPaperConnector(api_key=retrieval_config.openalex_api_key)
        semantic = SemanticScholarPaperConnector(api_key=retrieval_config.semantic_scholar_api_key)
        arxiv = ArxivPaperConnector()
        return {
            "openalex": openalex,
            "semantic_scholar": semantic,
            "semantic": semantic,
            "arxiv": arxiv,
        }

    def _select_connectors(
        self,
        source: str | None,
        sources: list[str] | None = None,
    ) -> dict[str, PaperSearchConnector]:
        """根据 source 或 sources 选择 connector。

        传入空 source 和空 sources 时，表示启用所有唯一 connector 做多源检索。
        """

        if source:
            normalized = source.strip().lower()
            connector = self._connectors.get(normalized)
            return {normalized: connector} if connector is not None else {}
        normalized_sources = [item.strip().lower() for item in sources or [] if str(item).strip()]
        if normalized_sources:
            selected: dict[str, PaperSearchConnector] = {}
            seen_connector_ids: set[int] = set()
            for name in normalized_sources:
                connector = self._connectors.get(name)
                if connector is None:
                    continue
                connector_id = id(connector)
                if connector_id in seen_connector_ids:
                    continue
                seen_connector_ids.add(connector_id)
                selected[name] = connector
            return selected
        unique: dict[int, tuple[str, PaperSearchConnector]] = {}
        for name, connector in self._connectors.items():
            unique[id(connector)] = (name, connector)
        return {name: connector for name, connector in unique.values()}

    def _search_many(
        self,
        connectors: dict[str, PaperSearchConnector],
        request: SearchRequest,
    ) -> dict[str, list[PaperDocument] | Exception]:
        """并发执行多源检索。

        中文注释：每个来源都拿完整 limit，后续再统一聚合、去重和评分排序，避免早期平均分配
        导致高质量来源被截断。
        """

        results: dict[str, list[PaperDocument] | Exception] = {}
        with ThreadPoolExecutor(max_workers=min(len(connectors), 4)) as executor:
            future_map = {
                executor.submit(
                    connector.search,
                    self._single_source_request(request, source_name),
                ): source_name
                for source_name, connector in connectors.items()
            }
            for future in as_completed(future_map):
                source_name = future_map[future]
                try:
                    results[source_name] = future.result()
                    logger.debug(
                        "单个来源检索完成",
                        extra={"source": source_name, "result_count": len(results[source_name])},
                    )
                except Exception as exc:
                    results[source_name] = exc
                    logger.exception("单个来源检索失败", extra={"source": source_name})
        return results

    async def _async_search_with_retry(
        self,
        source_name: str,
        connector: PaperSearchConnector,
        request: SearchRequest,
        shared_client: httpx.AsyncClient | None,
    ) -> list[PaperDocument]:
        """异步检索单个来源，遇到瞬时故障自动退避重试。

        中文说明：异步的单源路径和多源路径都走这里，好处是「什么时候该重试、
        怎么退避」全仓库只有这一份规则，不会两边写得不一样。
        重试策略：最多尝试 3 次；只有 429/5xx/网络超时 才重试
        （判断规则见 _is_retryable_search_error）；退避 5s → 15s → 45s，
        每次叠 0~1 秒随机抖动，避免多个来源的重试正好撞在同一时刻。
        试满仍失败就把异常抛出去，由调用方决定是记进 errors 还是直接向上报错。
        """

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                return await connector.async_search(
                    self._single_source_request(request, source_name),
                    client=shared_client,
                )
            except Exception as exc:
                last_exc = exc
                # 中文说明：不可重试的错误（参数拼错、返回内容解析不了等）直接放弃，
                # 因为重试也不会让错误参数变对。
                if not _is_retryable_search_error(exc) or attempt == 2:
                    logger.exception(
                        "异步检索单个来源失败",
                        extra={"source": source_name, "attempt": attempt + 1},
                    )
                    raise
                # 中文说明：退避刻意做得比常规慢（5s/15s/45s），避免短退避正好撞上 429 的持续期限。
                delay = min(60, 5 * (3 ** attempt)) + random.uniform(0, 1.0)
                logger.warning(
                    "单个来源检索遇到可重试错误，%0.1f 秒后重试",
                    delay,
                    extra={"source": source_name, "attempt": attempt + 1, "error": str(exc)},
                )
                await asyncio.sleep(delay)
        # 中文说明：循环里要么返回、要么抛出，正常走不到这里；留着是为了类型检查不报错。
        raise last_exc or RuntimeError("unreachable: retry loop should have raised or returned")

    async def _async_search_many(
        self,
        connectors: dict[str, PaperSearchConnector],
        request: SearchRequest,
        *,
        runtime_resources: WorkflowRuntimeResources | None,
    ) -> dict[str, list[PaperDocument] | Exception]:
        """异步并发执行多源检索，并用信号量限制同时访问的来源数量。"""

        semaphore = (
            runtime_resources.search_source_semaphore
            if runtime_resources is not None
            else asyncio.Semaphore(min(len(connectors), 4))
        )
        shared_client = runtime_resources.http_client if runtime_resources is not None else None

        async def _run_one(source_name: str, connector: PaperSearchConnector) -> tuple[str, list[PaperDocument] | Exception]:
            async with semaphore:
                # 中文说明：重试逻辑统一收在 _async_search_with_retry 里，这里只负责
                # 把失败「按源隔离」——某个源最终失败就把它当成该源的结果返回，
                # 其他源的结果照常合并，不会因为一个源挂掉就拖垮整次检索。
                try:
                    papers = await self._async_search_with_retry(
                        source_name, connector, request, shared_client
                    )
                    return source_name, papers
                except Exception as exc:
                    return source_name, exc

        pairs = await asyncio.gather(*[_run_one(source_name, connector) for source_name, connector in connectors.items()])
        return dict(pairs)

    def _merge_duplicates(
        self, papers: list[PaperDocument],
        rrf_scores: dict[str, float] | None = None,
    ) -> list[PaperDocument]:
        """把多源结果合并成"每篇论文只留一条代表"（这一层不做排序）。

        中文说明：
        同一篇论文可能被多个源同时返回（比如 OpenAlex 和 S2 都收录了某篇 arXiv preprint）。
        这里按 _paper_key 聚合，给每篇论文打上"被哪些源命中"的标记，
        并写入多源排名融合分（rrf_score），供后面的排序使用。
        同一 key 的多篇论文里，选 metadata["cited_by_count"] 最大的作为代表，
        这样 OpenAlex 的高引信息能被保留下来（OpenAlex 的 cited_by_count 数据最全）。
        """

        if not papers:
            return []

        rrf_scores = rrf_scores or {}

        # 第一步：按 paper_key 分组，记录每个 key 的所有论文和来源。
        groups: dict[str, list[tuple[str, PaperDocument]]] = {}
        for paper in papers:
            key = self._paper_key(paper)
            groups.setdefault(key, []).append((paper.source or "", paper))

        # 第二步：为每个分组选代表（cited_by_count 最大的），并写入 sources 标记。
        representatives: list[PaperDocument] = []
        for key, entries in groups.items():
            # 中文说明：sources 列表包含所有命中这篇论文的来源名称，按字母排序便于日志对比。
            sources = sorted({source for source, _ in entries if source})

            def _cited(pair: tuple[str, PaperDocument]) -> int:
                _, p = pair
                return int((p.metadata or {}).get("cited_by_count") or 0)

            _, best = max(entries, key=_cited)

            if best.metadata is None:
                best.metadata = {}
            best.metadata["sources"] = sources
            # 中文说明：融合分是按去重键累加出来的，所以这里也用同一个 key 去取，
            # 代表拿到的就是"这篇论文在各源里的名次合起来"的分数。
            best.metadata["rrf_score"] = rrf_scores.get(key, 0.0)

            # 中文注释：代表是按引用数选出来的，而 arXiv 记录拿不到引用数（它的数据源不提供），
            # 所以同一篇论文同时被 arXiv 和 OpenAlex 收录时，代表一定是 OpenAlex 那条，
            # arXiv 记录里那个稳定好用的 PDF 直链就跟着被丢掉了——这正是「很多论文下不了全文」的主因。
            # 下面把兄弟记录里可用的下载信息补给代表：保留代表的高引信息，同时不浪费能下载的链接。
            _backfill_fulltext_fields(best, [paper for _, paper in entries])
            representatives.append(best)

        return representatives

    def _rank_and_truncate(
        self, papers: list[PaperDocument],
        concept_groups: list[list[str]] | None = None,
        embedding_scores: dict[str, float] | None = None,
        limit: int | None = None,
    ) -> list[PaperDocument]:
        """对已经合并好的论文列表排序，再按 limit 截断。"""

        ranked = self._rank_merged_papers(
            papers,
            concept_groups=concept_groups or [],
            embedding_scores=embedding_scores or {},
        )
        if limit is None:
            return ranked
        return ranked[:limit]

    def _deduplicate_papers(
        self, papers: list[PaperDocument], limit: int | None,
        concept_groups: list[list[str]] | None = None,
        embedding_scores: dict[str, float] | None = None,
        rrf_scores: dict[str, float] | None = None,
    ) -> list[PaperDocument]:
        """合并去重后直接排序截断。

        中文说明：这是"不需要在中间插一步"的调用方用的组合入口。
        需要在去重与排序之间插入 embedding 重排的调用方（async_search 的两条路径），
        自己依次调 _merge_duplicates → 算 embedding → _rank_and_truncate。
        """

        return self._rank_and_truncate(
            self._merge_duplicates(papers, rrf_scores),
            concept_groups=concept_groups,
            embedding_scores=embedding_scores,
            limit=limit,
        )

    def _rank_merged_papers(
        self, papers: list[PaperDocument],
        concept_groups: list[list[str]] | None = None,
        embedding_scores: dict[str, float] | None = None,
    ) -> list[PaperDocument]:
        """对多源合并后的论文做多维度排序。

        中文说明：
        按六个维度排序（纯函数，不调用模型）。返回的是一个元组，Python 会从左到右
        逐个比较，所以越靠前的维度优先级越高：
        1. RRF 融合分：把这篇论文在各个源里的名次换算成分数再加起来
           （在某个源里排第 rank 名，就贡献 1/(RRF_K + rank)）。它同时反映了
           "被几个源命中"和"在每个源里排得多靠前"，比单纯数命中源数更细腻。
           单源检索、以及拿不到排名的引文扩展，这一维恒为 0。
        2. 命中源数量：多源检索时它通常已经被维度 1 决定了；它真正起作用的场合是
           RRF 没有值的时候（引文扩展），让排序退回改造前的口径。
        3. 概念组匹配度：每篇论文在每个概念组里命中几个同义词（出现在 title+abstract 里）。
           每组最多算 1 分，总匹配组数越多越靠前。
        4. 引用数：metadata["cited_by_count"]（OpenAlex + S2 都有值了）。
        5. 年份新近度：越新越靠前。
        6. embedding 余弦相似度：语义相关度信号，只在前面几维全部相同时才起作用。
           没配嵌入模型或调用失败时这一维给 0 分，不影响主流程。
        """

        if not papers:
            return []

        concept_groups = concept_groups or []
        embedding_scores = embedding_scores or {}

        def _score(paper: PaperDocument) -> tuple[float, int, int, int, int, float]:
            """返回一个六维元组用于排序（大的在前）。"""

            # 维度 1：多源排名融合分（合并阶段写在 metadata 里的 rrf_score）。
            rrf_score = 0.0
            if paper.metadata:
                rrf_score = float(paper.metadata.get("rrf_score") or 0.0)

            # 维度 2：命中源数量。RRF 只在"多源搜索"时才有值；引文扩展
            # （async_related）拿不到各源的排名，那一维就恒为 0，于是这里退回按
            # 命中源数排序，保持引文扩展改造前的行为不变。
            sources = paper.metadata.get("sources") if paper.metadata else []
            source_count = len(sources) if isinstance(sources, list) else 1

            # 维度 3：概念组匹配度（每组最多算 1 分）。
            title = (paper.title or "").lower()
            abstract = (paper.abstract or "").lower()
            text = title + " " + abstract
            group_hits = 0
            for group in concept_groups:
                if any(term.lower() in text for term in group if str(term).strip()):
                    group_hits += 1

            # 维度 4：引用数。
            cited_by = 0
            if paper.metadata:
                cited_by = int(paper.metadata.get("cited_by_count") or 0)

            # 维度 5：年份。
            year = int(paper.year) if paper.year else 0

            # 维度 6：embedding 余弦相似度（没配模型或调用失败时为 0）。
            paper_key = paper.paperId or paper.id
            embedding_sim = float(embedding_scores.get(paper_key, 0.0))

            return (rrf_score, source_count, group_hits, cited_by, year, embedding_sim)

        return sorted(papers, key=_score, reverse=True)

    # 中文说明：arXiv id 的标准格式（2007 年后的论文）：
    # 形如 "2401.12345" 或 "2401.12345v3"（4 位年份点号 + 4~5 位序号 + 可选 v + 版本号）。
    # 用这个正则识别 arXiv id，比旧版 "arxiv" in paperId 可靠得多：
    # arXiv connector 写入的 paperId 是裸 id（如 "2401.12345v3"），不含 "arxiv" 字样，
    # 旧版判据永不成立，导致 preprint 与期刊版跨源合并只能靠标题精确归一，经常漏掉。
    _ARXIV_ID_PATTERN = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$")

    def _paper_key(self, paper: PaperDocument) -> str:
        """生成稳定的去重键。

        中文说明：
        按优先级：DOI 归一 > arXiv id 去版本号 > 标题归一。
        arXiv 自己注册的 DOI（10.48550/arxiv.*）不当普通 DOI 看，而是按 arXiv 编号处理——
        这样"带 arXiv-DOI 的记录"和"带裸编号的记录"才会合并成同一条。
        """

        # 优先用 DOI（归一化：去前缀、小写）。
        doi = (paper.doi or "").strip()
        if doi:
            doi = doi.removeprefix("https://doi.org/").removeprefix("http://doi.org/").lower()
            # 中文说明：arXiv 给论文注册的 DOI 形如 10.48550/arxiv.2401.12345，而 arXiv 源
            # 自己返回的是裸编号（2401.12345）——这两条记录其实是同一篇论文。若按 DOI 和按
            # 编号各算各的键，它们永远合不到一起，同一篇论文就会在结果里出现两次
            # （实测那篇 RAG 综述正是这样重复的）。所以这种 DOI 一律归到 arxiv 键，
            # 并且和下面的裸编号分支一样去掉版本号。
            if doi.startswith(_ARXIV_DOI_PREFIX):
                return f"arxiv:{re.sub(r'v\d+$', '', doi[len(_ARXIV_DOI_PREFIX):])}"
            return f"doi:{doi}"
        # 中文说明：用正则判据识别 arXiv id，并去掉版本号（2401.12345v3 → 2401.12345）。
        # 这样 preprint 和正式发表版会被视为同一篇论文，避免跨源合并时漏掉。
        paper_id = (paper.paperId or "").strip()
        if paper_id and self._ARXIV_ID_PATTERN.match(paper_id):
            base_id = re.sub(r"v\d+$", "", paper_id)
            return f"arxiv:{base_id}"
        # 退而用标题归一（小写、去标点、折叠空白）。
        title = re.sub(r"[^\w\s]", "", paper.title.lower())
        title = re.sub(r"\s+", " ", title).strip()
        return f"title:{title}"

    def _request_summary(self, request: SearchRequest) -> str:
        """把结构化请求压缩成调试用摘要。

        中文说明：
        旧版摘要基于 query/keywords/keyword_expression。新版基于 topic + concept_groups。
        用于日志、错误信息、工具返回等处的简短描述。
        """

        parts: list[str] = []
        if request.topic.strip():
            parts.append(request.topic.strip())
        if request.concept_groups:
            # 中文说明：每个概念组取第一个同义词（规范写法）作为代表。
            group_reprs = [group[0] for group in request.concept_groups if group]
            if group_reprs:
                parts.append("concepts=[" + ", ".join(group_reprs[:4]) + "]")
        return " | ".join(parts) if parts else ""

    def _single_source_request(self, request: SearchRequest, source_name: str) -> SearchRequest:
        """把多源请求拆成单个来源的请求。

        中文说明：
        新版 SearchRequest 的字段都是单源的，只需要改 source 字段即可。
        """

        return SearchRequest(
            topic=request.topic,
            concept_groups=[list(g) for g in request.concept_groups],
            source=source_name,
            sources=[],
            limit=request.limit,
            year_from=request.year_from,
            year_to=request.year_to,
            excluded_terms=list(request.excluded_terms),
        )

    def _log_search_source(self, request: SearchRequest) -> str:
        """整理日志里展示的来源信息，方便排查到底是单源还是多源查询。"""

        if request.source:
            return request.source
        if request.sources:
            return ",".join(request.sources)
        return "all"
