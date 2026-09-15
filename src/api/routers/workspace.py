"""会话工作区的 REST 接口。

前端刷新页面后除了从 webui-thread 恢复消息流，还需要直接读取工作区的论文清单和
精读报告，这由两个读接口提供；用户在工作区面板上的操作（上传本地 PDF、改标注、
批量删除、导出）由写接口提供：
1. GET    /api/sessions/{key}/workspace                          —— 工作区论文清单快照；
2. POST   /api/sessions/{key}/workspace/papers/upload            —— 上传本地 PDF；
3. PATCH  /api/sessions/{key}/workspace/papers/{paper_id}        —— 改用户标注或论文元数据；
4. DELETE /api/sessions/{key}/workspace/papers                   —— 批量删除（只退出当前会话）；
5. DELETE /api/sessions/{key}/workspace/fulltext-cache           —— 清除一篇论文的本机全文（编号放请求体）；
6. GET    /api/sessions/{key}/workspace/export                   —— 导出清单；
7. GET    /api/sessions/{key}/workspace/papers/{paper_id}/report —— 单篇论文的精读报告；
8. GET    /api/sessions/{key}/workspace/papers/{paper_id}/report.md —— 下载精读报告 Markdown 文件。

路由层只做请求解析与响应适配，具体的读写分别交给 SessionWorkspace 和
services/workspace_upload.py（工程规范：路由不承载业务逻辑）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

from src.models.sessions import SessionError
from src.models.workspace import SessionWorkspace, sanitize_for_filename
from src.repositories.sessions.base import SessionRepository
from src.services.paper_memory import purge_paper_cache, sync_workspace_missing_cache, unbind_session_papers
from src.services.workspace_export import export_workspace
from src.services.workspace_upload import save_uploaded_pdf


JsonObject = dict[str, Any]


def create_workspace_router(repo: SessionRepository) -> APIRouter:
    """创建工作区相关的 FastAPI 路由。"""

    router = APIRouter(prefix="/api/sessions", tags=["workspace"])

    def _sessions_root():
        """取出会话文件的根目录（与 run 服务使用同一份存储位置）。"""

        return getattr(getattr(repo, "backend", None), "sessions_dir", None)

    async def _load_workspace(session_key: str) -> SessionWorkspace:
        """校验会话存在后加载工作区。

        中文说明：
        以前这里会把整个会话的消息和过程记录都读出来，只为了确认会话还在。
        打开工作区时那样做会把整页卡住。现在只查会话表有没有这一行，
        再读工作区那份论文清单。
        """

        def _load() -> SessionWorkspace:
            if not repo.session_exists(session_key):
                raise SessionError(f"session not found: {session_key}", 404)
            return SessionWorkspace.load(session_key, _sessions_root())

        return await asyncio.to_thread(_load)

    @router.get("/{session_key}/workspace")
    async def get_workspace(session_key: str) -> JsonObject:
        """返回工作区快照：研究主题 + 论文清单（含评价分数与精读状态）。"""

        workspace = await _load_workspace(session_key)
        # 中文说明：用户如果手工删掉了本机缓存目录，这里检查一遍。
        # 发现目录没了，只改当前会话里这篇的精读状态，卡片还留着。
        # 其它会话下次打开时再对齐，打开工作区时不去扫全部会话文件夹。
        await asyncio.to_thread(sync_workspace_missing_cache, workspace)
        papers: list[JsonObject] = []
        # 按收录时间排序，保证前端展示顺序稳定。
        entries = sorted(workspace.papers.items(), key=lambda pair: pair[1].added_at)
        for paper_id, entry in entries:
            paper = entry.paper
            evaluation = entry.evaluation
            deep_read = entry.deep_read
            papers.append(
                {
                    "paper_id": paper_id,
                    "title": str(paper.get("title") or ""),
                    "authors": list(paper.get("authors") or []),
                    "year": paper.get("year") or None,
                    "venue": str(paper.get("venue") or paper.get("journal_conference") or ""),
                    "source": str(paper.get("source") or ""),
                    # 中文注释：这里给完整摘要，不再截断。前端点论文编号弹出的信息卡片
                    # 就是靠这个字段展示全文摘要的；截断会让用户看到半句话。
                    # （对话流里的论文卡片另有一份截断长度，那张卡片本来就只显示三行。）
                    "abstract": str(paper.get("abstract") or ""),
                    "url": str(paper.get("url") or ""),
                    "pdf_url": str(paper.get("pdf_url") or ""),
                    "doi": str(paper.get("doi") or ""),
                    "status": entry.status(),
                    "score": evaluation.score if evaluation is not None else None,
                    "has_report": deep_read is not None,
                    "fulltext_cached": entry.fulltext_cached,
                    "added_at": entry.added_at,
                }
            )
        return {
            "session_key": session_key,
            "research_topic": workspace.research_topic,
            "updated_at": workspace.updated_at,
            "total": len(papers),
            "papers": papers,
        }

    @router.post("/{session_key}/workspace/papers/upload")
    async def upload_paper(session_key: str, file: UploadFile = File(...)) -> JsonObject:
        """上传本地 PDF 论文到工作区。不启动 run，不消耗模型。

        中文注释：
        表单里的文件字段固定叫 file。文件存到哪、标题怎么认、怎么登记进工作区，
        全部交给 services/workspace_upload.py，这里只负责把上传的内容转过去。
        """

        workspace = await _load_workspace(session_key)
        result = await save_uploaded_pdf(
            workspace,
            filename=file.filename or "",
            chunks=_upload_chunks(file),
        )
        return {
            "paper_id": result.paper_id,
            "title": result.title,
            "authors": result.authors,
            "year": result.year,
            "is_new": result.is_new,
            "page_count": result.page_count,
            "has_text_layer": result.has_text_layer,
            "abstract": result.abstract,
            "notice": result.notice,
        }

    @router.patch("/{session_key}/workspace/papers/{paper_id:path}")
    async def update_paper(session_key: str, paper_id: str, request: Request) -> JsonObject:
        """更新论文的用户标注（加星、标签、笔记）或论文元数据（标题、作者、年份、摘要）。

        中文注释：
        纯状态动作，直接写工作区 JSON，不用等 Agent 响应。
        面板上的「加星」「打标签」「写笔记」走这里；用户上传本地 PDF 后，在确认框里
        改标题这些信息也走这里。请求里带了哪一项就改哪一项，没带的保持原样。
        """

        body = await _json_body(request)
        workspace = await _load_workspace(session_key)

        # 中文注释：标注和元数据要分开处理——标注（加星、标签、笔记）是工作区条目
        # 自己的字段，元数据（标题、作者……）是嵌在该条目里那份论文信息上的字段。
        starred = body.get("starred") if "starred" in body else None
        tags = body.get("tags") if "tags" in body else None
        note = body.get("note") if "note" in body else None
        if any(value is not None for value in (starred, tags, note)):
            if not workspace.update_paper_annotations(paper_id, starred=starred, tags=tags, note=note):
                raise HTTPException(status_code=404, detail=f"paper not found: {paper_id}")

        title = body.get("title") if "title" in body else None
        authors = body.get("authors") if "authors" in body else None
        year = body.get("year") if "year" in body else None
        abstract = body.get("abstract") if "abstract" in body else None
        if any(value is not None for value in (title, authors, year, abstract)):
            if not workspace.update_paper_metadata(
                paper_id, title=title, authors=authors, year=year, abstract=abstract
            ):
                raise HTTPException(status_code=404, detail=f"paper not found: {paper_id}")

        entry = workspace.get_paper(paper_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"paper not found: {paper_id}")
        return {
            "paper_id": paper_id,
            "title": str(entry.paper.get("title") or ""),
            "authors": list(entry.paper.get("authors") or []),
            "year": entry.paper.get("year") or None,
            "abstract": str(entry.paper.get("abstract") or ""),
            "starred": entry.starred,
            "tags": list(entry.tags),
            "note": entry.note,
        }

    @router.delete("/{session_key}/workspace/papers")
    async def batch_remove_papers(session_key: str, request: Request) -> JsonObject:
        """批量删除工作区论文。不启动 run，不消耗模型。

        中文注释：
        前端面板的多选删除走这里。body 里传 {"paper_ids": [...]}。
        """

        body = await _json_body(request)
        paper_ids = list(body.get("paper_ids") or [])
        if not paper_ids:
            return {"removed": 0}
        workspace = await _load_workspace(session_key)
        removed = await asyncio.to_thread(workspace.remove_papers, paper_ids)
        # 中文说明：工作区删除只退出当前会话，本机缓存和其他会话里的同一篇都还在。
        await asyncio.to_thread(unbind_session_papers, session_key, paper_ids)
        return {"removed": removed}

    @router.delete("/{session_key}/workspace/fulltext-cache")
    async def purge_paper_local_cache(session_key: str, request: Request) -> JsonObject:
        """清除一篇论文的本机全文缓存。卡片留在所有会话里，已精读改回可精读。

        中文说明：
        请求体是 {"paper_id": "..."}。编号不放进网址，是因为很多论文编号自带斜杠
        （比如 DOI）。如果写成 /papers/编号/cache，斜杠会被拆成多段路径，容易撞上
        旁边那个只允许 PATCH 的改标注接口，浏览器就会看到 Method Not Allowed。

        这一步会删掉 data/paper_cache 里这篇论文的目录，并把所有会话工作区里
        对应卡片的精读报告清掉、全文标记关掉。标题、作者、摘要、评分、加星和
        笔记都还在。对话里已经发出去的精读卡片不改。
        """

        body = await _json_body(request)
        paper_id = str(body.get("paper_id") or "").strip()
        if not paper_id:
            raise HTTPException(status_code=400, detail="paper_id is required")
        workspace = await _load_workspace(session_key)
        entry = workspace.get_paper(paper_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"paper not found: {paper_id}")
        return await asyncio.to_thread(
            purge_paper_cache,
            session_key=session_key,
            workspace_paper_id=paper_id,
            paper=dict(entry.paper),
        )

    @router.get("/{session_key}/workspace/export")
    async def export_workspace_endpoint(
        session_key: str, format: str = "markdown", paper_ids: str = ""
    ):
        """导出工作区论文清单。format 可以是 bibtex / markdown / csv。

        中文注释：
        paper_ids 用逗号分隔，省略表示全部。返回文件流，带 Content-Disposition。
        """

        workspace = await _load_workspace(session_key)
        ids = [pid.strip() for pid in paper_ids.split(",") if pid.strip()] if paper_ids else None
        content_type, filename, file_content = await asyncio.to_thread(
            export_workspace, workspace, format=format, paper_ids=ids
        )
        return Response(
            content=file_content,
            media_type=content_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/{session_key}/workspace/papers/{paper_id:path}/report")
    async def get_paper_report(session_key: str, paper_id: str) -> JsonObject:
        """返回单篇论文的精读报告；论文不存在或尚未精读时返回 404。

        路径参数用 :path 转换器，兼容 DOI 这类自带斜杠的论文编号。
        """

        workspace = await _load_workspace(session_key)
        entry = workspace.get_paper(paper_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"paper not found in workspace: {paper_id}")
        if entry.deep_read is None:
            raise HTTPException(status_code=404, detail=f"paper has no deep-read report yet: {paper_id}")
        return {
            "session_key": session_key,
            "paper_id": paper_id,
            "report": entry.deep_read.to_dict(),
        }

    @router.get("/{session_key}/workspace/papers/{paper_id:path}/report.md")
    async def download_paper_report(session_key: str, paper_id: str) -> Response:
        """下载单篇论文的精读报告 Markdown 文件；论文不存在或尚未精读时返回 404。

        中文说明：
        和上面那个读报告接口用的是同一份数据，区别只在于这里把报告拼成
        Markdown 文本、当成文件发给浏览器下载。报告是现场拼出来的，不依赖
        磁盘上有没有别的产物文件，所以以前精读过的老论文也能照常下载。
        """

        workspace = await _load_workspace(session_key)
        entry = workspace.get_paper(paper_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"paper not found in workspace: {paper_id}")
        if entry.deep_read is None:
            raise HTTPException(status_code=404, detail=f"paper has no deep-read report yet: {paper_id}")
        return Response(
            content=entry.deep_read.to_markdown(),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{_report_filename(paper_id)}"'},
        )

    return router


async def _upload_chunks(file: UploadFile, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    """把上传的文件按 1MB 一块读出来。

    中文注释：不一次性读完，是为了让"文件太大"能在收到一半时就拦下来，
    而不是先把几十兆整个读进内存再判断。
    """

    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            return
        yield chunk


async def _json_body(request: Request) -> JsonObject:
    """读取 JSON body；空 body 按空对象处理。"""

    try:
        payload = await request.json()
    except Exception:
        return {}
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def _report_filename(paper_id: str) -> str:
    """把论文编号整理成一个安全、又能认出来是哪篇的文件名。

    中文说明：
    这个名字会写进下载的响应头，而响应头只允许纯英文和数字，直接放中文会报错。
    所以先把 Windows 不允许的字符（斜杠、冒号这些）换成下划线，再把中文这类
    非英文字符整个去掉。万一清理完什么都不剩，就用 paper 兜底。
    """

    safe = sanitize_for_filename(paper_id)
    safe = safe.encode("ascii", "ignore").decode("ascii")
    safe = "_".join(safe.split()).strip("_")
    return f"deep_read_{safe[:60] or 'paper'}.md"
