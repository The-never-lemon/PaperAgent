"""HTTP+SSE 驱动模块 - 用于端到端评估。

这个模块提供与 Paper-Agent 后端交互的完整 HTTP 接口，包括：
- 后端服务探活
- 会话创建与删除
- 运行启动与取消
- SSE 事件消费（带心跳与超时保护）

所有函数返回统一格式：{"status": "ok"|"failed", ...}
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncGenerator

import httpx

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]


@dataclass
class HttpDriverDeps:
    """HTTP 驱动模块的运行时依赖配置。

    Attributes:
        base_url: 后端服务基础 URL（默认 http://127.0.0.1:8000）
        timeout_s: 普通 HTTP 请求超时时间（秒），默认 30
    """
    base_url: str = "http://127.0.0.1:8000"
    timeout_s: float = 30


async def wait_server_ready(deps: HttpDriverDeps, *, max_wait_s: float = 5) -> dict:
    """探活后端服务，检查服务是否就绪。

    通过 GET /webui/bootstrap 端点检查后端是否在线。

    Args:
        deps: HttpDriverDeps 依赖对象
        max_wait_s: 最多等待时间（秒）

    Returns:
        成功: {"status": "ok"}
        失败: {"status": "failed", "reason": "..."}
    """
    # 中文注释：设置总超时，避免无限期等待
    try:
        async with asyncio.timeout(max_wait_s):
            # 中文注释：用 httpx.AsyncClient 发起异步 GET 请求
            async with httpx.AsyncClient(timeout=deps.timeout_s) as client:
                try:
                    response = await client.get(f"{deps.base_url}/webui/bootstrap")
                    # 中文注释：检查响应状态码
                    if response.status_code == 200:
                        logger.info("后端服务就绪")
                        return {"status": "ok"}
                    else:
                        reason = f"后端返回状态码 {response.status_code}"
                        logger.warning(reason)
                        return {"status": "failed", "reason": reason}
                except httpx.RequestError as e:
                    # 中文注释：网络连接错误，说明后端没有运行
                    reason = (
                        f"后端没起来，请先运行: "
                        f"conda run -n paper-agentic python -m uvicorn main:app "
                        f"--host 127.0.0.1 --port 8000（不要带 --reload）"
                    )
                    logger.error(f"探活失败: {e}")
                    return {"status": "failed", "reason": reason}
    except asyncio.TimeoutError:
        # 中文注释：超时表示没有在规定时间内收到响应
        reason = f"探活超时（超过 {max_wait_s} 秒）"
        logger.error(reason)
        return {"status": "failed", "reason": reason}


async def create_session(deps: HttpDriverDeps, *, title: str) -> dict:
    """创建一个新的会话。

    Args:
        deps: HttpDriverDeps 依赖对象
        title: 会话标题

    Returns:
        成功: {"status": "ok", "session_key": "..."}
        失败: {"status": "failed", "reason": "..."}
    """
    try:
        async with httpx.AsyncClient(timeout=deps.timeout_s) as client:
            # 中文注释：发送 POST 请求到会话创建端点
            response = await client.post(
                f"{deps.base_url}/api/sessions",
                json={"title": title},
            )

            # 中文注释：检查响应状态码
            if not (200 <= response.status_code < 300):
                reason = (
                    f"创建会话失败（HTTP {response.status_code}）: "
                    f"{response.text[:200]}"
                )
                logger.error(reason)
                return {"status": "failed", "reason": reason}

            # 中文注释：解析响应 JSON，获取会话 key
            data = response.json()
            session_info = data.get("session", {})
            session_key = session_info.get("key")

            if not session_key:
                reason = "响应中缺少 session.key 字段"
                logger.error(reason)
                return {"status": "failed", "reason": reason}

            logger.info(f"会话创建成功: {session_key}")
            return {"status": "ok", "session_key": session_key}

    except Exception as e:
        reason = f"创建会话异常: {e}"
        logger.exception(reason)
        return {"status": "failed", "reason": reason}


async def post_run(
    deps: HttpDriverDeps,
    *,
    session_key: str,
    content: str,
) -> dict:
    """向会话发起一次运行（turn）。

    Args:
        deps: HttpDriverDeps 依赖对象
        session_key: 会话 key
        content: 用户输入内容

    Returns:
        成功: {
            "status": "ok",
            "run_id": "...",
            "turn_id": "...",
            "stream_url": "/api/sessions/.../stream"
        }
        会话已有运行中任务 (409): {
            "status": "failed",
            "reason": "会话已有运行中任务"
        }
        其他失败: {"status": "failed", "reason": "..."}
    """
    try:
        async with httpx.AsyncClient(timeout=deps.timeout_s) as client:
            # 中文注释：发送 POST 请求启动运行
            response = await client.post(
                f"{deps.base_url}/api/sessions/{session_key}/runs",
                json={"content": content},
            )

            # 中文注释：特殊处理 409 冲突状态（会话已有运行）
            if response.status_code == 409:
                logger.warning(f"会话 {session_key} 已有运行中任务")
                return {
                    "status": "failed",
                    "reason": "会话已有运行中任务",
                    "http_status": 409,
                }

            # 中文注释：检查其他错误状态码
            if not (200 <= response.status_code < 300):
                reason = (
                    f"启动运行失败（HTTP {response.status_code}）: "
                    f"{response.text[:200]}"
                )
                logger.error(reason)
                return {"status": "failed", "reason": reason}

            # 中文注释：解析响应获取运行信息
            data = response.json()
            run_id = data.get("run_id")
            turn_id = data.get("turn_id")
            stream_url = data.get("stream_url")

            # 中文注释：验证必要字段存在
            if not all([run_id, turn_id, stream_url]):
                reason = "响应缺少必要字段（run_id/turn_id/stream_url）"
                logger.error(reason)
                return {"status": "failed", "reason": reason}

            # 中文注释：如果 stream_url 是相对路径，补充基础 URL
            if not stream_url.startswith("http"):
                stream_url = f"{deps.base_url}{stream_url}"

            logger.info(f"运行启动成功: run_id={run_id}, turn_id={turn_id}")
            return {
                "status": "ok",
                "run_id": run_id,
                "turn_id": turn_id,
                "stream_url": stream_url,
            }

    except Exception as e:
        reason = f"启动运行异常: {e}"
        logger.exception(reason)
        return {"status": "failed", "reason": reason}


async def consume_sse(
    deps: HttpDriverDeps,
    *,
    stream_url: str,
    timeout_s: float,
) -> AsyncGenerator[dict, None]:
    """异步消费 SSE 事件流。

    按 SSE 规范解析事件，支持：
    - 以冒号开头的注释行（心跳）
    - 标准 event/data 事件对（data 支持多行拼接）
    - 流结束标记（": stream closed"）

    产出的事件字典格式 {"event": "...", "data": {...}}，其中外层的 "event" 字段
    是 SSE 流的事件名称（来自 "event:" 行），与内层 data JSON 中可能自带的同名字段无关。
    两层含义不同：外层标识消息类型，内层是消息内容。

    Args:
        deps: HttpDriverDeps 依赖对象
        stream_url: SSE 流的完整 URL
        timeout_s: 整个流的总超时时间（秒）

    Yields:
        解析后的事件字典: {"event": "...", "data": {...}} 或 {"event": "...", "raw": "..."}

    Raises:
        asyncio.TimeoutError: 如果整体流超过 timeout_s
    """
    try:
        # 中文注释：设置整体超时
        async with asyncio.timeout(timeout_s):
            # 中文注释：用 httpx 流式读取 SSE，设置较短的读超时以避免在数据流中卡死
            async with httpx.AsyncClient(timeout=deps.timeout_s) as client:
                async with client.stream("GET", stream_url) as response:
                    # 中文注释：检查 HTTP 状态码
                    if response.status_code != 200:
                        logger.error(
                            f"SSE 流响应异常（HTTP {response.status_code}）"
                        )
                        return

                    # 中文注释：状态机：累积事件字段直到空行时发送
                    event_name = None
                    data_lines = []  # 中文注释：累积多行 data

                    # 中文注释：逐行读取流内容
                    async for line in response.aiter_lines():
                        # 中文注释：检查流结束标记
                        if line.strip() == ": stream closed":
                            logger.debug("收到流结束标记")
                            return

                        # 中文注释：空行表示事件结束，发送累积的事件
                        if not line.strip():
                            if event_name and data_lines:
                                # 中文注释：将多行 data 用换行符拼接（SSE 规范）
                                data_str = "\n".join(data_lines)
                                try:
                                    data = json.loads(data_str)
                                    yield {
                                        "event": event_name,
                                        "data": data,
                                    }
                                except json.JSONDecodeError as e:
                                    logger.warning(
                                        f"事件 {event_name} 的 data 无法解析为 JSON: {e}"
                                    )
                                    yield {
                                        "event": event_name,
                                        "raw": data_str,
                                    }
                                # 中文注释：重置状态以准备下一个事件
                                event_name = None
                                data_lines = []
                            continue

                        # 中文注释：跳过纯注释行（以冒号开头，包括心跳）
                        if line.startswith(":"):
                            logger.debug(f"收到心跳/注释: {line[:50]}")
                            continue

                        # 中文注释：解析 event: 行
                        if line.startswith("event:"):
                            event_name = line[6:].strip()
                            logger.debug(f"收到事件: {event_name}")

                        # 中文注释：累积 data: 行（多行拼接）
                        elif line.startswith("data:"):
                            # 中文注释：提取 data 内容（移除前缀，保留首个空格后的内容）
                            data_content = line[5:]
                            # 中文注释：如果 data: 后有空格，移除第一个空格；否则保持原样
                            if data_content.startswith(" "):
                                data_content = data_content[1:]
                            data_lines.append(data_content)

    except asyncio.TimeoutError:
        logger.error(f"SSE 流超时（超过 {timeout_s} 秒）")
        raise
    except Exception as e:
        # 中文注释：流提前断开或其他 I/O 异常被外层 except 捕获
        # 这里包括网络断开、连接重置、premature EOF 等
        logger.exception(f"SSE 流消费异常: {e}")
        raise



async def cancel_run(
    deps: HttpDriverDeps,
    *,
    session_key: str,
    run_id: str,
) -> dict:
    """取消一次正在进行的运行。

    Args:
        deps: HttpDriverDeps 依赖对象
        session_key: 会话 key
        run_id: 运行 id

    Returns:
        成功: {"status": "ok"}
        失败: {"status": "failed", "reason": "..."}
    """
    try:
        async with httpx.AsyncClient(timeout=deps.timeout_s) as client:
            # 中文注释：发送 POST 请求取消运行
            response = await client.post(
                f"{deps.base_url}/api/sessions/{session_key}/runs/{run_id}/cancel"
            )

            # 中文注释：检查响应状态码
            if not (200 <= response.status_code < 300):
                reason = (
                    f"取消运行失败（HTTP {response.status_code}）: "
                    f"{response.text[:200]}"
                )
                logger.error(reason)
                return {"status": "failed", "reason": reason}

            logger.info(f"运行已取消: {run_id}")
            return {"status": "ok"}

    except Exception as e:
        reason = f"取消运行异常: {e}"
        logger.exception(reason)
        return {"status": "failed", "reason": reason}


async def delete_session(
    deps: HttpDriverDeps,
    *,
    session_key: str,
) -> dict:
    """删除一个会话。

    Args:
        deps: HttpDriverDeps 依赖对象
        session_key: 会话 key

    Returns:
        成功: {"status": "ok"}
        失败: {"status": "failed", "reason": "..."}
    """
    try:
        async with httpx.AsyncClient(timeout=deps.timeout_s) as client:
            # 中文注释：发送 DELETE 请求删除会话
            response = await client.delete(
                f"{deps.base_url}/api/sessions/{session_key}"
            )

            # 中文注释：检查响应状态码
            if not (200 <= response.status_code < 300):
                reason = (
                    f"删除会话失败（HTTP {response.status_code}）: "
                    f"{response.text[:200]}"
                )
                logger.error(reason)
                return {"status": "failed", "reason": reason}

            logger.info(f"会话已删除: {session_key}")
            return {"status": "ok"}

    except Exception as e:
        reason = f"删除会话异常: {e}"
        logger.exception(reason)
        return {"status": "failed", "reason": reason}
