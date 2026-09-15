from __future__ import annotations

import asyncio
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.models.sessions import SessionError
from src.repositories.sessions.base import SessionRepository
from src.repositories.sessions.sqlite import SQLiteSessionRepository
from src.repositories.settings.json import SettingsRepository
from src.services.session_runs import SessionRunService
from src.services.chat_runtime import build_chat_message_handler
from src.services.sessions import MessageHandler
from src.utils import get_logger, logging_context, setup_logging

from .routers.sessions import create_sessions_router
from .routers.settings import create_settings_router
from .routers.workspace import create_workspace_router


JsonObject = dict[str, Any]
logger = get_logger(__name__)

# 中文注释：下面的 Windows 处理只需要做一次。create_app 在测试里可能被多次调用，
# 不能反复给系统函数包一层，否则关连接时会套很多层。
_windows_asyncio_prepared = False


@dataclass(slots=True)
class GatewayConfig:
    """前端网关配置对象。

    中文说明：
    这个数据类只负责承载前端在启动阶段需要读取的最小运行时配置，
    目前主要是 `api_base`。之所以单独保留一层对象，是为了后续补充
    bootstrap 字段时仍然维持统一的应用装配入口。
    """

    api_base: str = ""


def create_app(
    settings_repo: SettingsRepository | None = None,
    sessions_repo: SessionRepository | None = None,
    config: GatewayConfig | None = None,
    message_handler: MessageHandler | None = None,
) -> FastAPI:
    """创建面向前端工作台的 FastAPI 应用。

    中文说明：
    该函数是后端 HTTP 层唯一的总装配入口，负责：
    1. 初始化统一日志系统。
    2. 组装设置仓储与会话仓储。
    3. 注册 settings/sessions 路由。
    4. 统一配置异常处理与静态前端挂载。
    """

    setup_logging()
    # 中文注释：Windows 关掉已经断开的网络连接时，系统自带的异步循环会多报一段
    # 看起来像崩溃的堆栈。服务一启动就处理掉，后面关模型客户端、关下载连接都不会再刷屏。
    _prepare_windows_asyncio()
    settings_repo = settings_repo or SettingsRepository(_default_settings_path())
    sessions_repo = sessions_repo or SQLiteSessionRepository()
    config = config or GatewayConfig()
    # 中文注释：对话式调研改造的挂接点（实施方案 1.3）。
    # 原来的固定流水线处理器（build_paper_workflow_message_handler）已替换为
    # 主对话 Agent 处理器；runs/SSE/取消/持久化机制全部原样复用。
    message_handler = message_handler or build_chat_message_handler(sessions_repo, settings_repo)
    run_service = SessionRunService(repo=sessions_repo, message_handler=message_handler)

    # 启动自愈：把上一次进程崩溃时残留的「正在运行」会话全部置成「已中断」，
    # 清空 run_started_at 和 metadata.active_run_id，让用户可以继续对话。
    # 同时清掉历史上堆积的流式 token 事件，释放被它们占满的数据库空间。
    try:
        interrupted = sessions_repo.reset_stale_runs()
        purged = sessions_repo.purge_stream_events()
        if interrupted or purged:
            logger.info(
                "启动自愈完成",
                extra={"interrupted_sessions": interrupted, "purged_stream_events": purged},
            )
    except Exception:
        logger.exception("启动自愈失败，会话数据可能需要手动检查")

    app = FastAPI(title="Papers Agents API")
    app.include_router(create_settings_router(settings_repo))
    app.include_router(
        create_sessions_router(
            sessions_repo,
            message_handler=message_handler,
            run_service=run_service,
        )
    )
    # 中文注释：对话式调研改造新增的工作区只读接口（论文清单快照 + 单篇精读报告）。
    app.include_router(create_workspace_router(sessions_repo))

    @app.middleware("http")
    async def access_log_middleware(request: Request, call_next):
        """记录每一次 HTTP 请求的访问日志。

        中文说明：
        这里统一补充 request_id、方法、路径、客户端地址、状态码与耗时，
        方便后续排查前后端联调问题以及某次请求落到了哪个会话。
        """

        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        started_at = time.perf_counter()
        client_host = request.client.host if request.client else None
        with logging_context(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            client_host=client_host,
        ):
            try:
                response = await call_next(request)
            except Exception:
                duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
                logger.exception("HTTP 请求处理失败", extra={"duration_ms": duration_ms, "status_code": 500})
                raise
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            response.headers["X-Request-ID"] = request_id
            log_level = "warning" if response.status_code >= 400 else "info"
            getattr(logger, log_level)(
                "HTTP 请求完成",
                extra={"duration_ms": duration_ms, "status_code": response.status_code},
            )
            return response

    @app.exception_handler(SessionError)
    async def session_error_handler(_: Request, exc: SessionError) -> JSONResponse:
        """把会话业务异常转换成统一的前端错误结构。"""

        return JSONResponse(status_code=exc.status, content={"error": {"message": str(exc), "status": exc.status}})

    @app.exception_handler(ValueError)
    async def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
        """把请求体格式错误转换成 400 响应。"""

        return JSONResponse(status_code=400, content={"error": {"message": str(exc), "status": 400}})

    @app.get("/webui/bootstrap")
    async def bootstrap() -> JsonObject:
        """返回前端启动所需的最小运行时能力声明。

        中文说明：
        这里不做鉴权协商，只告诉前端当前后端支持哪些调用方式，
        让单机版界面可以在启动时一次性拿到能力快照。
        """

        logger.debug("返回前端 bootstrap 配置")
        return {
            "expires_in": 0,
            "api_base": config.api_base,
            "runtime_surface": "paper_agent_workspace",
            "runtime_capabilities": {
                "fastapi_rest": True,
                "rest_management": True,
                "http_message_submit": True,
                "session_runs": True,
                "sse_streaming": True,
                "multi_chat_socket": False,
                "settings_snapshot": True,
                "auth_required": False,
            },
        }

    _mount_frontend(app)
    logger.info(
        "FastAPI 应用创建完成",
        extra={
            "title": app.title,
            "has_frontend_dist": (Path("front/dist") / "index.html").exists(),
            "settings_file": str(_default_settings_path()),
        },
    )
    return app


def _mount_frontend(app: FastAPI) -> None:
    """挂载前端构建产物，并保留 SPA 路由回退能力。"""

    dist_dir = Path("front/dist")
    index_file = dist_dir / "index.html"
    assets_dir = dist_dir / "assets"

    if assets_dir.exists():
        # 中文注释：构建产物中的静态资源单独挂载，浏览器可以直接命中资源文件。
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="front-assets")

    if not index_file.exists():
        logger.warning("未发现前端构建产物，SPA 静态页面不会被挂载", extra={"dist_dir": str(dist_dir)})
        return

    @app.get("/", include_in_schema=False)
    async def front_index() -> FileResponse:
        """返回前端首页。"""

        return FileResponse(index_file)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def front_routes(full_path: str) -> FileResponse:
        """为前端静态文件与 SPA 路由提供统一出口。"""

        candidate = dist_dir / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        # 中文注释：未知前端路径统一回退到 index.html，由前端路由系统接管。
        return FileResponse(index_file)


def _prepare_windows_asyncio() -> None:
    """处理 Windows 上关闭网络连接时的一段多余报错。

    中文说明：
    Windows 默认的异步循环在关掉已经断开的连接时，还会再对插座做一次
    “两边都关掉”。如果对面已经把连接掐了，这一步就会抛出 ConnectionResetError
    （常见错误码 10054），控制台里看起来像程序崩了，其实这次请求多半早就结束了。

    这里做两件事：
    1. 如果异步循环还没启动，就换成 Windows 上更不容易在关连接时报错的那一套；
    2. 如果循环已经在跑（例如用 uvicorn 命令行拉起服务），就给系统关连接的那一步
       加一层保护：碰到“连接已被对面掐掉”就忽略，并把插座收干净。
    """

    global _windows_asyncio_prepared
    if sys.platform != "win32" or _windows_asyncio_prepared:
        return

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 中文注释：还没有正在跑的异步循环时才能换循环类型，换完之后 uvicorn 会按新的来。
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
    except ImportError:
        _windows_asyncio_prepared = True
        return

    original = _ProactorBasePipeTransport._call_connection_lost

    def _call_connection_lost(self, exc):  # noqa: ANN001
        """关连接时如果对面已经掐线，就把插座收干净，不再把堆栈打到控制台。"""

        try:
            return original(self, exc)
        except OSError:
            # 中文注释：系统原函数在 shutdown 这一步抛错后，后面的 close 不会执行。
            # 这里补上关闭，避免插座一直挂着。
            sock = getattr(self, "_sock", None)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                self._sock = None
            server = getattr(self, "_server", None)
            if server is not None:
                try:
                    server._detach()
                except Exception:
                    pass
                self._server = None
            self._called_connection_lost = True

    _ProactorBasePipeTransport._call_connection_lost = _call_connection_lost
    _windows_asyncio_prepared = True


def _default_settings_path() -> Path:
    """返回本地模型配置文件路径。

    中文说明：
    即使文件还不存在也返回固定路径，这样设置页保存时会自动创建
    config/model.json 并落盘；如果这里返回 None，保存只会写进内存，
    重启后配置就丢了。
    """

    return Path("config/model.json")
