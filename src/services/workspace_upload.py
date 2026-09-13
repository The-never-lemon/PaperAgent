"""把用户上传的本地 PDF 收下来，放进论文缓存目录和会话工作区。

中文说明：
用户在界面上传一篇本地 PDF 后，这里要做三件事：
1. 把文件边收边写到 data/paper_cache/{论文编号}/original.pdf —— 这个位置正是
   精读流程"找全文"时第一个会去看的地方（见 paper_retrieval/download.py 里的
   缓存查找），所以写完以后，精读、追问这些流程一行代码都不用改就能用上这篇论文；
2. 从 PDF 第一页尽量认出标题、作者、年份、摘要（见 utils/read_utils/pdf_metadata.py），
   让这篇论文在列表里显示得像一篇正常论文，也能参与综述写作；
3. 把这篇论文登记进会话工作区。

本模块不启动 run、不调用模型，和"加星""删除"一样属于纯状态动作。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

from src.llm.config import SystemConfig
from src.models.workspace import SessionWorkspace
from src.paper_retrieval.models import PaperDocument
from src.utils import get_logger
from src.utils.read_utils.cache import PRIMARY_PDF_NAME, paper_cache_dir, write_metadata
from src.utils.read_utils.pdf_metadata import PdfFirstPageInfo, extract_first_page_info


logger = get_logger(__name__)

# 上传论文的编号前缀。中文注释：用 local 打头，一眼就能和检索来的论文编号
# （arxiv 的 2401.00001v1、DOI 之类）区分开。
UPLOAD_PAPER_ID_PREFIX = "local-"

# 用文件内容算出的哈希取前多少位当编号。
# 中文注释：16 位十六进制约等于 64 个二进制位，重复的概率低到不用考虑；
# 编号总长只有 22 个字符，远小于缓存目录名 160 字符的截断线，所以缓存目录名
# 和你在工作区里看到的论文编号一定是同一个，排查问题时对得上。
UPLOAD_HASH_CHARS = 16

# 标题最多保留多少字。中文注释：有的文件名是一整段话，截一下免得列表被撑爆。
TITLE_MAX_CHARS = 300

# 收文件时用的临时文件名前缀。
# 中文注释：临时文件必须和最终文件放在同一个目录里，这样最后一步换名字
# （os.replace）才是"一步到位"的原子操作。跨磁盘换名会失败。
TEMP_UPLOAD_PREFIX = ".upload-"

# PDF 文件开头那几个字节。中文注释：和下载流程认格式用的是同一个判据，
# 保证"上传认的格式"和"下载认的格式"完全一致。只认内容，不认文件后缀。
PDF_MAGIC = b"%PDF-"

# 同一个会话的上传锁。
# 中文注释：一次上传做的事是"读工作区 → 改 → 写回"。两次上传同时进来会互相
# 覆盖（后写的那次用的是自己读到的旧内容，会把前一次加的论文冲掉），所以
# 同一个会话的上传要排队执行。
_upload_locks: dict[str, asyncio.Lock] = {}


@dataclass(slots=True)
class UploadResult:
    """一次上传的结果，字段和接口返回的内容一一对应。"""

    paper_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    # 中文注释：True 表示这是一篇新加入的论文；False 表示工作区里本来就有这一篇
    # （同一个文件重复上传会得到同一个编号，所以不会重复添加）。
    is_new: bool = True
    page_count: int | None = None
    # 中文注释：False 说明这份 PDF 读不出文字（扫描版）；None 表示判断不了。
    # 界面上只有明确读到 False 时才提醒用户"精读会打折扣"。
    has_text_layer: bool | None = None
    # 从第一页认出来的摘要。界面上让用户核对，也能直接存进工作区参与综述写作。
    abstract: str = ""
    # 给用户看的一句中文提示，界面上直接显示。
    notice: str = ""


async def save_uploaded_pdf(
    workspace: SessionWorkspace,
    *,
    filename: str,
    chunks: AsyncIterator[bytes],
) -> UploadResult:
    """把上传的 PDF 存进缓存目录，并登记到工作区。

    Args:
        workspace: 已经加载好的会话工作区（会话是否存在由路由层负责校验）。
        filename: 浏览器传来的原始文件名，认不出标题时拿它兜底。
        chunks: 文件内容，一块一块地送进来。
            中文注释：故意收"一块一块的字节"而不是一整份，这样 50MB 的文件
            也是边收边写，不会整个读进内存。

    Raises:
        HTTPException: 400 空文件或不是 PDF，413 超过大小上限，500 存盘失败。
    """

    read_config = SystemConfig.load().read
    cache_root = Path(read_config.paper_cache_dir)
    max_bytes = max(1, int(read_config.max_file_size_mb)) * 1024 * 1024

    # 第 1 步：边收边写进临时文件，同时算内容哈希、卡大小上限、验 PDF 身份。
    # 中文注释：缓存目录不存在时（同事第一次运行、或者手动删过）这里顺手建出来。
    cache_root.mkdir(parents=True, exist_ok=True)
    temp_path = cache_root / f"{TEMP_UPLOAD_PREFIX}{uuid.uuid4().hex}.part"
    try:
        digest = await _receive_to_temp_file(chunks, temp_path, max_bytes=max_bytes)
        paper_id = f"{UPLOAD_PAPER_ID_PREFIX}{digest[:UPLOAD_HASH_CHARS]}"

        # 第 2 步：把临时文件搬进这篇论文的缓存目录，文件名用全文查找认得的那个。
        # 中文注释：先用只带编号的论文对象算出目录（目录名只由编号决定）。
        pdf_dir = paper_cache_dir(cache_root, _build_paper_document(paper_id=paper_id, filename=filename))
        await asyncio.to_thread(pdf_dir.mkdir, parents=True, exist_ok=True)
        pdf_path = pdf_dir / PRIMARY_PDF_NAME
        await asyncio.to_thread(os.replace, temp_path, pdf_path)
    except BaseException:
        # 中文注释：不管是超了大小、不是 PDF，还是用户中途取消（uvicorn 抛的
        # CancelledError 不算 Exception，所以这里必须接 BaseException），
        # 临时文件都不能留在磁盘上。
        temp_path.unlink(missing_ok=True)
        raise

    # 第 3 步：读第一页，尽量把标题、作者、年份、摘要认出来。
    # 中文注释：这一步只是锦上添花，认不出或者读失败都不该让上传整体失败，
    # 所以异常在这里就地吃掉，退回到"只有文件名"的状态。
    try:
        info = await asyncio.to_thread(extract_first_page_info, pdf_path)
    except Exception as exc:
        logger.warning(
            "读取 PDF 首页信息失败，改用文件名兜底",
            extra={"paper_id": paper_id, "error": str(exc)[:200]},
        )
        info = PdfFirstPageInfo()

    # 第 4 步：拼出这篇论文的元数据。标题优先用从 PDF 里认出来的，认不出用文件名。
    paper = _build_paper_document(
        paper_id=paper_id,
        filename=filename,
        title=info.title or _title_from_filename(filename),
        info=info,
    )

    # 第 5 步：写一份 metadata.json。中文注释：网上下回来的论文缓存里都有这个文件，
    # 上传的也补一份，缓存目录的结构就完全一致，以后加"清理缓存"之类的功能不会踩坑。
    try:
        await asyncio.to_thread(
            write_metadata, pdf_dir, paper, source_url=None, content_type="application/pdf"
        )
    except OSError as exc:
        logger.warning("写入缓存元数据失败", extra={"paper_id": paper_id, "error": str(exc)[:200]})

    # 第 6 步：确认"精读流程真的能认出这份文件"。
    # 中文注释：这里用的是精读流程自己那个查找函数。万一以后缓存文件名的规矩变了，
    # 这一步会当场报错并留下日志，而不是等用户点完精读才莫名其妙地拿到一份摘要报告。
    from src.paper_retrieval.download import _find_cached_file

    if _find_cached_file(pdf_dir) is None:
        logger.error("上传的 PDF 没有被全文查找逻辑认出", extra={"paper_id": paper_id, "path": str(pdf_dir)})
        raise HTTPException(status_code=500, detail="论文已经保存，但系统没能识别到它，请把这个情况反馈给开发者")

    # 第 7 步：登记进会话工作区。同一会话的上传排队执行，免得互相覆盖。
    lock = _upload_locks.setdefault(workspace.session_key, asyncio.Lock())
    async with lock:
        outcomes = await asyncio.to_thread(workspace.upsert_paper, paper.to_dict())
    paper_id, is_new = outcomes

    notice = _build_notice(info=info, is_new=is_new, page_count=info.page_count)
    logger.info(
        "本地上传论文已加入工作区",
        extra={
            "session_key": workspace.session_key,
            "paper_id": paper_id,
            "is_new": is_new,
            "has_text_layer": info.has_text_layer,
        },
    )
    return UploadResult(
        paper_id=paper_id,
        title=paper.title,
        authors=list(info.authors),
        year=info.year,
        is_new=is_new,
        page_count=info.page_count,
        has_text_layer=info.has_text_layer,
        abstract=info.abstract,
        notice=notice,
    )


async def _receive_to_temp_file(chunks: AsyncIterator[bytes], temp_path: Path, *, max_bytes: int) -> str:
    """边收边把文件写进临时文件，返回文件内容的 SHA-256。

    中文注释：三个限制都在这一个循环里做完——
    1. 收到第一块时先看开头是不是 %PDF-，不是就直接拒绝，不用把整份收完；
    2. 累计大小超过上限立刻停下，不再继续收；
    3. 顺手算哈希，用来当论文编号——同一个文件重复上传就会得到同一个编号。
    """

    hasher = hashlib.sha256()
    total = 0
    first_chunk = True
    with temp_path.open("wb") as handle:
        async for chunk in chunks:
            if not chunk:
                continue
            if first_chunk:
                first_chunk = False
                if not chunk.startswith(PDF_MAGIC):
                    raise HTTPException(status_code=400, detail="这个文件不是 PDF，请选择 PDF 格式的论文")
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"文件超过上限（{max_bytes // 1024 // 1024} MB），请换一个小一点的文件",
                )
            hasher.update(chunk)
            handle.write(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="上传的文件是空的")
    return hasher.hexdigest()


def _build_paper_document(
    paper_id: str,
    filename: str,
    *,
    title: str = "",
    info: PdfFirstPageInfo | None = None,
) -> PaperDocument:
    """拼出这篇论文的元数据，形状和检索来的论文完全一致。

    中文注释：
    - id 和 paperId 都填同一个编号。工作区判断"是不是同一篇论文"时优先看 paperId，
      缓存目录名也优先看 paperId，两处填成一样，编号在哪一层都是同一个值。
    - source 填 "local"，界面上"来源"筛选下拉框是现场从论文里收集的，不用改代码
      就会多出"本地上传"这一项。
    - 网页地址、PDF 地址一律留空：这篇论文没有对应的网页，留空就不会在界面上
      冒出一个点不开的"原文"入口。
    - 摘要、作者、年份来自第一页的识别结果，认不出来就是空值。
    """

    detail = info or PdfFirstPageInfo()
    return PaperDocument(
        id=paper_id,
        paperId=paper_id,
        title=title or _title_from_filename(filename),
        authors=list(detail.authors),
        abstract=detail.abstract or None,
        year=detail.year,
        source="local",
        metadata={
            "local_upload": True,
            "original_filename": _safe_original_filename(filename),
            "page_count": detail.page_count,
            "has_text_layer": detail.has_text_layer,
        },
    )


def _title_from_filename(filename: str) -> str:
    """把文件名当成标题（认不出 PDF 里的标题时用）。

    中文注释：有的浏览器会把整个路径传上来，所以先按斜杠切掉目录部分，再去掉
    .pdf 后缀。切完什么都不剩就用"未命名论文"。
    """

    name = re.split(r"[\\/]", str(filename or ""))[-1]
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    cleaned = re.sub(r"\s+", " ", name).strip().strip(".") or "未命名论文"
    return cleaned[:TITLE_MAX_CHARS]


def _safe_original_filename(filename: str) -> str:
    """只留下文件名本身（去掉目录和换行），存进缓存元数据里方便事后排查。"""

    return re.sub(r"[\r\n\t]", " ", re.split(r"[\\/]", str(filename or ""))[-1])[:200]


def _build_notice(*, info: PdfFirstPageInfo, is_new: bool, page_count: int | None) -> str:
    """拼一句给用户看的中文提示。

    中文注释：这里要把"扫描版"这件事提前说清楚。扫描版 PDF 读不出文字，精读只能
    退到"只看摘要"，而上传的论文又常常没有摘要，用户点完精读等半天才拿到一份
    很空的报告，体验很差——不如上传当时就告诉他。
    """

    if not is_new:
        return "这篇论文已经在工作区里了，没有重复添加。"
    pages = f"共 {page_count} 页，" if page_count else ""
    if info.has_text_layer is False:
        return (
            f"已加入工作区（{pages}但这份 PDF 读不出文字，多半是扫描版或整页图片）。"
            "精读只能基于摘要，报告内容会非常有限。"
        )
    if not info.abstract:
        return f"已加入工作区（{pages}没能自动提取到摘要）。生成综述时这篇只能提供标题和作者信息。"
    return f"已加入工作区（{pages}可以点「精读」）。"
