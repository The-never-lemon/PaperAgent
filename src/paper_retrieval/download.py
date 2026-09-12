from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from src.graph.runtime_resources import WorkflowRuntimeResources
from urllib.parse import urlparse

import httpx

from src.paper_retrieval.models import PaperDocument
# 中文注释：引入项目统一的日志工具。以前下载模块里一条日志都没有，
# 出了问题（比如下回来的其实是 HTML 介绍页而不是全文）完全没法排查。
# 现在下载成功、失败都会在日志里留下记录，方便定位问题。
from src.utils import get_logger
from src.utils.read_utils.cache import paper_cache_dir, read_cached_source_url, write_metadata


# 中文注释：模块级日志器，本文件里所有下载相关的日志都通过它输出。
logger = get_logger(__name__)

_DOWNLOAD_SEMAPHORE = asyncio.Semaphore(4)

# 中文注释：arXiv 新式编号的长相（2401.12345，可带版本号 v3）。
# 老式编号（cs.CL/0701001）在数据源里常常只剩后半截，硬拼链接会拼出一个打不开的地址，
# 所以这里只认新式编号；老式的继续靠论文自带的 pdf_url 兜底。
_ARXIV_ID_PATTERN = re.compile(r"^\d{4}\.\d{4,5}(?:v\d+)?$")

# arXiv 给论文注册的 DOI 前缀，形如 10.48550/arXiv.2401.12345。
_ARXIV_DOI_PREFIX = "10.48550/arxiv."

# 中文注释：DOI 跳转地址的长相（doi.org / dx.doi.org）。
# 有些数据源拿不到真正的 PDF 时，会把 DOI 落地页填进"PDF 直链"字段
# （见 connectors/openalex.py 的 _pick_pdf_url 兜底），这种地址不能当全文地址用。
_DOI_LINK_PATTERN = re.compile(r"^https?://(?:dx\.)?doi\.org/", re.IGNORECASE)


def _is_doi_link(url: str) -> bool:
    """判断一个地址是不是 DOI 跳转地址。"""

    return bool(_DOI_LINK_PATTERN.match(url.strip()))


@dataclass(slots=True)
class DownloadedPaper:
    """保存一次全文获取的结果，调用方只需根据状态继续处理。"""

    status: str
    reason: str = ""
    source_url: str | None = None
    file_path: Path | None = None
    content_type: str | None = None
    reused_cache: bool = False


# 中文注释：这里以前有一个"同步版下载入口"的兼容壳（download_paper_fulltext），
# 全仓已经没有调用方（所有地方都直接 await 下面的异步入口），按"要改就改干净"
# 的原则删掉，避免留着一份没人用、以后还要跟着维护的重复入口。
async def async_download_paper_fulltext(
    paper: PaperDocument,
    *,
    cache_dir: str | Path,
    connect_timeout_seconds: int,
    download_timeout_seconds: int,
    max_file_size_mb: int,
    runtime_resources: WorkflowRuntimeResources | None = None,
) -> DownloadedPaper:
    """异步下载论文全文，并优先复用当前 run 的下载资源。"""

    paper_dir = paper_cache_dir(cache_dir, paper)
    cached = _find_cached_file(paper_dir)
    if cached is not None:
        return DownloadedPaper(
            status="downloaded",
            source_url=read_cached_source_url(paper_dir),
            file_path=cached,
            content_type="application/pdf" if cached.suffix.lower() == ".pdf" else "text/html",
            reused_cache=True,
        )

    source_url = _find_fulltext_url(paper)
    if source_url is None:
        # 中文注释：走到这里说明数据源没有给出任何开放获取的全文直链。
        # 原因用中文写清楚，上层精读流程会把这句话带给用户，并自动降级为"摘要精读"。
        reason = "该论文未提供开放获取的全文链接，将降级为摘要精读"
        logger.warning("论文没有可下载的全文地址", extra={"paper_id": _paper_cache_name(paper), "reason": reason})
        return DownloadedPaper(status="no_url", reason=reason)
    if not _is_safe_http_url(source_url):
        return _download_failed("全文地址只允许使用 http 或 https", source_url)

    maximum_bytes = max(1, max_file_size_mb) * 1024 * 1024
    timeout = httpx.Timeout(float(max(1, download_timeout_seconds)), connect=float(max(1, connect_timeout_seconds)))
    # 中文注释：优先复用当前 run 里的下载信号量和 AsyncClient；如果这次调用不是从
    # run 级工作流进来的，就退回原来的模块级信号量和局部 client 逻辑，保证旧入口不受影响。
    semaphore = runtime_resources.download_semaphore if runtime_resources is not None else _DOWNLOAD_SEMAPHORE
    client = runtime_resources.http_client if runtime_resources is not None else httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        max_redirects=5,
    )
    owns_client = runtime_resources is None
    async with semaphore:
        try:
            async with client.stream(
                "GET",
                source_url,
                headers={"Accept": "application/pdf, text/html;q=0.9"},
                timeout=timeout,
            ) as response:
                final_url = str(response.url)
                if not _is_safe_http_url(final_url):
                    return _download_failed("跳转后的全文地址不安全", final_url)
                if response.status_code < 200 or response.status_code >= 300:
                    return _download_failed(f"下载地址返回 HTTP {response.status_code}", final_url)
                declared_length = _safe_content_length(response.headers.get("content-length"))
                if declared_length is not None and declared_length > maximum_bytes:
                    return _download_failed("文件超过允许大小", final_url)
                content = await _read_limited_content_async(response, maximum_bytes)
                if content is None:
                    return _download_failed("文件超过允许大小", final_url)
                content_type = response.headers.get("content-type", "")
                content_kind = _detect_content_kind(content, content_type)
                if content_kind is None:
                    return _download_failed("下载内容不是可读取的 PDF 或 HTML", final_url)
        except httpx.TimeoutException:
            return _download_failed("下载全文超时", source_url)
        except httpx.HTTPError as exc:
            return _download_failed(f"下载全文失败：{exc}", source_url)
        finally:
            if owns_client:
                await client.aclose()

    # 文件写入仍是本地阻塞操作，先放到线程里，避免在异步流程里直接卡住事件循环。
    await asyncio.to_thread(paper_dir.mkdir, parents=True, exist_ok=True)
    suffix = ".pdf" if content_kind == "pdf" else ".html"
    file_path = paper_dir / f"original{suffix}"
    await asyncio.to_thread(file_path.write_bytes, content)
    await asyncio.to_thread(write_metadata, paper_dir, paper, source_url=final_url, content_type=content_type)
    # 中文注释：下载成功时记一条 info 日志，写清楚从哪个站点、下回来的是 PDF 还是 HTML、有多少字节。
    # 这样"以为下了全文、实际下回来的是 HTML 介绍页"这类情况，在日志里一眼就能看出端倪。
    logger.info(
        "论文全文下载完成",
        extra={
            "paper_id": _paper_cache_name(paper),
            "host": urlparse(final_url).netloc,
            "content_kind": content_kind,
            "bytes": len(content),
        },
    )
    return DownloadedPaper(
        status="downloaded",
        source_url=final_url,
        file_path=file_path,
        content_type="application/pdf" if content_kind == "pdf" else "text/html",
    )


def _arxiv_pdf_url(paper: PaperDocument) -> str | None:
    """论文有 arXiv 编号时，拼出 arXiv 的 PDF 直链。

    中文说明：arXiv 的 PDF 链接几乎总是能直接下载，而出版社给的"开放获取"链接
    经常跳到要登录的落地页、或者直接返回 403——这是"下载经常失败"最主要的原因。
    所以只要能认出 arXiv 编号，就优先走 arXiv，全文下载成功率会明显高一些。

    编号有三个来源，按可靠程度依次尝试：
    1. metadata 里的 arxiv_id —— arXiv 和 Semantic Scholar 两个数据源都会写这个字段；
    2. paperId 本身就是 arXiv 编号（arXiv 源在没有 DOI 时直接用它当编号）；
    3. doi 是 arXiv 的 DOI 形式（10.48550/arXiv.xxxx）。

    认不出编号时返回 None，交给后面的常规链接继续找。
    """

    metadata = paper.metadata or {}
    candidates: list[Any] = [metadata.get("arxiv_id"), metadata.get("arxivId"), paper.paperId]
    doi = str(paper.doi or "").strip()
    if doi.lower().startswith(_ARXIV_DOI_PREFIX):
        candidates.append(doi[len(_ARXIV_DOI_PREFIX):])
    for value in candidates:
        text = str(value).strip() if value is not None else ""
        if text and _ARXIV_ID_PATTERN.match(text):
            return f"https://arxiv.org/pdf/{text}"
    return None


def _find_fulltext_url(paper: PaperDocument) -> str | None:
    """按可靠程度从论文对象中找出第一个可用的开放获取全文直链。

    中文注释：第一优先走 arXiv（见 _arxiv_pdf_url）。arXiv 的链接最稳，
    而出版社的开放获取链接经常跳登录页或直接 403，先试 arXiv 能减少下载失败。

    DOI 跳转地址（doi.org）一律跳过：它不是论文文件本身，拿它去下载只会被
    出版社的门禁挡回来，或者下回来一个介绍页。跳过之后如果没有别的地址，
    调用方会给出"未提供开放获取全文"的状态——对用户来说，
    "这篇没有开放全文"比一个看不懂的 403 失败更准确。

    这里特意不再把 paper.url 当兜底。paper.url 只是论文的"介绍网页"
    （比如 OpenAlex / Semantic Scholar 的详情页），网页内容不是论文正文；
    以前把它当全文地址，导致精读流程把落地页 HTML 当成论文去读。
    pdf_url 和 metadata 里的开放获取 PDF 链接是数据源自己标记的、可以免费下载
    的正文直链，只有这些才值得尝试。一个都找不到时返回 None，
    由调用方给出"无全文链接"的状态，上层会自动降级为摘要精读。
    """

    arxiv_url = _arxiv_pdf_url(paper)
    if arxiv_url:
        return arxiv_url

    candidates: list[Any] = [paper.pdf_url]
    metadata = paper.metadata or {}
    candidates.extend([metadata.get("open_access_pdf"), metadata.get("openAccessPdf"), metadata.get("pdf_url")])
    for value in candidates:
        if isinstance(value, dict):
            value = value.get("url")
        text = str(value).strip() if value is not None else ""
        if not text:
            continue
        if _is_doi_link(text):
            # 中文注释：把跳过的地址记进日志，排查"为什么这篇没下载全文"时能一眼看到原因。
            logger.debug(
                "跳过 DOI 落地页，不作为全文下载地址",
                extra={"paper_id": _paper_cache_name(paper), "url": text},
            )
            continue
        return text
    return None


def _download_failed(reason: str, source_url: str | None = None) -> DownloadedPaper:
    """统一生成"下载失败"的结果，并把失败原因写进 warning 日志。

    中文注释：下载失败的出口很多（地址不安全、HTTP 报错、超时、内容不对等），
    全部从这一个函数返回，就不会漏掉任何一个出口的日志。
    排查"为什么没下到全文"时，在日志里能看到每一次失败的具体原因。
    """

    logger.warning("论文全文下载失败", extra={"reason": reason, "source_url": source_url})
    return DownloadedPaper(status="download_failed", reason=reason, source_url=source_url)


def _paper_cache_name(paper: PaperDocument) -> str:
    """取出论文的可读标识（编号、DOI 或标题），写日志时使用。

    中文注释：日志里带上这个标识，和工作区里的论文编号是一致的，
    看到某条下载日志就能直接对应到具体是哪一篇论文。
    """

    return str(paper.paperId or paper.id or paper.doi or paper.title).strip()


def _find_cached_file(paper_dir: Path) -> Path | None:
    """读取已成功保存的原始全文，避免相同论文重复下载。"""

    for name in ("original.pdf", "original.html", "source.pdf", "source.html"):
        candidate = paper_dir / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def _is_safe_http_url(url: str) -> bool:
    """只允许网络下载所需的 http 和 https 地址。"""

    parsed = urlparse(url)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _safe_content_length(value: str | None) -> int | None:
    """把响应头中的文件大小转换为整数，无法识别时不提前拦截。"""

    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


async def _read_limited_content_async(response: httpx.Response, maximum_bytes: int) -> bytes | None:
    """异步分段读取网络内容，超过限制时立刻停止并返回空值。"""

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > maximum_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _detect_content_kind(content: bytes, content_type: str) -> str | None:
    """根据真实内容判断下载结果是 PDF、HTML 还是无效页面。"""

    if not content:
        return None
    if content.startswith(b"%PDF-"):
        return "pdf"
    lowered = content[:1024].lower()
    if "html" in content_type.lower() or b"<!doctype html" in lowered or b"<html" in lowered:
        return "html"
    return None
