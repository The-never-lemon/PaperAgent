"""对话式论文调研主 Agent（实施方案第一节的主对话 Agent）。

对外只暴露一个主入口：run_conversation_agent(...)（工程规范 8.1）。

它的工作方式（实施方案 1.1）：
1. 从会话消息表重建历史，拼上系统提示词（含工作区动态状态）；
2. 手写 while 工具循环：模型每轮要么直接给出最终回复，要么请求调用工具；
3. 工具结果统一经 render_tool_result 截断后回填给模型，让它自行决策下一步；
4. 每条 assistant/tool 消息产生的同时就落库，中断后可以完整重建上下文；
5. 轮次达到上限时注入系统提示，不带工具再调一次模型强制收敛。

本模块不直接触碰 SSE：所有事件通过传入的 reporter 发出（工程规范 8.1），
取消检查通过传入的 cancellation 完成（工程规范 8.6）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

from src.llm.base import StreamCallbacks, normalize_token_usage
from src.models.workspace import (
    PAPER_STATUS_DEEP_READ,
    PAPER_STATUS_EVALUATED,
    PAPER_STATUS_NEW,
    SessionWorkspace,
)
from src.utils import get_logger

from .Prompts import RESEARCH_AGENT_SYSTEM_PROMPT
from .context_budget import budget_tokens, estimate_messages_tokens
from .research_tools import ActiveToolCall, ResearchToolContext, render_tool_result
from .tools import ToolRegistry, ToolSpec


if TYPE_CHECKING:
    from src.graph.runtime import WorkflowNodeReporter


logger = get_logger(__name__)

JsonObject = dict[str, Any]

# 主 Agent 使用的模型档位名（对应 config/model.json 里 agents 的键；
# 用户没有单独配置这个档位时会自动回退到 default_agent）。
RESEARCH_LLM_PROFILE = "research_agent"

# 一次对话回合里工具调用的最大轮数，防止模型陷入无限调用工具的循环（实施方案 1.1）。
MAX_TOOL_ROUNDS = 20

# 同轮工具调用的最大并发数。多方向检索、多篇评价等场景会触发并行。
TOOL_PARALLEL_LIMIT = 3

# 历史预算管理（替代旧的"最近 10 轮"硬截断，实施方案"上下文分配与自动压缩"）：
# 历史能占多少 token = 模型上下文窗口 × HISTORY_BUDGET_RATIO，超出时：
#   阶段 A（零成本）：把最老轮次的工具结果替换成占位符、清空思考块；
#   阶段 B（LLM 压缩）：把装不下的最老整轮交给模型压成一段要点摘要。
# 截断/压缩永远按"用户轮"整轮进出，保证 assistant 的 tool_calls 和 tool
# 结果配对完整，缺一半模型接口会直接报错。
HISTORY_BUDGET_RATIO = 0.8

# 阶段 A 里"最近多少轮"保证只剥工具结果、绝不被 LLM 压缩掉。
KEEP_RECENT_TURNS_LITE = 6

# 阶段 B 一次压缩调用的输入上限（字符）与输出摘要上限（字符）。
COMPRESS_INPUT_MAX_CHARS = 16000
COMPRESSED_SUMMARY_MAX_CHARS = 400

# 旧轮次工具结果的占位符模板，{tool_name} 会被替换成实际工具名。
TOOL_RESULT_PLACEHOLDER = (
    "[历史工具结果已归档：{tool_name}。可用 get_history 工具读取旧轮次原文，"
    "或用对应工具重新获取。]"
)

# 中文注释：实测字符率的内存缓存（"最近一次实测：多少字符 ≈ 1 token"）。
# 每轮模型调用返回真实 usage 后回填，越用越准；无锁、无落库，偶发并发
# 写入也只是多算一次的事，不影响正确性。
_RECENT_CHARS_PER_TOKEN = 4.5

# 中文注释：上一轮请求供应商实际数出来的 prompt_tokens。它是唯一能反映
# "真实上下文占用"的数字（估算器再好也只是近似）——用它做自适应校准：
# 上一轮已经吃到窗口 75% 以上时，下一轮按比例压低预算强制收缩，避免
# 估算误差或配置失真（比如窗口填了 1M 但供应商实际更小）导致请求被拒。
_LAST_OBSERVED_PROMPT_TOKENS = 0

# 上一轮实测 prompt_tokens 达到窗口的这个比例时，自适应校准开始干预。
_ADAPTIVE_CALIBRATION_RATIO = 0.75

# 更早轮次摘要里，单条用户诉求保留的最大字符数。
EARLIER_SUMMARY_ITEM_CHARS = 100

# 系统提示词里工作区论文预览的最多条数与标题截断长度。
WORKSPACE_PREVIEW_MAX_PAPERS = 10
WORKSPACE_PREVIEW_TITLE_CHARS = 80

# 达到轮次上限后注入的收敛指令（实施方案 1.1）。
ROUND_LIMIT_INSTRUCTION = (
    "[系统提示] 工具调用轮次已达上限，请不要再调用任何工具，"
    "直接基于以上已获得的信息给出最终回答。"
)

# 终答里如果出现工作区里不存在的 paper_id，注入这条指令让模型自我纠正。
# 最多修一次（FINAL_ANSWER_MAX_REPAIR），避免陷入无限纠正循环。
UNKNOWN_PAPER_ID_INSTRUCTION = (
    "[系统提示] 你在上一条回复里引用了工作区里不存在的论文编号：{unknown_ids}。"
    "请重新组织回复，只使用当前工作区里真实存在的论文编号（可用 list_papers 查看），"
    "不要再调用任何工具，直接给出修正后的最终回答。"
)

# 终答一致性自检最多修一次。
FINAL_ANSWER_MAX_REPAIR = 1

# 中文注释：哪些工具返回状态算"这次调用失败了"。
# 工具的失败一共有三种形态，这里收口成一张表，避免以后再往各处散着加判断：
#   1）轻量工具直接返回 {"error": ...}（下面单独判断，不在这张表里）；
#   2）子 Agent 按约定返回 {"status": "failed", "reason": ...}；
#   3）下载工具返回 {"status": "download_failed", "reason": ...}——它确实去下载了但没成功
#      （被 403 拒绝、超时、文件过大、拿回来的不是 PDF 等）。
#
# 特别注意：**不要**把下载工具的 {"status": "no_url"} 列进来。
# no_url 的意思是"这个数据源没给出开放获取的全文直链"，属于外部限制导致的**优雅降级**
# （系统会自动改用摘要精读，并把原因告诉用户），不是程序出错。
# 如果把它也算失败，付费墙论文会让用户看到一个本不该出现的报错。
FAILED_TOOL_STATUSES = frozenset({"failed", "download_failed"})

# 自检终答引用时，候选编号必须整体符合这个"长相"：只由字母、数字、下划线、
# 连字符、点、斜杠、冒号组成（比如 p0001、arXiv:2301.00001）。
# 这样含中文、空格、加号、尖角号等内容的方括号（比如 [a+b]、[^1]、[论文一]）
# 天然不会被当成引用编号，避免把正常文字误判成"虚构引用"，白白触发一次
# 模型纠偏调用，甚至把本来正确的回答改坏。
_PAPER_ID_SHAPE_PATTERN = re.compile(r"^[A-Za-z0-9_\-./:]+$")

# 收集方括号引用时用这个正则：右括号后面紧跟左括号的（比如 [arXiv 页面](https://...)）
# 是 Markdown 的行内链接写法，方括号里是链接的显示文字，不是论文编号，要跳过。
_BRACKET_CITATION_PATTERN = re.compile(r"\[([^\[\]]+)\](?!\()")

# 常见的占位词（比较时不区分大小写）：[TODO]、[NOTE]、[FIXME] 这类方括号内容
# 是写作提醒，不是论文编号，不应该触发纠偏。
_CITATION_PLACEHOLDER_WORDS = {"todo", "note", "fixme"}

# 模型调用失败时给用户的固定文案模板。
LLM_FAILURE_MESSAGE = "抱歉，模型服务暂时不可用（{reason}），请稍后重试。"

# 工具失败事件里错误说明的截断长度（运行卡片只显示摘要）。
TOOL_ERROR_DISPLAY_CHARS = 200

# 论文状态在系统提示词里的中文标签。
_STATUS_LABELS = {
    PAPER_STATUS_NEW: "未评价",
    PAPER_STATUS_EVALUATED: "已评价",
    PAPER_STATUS_DEEP_READ: "已精读",
}


async def run_conversation_agent(
    *,
    context: ResearchToolContext,
    registry: ToolRegistry,
    chat_reporter: "WorkflowNodeReporter",
) -> None:
    """执行一次完整的对话回合：理解用户输入 → 工具循环 → 给出最终回复。

    Args:
        context: 运行时上下文（会话编号、工作区、仓储、取消控制、模型快照等），
            由 chat_runtime 组装；context.llm 必须已经装配好。
        registry: 当前阶段已注册的工具集合，模型只能看到并调用这些工具。
        chat_reporter: "对话调研"主节点的事件上报器，正文增量、思考增量、
            每轮用量和最终回复都通过它推给前端。

    本函数不抛业务异常：模型失败会转化为用户可读的回复；用户取消
    （CancelledError）原样上抛，由 run 服务统一收尾。
    """

    if context.llm is None:
        # 装配缺口属于程序错误，直接抛给 chat_runtime 的顶层兜底处理。
        raise ValueError("run_conversation_agent 需要已装配的模型快照（context.llm）")

    session_key = context.session_key
    turn_id = context.turn_id
    repo = context.repo
    workspace = context.workspace

    # 主卡片先亮起来：整理历史可能要读库、压缩上下文，这段时间前端不能一片空白。
    chat_reporter.started("正在整理对话上下文")

    # 第一步：从消息表重建对话历史（本轮 user 消息已由 run 服务先行落库），
    # 拼出系统提示词 + 按预算压缩后的历史消息。文件/数据库读取放到线程里，避免卡住事件循环。
    record = await asyncio.to_thread(repo.get, session_key)
    # 中文注释：历史预算 = 模型上下文窗口 × 0.8（窗口没配按 1M 计，system.yaml
    # 的 context_window_tokens 可调）；字符率用"最近一次实测值"，首轮没有实测
    # 数据时用保守默认 4.5。
    history_budget = budget_tokens(
        getattr(context.llm, "context_window_tokens", None)
    )
    # 中文注释：自适应校准——上一轮请求的实际 prompt_tokens 已经逼近窗口
    # （比如供应商的实际窗口比配置小、或估算偏保守）时，按实际占用反推一个
    # 更紧的预算，防止估算失真导致请求被供应商拒绝。
    window_tokens = int(getattr(context.llm, "context_window_tokens", None) or 1048576)
    if (
        _LAST_OBSERVED_PROMPT_TOKENS > 0
        and _LAST_OBSERVED_PROMPT_TOKENS / window_tokens >= _ADAPTIVE_CALIBRATION_RATIO
    ):
        adaptive_budget = int(_LAST_OBSERVED_PROMPT_TOKENS * 0.6)
        history_budget = min(history_budget, adaptive_budget)
        logger.info(
            "上一轮 prompt_tokens 逼近窗口，启用自适应预算校准",
            extra={
                "session_key": session_key,
                "observed_prompt_tokens": _LAST_OBSERVED_PROMPT_TOKENS,
                "window_tokens": window_tokens,
                "history_budget": history_budget,
            },
        )
    messages = await build_llm_messages(
        record.messages,
        workspace,
        history_budget=history_budget,
        chars_per_token=_RECENT_CHARS_PER_TOKEN,
        compress_llm=context.llm,
    )

    # 工具执行的事件统一挂在 "tool" 节点下（对应 runtime.py 里 ("tool", 工具名) 的显示映射）。
    tool_reporter = chat_reporter.sync_port.for_node("tool", "工具执行")
    tool_call_seq = 0
    tool_names = sorted(tool["function"]["name"] for tool in registry.as_llm_tools())
    logger.info(
        "对话调研 run 开始",
        extra={
            "session_key": session_key,
            "turn_id": turn_id,
            "history_messages": len(messages),
            "tools": tool_names,
        },
    )

    for round_no in range(1, MAX_TOOL_ROUNDS + 1):
        # 取消检查点①：每轮开头检查用户是否点了停止（实施方案 1.1）。
        context.check_cancelled()

        # 第二步：流式调用模型。正文增量推 delta、思考增量推 reasoning_delta，
        # 前端靠它们实时渲染打字效果。工具名一旦在流里出现，就先开一张「准备调用」
        # 卡片；参数分片不再重复发卡，真正执行时复用同一张卡改成「正在执行」。
        chat_reporter.progress(f"正在思考（第 {round_no} 轮）", stage="llm_round")
        preview_keys: dict[int, str] = {}

        def on_tool_call_delta(item: JsonObject) -> None:
            noted = note_streaming_tool_call(preview_keys, item, round_no)
            if noted is None:
                return
            _index, name, event_key = noted
            tool_reporter.started(
                f"准备调用 {name}",
                stage=name,
                event_key=event_key,
            )

        callbacks = StreamCallbacks(
            on_content_delta=chat_reporter.delta,
            on_thinking_delta=chat_reporter.reasoning_delta,
            on_tool_call_delta=on_tool_call_delta,
        )
        response = await context.llm.provider.chat_stream(
            messages,
            callbacks,
            tools=registry.as_llm_tools(),
        )
        _report_round_usage(chat_reporter, round_no, response, messages)
        if response.reasoning_content:
            # 思考流结束要给前端一个明确信号，界面上的"思考中"区域才会收起。
            chat_reporter.reasoning_end()

        if not response.ok:
            # 模型调用失败（provider 已内置重试，走到这里说明重试也失败了）：
            # 如实告诉用户并结束本回合，turn_end 由 run 服务兜底补发。
            logger.error(
                "主 Agent 模型调用失败",
                extra={
                    "session_key": session_key,
                    "round": round_no,
                    "error_kind": response.error_kind,
                    "error_status_code": response.error_status_code,
                    # 中文注释：把上游返回的错误原文也记下来。只记 error_kind 的话，
                    # 排查时根本看不出对方到底在抱怨什么——上次就是因此绕了很多弯路。
                    "upstream_message": str(response.content or "")[:500],
                },
            )
            chat_reporter.message(
                role="assistant",
                content=LLM_FAILURE_MESSAGE.format(reason=response.error_kind or "未知错误"),
                metadata={"kind": "text", "error": True},
            )
            return

        content = (response.content or "").strip()

        if not response.tool_calls:
            # 第三步：没有工具调用 → 这就是最终回复。
            # 但先做一次 paper_id 一致性自检：如果回复里引用了工作区里不存在的
            # paper_id，让模型自我纠正一次（最多修一次，避免无限循环）；
            # 修完仍有问题时，循环结束后会在终答里如实标注给用户（见下方兜底检查）。
            repair_count = 0
            unknown = _unknown_paper_ids(content, workspace)
            while unknown and repair_count < FINAL_ANSWER_MAX_REPAIR:
                repair_count += 1
                logger.warning(
                    "终答一致性自检：发现不存在的 paper_id，触发自我纠正",
                    extra={
                        "session_key": session_key,
                        "turn_id": turn_id,
                        "unknown_ids": unknown,
                        "repair_attempt": repair_count,
                    },
                )
                # 把纠偏指令追加到上下文，不带 tools 再调一次
                messages.append({"role": "assistant", "content": content})
                repair_instruction = UNKNOWN_PAPER_ID_INSTRUCTION.format(
                    unknown_ids=", ".join(unknown)
                )
                messages.append({"role": "user", "content": repair_instruction})
                callbacks = StreamCallbacks(
                    on_content_delta=chat_reporter.delta,
                    on_thinking_delta=chat_reporter.reasoning_delta,
                )
                response = await context.llm.provider.chat_stream(messages, callbacks)
                _report_round_usage(chat_reporter, round_no + repair_count, response)
                if not response.ok:
                    # 纠偏的模型调用失败：先退出循环。循环结束后的兜底检查会把
                    # 没核实的引用编号如实标注进终答，不会静默放行。
                    break
                content = (response.content or "").strip()
                unknown = _unknown_paper_ids(content, workspace)
                if not unknown:
                    logger.info(
                        "终答一致性自检通过",
                        extra={
                            "session_key": session_key,
                            "turn_id": turn_id,
                            "repair_attempts": repair_count,
                        },
                    )
                    break
            # 修复循环结束后的统一兜底检查。两条"静默放行"的路径都会落到这里：
            # 1) 修了一次之后，回复里仍然有工作区找不到的论文编号（while 条件不满足直接退出）；
            # 2) 纠偏那次模型调用本身失败了（上面 not response.ok 的 break）。
            # 处理原则：宁可明确告诉用户哪几条引用没核实，也不装作没问题、
            # 把带着虚构引用的回答原样发出去，让用户自己去分辨哪些引用可信。
            if unknown:
                content += (
                    "\n\n> 注意：以下引用编号未能在当前工作区找到对应论文，未经核实："
                    + "、".join(unknown)
                )
                logger.warning(
                    "终答一致性自检：修复后仍有虚构 paper_id，已在终答里如实标注给用户",
                    extra={
                        "session_key": session_key,
                        "turn_id": turn_id,
                        "still_unknown": unknown,
                        "repair_attempts": repair_count,
                    },
                )
            # 发一条完整的 message 事件：前端用它定格助手气泡，
            # run 服务的助手消息缓冲区也会以它为准把终稿写进消息表。
            chat_reporter.message(role="assistant", content=content, metadata={"kind": "text"})
            logger.info(
                "对话调研 run 结束",
                extra={
                    "session_key": session_key,
                    "turn_id": turn_id,
                    "rounds": round_no,
                    "tool_calls": tool_call_seq,
                    "final_chars": len(content),
                    "repair_attempts": repair_count,
                },
            )
            return

        # 第四步：有工具调用的轮次。本轮正文先定格成一条消息（前端立即渲染），
        # 然后把 assistant(tool_calls) 消息加入上下文并同步落库（工程规范 8.4）。
        if content:
            chat_reporter.message(
                role="assistant",
                content=content,
                metadata={"kind": "text", "llm_round": round_no},
            )

        calls = _normalize_tool_calls(response.tool_calls, round_no)
        # 中文注释：这一轮的思考块要走两条路——一条进本次运行的上下文（供下一轮请求回传），
        # 一条落库（供下次对话重建历史时回传）。少任何一条，推理模型都会拒绝后续请求。
        thinking_blocks = response.reasoning_blocks
        messages.append(_assistant_tool_call_message(content, calls, thinking_blocks))
        await asyncio.to_thread(
            repo.append_message,
            session_key,
            "assistant",
            content,
            tool_calls=[{"id": call["id"], "name": call["name"], "arguments": call["arguments"]} for call in calls],
            thinking_blocks=thinking_blocks,
            llm_round=round_no,
            turn_id=turn_id,
        )

        # 第五步：并行执行本轮请求的工具调用。
        # 先给每个调用预分配 event_key 和 seq，避免并发时互相覆盖。
        # 流式预览已经按供应商 index 开过卡的，这里必须复用同一把键，
        # 否则前端会先看到「准备调用」再突然多出一张「正在执行」。
        ordered_preview_keys = [preview_keys[index] for index in sorted(preview_keys)]
        call_metadata = []
        for call_index, call in enumerate(calls):
            tool_call_seq += 1
            event_key = (
                ordered_preview_keys[call_index]
                if call_index < len(ordered_preview_keys)
                else f"{call['name']}_{tool_call_seq}"
            )
            call_metadata.append({
                "call": call,
                "seq": tool_call_seq,
                "event_key": event_key,
            })

        # 用信号量控制并发上限，asyncio.gather 并行执行所有工具调用。
        semaphore = asyncio.Semaphore(TOOL_PARALLEL_LIMIT)

        async def _execute_single_tool(meta: JsonObject) -> JsonObject:
            """执行单个工具调用，包含事件上报和结果收集。

            中文注释：
            每个 task 独立设置 active_call（ContextVar task-local），
            子 Agent 的进度和用量都聚合到对应的工具卡片上。
            """
            call = meta["call"]
            event_key = meta["event_key"]

            async with semaphore:
                # 取消检查点：每个工具执行前检查。
                context.check_cancelled()
                # 设置当前 task 的活跃工具调用（ContextVar，task-local）。
                active_call_obj = ActiveToolCall(
                    tool_name=call["name"],
                    event_key=event_key,
                    reporter=tool_reporter,
                )
                context.set_active_call(active_call_obj)
                tool_reporter.started(
                    f"正在执行 {call['name']}",
                    stage=call["name"],
                    event_key=event_key,
                    arguments=call["arguments"],
                    arguments_summary=_arguments_summary(call["arguments"]),
                )
                logger.info(
                    "执行工具调用",
                    extra={
                        "session_key": session_key,
                        "round": round_no,
                        "tool_name": call["name"],
                        "arguments_summary": _arguments_summary(call["arguments"]),
                    },
                )

                result = await _execute_tool(registry, call)
                # 失败有两种形态：轻量工具返回 {"error": ...}；
                # 子 Agent 和下载工具返回 {"status": ...}，具体哪些状态算失败见 FAILED_TOOL_STATUSES。
                failed = isinstance(result, dict) and (
                    bool(result.get("error")) or result.get("status") in FAILED_TOOL_STATUSES
                )
                if failed:
                    failure_text = str(result.get("error") or result.get("reason") or "工具执行失败")
                    tool_reporter.failed(
                        failure_text[:TOOL_ERROR_DISPLAY_CHARS],
                        stage=call["name"],
                        event_key=event_key,
                    )
                else:
                    tool_reporter.completed(None, stage=call["name"], event_key=event_key)

                # 返回结果供主循环按顺序回填上下文和落库。
                return {
                    "call": call,
                    "result": result,
                    "rendered": render_tool_result(call["name"], result),
                }

        # 并行执行所有工具调用，gather 保序（返回结果顺序和 calls 一致）。
        parallel_results = await asyncio.gather(*[_execute_single_tool(meta) for meta in call_metadata])
        context.set_active_call(None)

        # 第六步：按原始顺序把工具结果回填上下文并落库。
        for pr in parallel_results:
            call = pr["call"]
            rendered = pr["rendered"]
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": rendered,
                }
            )
            await asyncio.to_thread(
                repo.append_message,
                session_key,
                "tool",
                rendered,
                tool_call_id=call["id"],
                tool_name=call["name"],
                llm_round=round_no,
                turn_id=turn_id,
            )



    # 第六步：轮次用满仍未收敛 → 注入系统提示，不带工具再调一次，强制给出最终回答。
    context.check_cancelled()
    logger.warning(
        "主 Agent 工具轮次达到上限，强制收敛",
        extra={"session_key": session_key, "turn_id": turn_id, "max_rounds": MAX_TOOL_ROUNDS},
    )
    messages.append({"role": "user", "content": ROUND_LIMIT_INSTRUCTION})
    callbacks = StreamCallbacks(
        on_content_delta=chat_reporter.delta,
        on_thinking_delta=chat_reporter.reasoning_delta,
    )
    response = await context.llm.provider.chat_stream(messages, callbacks)
    _report_round_usage(chat_reporter, MAX_TOOL_ROUNDS + 1, response, messages)
    final_content = (response.content or "").strip() if response.ok else LLM_FAILURE_MESSAGE.format(
        reason=response.error_kind or "未知错误"
    )
    # 中文注释：强制收敛拿到的终答也要过同一套 paper_id 一致性自检——不能因为
    # 这条路径是"轮次用满后收敛"就留一个口子，否则虚构引用会从这里溜出去。
    # 检查逻辑和标注格式复用前面正常终答路径的同一套，保证两条路径行为一致：
    # 宁可明确告诉用户哪几条引用没核实，也不把带虚构引用的回答原样发出去。
    if response.ok:
        forced_unknown = _unknown_paper_ids(final_content, workspace)
        if forced_unknown:
            final_content += (
                "\n\n> 注意：以下引用编号未能在当前工作区找到对应论文，未经核实："
                + "、".join(forced_unknown)
            )
            logger.warning(
                "终答一致性自检：强制收敛的终答里仍有虚构 paper_id，已如实标注给用户",
                extra={
                    "session_key": session_key,
                    "turn_id": turn_id,
                    "still_unknown": forced_unknown,
                },
            )
    chat_reporter.message(role="assistant", content=final_content, metadata={"kind": "text"})


# ---------------------------------------------------------------------------
# 历史消息组装（工程规范 8.5：滑窗截断逻辑集中在这里的纯函数）
# ---------------------------------------------------------------------------


# 阶段 B 的压缩摘要内存缓存：key = 被压缩切片的 sha1 指纹 → 要点摘要文本。
# 压缩摘要从不落库（消息表 append-only 且前端渲染复用同一份数据），
# 同一段历史每次重建都用同一把 key 命中缓存，避免重复调模型。
_COMPRESSION_CACHE: dict[str, str] = {}


async def build_llm_messages(
    history_messages: list[JsonObject],
    workspace: SessionWorkspace,
    *,
    history_budget: int | None = None,
    chars_per_token: float = 4.5,
    compress_llm: Any | None = None,
) -> list[JsonObject]:
    """把落库的会话历史重建成发给模型的消息列表。

    做三件事（方案"上下文分配与自动压缩"）：
    1. 把消息表里的每一行转成模型协议格式：普通文本、带 tool_calls 的
       assistant 消息、tool 结果消息；
    2. 按上下文预算管理历史（替代旧的"最近 N 轮"硬截断）：
       - 阶段 A（零成本剥离）：估算整份历史占用的 token，超过 history_budget 时，
         从最老的轮次开始把 tool 结果替换成占位符、清空思考块、逐轮剥离；
       - 阶段 B（LLM 压缩）：阶段 A 剥完仍超预算时，把装不下的最老整轮交给
         模型压成一段要点摘要，被压缩的整轮从上下文移除，摘要并入系统提示词。
         需要 compress_llm 提供 provider；不传则跳过阶段 B，只用阶段 A 结果
         兜底——宁可历史超一点预算，也不让组装过程失败；
       截断/压缩永远按"用户轮"整轮进出（轮次边界 = user 消息），保证 assistant
       的 tool_calls 和对应的 tool 结果永远成对出现，缺一半模型接口会直接报错；
    3. 被压缩/剥离轮次的用户诉求摘要和系统提示词（含工作区动态状态）一起
       放在最前面。

    摘要产物只在内存里（模块级缓存），绝不写回消息表——消息表是 append-only
    的持久链路，前端历史渲染也读同一份数据，写了摘要会污染展示。
    """

    # 第一步：逐行转换消息表记录，并记下每个"用户轮"的起始位置。
    converted: list[JsonObject] = []
    turn_starts: list[int] = []
    for row in history_messages or []:
        role = str(row.get("role") or "")
        content = str(row.get("content") or "")
        if role == "user":
            if not content.strip():
                continue
            turn_starts.append(len(converted))
            converted.append({"role": "user", "content": content})
        elif role == "assistant":
            tool_calls = row.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                # 带工具调用的中间轮消息：arguments 落库时是字典，
                # 回传给模型接口时要按协议序列化回 JSON 字符串。
                # 中文注释：还要把当时存下来的思考块一起带上。推理模型缺了它就会拒绝
                # 这次请求，表现为「模型服务暂时不可用」。
                stored_thinking = row.get("thinking_blocks")
                converted.append(
                    {
                        "role": "assistant",
                        "content": content or "",
                        "tool_calls": [
                            {
                                "id": str(call.get("id") or ""),
                                "type": "function",
                                "function": {
                                    "name": str(call.get("name") or ""),
                                    "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                                },
                            }
                            for call in tool_calls
                            if isinstance(call, dict)
                        ],
                        "thinking_blocks": stored_thinking if isinstance(stored_thinking, list) else [],
                    }
                )
            elif content.strip():
                # 每轮的最终回复（由 run 服务的助手消息缓冲区落库）。
                converted.append({"role": "assistant", "content": content})
        elif role == "tool":
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": str(row.get("tool_call_id") or ""),
                    "name": str(row.get("tool_name") or ""),
                    "content": content,
                }
            )
        # 其他角色的行（例如历史遗留数据）不参与模型上下文，直接跳过。

    # 第 1.5 步：配对净化。
    # 中文注释：run 被取消（用户点停止、服务重启）时，可能留下"带了 tool_calls
    # 却没有工具结果"或"孤儿 tool 消息"的残缺轮次——上游网关按协议校验会直接
    # 400 拒绝整次请求（"An assistant message with 'tool_calls' must be followed
    # by tool messages"），而且这个会话从此每次都会撞同一堵墙。重建历史时
    # 在这里把残缺修成合法序列：
    #   - 带多笔 tool_calls 但只有部分回应 → 只保留有回应的调用项；
    #   - 一笔回应都没有 → 降级成一条普通正文 assistant 消息（保留思考块），
    #     tool_calls 框架直接去掉；
    #   - 找不到父亲的孤儿 tool 消息 → 丢弃。
    converted = _repair_tool_call_pairing(converted)

    # 第二步：按预算逐层收缩历史。
    # 旧轮次的用户诉求先收集起来，无论走阶段 A 还是 B 都会进系统提示词的摘要节。
    # 阶段 B（LLM 压缩）需要调模型，由本函数完成，因此整个组装定义为 async。
    earlier_user_lines: list[str] = []
    if history_budget and turn_starts:
        earlier_user_lines, converted = await _shrink_history_within_budget(
            converted,
            turn_starts,
            history_budget=history_budget,
            chars_per_token=chars_per_token,
            compress_llm=compress_llm,
        )

    # 第三步：拼装系统提示词 = 静态规则 + 工作区动态状态 + 更早轮次摘要。
    system_content = RESEARCH_AGENT_SYSTEM_PROMPT + "\n\n" + _workspace_state_section(workspace)
    if earlier_user_lines:
        summary_text = "\n".join(
            f"{index}. {line}" for index, line in enumerate(earlier_user_lines, start=1)
        )
        # 中文注释：单条诉求在收集时已各自截过 100 字符，且每条都很短，
        # 轮次多时摘要节自然变长但总体可控，不再额外截总量。
        system_content += (
            "\n\n## 此前对话摘要\n"
            "更早轮次的细节已不在上下文中（工具结果已归档），用户此前提过这些诉求"
            "（如需旧轮次细节可用 get_history 工具读取）：\n" + summary_text
        )

    return [{"role": "system", "content": system_content}, *converted]


def _repair_tool_call_pairing(converted: list[JsonObject]) -> list[JsonObject]:
    """把残缺的 tool_calls/tool 配对修成合法序列（就地重建消息列表）。

    中文注释：run 被取消或崩溃时，assistant(tool_calls) 落库了、工具结果却没
    来得及落库，消息表里就留下残缺轮次。以后每次重建历史都会带上它，上游按
    协议校验直接拒绝整次请求，会话从此卡死。这里在组装时一次性修复：
      1) 先收集所有 tool 消息的 tool_call_id；
      2) 带 tool_calls 的 assistant 消息：只保留有回应的调用项（消息里其余
         调用项删掉）；一笔回应都没有 → 降级为普通正文消息，tool_calls 移除，
         thinking 块保留（推理模型仍需回传）；
      3) 孤儿 tool 消息（tool_call_id 找不到任何父亲）→ 丢弃。
    """

    responded_ids = {
        str(message.get("tool_call_id") or "")
        for message in converted
        if message.get("role") == "tool"
    }
    repaired: list[JsonObject] = []
    seen_call_ids: set[str] = set()
    for message in converted:
        role = str(message.get("role") or "")
        if role == "tool":
            # 孤儿 tool 消息：它的 tool_call_id 没有任何 assistant 声明过，丢弃。
            if str(message.get("tool_call_id") or "") not in seen_call_ids:
                continue
            repaired.append(message)
            continue
        tool_calls = message.get("tool_calls")
        if role == "assistant" and tool_calls:
            # 过滤掉没有回应的调用项；有回应的留着，并把它们的 id 记入"已声明"。
            calls_with_results = [
                call for call in tool_calls if str(call.get("id") or "") in responded_ids
            ]
            if calls_with_results:
                message["tool_calls"] = calls_with_results
                seen_call_ids.update(str(call.get("id") or "") for call in calls_with_results)
                repaired.append(message)
            else:
                # 一笔回应都没有：降级成普通正文消息。正文可能为空（模型把整轮
                # 都写在工具调用里），此时用一句说明兜底，避免发空 content。
                fallback_text = str(message.get("content") or "").strip()
                if not fallback_text:
                    fallback_text = "（这轮的工具调用被中断，未取得结果；如有需要请重新调用对应工具。）"
                demoted = {"role": "assistant", "content": fallback_text}
                thinking = message.get("thinking_blocks")
                if isinstance(thinking, list) and thinking:
                    demoted["thinking_blocks"] = thinking
                repaired.append(demoted)
                continue
        else:
            repaired.append(message)
    return repaired


async def _shrink_history_within_budget(
    converted: list[JsonObject],
    turn_starts: list[int],
    *,
    history_budget: int,
    chars_per_token: float,
    compress_llm: Any | None = None,
) -> tuple[list[str], list[JsonObject]]:
    """把历史收缩到预算内，返回 (更早轮次的用户诉求/摘要列表, 收缩后的消息列表)。

    两层闸门，从最老的轮次开始逐层收紧：
      阶段 A：tool 结果 → 占位符、思考块 → 空、终答正文 → 只留前 400 字符；
      阶段 B：整轮移除（交由 LLM 压缩成一段要点摘要）。
    全程按"用户轮"整轮操作，tool_calls/tool 配对天然完整。
    """

    working = converted
    summary_lines: list[str] = []

    # ---------- 阶段 A：剥工具结果（零成本，不调模型） ----------
    # 从最老的轮次开始，把"最近 KEEP_RECENT_TURNS_LITE 轮"以外的旧轮次逐轮剥离，
    # 每剥一轮重新估算一次，直到估算 token 不超过预算，或者可剥的旧轮剥完。
    if len(turn_starts) > KEEP_RECENT_TURNS_LITE:
        # 受保护的最近几轮从 message 列表的这个下标开始，阶段 A 绝不越过它。
        protected_start = turn_starts[-KEEP_RECENT_TURNS_LITE]
        peelable_starts = turn_starts[:-KEEP_RECENT_TURNS_LITE]
    else:
        protected_start = len(working)
        peelable_starts = []
    if peelable_starts and estimate_messages_tokens(working, chars_per_token) > history_budget:
        peel_start = 0
        for peeled_index in range(len(peelable_starts)):
            # 这个区间的终点 = 下一个候选轮次的起点；最后一个候选轮的终点
            # 是受保护区起点（绝不能把最近几轮也剥了）。
            peel_end = (
                peelable_starts[peeled_index + 1]
                if peeled_index + 1 < len(peelable_starts)
                else protected_start
            )
            segment = working[peel_start:peel_end]
            for message in segment:
                role = str(message.get("role") or "")
                if role == "tool":
                    # 工具结果 → 一句占位符（写明工具名和取回方式）。
                    message["content"] = TOOL_RESULT_PLACEHOLDER.format(
                        tool_name=str(message.get("name") or "未知工具")
                    )
                elif role == "assistant":
                    if message.get("tool_calls"):
                        # 带工具调用的中间消息：只清思考块（占空间最大、模型不看），
                        # tool_calls 框架保留——协议要求它和随后的 tool 消息配对。
                        message["thinking_blocks"] = []
                    else:
                        # 旧轮次的最终回复：思考块清空，正文保留前 400 字符——
                        # 结论基本都在开头，太长的列表尾巴对后续对话价值不大。
                        message["thinking_blocks"] = []
                        content = str(message.get("content") or "")
                        if len(content) > 400:
                            message["content"] = content[:400] + "……[旧回复已截断]"
            peel_start = peel_end
            if estimate_messages_tokens(working, chars_per_token) <= history_budget:
                break

    # ---------- 阶段 B：LLM 压缩最老的整轮 ----------
    # 阶段 A 剥完仍超预算：把从最老轮次起装不下的整轮交给 LLM 压缩。
    # 每次取一整轮（user 轮起点之间的所有消息都属于这一轮，assistant(tool_calls)
    # 和 tool 结果天然整体进出），直到估算不超预算、或只剩 KEEP_RECENT_TURNS_LITE 轮。
    if estimate_messages_tokens(working, chars_per_token) > history_budget:
        removed_turns: list[list[JsonObject]] = []
        remaining_starts = list(turn_starts)
        while (
            len(remaining_starts) > KEEP_RECENT_TURNS_LITE
            and estimate_messages_tokens(working, chars_per_token) > history_budget
        ):
            # 每次取最老的一整轮：它的终点 = 下一轮起点相对当前 working 开头的偏移。
            # 中文注释：remaining_starts 里存的是原始列表下标，每切掉一轮 working
            # 就变短一段，所以必须用"相邻两项相减"算相对偏移，直接用原始下标会多切。
            next_cut = (
                remaining_starts[1] - remaining_starts[0]
                if len(remaining_starts) > 1
                else len(working)
            )
            removed_turns.append(working[:next_cut])
            working = working[next_cut:]
            remaining_starts = remaining_starts[1:]
        if removed_turns:
            summary = await _compress_completed_turns(removed_turns, compress_llm)
            if summary:
                summary_lines.append(summary)
            else:
                # 压缩失败（没传模型/调用失败/输出为空）兜底：不重试也不崩，
                # 只罗列这些轮次里用户的原始诉求，信息量少但比直接丢失好。
                summary_lines.extend(_fallback_user_request_lines(removed_turns))

    return summary_lines or [], working


def _fallback_user_request_lines(removed_turns: list[list[JsonObject]]) -> list[str]:
    """压缩失败时的兜底：罗列被移除轮次里用户的原始诉求（各截 100 字符）。"""

    lines: list[str] = []
    for turn_slice in removed_turns:
        for message in turn_slice:
            if str(message.get("role") or "") == "user":
                text = str(message.get("content") or "").strip()
                if text:
                    lines.append(text[:EARLIER_SUMMARY_ITEM_CHARS])
    return lines


async def _compress_completed_turns(removed_turns: list[list[JsonObject]], compress_llm: Any | None) -> str:
    """把被移除的整轮交给 LLM 压成一段要点摘要（带缓存）。

    中文注释：被压缩的轮次在多次 run 重建历史时是同一份内容——按被压缩切片
    的内容指纹做缓存 key，命中就不再调模型。摘要在内存里，不落库。
    compress_llm 为空（模型没装配）时直接返回空字符串，由调用方走确定性兜底。
    """

    if compress_llm is None:
        return ""

    # 中文注释：把切片序列化成稳定文本做指纹。换 provider/模型后重新压一次摘要
    # 结果也是对的，所以缓存 key 里不放模型名。
    payload_lines: list[str] = []
    for turn_slice in removed_turns:
        for message in turn_slice:
            role = str(message.get("role") or "")
            payload_lines.append(f"### {role}")
            payload_lines.append(str(message.get("content") or "")[:2000])
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    call_name = str((call or {}).get("function", {}).get("name") or "")
                    if call_name:
                        payload_lines.append(f"[调用工具 {call_name}]")
    raw = "\n".join(payload_lines)
    if len(raw) > COMPRESS_INPUT_MAX_CHARS:
        raw = raw[:COMPRESS_INPUT_MAX_CHARS] + "……[更早内容已截断]"

    cache_key = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    cached = _COMPRESSION_CACHE.get(cache_key)
    if cached:
        return cached

    summary = await _run_compression_call(raw, compress_llm)
    if summary:
        _COMPRESSION_CACHE[cache_key] = summary
    return summary


async def _run_compression_call(raw_text: str, compress_llm: Any) -> str:
    """执行一次真正的 LLM 压缩调用。

    中文注释：compress_llm 是调用方传进来的模型快照（ProviderSnapshot），
    复用主 Agent 的模型档位，走 provider 的非流式 chat 接口，温度 0 追求
    稳定的要点压缩。任何失败直接返回空字符串，由调用方兜底——这一步不加重试。
    """

    prompt = (
        "请把下面这段更早的对话轮次压缩成不超过 400 字的要点摘要，"
        "必须保留：用户当时提出什么诉求、得到了什么结论、动过哪些论文（编号）、"
        "尚未完成的事项。用中文小短句分条写，不要展开原文。\n\n---\n\n" + raw_text
    )
    try:
        provider = getattr(compress_llm, "provider", None)
        if provider is None:
            return ""
        response_model = await provider.chat(
            [
                {"role": "system", "content": "你是对话历史压缩器，只输出摘要正文，不要解释。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=800,
        )
    except Exception as exc:  # noqa: BLE001 - 压缩是锦上添花，任何失败都走兜底
        logger.warning("历史压缩模型调用失败，走确定性兜底", extra={"error": str(exc)[:200]})
        return ""
    if not response_model.ok:
        logger.warning(
            "历史压缩模型调用失败，走确定性兜底",
            extra={"error_kind": response_model.error_kind or "未知错误"},
        )
        return ""
    summary = (response_model.content or "").strip()
    if len(summary) > COMPRESSED_SUMMARY_MAX_CHARS:
        summary = summary[:COMPRESSED_SUMMARY_MAX_CHARS] + "……"
    return summary


def _workspace_state_section(workspace: SessionWorkspace) -> str:
    """生成系统提示词里的"当前工作区状态"小节（消息组装的动态部分）。"""

    total = len(workspace.papers)
    status_counts = {PAPER_STATUS_NEW: 0, PAPER_STATUS_EVALUATED: 0, PAPER_STATUS_DEEP_READ: 0}
    entries = sorted(workspace.papers.items(), key=lambda pair: pair[1].added_at)
    for _, entry in entries:
        status_counts[entry.status()] = status_counts.get(entry.status(), 0) + 1
    lines = [
        "## 当前工作区状态",
        f"研究主题：{workspace.research_topic or '（尚未设定，请结合对话确认）'}",
        (
            f"已收录论文：{total} 篇"
            f"（未评价 {status_counts[PAPER_STATUS_NEW]} / "
            f"已评价 {status_counts[PAPER_STATUS_EVALUATED]} / "
            f"已精读 {status_counts[PAPER_STATUS_DEEP_READ]}）"
        ),
    ]
    if entries:
        lines.append(f"论文预览（按收录顺序，最多 {WORKSPACE_PREVIEW_MAX_PAPERS} 条）：")
        for paper_id, entry in entries[:WORKSPACE_PREVIEW_MAX_PAPERS]:
            title = str(entry.paper.get("title") or "")[:WORKSPACE_PREVIEW_TITLE_CHARS]
            year = entry.paper.get("year") or ""
            status_label = _STATUS_LABELS.get(entry.status(), entry.status())
            lines.append(f"- [{paper_id}] {title}（{year}）{status_label}")
        if total > WORKSPACE_PREVIEW_MAX_PAPERS:
            lines.append(f"（其余 {total - WORKSPACE_PREVIEW_MAX_PAPERS} 篇未列出，可用 list_papers 查询）")
    # 中文说明：检索历史（D5 引入）。最近 5 条，避免主 Agent 跨轮次重复检索白烧轮次。
    if workspace.search_history:
        lines.append("")
        lines.append("## 已尝试过的检索（避免重复）")
        for entry in workspace.search_history[-5:]:
            relaxed_mark = "（已放宽）" if entry.relaxed else ""
            timestamp_short = entry.timestamp[11:16] if len(entry.timestamp) >= 16 else entry.timestamp
            lines.append(
                f"- 「{entry.concept_groups_summary or entry.topic}」"
                f"（{timestamp_short}）→ 命中 {entry.hits} / 新增 {entry.added}{relaxed_mark}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 工具调用相关的内部辅助函数
# ---------------------------------------------------------------------------


def _unknown_paper_ids(content: str, workspace: SessionWorkspace) -> list[str]:
    """从终答文本里挑出工作区里不存在的 paper_id。

    中文注释：
    主 Agent 有时候会在回复里引用一个不存在的 paper_id（可能是模型幻觉，
    也可能是它记错了编号）。这个函数把方括号里的候选编号挑出来，和工作区里
    的真实 paper_id 集合做差集，返回不存在的编号。

    误报的代价很高（白白触发一次完整的模型纠偏调用，还可能把正确的回答改坏），
    所以候选编号要过四道过滤，只留下"真的像论文编号"的内容：
    1. Markdown 行内链接不算：[链接文字](网址) 里右括号后面紧跟左括号的，
       方括号部分是链接的显示文字，不是引用编号（正则规则：右括号后面紧跟左括号的不算）；
    2. 整体必须符合编号长相：只由字母、数字、下划线、连字符、点、斜杠、冒号
       组成（比如 p0001、arXiv:2301.00001），含中文、空格、加号、尖角号的
       （比如 [a+b]、[^1]、[论文一]）一律不算；
    3. 常见占位词不算：[TODO]、[NOTE]、[FIXME] 这类是写作提醒（不区分大小写）；
    4. 至少 2 个字符且不是纯数字：[1]、[12] 这种是普通的数字角标引用。
    """

    if not content:
        return []
    # 挑出所有方括号引用，跳过 Markdown 行内链接（右括号后紧跟左括号的）
    bracket_matches = _BRACKET_CITATION_PATTERN.findall(content)
    candidate_ids: list[str] = []
    for match in bracket_matches:
        candidate = match.strip()
        # 过滤 4：太短或纯数字的（比如 [1]）是普通数字角标，不是论文编号
        if len(candidate) < 2 or candidate.isdigit():
            continue
        # 过滤 2：整体不像编号长相的（含中文、空格、+、^ 等）不算引用
        if not _PAPER_ID_SHAPE_PATTERN.match(candidate):
            continue
        # 过滤 3：[TODO] 这类占位词是写作提醒，不是论文编号
        if candidate.lower() in _CITATION_PLACEHOLDER_WORDS:
            continue
        candidate_ids.append(candidate)
    # 和工作区里的真实 paper_id 集合做差集
    known_ids = set(workspace.papers.keys())
    unknown = [pid for pid in candidate_ids if pid not in known_ids]
    # 去重，保持出现顺序
    seen: set[str] = set()
    result: list[str] = []
    for pid in unknown:
        if pid not in seen:
            seen.add(pid)
            result.append(pid)
    return result


def note_streaming_tool_call(
    preview_keys: dict[int, str],
    item: dict[str, Any],
    round_no: int,
) -> tuple[int, str, str] | None:
    """流式工具名首次出现时登记卡片键；参数碎片不重复发卡。"""
    try:
        index = int(item.get("index", -1))
    except (TypeError, ValueError):
        return None
    if index < 0:
        return None
    name = str(item.get("name") or "").strip()
    if not name:
        function = item.get("function")
        if isinstance(function, dict):
            name = str(function.get("name") or "").strip()
    if not name or index in preview_keys:
        return None
    event_key = f"{name}_r{round_no}_{index}"
    preview_keys[index] = event_key
    return index, name, event_key


def _normalize_tool_calls(raw_calls: list[Any], round_no: int) -> list[JsonObject]:
    """把模型返回的工具调用整理成统一结构（工程规范 8.3）。

    处理两类脏数据：
    1. id 缺失：部分供应商不返回调用 id，这里按轮次和序号补一个稳定 id，
       保证 assistant(tool_calls) 和 tool 结果消息能正确配对；
    2. arguments 可能是 JSON 字符串也可能是字典：统一转成字典再交给 handler，
       字符串不是合法 JSON 时按空参数处理并留日志。
    """

    calls: list[JsonObject] = []
    for index, raw in enumerate(raw_calls, start=1):
        call_id = str(getattr(raw, "id", "") or "").strip() or f"call_r{round_no}_{index}"
        name = str(getattr(raw, "name", "") or "").strip()
        arguments = getattr(raw, "arguments", None)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                logger.warning(
                    "工具调用参数不是合法 JSON，按空参数处理",
                    extra={"tool_name": name, "round": round_no, "arguments_chars": len(arguments)},
                )
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append({"id": call_id, "name": name, "arguments": arguments})
    return calls


def _assistant_tool_call_message(content: str, calls: list[JsonObject], thinking_blocks: list[JsonObject] | None = None) -> JsonObject:
    """构造回填给模型的 assistant(tool_calls) 消息（OpenAI 协议格式）。

    arguments 必须序列化回 JSON 字符串：openai 兼容协议原样透传，
    anthropic 适配器会自行解析字符串，两种后端都兼容。

    thinking_blocks 是这一轮模型给出的思考内容块。推理模型要求下一轮请求把上一轮的
    思考一起传回去，所以这里必须原样挂在消息上，丢了这次请求就会被上游拒绝。
    """

    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                },
            }
            for call in calls
        ],
        "thinking_blocks": list(thinking_blocks or []),
    }


async def _execute_tool(registry: ToolRegistry, call: JsonObject) -> Any:
    """执行单个工具调用，任何异常都折叠成结构化错误回填给模型。

    工程规范 8.3：工具失败不中断整个 run，把 {"error": ...} 交回主 Agent，
    让它自行决定重试、换路还是告诉用户。
    唯一例外是用户取消（CancelledError）——必须原样上抛让 run 尽快退出。
    """

    try:
        tool = registry.require(call["name"])
    except ValueError:
        # 模型幻觉出一个没注册的工具名：回填错误让它改用真实存在的工具。
        return {"error": f"未知工具：{call['name']}，请从可用工具列表中选择"}
    try:
        return await tool.acall(**call["arguments"])
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("工具执行失败", extra={"tool_name": call["name"]})
        return {"error": f"工具执行失败：{exc}"}


def _report_round_usage(
    chat_reporter: "WorkflowNodeReporter",
    round_no: int,
    response: Any,
    messages: list[JsonObject] | None = None,
) -> None:
    """把一轮模型调用的 token 用量上报到"对话调研"主卡片（工程规范 8.3/8.7）。

    中文注释：顺带用供应商返回的真实 prompt_tokens 反推"本轮多少字符 ≈ 1 token"，
    回填到模块级的实测字符率里。下一次组装历史时用这个更准的比例估算占用，
    混合了中文的对话会自动趋向中文的字符率，英文对话趋向英文的，比任何
    静态系数都可靠。
    """

    usage = normalize_token_usage(getattr(response, "usage", None))
    chat_reporter.progress(
        f"第 {round_no} 轮模型调用完成",
        stage="llm_round",
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
    )
    # 中文注释：回填实测字符率。prompt_tokens 是供应商数出来的真实值，除以
    # 本轮发送的所有消息总字符数就是最实在的比例；极端异常的测量值不采纳。
    prompt_tokens = usage.get("input_tokens") or 0
    if prompt_tokens > 0 and messages:
        total_chars = sum(
            len(str(m.get("content") or "")) + len(str(m.get("role") or "")) for m in messages
        )
        if total_chars > 0:
            global _RECENT_CHARS_PER_TOKEN, _LAST_OBSERVED_PROMPT_TOKENS
            measured = total_chars / prompt_tokens
            if 1.0 <= measured <= 100.0:
                _RECENT_CHARS_PER_TOKEN = round(measured, 3)
            # 中文注释：实测 prompt_tokens 一并留档——下一轮 run 开始时用来做
            # 自适应预算校准（见 run_conversation_agent 第一步）。
            _LAST_OBSERVED_PROMPT_TOKENS = int(prompt_tokens)

def _arguments_summary(arguments: JsonObject) -> str:
    """把工具参数压成日志和评测用的短 JSON 字符串（不打印完整大对象）。"""

    try:
        text = json.dumps(arguments, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(arguments)
    return text if len(text) <= 200 else text[:200] + "…"
