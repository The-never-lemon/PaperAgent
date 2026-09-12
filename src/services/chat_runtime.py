"""对话式调研的挂接层（实施方案 1.3 的挂接点）。

这个模块只做一件事：把"主对话 Agent"接到现有的 run 服务上。
app.py 里原来挂的是 build_paper_workflow_message_handler（固定流水线），
现在换成这里的 build_chat_message_handler，之后：
- runs 创建 / SSE 推流 / 取消 / 消息与事件持久化 —— 全部复用 run 服务的现有实现，零改动；
- 本模块负责组装主 Agent 需要的一切：模型快照、会话工作区、工具上下文、事件上报器。

依赖方向（工程规范 8.1）：services → agents/models/repositories，本模块不反向依赖。
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.agents.research_tools import ResearchToolContext, build_research_tool_registry
from src.agents.researchAgent import RESEARCH_LLM_PROFILE, run_conversation_agent
from src.graph.runtime import InlineWorkflowSyncPort, WorkflowCancellation, WorkflowRuntimeContext
from src.llm import ModelConfig, make_provider
from src.models.sessions import SessionError
from src.models.workspace import SessionWorkspace
from src.repositories.sessions.base import SessionRepository
from src.repositories.settings.json import SettingsRepository
from src.services.sessions import RuntimeEventEmitter
from src.utils import get_logger


JsonObject = dict[str, Any]
logger = get_logger(__name__)

# 对话主 Agent 在运行事件树里的主节点标识（与 runtime.py 里 ("chat", ...) 的显示映射对应）。
CHAT_NODE_KEY = "chat"
CHAT_NODE_TITLE = "对话调研"

# 工具执行事件挂载的节点标识（与 runtime.py 里 ("tool", ...) 的显示映射对应）。
TOOL_NODE_KEY = "tool"
TOOL_NODE_TITLE = "工具执行"

# 同步消息接口兜底创建运行上下文时使用的工作流名称。
CHAT_WORKFLOW_NAME = "chat_agent"


def build_chat_message_handler(repo: SessionRepository, settings_repo: SettingsRepository):
    """构建把会话消息交给对话式调研主 Agent 执行的处理器。

    返回的 handler 遵循 run 服务的既有约定：
    async handler(chat_id, content, frame, emit)，
    frame 里携带 run 服务组装好的运行上下文（含取消控制与共享资源）。
    """

    async def _handler(chat_id: str, content: str, frame: JsonObject, emit: RuntimeEventEmitter) -> None:
        """执行一次对话回合：装配上下文 → 交给主 Agent 工具循环。"""

        turn_id = str(frame.get("turn_id") or "")
        run_id = str(frame.get("run_id") or "") or None
        runtime = frame.get("runtime_context")
        if not isinstance(runtime, WorkflowRuntimeContext):
            # 中文注释：旧的同步消息接口没有独立的 run service，
            # 这里兜底创建一个本地运行上下文，保证 handler 在两种入口下都能工作。
            runtime = WorkflowRuntimeContext(
                session_key=chat_id,
                run_id=run_id,
                turn_id=turn_id,
                workflow_name=CHAT_WORKFLOW_NAME,
                sync_port=InlineWorkflowSyncPort(
                    emit,
                    session_key=chat_id,
                    run_id=run_id,
                    turn_id=turn_id,
                    workflow_name=CHAT_WORKFLOW_NAME,
                ),
                cancellation=(
                    frame.get("cancellation")
                    if isinstance(frame.get("cancellation"), WorkflowCancellation)
                    else None
                ),
            )
        sync_port = runtime.sync_port or InlineWorkflowSyncPort(
            emit,
            session_key=chat_id,
            run_id=run_id,
            turn_id=turn_id,
            workflow_name=CHAT_WORKFLOW_NAME,
        )

        # 第一步：装配模型快照（research_agent 档位）。
        # 配置不完整时抛出带清楚中文说明的业务异常，run 服务会统一转成
        # error 事件 + turn_end(failed)，前端永远不会死等。
        llm = _load_llm_snapshot(settings_repo)
        try:
            # 第二步：加载会话工作区。文件读取放进线程，避免阻塞事件循环。
            sessions_root = getattr(getattr(repo, "backend", None), "sessions_dir", None)
            workspace = await asyncio.to_thread(SessionWorkspace.load, chat_id, sessions_root)

            # 第三步：组装工具与子 Agent 共享的运行时上下文。
            # 工具和子 Agent 不直接触碰 SSE，发事件统一通过这里的 reporter/cancellation（工程规范 8.1）。
            chat_reporter = sync_port.for_node(CHAT_NODE_KEY, CHAT_NODE_TITLE)
            context = ResearchToolContext(
                session_key=chat_id,
                turn_id=turn_id,
                run_id=run_id,
                workspace=workspace,
                repo=repo,
                reporter=sync_port.for_node(TOOL_NODE_KEY, TOOL_NODE_TITLE),
                cancellation=runtime.cancellation,
                resources=runtime.resources,
                llm=llm,
            )
            registry = build_research_tool_registry(context)

            # 第四步：交给主 Agent 的多轮工具循环。
            # 注意：本轮的 user 消息已由 run 服务先行落库，主 Agent 直接从消息表重建历史。
            logger.info(
                "开始执行对话式调研",
                extra={"session_key": chat_id, "turn_id": turn_id, "content_length": len(content)},
            )
            await run_conversation_agent(
                context=context,
                registry=registry,
                chat_reporter=chat_reporter,
            )
        except asyncio.CancelledError:
            # 用户点了停止：原样上抛，run 服务会保留已产生的部分结果并推 turn_end(cancelled)。
            logger.info("对话式调研被用户取消", extra={"session_key": chat_id, "turn_id": turn_id})
            raise
        except Exception:
            # 顶层兜底（工程规范 8.6）：记录完整堆栈后上抛，
            # run 服务统一推 error 事件 + turn_end(failed)，前端不会死等。
            logger.exception("对话式调研执行失败", extra={"session_key": chat_id, "turn_id": turn_id})
            raise
        finally:
            # 本次 run 的模型快照是短生命周期对象，用完立即关闭它自己的连接池。
            await llm.aclose()

    return _handler


def _load_llm_snapshot(settings_repo: SettingsRepository):
    """按当前保存的模型配置装配主 Agent 使用的模型快照。

    档位名是 research_agent；用户没有单独配置这个档位时，
    ModelConfig.resolve_agent 会自动回退到必配的 default_agent，
    保证对话能力开箱可用（实施方案第七节：config 增加 research_agent 档位）。
    """

    try:
        config = ModelConfig.from_dict(settings_repo.load(), settings_repo.system())
        return make_provider(config, RESEARCH_LLM_PROFILE)
    except ValueError as exc:
        # from_dict 在缺少 default_agent 等必要配置时会抛 ValueError，
        # 这里转成用户能看懂的业务错误。
        raise SessionError(
            f"模型配置不完整，无法启动对话调研：{exc}。请先在系统设置页完成模型配置。",
            500,
        ) from exc
