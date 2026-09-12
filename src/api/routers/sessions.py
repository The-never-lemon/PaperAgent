from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from src.repositories.sessions.base import SessionRepository
from src.models.sessions import SESSION_STATUS_INTERRUPTED, SessionError, utc_now
from src.services.session_runs import SessionRunService, encode_sse
from src.services.sessions import (
    MessageHandler,
    create_session,
    delete_session,
    fetch_thread,
    list_sessions,
)


JsonObject = dict[str, Any]

# SSE 心跳间隔（秒）：超过这个时间没有任何事件，就发一条注释行保活，
# 防止模型长时间调用（限流重试可达数分钟）期间连接被客户端或中间层掐断。
STREAM_HEARTBEAT_SECONDS = 15


def create_sessions_router(
    repo: SessionRepository,
    message_handler: MessageHandler | None = None,
    run_service: SessionRunService | None = None,
) -> APIRouter:
    """创建会话相关的 FastAPI 路由。

    中文说明：
    该模块只负责请求解析与响应适配，不直接承载会话落库、run 编排和
    SSE 事件管理逻辑，核心业务统一委托给 service 层处理。
    """

    router = APIRouter(prefix="/api/sessions", tags=["sessions"])
    resolved_run_service = run_service or SessionRunService(repo=repo, message_handler=message_handler)

    @router.get("")
    async def get_sessions() -> JsonObject:
        """返回会话列表。"""

        return list_sessions(repo)

    @router.post("", status_code=201)
    async def post_session(request: Request) -> JsonObject:
        """创建一个新会话。"""

        return create_session(repo, await _json_body(request))

    @router.get("/{session_key}/webui-thread")
    async def get_thread(session_key: str) -> JsonObject:
        """读取指定会话的完整线程快照。"""

        return fetch_thread(repo, session_key)

    @router.get("/{session_key}/artifacts/{artifact_id}")
    async def get_artifact(session_key: str, artifact_id: str) -> FileResponse:
        """提供指定会话产物文件的预览或下载。

        中文说明：
        产物文件路径由仓储层做安全校验，只有位于该会话目录内的文件才会被返回；
        记录不存在、路径越界或文件已被删除时统一返回 404。
        """

        file_path = repo.read_artifact_path(session_key, artifact_id)
        if file_path is None:
            raise HTTPException(status_code=404, detail="artifact not found or unavailable")
        return FileResponse(
            file_path,
            filename=file_path.name,
            content_disposition_type="inline",
            media_type=_artifact_media_type(file_path),
        )

    @router.delete("/{session_key}")
    async def remove_session(session_key: str) -> JsonObject:
        """删除指定会话。"""

        return delete_session(repo, session_key)

    @router.post("/{session_key}/runs", status_code=202)
    async def post_run(session_key: str, request: Request) -> JsonObject:
        """创建一次新的后台运行，并返回对应的流地址。"""

        return await resolved_run_service.start_run(session_key, await _json_body(request))

    @router.post("/{session_key}/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(session_key: str, run_id: str) -> JsonObject:
        """接收用户主动停止请求，后台任务会在当前等待点尽快结束。"""

        return await resolved_run_service.cancel_run(session_key, run_id)

    @router.get("/{session_key}/runs/{run_id}/stream")
    async def stream_run(session_key: str, run_id: str) -> StreamingResponse:
        """以 SSE 形式持续返回指定 run 的实时事件。"""

        async def _event_generator():
            """持续输出 SSE 事件，长时间没有事件时发送心跳注释行保活。

            中文说明：
            主 Agent 的一次模型调用可能持续几分钟（比如模型服务限流重试时），
            期间不会产生任何事件。没有心跳的话，这段静默会让带读超时的客户端
            或中间层把连接当成"死了"而断开。SSE 规范里以冒号开头的注释行会被
            浏览器和各类客户端自动忽略，正好用来保活。

            当后端进程重启后，旧 run 的 broker 内存状态已经丢失。此时前端拿着
            旧的 stream_url 接回来，broker 里找不到这个 run。这种情况不再挂死
            或者抛 500，而是推一条 turn_end(status="interrupted") 让前端知道
            这个 run 已经结束了，可以恢复到可继续对话的状态。
            """

            # 中文注释：stream_events 是"异步生成器"，这一行调用它只是创建了一个
            # 生成器对象，函数体里的任何代码都还没有真正执行。所以"run 不存在"
            # 这个错误不可能在这一步抛出来——它要等到我们第一次真正去"取事件"
            # （也就是下面的 await anext）时才会被抛出。这就是为什么必须先把
            # 第一条事件取到手，才能发现这个 run 已经不在后端内存里了。
            event_iterator = resolved_run_service.stream_events(session_key, run_id)
            try:
                first_event = await anext(event_iterator)
            except SessionError:
                # run 不在 broker 里（通常是后端进程重启后旧 run 的内存状态丢失）。
                # 推一条 turn_end 让前端知道这次 run 已经结束，避免死等。
                # 注意：yield 完必须马上 return 结束这个生成器，否则代码会继续
                # 往下走，又去一个不存在的 run 上取事件。
                interrupted_event = {
                    "event": "turn_end",
                    "session_key": session_key,
                    "run_id": run_id,
                    "status": SESSION_STATUS_INTERRUPTED,
                    "timestamp": utc_now(),
                    "message": "本次运行已中断（后端进程重启），请继续对话或重试。",
                }
                yield encode_sse(interrupted_event)
                yield ": stream closed\n\n"
                return
            except StopAsyncIteration:
                # 一条事件都取不到，说明这个流本身就是空的（run 已关闭且没有历史）。
                # 直接发一行"流已结束"的注释让前端收尾，不再继续等。
                yield ": stream closed\n\n"
                return

            # 成功拿到第一条事件：先把它发给前端，再进入下面的常规心跳循环。
            yield encode_sse(first_event)

            # 中文注释：把"等下一条事件"包成任务，心跳超时只影响等待方，
            # 用 shield 保护任务本身不被取消，避免超时瞬间恰好到达的事件被丢掉。
            next_task: asyncio.Task | None = asyncio.ensure_future(anext(event_iterator))
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(
                            asyncio.shield(next_task),
                            timeout=STREAM_HEARTBEAT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    except StopAsyncIteration:
                        break
                    yield encode_sse(event)
                    next_task = asyncio.ensure_future(anext(event_iterator))
                yield ": stream closed\n\n"
            finally:
                # 客户端断开连接时，取消还挂着的"等下一条事件"任务，避免遗留孤儿任务。
                if next_task is not None and not next_task.done():
                    next_task.cancel()

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

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


def _artifact_media_type(file_path: Path) -> str:
    """根据文件后缀返回适合浏览器预览的媒体类型。

    中文说明：
    Markdown 和 JSON 可以在新标签页里直接查看，其他类型走通用的二进制下载。
    """

    suffix = file_path.suffix.lower()
    if suffix == ".md":
        return "text/markdown; charset=utf-8"
    if suffix == ".json":
        return "application/json; charset=utf-8"
    return "application/octet-stream"
