from __future__ import annotations

import asyncio
import copy
import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from src.graph.runtime import InlineWorkflowSyncPort, WorkflowCancellation, WorkflowRuntimeContext
from src.graph.runtime_resources import WorkflowRuntimeResources
from src.models.sessions import SESSION_STATUS_INTERRUPTED, SessionError, utc_now
from src.repositories.sessions.base import SessionRepository
from src.services.sessions import AssistantMessageBuffer, MessageHandler, invoke_message_handler_async
from src.utils import get_logger, logging_context
from src.utils.readable_id import create_readable_id


JsonObject = dict[str, Any]
logger = get_logger(__name__)

# 这些事件类型是流式输出的每一个 token，一次调研能产生上万条。
# 改造前每一条都会往数据库里插一条记录，让事件表膨胀到几十 MB；
# 前端 hydrate 时又要逐条重放，长会话必然卡死。
# 现在这些 token 不再落库，它们的最终形态由 AssistantMessageBuffer
# 聚合进 session_message 表的 assistant 消息里（含正文和完整思考内容）。
# 前端 hydrate 路径本来就不消费这两类事件（applyMessageEvent 用的是 message 事件），
# 所以删掉落库不会丢失任何对话内容，只会让数据库小一个数量级。
NON_PERSISTED_EVENT_TYPES = ("delta", "reasoning_delta")



@dataclass(slots=True)
class RunBrokerState:
    """保存单次 run 在当前服务进程中的流式状态。"""

    session_key: str
    run_id: str
    turn_id: str
    created_at: str
    events: list[JsonObject] = field(default_factory=list)
    subscribers: list[asyncio.Queue[JsonObject | None]] = field(default_factory=list)
    closed: bool = False
    cancellation: WorkflowCancellation = field(default_factory=WorkflowCancellation)
    task: asyncio.Task[None] | None = None


class SessionRunBroker:
    """负责 run 级别的内存事件分发。"""

    def __init__(self):
        """初始化内存 broker。"""

        self._runs: dict[str, RunBrokerState] = {}
        # 中文注释：这里改成普通锁，是为了同一套 broker 同时支持 async 调用
        # 和“当前事件循环里直接同步发布事件”的 nowait 调用，不再强依赖 await lock。
        self._lock = threading.RLock()

    async def open_run(self, session_key: str, run_id: str, turn_id: str, created_at: str) -> None:
        """注册一条新的 run 记录，让后续事件可以被订阅。"""

        with self._lock:
            self._runs[run_id] = RunBrokerState(
                session_key=session_key,
                run_id=run_id,
                turn_id=turn_id,
                created_at=created_at,
            )

    def attach_task(self, run_id: str, task: asyncio.Task[None]) -> None:
        """记录后台任务句柄，取消接口才能真正停止对应的工作流。"""

        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                raise SessionError(f"run not found: {run_id}", 404)
            state.task = task

    def get_run(self, run_id: str) -> RunBrokerState | None:
        """读取当前进程里的 run 状态，供取消接口校验会话归属。"""

        with self._lock:
            return self._runs.get(run_id)

    def cancellation_for(self, run_id: str) -> WorkflowCancellation:
        """返回指定 run 的停止控制对象，供工作流运行时使用。"""

        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                raise SessionError(f"run not found: {run_id}", 404)
            return state.cancellation

    def request_cancel(self, run_id: str) -> asyncio.Task[None] | None:
        """记录停止请求，并返回后台任务句柄供服务层发出取消信号。"""

        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                raise SessionError(f"run not found: {run_id}", 404)
            state.cancellation.request()
            return state.task

    def is_cancel_requested(self, run_id: str) -> bool:
        """查询指定 run 是否已经收到停止请求，避免停止与完成同时发生时误报成功。"""

        with self._lock:
            state = self._runs.get(run_id)
            return bool(state and state.cancellation.is_requested())

    async def publish(self, run_id: str, event: JsonObject) -> JsonObject:
        """向指定 run 广播一条事件，并保留一份内存历史。"""

        return self.publish_nowait(run_id, event)

    def publish_nowait(self, run_id: str, event: JsonObject) -> JsonObject:
        """在当前事件循环里立即广播事件，避免后台 run 再走线程桥接。"""

        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                raise SessionError(f"run not found: {run_id}", 404)
            event_copy = copy.deepcopy(event)
            event_copy.setdefault("stream_seq", len(state.events) + 1)
            state.events.append(event_copy)
            subscribers = list(state.subscribers)

        for queue in subscribers:
            queue.put_nowait(copy.deepcopy(event_copy))
        return event_copy

    async def close_run(self, run_id: str) -> None:
        """标记指定 run 已经结束，并通知所有订阅者退出。"""

        self.close_run_nowait(run_id)

    def close_run_nowait(self, run_id: str) -> None:
        """同步关闭事件流，供异步后台任务在同一事件循环里直接调用。"""

        with self._lock:
            state = self._runs.get(run_id)
            if state is None or state.closed:
                return
            state.closed = True
            subscribers = list(state.subscribers)

        for queue in subscribers:
            queue.put_nowait(None)

    async def subscribe(self, run_id: str):
        """订阅指定 run 的事件流，并在订阅开始时补发已有历史。"""

        queue: asyncio.Queue[JsonObject | None] = asyncio.Queue()
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                raise SessionError(f"run not found: {run_id}", 404)
            history = [copy.deepcopy(item) for item in state.events]
            closed = state.closed
            if not closed:
                state.subscribers.append(queue)

        try:
            for event in history:
                yield event
            if closed:
                return
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield copy.deepcopy(item)
        finally:
            with self._lock:
                state = self._runs.get(run_id)
                if state is not None and queue in state.subscribers:
                    state.subscribers.remove(queue)


class SessionRunService:
    """负责启动后台 run、持久化事件并对接 SSE。"""

    def __init__(
        self,
        repo: SessionRepository,
        message_handler: MessageHandler | None = None,
        broker: SessionRunBroker | None = None,
    ):
        """初始化 run service。"""

        self.repo = repo
        self.message_handler = message_handler
        self.broker = broker or SessionRunBroker()

    async def start_run(self, session_key: str, body: JsonObject | None = None) -> JsonObject:
        """创建一次新的后台运行，并立即返回 SSE 地址。"""

        body = body or {}
        # 中文注释：先确认会话存在；会话正在运行时拒绝并发提交。
        record = self.repo.get(session_key)
        content = str(body.get("content") or "")
        if not content.strip() and not body.get("media"):
            raise ValueError("content is required")

        if record.run_started_at or record.status == "running":
            raise SessionError("session is already running", 409)

        # 前端通常会传入回合编号；其他调用方未传时，后端也按统一的人类可读格式生成。
        turn_id = str(body.get("turn_id") or create_readable_id())
        run_id = str(body.get("run_id") or f"run_{uuid.uuid4().hex}")
        media = list(body.get("media") or [])
        started_at = utc_now()

        self.repo.append_message(session_key, "user", content, media=media, turn_id=turn_id)
        self.repo.set_status(session_key, "running")
        self.repo.set_run_started_at(session_key, started_at)
        # 把 run_id 写进 session metadata，前端刷新后靠它接回实时流。
        self.repo.set_active_run_id(session_key, run_id)
        await self.broker.open_run(session_key, run_id, turn_id, started_at)

        await self._publish_runtime_event(
            session_key,
            run_id,
            turn_id,
            {
                "event": "message",
                "role": "user",
                "content": content,
                "media": media,
                "turn_id": turn_id,
                "timestamp": started_at,
            },
            persist_event=True,
        )
        await self._publish_runtime_event(
            session_key,
            run_id,
            turn_id,
            {
                "event": "status",
                "status": "running",
                "run_started_at": started_at,
                "turn_id": turn_id,
                "timestamp": started_at,
            },
            persist_event=True,
        )

        task = asyncio.create_task(
            self._run_in_background(
                session_key=session_key,
                run_id=run_id,
                turn_id=turn_id,
                content=content,
                body=copy.deepcopy(body),
            )
        )
        self.broker.attach_task(run_id, task)
        return {
            "session_key": session_key,
            "run_id": run_id,
            "turn_id": turn_id,
            "status": "accepted",
            "stream_url": f"/api/sessions/{session_key}/runs/{run_id}/stream",
        }

    async def cancel_run(self, session_key: str, run_id: str) -> JsonObject:
        """处理用户主动停止请求，并让后台任务尽快在当前等待点退出。"""

        state = self.broker.get_run(run_id)
        if state is None or state.session_key != session_key:
            raise SessionError("run not found", 404)

        if state.closed or (state.task is not None and state.task.done()):
            return {
                "session_key": session_key,
                "run_id": run_id,
                "status": "already_finished",
            }

        already_requested = state.cancellation.is_requested()
        task = self.broker.request_cancel(run_id)
        turn_id = state.turn_id
        # 中文注释：先把“正在停止”告诉前端，再取消后台任务，避免任务太快结束时前端看不到中间状态。
        if not already_requested:
            self._publish_runtime_event_nowait(
                session_key,
                run_id,
                turn_id,
                {
                    "event": "status",
                    "status": "cancel_requested",
                    "message": "已收到停止请求，正在保存当前进度",
                    "turn_id": turn_id,
                    "timestamp": utc_now(),
                },
                persist_event=True,
            )
        if task is not None and not task.done():
            task.cancel()
        return {
            "session_key": session_key,
            "run_id": run_id,
            "status": "cancel_requested",
        }

    async def stream_events(self, session_key: str, run_id: str):
        """返回指定 run 的异步事件流。"""

        async for event in self.broker.subscribe(run_id):
            if str(event.get("session_key") or "") != session_key:
                continue
            yield event

    async def _run_in_background(
        self,
        *,
        session_key: str,
        run_id: str,
        turn_id: str,
        content: str,
        body: JsonObject,
    ) -> None:
        """直接在当前事件循环里执行异步工作流，不再把整套图塞进线程。"""

        resolved_handler = self.message_handler
        assistant_buffer = AssistantMessageBuffer()
        saw_turn_end = False
        # 中文注释：每次后台 run 都创建自己独立的一份运行时资源，后面节点可以通过
        # runtime_context 共享这些资源，不会和别的 run 混用。
        resources = WorkflowRuntimeResources()
        cancellation = self.broker.cancellation_for(run_id)

        def emit(event: JsonObject) -> JsonObject:
            """同步发布当前 run 事件，同时更新本地助手缓冲区。

            中文说明：
            流式 token 事件（delta / reasoning_delta）每一个都会过这个函数，
            但改造后它们不再落库——这些 token 的最终形态由 AssistantMessageBuffer
            聚合进 session_message 表的 assistant 消息里。其他事件（message、
            runtime_event、status、turn_end 等）照常落库，保持行为不变。
            """

            nonlocal saw_turn_end
            event_copy = copy.deepcopy(event)
            assistant_buffer.apply(event_copy)
            event_name = str(event_copy.get("event") or "")
            if event_name == "turn_end":
                saw_turn_end = True
            # 流式 token 不写数据库；其他事件照常落库。
            should_persist = event_name not in NON_PERSISTED_EVENT_TYPES
            return self._publish_runtime_event_nowait(
                session_key,
                run_id,
                turn_id,
                event_copy,
                persist_event=should_persist,
            )

        runtime_context = WorkflowRuntimeContext(
            session_key=session_key,
            run_id=run_id,
            turn_id=turn_id,
            workflow_name="paper_graph",
            sync_port=InlineWorkflowSyncPort(
                emit,
                session_key=session_key,
                run_id=run_id,
                turn_id=turn_id,
                workflow_name="paper_graph",
            ),
            resources=resources,
            cancellation=cancellation,
        )

        try:
            with logging_context(session_key=session_key, turn_id=turn_id, run_id=run_id):
                logger.info("后台 run 开始执行", extra={"content_length": len(content)})
                emit(
                    {
                        "event": "message",
                        "kind": "progress",
                        "role": "system",
                        "content": "正在启动论文工作流",
                        "step": "bootstrap",
                        "turn_id": turn_id,
                        "timestamp": utc_now(),
                    }
                )
                if resolved_handler is None:
                    raise SessionError("message handler is not configured", 500)
                await invoke_message_handler_async(
                    resolved_handler,
                    session_key,
                    content,
                    {"run_id": run_id, "turn_id": turn_id, "runtime_context": runtime_context, **body},
                    emit,
                )
                if self.broker.is_cancel_requested(run_id):
                    self._finalize_cancelled(
                        session_key,
                        run_id,
                        turn_id,
                        assistant_buffer,
                        already_closed=saw_turn_end,
                    )
                    logger.info("run cancelled after user request")
                    return
                if not saw_turn_end:
                    emit(
                        {
                            "event": "turn_end",
                            "status": "completed",
                            "turn_id": turn_id,
                            "timestamp": utc_now(),
                        }
                    )
                self._finalize_success(session_key, run_id, turn_id, assistant_buffer)
                logger.info("后台 run 执行完成")
        except asyncio.CancelledError:
            if self.broker.is_cancel_requested(run_id):
                # 中文注释：用户主动停止不是程序错误，不发送 error 事件，也不把会话标成 failed。
                self._finalize_cancelled(session_key, run_id, turn_id, assistant_buffer, already_closed=saw_turn_end)
                logger.info("run cancelled by user")
            else:
                # 中文注释：如果没有用户停止标记，说明是服务关闭或其他外部取消，不能伪装成用户操作。
                self._finalize_failure(
                    session_key=session_key,
                    run_id=run_id,
                    turn_id=turn_id,
                    assistant_buffer=assistant_buffer,
                    error=RuntimeError("后台 run 被外部取消"),
                    already_closed=saw_turn_end,
                )
        except Exception as error:
            logger.exception("后台 run 执行失败")
            self._finalize_failure(
                session_key=session_key,
                run_id=run_id,
                turn_id=turn_id,
                assistant_buffer=assistant_buffer,
                error=error,
                already_closed=saw_turn_end,
            )
        finally:
            await resources.aclose()

    def _finalize_success(
        self,
        session_key: str,
        run_id: str,
        turn_id: str,
        assistant_buffer: AssistantMessageBuffer,
    ) -> None:
        """在 run 成功时写回助手消息，并通知前端「这次运行结束了」。"""

        try:
            assistant_buffer.persist(self.repo, session_key, turn_id)
            self.repo.set_status(session_key, "completed")
            self.repo.set_run_started_at(session_key, None)
            self.repo.set_active_run_id(session_key, None)
        except SessionError:
            # 中文注释：会话可能在 run 执行期间被用户删掉了，这时状态写回没有
            # 意义；但下面仍要照常给前端发"运行结束"的信号，否则页面会永远等不到结果。
            self._log_session_gone(session_key, run_id)
        except Exception:
            # 中文注释：收尾写库还可能出别的意外错误，最典型的是 SQLite 锁等待
            # 超时（抛 sqlite3.OperationalError）。以前这类错误会直接穿透整个收尾
            # 函数，把下面"通知前端运行结束"这一步也跳过，页面就永远等不到结果、
            # 一直转圈。现在先把意外异常记进日志，然后照常往下走：就算收尾写库
            # 失败，也必须先让等着的页面收到结束信号，不能让用户界面永远卡住。
            logger.exception(
                "run 成功收尾时写库出现意外异常，仍会关闭事件流",
                extra={"session_key": session_key, "run_id": run_id},
            )
        self.broker.close_run_nowait(run_id)

    def _finalize_cancelled(
        self,
        session_key: str,
        run_id: str,
        turn_id: str,
        assistant_buffer: AssistantMessageBuffer,
        already_closed: bool = False,
    ) -> None:
        """保存用户停止前已经产生的回复，并把 run 结束为 cancelled。"""

        try:
            if not already_closed:
                self._publish_runtime_event_nowait(
                    session_key,
                    run_id,
                    turn_id,
                    {
                        "event": "turn_end",
                        "status": "cancelled",
                        "message": "任务已按用户请求停止，已保留当前进度",
                        "turn_id": turn_id,
                        "timestamp": utc_now(),
                    },
                    persist_event=True,
                )
            assistant_buffer.persist(self.repo, session_key, turn_id)
            self.repo.set_status(session_key, "cancelled")
            self.repo.set_run_started_at(session_key, None)
            self.repo.set_active_run_id(session_key, None)
        except SessionError:
            # 会话已被删除时跳过落库，但仍要给前端发结束信号（页面不能一直傻等）。
            self._log_session_gone(session_key, run_id)
        except Exception:
            # 中文注释：补发停止事件或写库时也可能出意外错误（比如 SQLite 锁
            # 等待超时）。不能让这种错误穿透收尾函数，否则下面"通知前端运行结束"
            # 会被跳过，页面永远等不到结果。先记日志，再照常发结束信号：就算收尾
            # 写库失败，也必须先让等着的页面收到结束信号，不能让用户界面永远转圈。
            logger.exception(
                "run 停止收尾时出现意外异常，仍会关闭事件流",
                extra={"session_key": session_key, "run_id": run_id},
            )
        self.broker.close_run_nowait(run_id)

    def _finalize_failure(
        self,
        *,
        session_key: str,
        run_id: str,
        turn_id: str,
        assistant_buffer: AssistantMessageBuffer,
        error: Exception,
        already_closed: bool,
    ) -> None:
        """在 run 失败时补发错误和终止事件，并统一收口状态。"""

        try:
            self._publish_runtime_event_nowait(
                session_key,
                run_id,
                turn_id,
                {
                    "event": "error",
                    "message": str(error),
                    "content": str(error),
                    "status": "failed",
                    "turn_id": turn_id,
                    "timestamp": utc_now(),
                },
                persist_event=True,
            )
            if not already_closed:
                self._publish_runtime_event_nowait(
                    session_key,
                    run_id,
                    turn_id,
                    {
                        "event": "turn_end",
                        "status": "failed",
                        "turn_id": turn_id,
                        "timestamp": utc_now(),
                    },
                    persist_event=True,
                )
            assistant_buffer.persist(self.repo, session_key, turn_id)
            self.repo.set_status(session_key, "failed")
            self.repo.set_run_started_at(session_key, None)
            self.repo.set_active_run_id(session_key, None)
        except SessionError:
            # 会话已被删除时跳过补发事件与落库，但仍要给前端发结束信号（页面不能一直傻等）。
            self._log_session_gone(session_key, run_id)
        except Exception:
            # 中文注释：补发错误事件或写库时也可能再出意外错误（比如 SQLite 锁
            # 等待超时）。run 本来就已经失败了，收尾再抛异常会把下面"通知前端
            # 运行结束"也跳过，页面就永远等不到结果。先记日志，再照常发结束信号：
            # 就算收尾写库失败，也必须先让等着的页面收到结束信号，不能让用户界面永远转圈。
            logger.exception(
                "run 失败收尾时出现意外异常，仍会关闭事件流",
                extra={"session_key": session_key, "run_id": run_id},
            )
        self.broker.close_run_nowait(run_id)

    def _log_session_gone(self, session_key: str, run_id: str) -> None:
        """记录"run 收尾时会话已被删除"的情况，方便排查用户删会话与运行中任务的竞争。"""

        logger.warning(
            "run 收尾时会话已不存在，跳过持久化并直接关闭事件流",
            extra={"session_key": session_key, "run_id": run_id},
        )

    async def _publish_runtime_event(
        self,
        session_key: str,
        run_id: str,
        turn_id: str,
        event: JsonObject,
        *,
        persist_event: bool,
    ) -> JsonObject:
        """标准化一条运行事件，并同时完成落库和广播。"""

        return self._publish_runtime_event_nowait(
            session_key,
            run_id,
            turn_id,
            event,
            persist_event=persist_event,
        )

    def _publish_runtime_event_nowait(
        self,
        session_key: str,
        run_id: str,
        turn_id: str,
        event: JsonObject,
        *,
        persist_event: bool,
    ) -> JsonObject:
        """在当前事件循环里同步落库并广播事件，避免再做线程安全桥接。"""

        normalized_event = self._normalize_event(session_key, run_id, turn_id, event)
        if persist_event:
            event_record = self.repo.append_event(
                session_key,
                str(normalized_event.get("event") or "unknown"),
                content=str(
                    normalized_event.get("content")
                    or normalized_event.get("delta")
                    or normalized_event.get("message")
                    or ""
                ),
                metadata={
                    key: copy.deepcopy(value)
                    for key, value in normalized_event.items()
                    if key != "content"
                },
                created_at=str(normalized_event["timestamp"]),
            )
            normalized_event["event_id"] = str(event_record.get("id") or "")
            normalized_event["stream_seq"] = int(event_record.get("seq_no") or 0)
        return self.broker.publish_nowait(run_id, normalized_event)

    def _normalize_event(self, session_key: str, run_id: str, turn_id: str, event: JsonObject) -> JsonObject:
        """补齐统一字段，保证历史事件和实时事件结构一致。"""

        normalized_event = copy.deepcopy(event)
        normalized_event.setdefault("event", "message")
        normalized_event["session_key"] = session_key
        normalized_event["chat_id"] = session_key
        normalized_event["run_id"] = run_id
        normalized_event["turn_id"] = str(normalized_event.get("turn_id") or turn_id)
        normalized_event["timestamp"] = str(normalized_event.get("timestamp") or utc_now())
        return normalized_event


def encode_sse(event: JsonObject) -> str:
    """把结构化事件编码成 SSE 文本块。"""

    event_name = str(event.get("event") or "message")
    payload = json.dumps(event, ensure_ascii=False)
    event_id = str(event.get("stream_seq") or "")
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_name}")
    lines.append(f"data: {payload}")
    return "\n".join(lines) + "\n\n"

