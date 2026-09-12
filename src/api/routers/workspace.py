"""会话工作区的只读 REST 接口（实施方案第五节新增的 2 个端点）。

前端刷新页面后除了从 webui-thread 恢复消息流，还需要直接读取工作区的
论文清单和精读报告，这两个端点就是为此服务的：
1. GET /api/sessions/{key}/workspace                       —— 工作区论文清单快照；
2. GET /api/sessions/{key}/workspace/papers/{paper_id}/report —— 单篇论文的精读报告。

路由层只做请求解析与响应适配，数据统一从 SessionWorkspace 读取（工程规范：
路由不承载业务逻辑）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from src.models.workspace import SessionWorkspace
from src.repositories.sessions.base import SessionRepository
from src.services.workspace_export import export_workspace


JsonObject = dict[str, Any]

# 工作区清单里论文摘要的截断长度（REST 快照只给前端渲染卡片用，不需要全文摘要）。
ABSTRACT_PREVIEW_CHARS = 400


def create_workspace_router(repo: SessionRepository) -> APIRouter:
    """创建工作区相关的 FastAPI 路由。"""

    router = APIRouter(prefix="/api/sessions", tags=["workspace"])

    def _sessions_root():
        """取出会话文件的根目录（与 run 服务使用同一份存储位置）。"""

        return getattr(getattr(repo, "backend", None), "sessions_dir", None)

    async def _load_workspace(session_key: str) -> SessionWorkspace:
        """校验会话存在后加载工作区（文件读取放进线程，避免阻塞事件循环）。"""

        repo.get(session_key)  # 会话不存在时抛 SessionError(404)，由 app 统一转错误响应
        return await asyncio.to_thread(SessionWorkspace.load, session_key, _sessions_root())

    @router.get("/{session_key}/workspace")
    async def get_workspace(session_key: str) -> JsonObject:
        """返回工作区快照：研究主题 + 论文清单（含评价分数与精读状态）。"""

        workspace = await _load_workspace(session_key)
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
                    "abstract": str(paper.get("abstract") or "")[:ABSTRACT_PREVIEW_CHARS],
                    "url": str(paper.get("url") or ""),
                    "pdf_url": str(paper.get("pdf_url") or ""),
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

    @router.patch("/{session_key}/workspace/papers/{paper_id:path}")
    async def update_paper_annotations(session_key: str, paper_id: str, request: Request) -> JsonObject:
        """更新论文的用户标注（加星、标签、笔记）。不启动 run，不消耗模型。

        中文注释：
        纯状态动作，直接写工作区 JSON。前端面板的「加星」「打标签」「写笔记」
        都走这里，不需要等 Agent 响应。
        """

        body = await _json_body(request)
        workspace = await _load_workspace(session_key)
        starred = body.get("starred") if "starred" in body else None
        tags = body.get("tags") if "tags" in body else None
        note = body.get("note") if "note" in body else None
        if not workspace.update_paper_annotations(paper_id, starred=starred, tags=tags, note=note):
            raise HTTPException(status_code=404, detail=f"paper not found: {paper_id}")
        entry = workspace.get_paper(paper_id)
        return {
            "paper_id": paper_id,
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
        return {"removed": removed}

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
        from fastapi.responses import Response
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

    return router


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
