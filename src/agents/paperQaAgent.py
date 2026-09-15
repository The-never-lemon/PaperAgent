"""论文问答子 Agent（agent-as-tool 模式，按需取原文）。

这个模块是对话式调研"追问已精读论文"链路的执行核心。主 Agent 通过
ask_paper 工具把用户关于某篇论文的细节问题委派到这里。本模块负责：
从工作区加载精读报告 → 定位论文全文缓存（chunk.json 切片） → 组装一个
"只放报告 + 全文目录 + 问题"的轻上下文 → 小工具循环（模型通过
read_sections 工具按需拉取相关原文片段）→ 解析最终回答
返回 {answer, source_sections}。

按需加载的设计动机（方案"上下文分配与自动压缩"）：长论文全文动辄几万
字符，一次塞进提示词既浪费预算，超过上限还要硬截断、把中间内容弄丢。
现在提示词里只放全文的"目录"（每个片段的编号 + 起始文字预览），模型按
问题自己取回相关片段，长论文中间内容不再有截断盲区。

整个流程自包含，不依赖 research_tools / researchAgent / deepReadAgent，所有
运行时依赖通过 PaperQaDeps 显式传入，避免 Agent 间横向依赖和循环导入。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.llm.base import attach_reasoning, normalize_token_usage
from src.llm.config import SystemConfig
from src.utils import get_logger
from src.utils.llm_json import parse_llm_json
from src.utils.read_utils.chunkers import load_chunks_file

from .contracts import JsonObject
from .Prompts import PAPER_QA_SYSTEM_PROMPT


if TYPE_CHECKING:
    from src.graph.runtime import WorkflowCancellation, WorkflowNodeReporter
    from src.llm import ProviderSnapshot
    from src.llm.base import LLMResponse
    from src.models.workspace import SessionWorkspace, WorkspacePaperEntry
    from src.repositories.sessions.base import SessionRepository
    from src.utils.read_utils.chunkers import TextChunk


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 模块常量（全部集中在顶部，禁止魔法数字散落函数体）
# ---------------------------------------------------------------------------

# 问答进度事件的 stage 名，对应 runtime.py 里 ("tool","ask_paper") 映射。
QA_STAGE = "ask_paper"

# 一次 read_sections 最多取回的片段数。
# 中文注释：片段上限 ≈ 精读切片大小（1200 字），一次最多 6 片，既让模型
# 拿到足够上下文，又不会一小轮调用把问答上下文撑爆。
QA_READ_MAX_CHUNKS = 6
QA_READ_CHUNK_MAX_CHARS = 1200

# 问答上下文里全文目录的最多条目数（切片更多时只展示前 N 条 + 一句"共 N 段"）。
QA_TOC_MAX_ENTRIES = 60

# 目录条目里每个片段预览文本的截断长度。
QA_TOC_PREVIEW_CHARS = 60

# 模型调用失败时，写进失败原因的错误正文截断长度。
# 中文注释：上游 400 的真正原因往往写在很长一段 JSON 的 message 字段末尾，
# 截太短时界面上只能看到 "invalid_request_error"，排查不到具体是哪条协议被拒。
QA_ERROR_DETAIL_CHARS = 500

# 问答小工具循环的最大轮数（每轮模型可调 read_sections 拉原文片段）。
QA_MAX_TOOL_ROUNDS = 4

# 未找到相关片段时 read_sections 返回的提示语。
QA_READ_MISS_NOTE = "[未找到匹配的原文片段，可换关键词重试，或直接基于精读报告回答]"


# ---------------------------------------------------------------------------
# 运行时依赖
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PaperQaDeps:
    """问答子 Agent 的运行时依赖（由工具 handler 组装传入）。

    本模块禁止 import research_tools / researchAgent / deepReadAgent（避免
    Agent 间横向依赖与循环导入），需要的一切通过 Deps 显式传入。

    Attributes:
        session_key: 会话编号。
        workspace: 会话工作区（论文状态都在这里）。
        repo: 会话仓储，读全文产物统一走它的 read_artifact_path。
        reporter: 绑定到"工具执行"节点的事件上报器，进度事件从这里发。
        event_key: 本次工具调用的事件键（进度/用量聚合到这张卡片）。
        cancellation: 用户停止请求的控制对象，流程中要定期检查。
        llm: 问答使用的模型快照，单次调用走它的 provider.chat。
    """

    session_key: str
    workspace: "SessionWorkspace"
    repo: "SessionRepository"
    reporter: "WorkflowNodeReporter"
    event_key: str
    cancellation: "WorkflowCancellation | None" = None
    llm: "ProviderSnapshot | None" = None


# ---------------------------------------------------------------------------
# 问答主入口
# ---------------------------------------------------------------------------


async def run_paper_qa(*, paper_id: str, question: str, deps: PaperQaDeps) -> JsonObject:
    """问答主入口。

    成功时返回：
        {"answer": str, "source_sections": [str]}
    失败时返回：
        {"status": "failed", "reason": str}
        —— 内部任何业务异常都折叠成这个结构，绝不上抛；
          唯一例外 asyncio.CancelledError 原样上抛。
    """

    # 用一层 try/except 把所有业务异常都折成结构化返回。
    # asyncio.CancelledError 继承自 BaseException，不会被这里捕获，会原样上抛。
    try:
        return await _run_paper_qa_impl(paper_id=paper_id, question=question, deps=deps)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "论文问答过程出现意外错误",
            extra={"paper_id": paper_id, "error": str(exc)[:200]},
        )
        return _fail(deps, f"论文问答过程出现意外错误：{exc}")


async def _run_paper_qa_impl(*, paper_id: str, question: str, deps: PaperQaDeps) -> JsonObject:
    """问答主流程实现（由 run_paper_qa 包裹异常折叠）。"""

    # 第一步：按编号取出论文，工作区里没有就直接报失败。
    _check_cancellation(deps)
    entry = deps.workspace.get_paper(paper_id)
    if entry is None:
        return _fail(deps, f"工作区里没有这篇论文：{paper_id}")

    # 第二步：还没有精读报告就没法基于全文回答（防御层，handler 还会先拦一次）。
    if entry.deep_read is None:
        return _fail(deps, f"这篇论文还没有精读：{paper_id}")

    # 第三步：模型没装配就没法回答。
    if deps.llm is None:
        return _fail(deps, "模型未装配，无法回答")

    # 第四步：定位材料。报告直接从工作区取；全文优先从会话产物读，
    # 读不到时（摘要降级精读的论文本来就没有全文）用摘要代替。
    _check_cancellation(deps)
    deps.reporter.progress("正在准备问答材料", stage=QA_STAGE, event_key=deps.event_key)
    report = entry.deep_read.to_dict()

    chunks: "list[TextChunk]" | None = None
    abstract = ""
    fallback_fulltext = ""
    if report.get("fulltext_artifact_id"):
        # 有全文产物：读全文文本，并尝试加载它的切片目录。
        fulltext = await _load_fulltext(deps, str(report["fulltext_artifact_id"]))
        if fulltext:
            chunks = await _load_chunks_from_cache(deps, paper_id, entry.paper)
            if chunks is None:
                # 中文注释：全文在但切片缓存没了（paper_cache 被清理、换机器等）。
                # 不学旧版把全文白白丢掉——把全文截断后直接放进提示词，
                # 比"基于摘要回答"保留多得多的论文信息。
                fallback_fulltext = fulltext
                deps.reporter.progress(
                    "切片缓存不可用，将截断后的全文直接放进提示词",
                    stage=QA_STAGE,
                    event_key=deps.event_key,
                )
    if chunks is None and not fallback_fulltext:
        # 既没有全文也没有可用切片：退回"报告 + 摘要"语义，和摘要降级精读一致。
        abstract = str(entry.paper.get("abstract") or "")
        deps.reporter.progress(
            "未找到全文，使用报告与摘要回答",
            stage=QA_STAGE,
            event_key=deps.event_key,
        )

    # 第五步：组装轻上下文（报告 + 全文目录或截断全文（可选） + 问题 + 工具说明）。
    _check_cancellation(deps)
    user_content = json.dumps(
        {
            "精读报告": report,
            "论文全文目录": _build_toc(chunks) if chunks else None,
            # 中文注释：切片不可用但有全文时，退回旧版语义——截断后的全文直接
            # 放进提示词（保留前 2/3 + 后 1/3），保证回答仍基于正文。
            "论文全文": _truncate_fulltext(fallback_fulltext) if fallback_fulltext else None,
            "论文摘要": abstract or None,
            "论文标题": str(entry.paper.get("title") or ""),
            "用户问题": question,
        },
        ensure_ascii=False,
    )

    # 第六步：小工具循环。模型可调 read_sections 拉取相关原文片段，
    # 最多 QA_MAX_TOOL_ROUNDS 轮；直到模型不再调工具，最后一轮输出就是回答。
    _check_cancellation(deps)
    deps.reporter.progress("正在生成回答", stage=QA_STAGE, event_key=deps.event_key)
    system_prompt = PAPER_QA_SYSTEM_PROMPT + _build_tool_instructions(chunks is not None)

    messages: list[JsonObject] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    qa_tools = _build_qa_tools(chunks) if chunks else []

    # token 用量累加器：主调用 + 取文轮次 + 可能的 repair 重试都累加到这里。
    total_input = 0
    total_output = 0

    # 未压缩的原始对话（含 assistant(tool_calls) 与 tool 结果），repair 时复用。
    conversation: list[JsonObject] = list(messages)
    final_text = ""
    # 中文注释：记下产出最终回答的那一次模型响应，补救重试时要把思考原文一起带回。
    final_response: "LLMResponse | None" = None

    for qa_round in range(1, QA_MAX_TOOL_ROUNDS + 1):
        _check_cancellation(deps)
        # 最后一轮不带工具收敛（和主 Agent 的强制收敛同一思路）：
        # 让模型基于已取回的片段直接给出（最后一次）最终回答，
        # 避免把"正在调用工具的过场白"当成最终回答。
        tools_for_this_round = qa_tools if qa_round < QA_MAX_TOOL_ROUNDS else []
        response = await deps.llm.provider.chat(conversation, temperature=0, tools=tools_for_this_round)
        usage = normalize_token_usage(response.usage)
        total_input += usage["input_tokens"]
        total_output += usage["output_tokens"]

        if not response.ok:
            return _fail(deps, _model_error_summary(response))

        # 模型没调工具（或者已经到了最后一轮）：这份输出就是最终回答。
        if not response.tool_calls or qa_round == QA_MAX_TOOL_ROUNDS:
            final_text = response.content or ""
            final_response = response
            break

        # 有工具调用：执行 read_sections，把结果回填进对话，进入下一轮。
        # 中文注释：模型一次回复里可能同时点名取好几段原文。这些调用必须
        # 写在同一条 assistant 消息里，后面再按顺序跟对应的 tool 结果。
        # 如果拆成「一条 assistant + 一条 tool」反复拼接，上游会当成非法请求
        # 直接返回 400（实测 deepseek-flash：一次发两条 read_sections 就会失败）。
        tool_calls = _normalize_tool_calls(response.tool_calls)
        assistant_message: JsonObject = {
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                    },
                }
                for call in tool_calls
            ],
        }
        # 中文注释：开了思考档位的模型，上一轮的思考原文必须原样带回下一轮。
        attach_reasoning(assistant_message, response)
        conversation.append(assistant_message)
        for call in tool_calls:
            tool_result = _handle_read_sections(call["arguments"], chunks)
            conversation.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": tool_result,
                }
            )

    # final_text 一直为空说明循环跑满了还在调工具（防御）。此时用最后一段
    # 正文兜底；连正文都没有就报失败。
    if not final_text:
        for message in reversed(conversation):
            if str(message.get("role") or "") == "assistant":
                text = str(message.get("content") or "").strip()
                if text:
                    final_text = text
                    break
    if not final_text:
        return _fail(deps, "模型没有给出回答内容")

    # 第七步：解析模型输出。解析失败会带错误信息重试一次（在 _repair 闭包里发生）。
    # 这就是"失败重试1次"，业务层不再自己套重试循环。
    async def _repair(error: str) -> str:
        """带着原始材料、上次的坏输出和解析错误信息让模型重新输出一次。

        中文注释：重试时把完整对话（含取回的原文片段）一起还给模型——
        只发一句错误提示的话，模型看不到材料，只能编一个空洞回答。
        """

        nonlocal total_input, total_output
        repair_assistant: JsonObject = {"role": "assistant", "content": final_text}
        if final_response is not None:
            attach_reasoning(repair_assistant, final_response)
        repair_messages = [
            *conversation,
            repair_assistant,
            {
                "role": "user",
                "content": (
                    f"你上次的输出无法解析成要求的 JSON：{error}。"
                    "请重新只输出一个符合要求的 JSON 对象（answer + source_sections）。"
                ),
            },
        ]
        repair_response = await deps.llm.provider.chat(repair_messages, temperature=0)
        repair_usage = normalize_token_usage(repair_response.usage)
        total_input += repair_usage["input_tokens"]
        total_output += repair_usage["output_tokens"]
        return repair_response.content or ""

    payload = await parse_llm_json(final_text, fallback={}, repair=_repair)
    # payload 含 parse_error 说明解析彻底失败（连重试也没救回来）。
    if "parse_error" in payload:
        logger.warning(
            "论文回答解析失败",
            extra={"paper_id": paper_id, "error": str(payload["parse_error"])[:200]},
        )
        return _fail(deps, f"回答解析失败：{payload['parse_error']}")

    # 第八步：取出回答正文，为空就报失败。
    answer = str(payload.get("answer") or "").strip()
    if not answer:
        return _fail(deps, "模型没有给出回答内容")

    # 第九步：整理出处列表（宽容解析，逐项转字符串，去掉空白项）。
    source_sections = _safe_string_list(payload.get("source_sections"))

    # 第十步：用量聚合，推一条带用量的完成进度事件（回答不推卡片，由主 Agent 转述）。
    deps.reporter.progress(
        "追问完成",
        stage=QA_STAGE,
        event_key=deps.event_key,
        input_tokens=total_input,
        output_tokens=total_output,
    )

    logger.info(
        "论文问答完成",
        extra={
            "session_key": deps.session_key,
            "paper_id": paper_id,
            "chunked": chunks is not None,
            "answer_chars": len(answer),
            "input_tokens": total_input,
            "output_tokens": total_output,
        },
    )

    return {"answer": answer, "source_sections": source_sections}


# ---------------------------------------------------------------------------
# 材料加载
# ---------------------------------------------------------------------------


async def _load_fulltext(deps: PaperQaDeps, artifact_id: str) -> str:
    """从会话产物加载论文全文文本，读不到返回空字符串（不报错）。

    中文注释：全文正文本身这里只用来测"有没有全文"和触发切片定位，
    不再整体塞进提示词——它是模板 read_sections 工具背后的数据源。
    """

    try:
        # 走仓储的 read_artifact_path 拿路径（自带路径安全校验），放在线程里避免阻塞。
        path = await asyncio.to_thread(deps.repo.read_artifact_path, deps.session_key, artifact_id)
        if path is not None:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            return text or ""
    except Exception as exc:
        # 读不到全文不报错，降级用摘要回答（不捕获 CancelledError，它属于 BaseException）。
        logger.warning(
            "读取论文全文产物失败，改用摘要",
            extra={"session_key": deps.session_key, "artifact_id": artifact_id, "error": str(exc)[:200]},
        )
    return ""


async def _load_chunks_from_cache(
    deps: PaperQaDeps, paper_id: str, paper: JsonObject | None = None
) -> "list[TextChunk] | None":
    """从论文缓存目录加载 chunk.json 切片，成功返回 TextChunk 列表。

    中文注释：精读时切好的切片躺在 data/paper_cache 里。问答直接复用，
    不需要重新切分。目录名可能和当前工作区编号不一样（换过数据源），
    所以先按长期记忆里记下的目录找，找不到再按当前编号起名。
    """

    try:
        cache_dir = SystemConfig.load().read.paper_cache_dir
        from pathlib import Path

        from src.services.paper_memory import resolve_paper_cache_dir

        root = Path(cache_dir)
        if not root.exists():
            return None
        directory = resolve_paper_cache_dir(root, paper or paper_id)
        chunks_path = directory / "chunk.json"
        if chunks_path.exists():
            chunks = await asyncio.to_thread(load_chunks_file, chunks_path)
            if chunks:
                logger.info(
                    "问答加载到全文切片",
                    extra={"session_key": deps.session_key, "paper_id": paper_id, "chunks": len(chunks)},
                )
                return chunks
    except Exception as exc:
        logger.warning(
            "定位问答切片缓存失败，回退摘要模式",
            extra={"session_key": deps.session_key, "paper_id": paper_id, "error": str(exc)[:200]},
        )
    return None


# ---------------------------------------------------------------------------
# 轻上下文组装
# ---------------------------------------------------------------------------


def _build_toc(chunks: "list[TextChunk] | None") -> JsonObject | None:
    """把切片清单压成一份"目录"（编号 + 章节 + 起始文字预览）。

    中文注释：目录远小于全文，几万字符的论文在提示词里只占几百到一两千
    字符。模型看到"哪一段大概在讲什么"，需要细节时再用 read_sections 按
    编号精确取原文。
    """

    if not chunks:
        return None
    entries = []
    for chunk in chunks[:QA_TOC_MAX_ENTRIES]:
        preview = " ".join(chunk.content.split())[:QA_TOC_PREVIEW_CHARS]
        entries.append(
            {
                "chunkId": chunk.chunk_id,
                "章节": chunk.section or "（未标注章节）",
                "起始文字": preview,
            }
        )
    toc: JsonObject = {"总段数": len(chunks), "目录": entries}
    if len(chunks) > QA_TOC_MAX_ENTRIES:
        toc["提醒"] = f"只展示前 {QA_TOC_MAX_ENTRIES} 段，更多段可用 read_sections 按关键词查找"
    return toc


def _build_tool_instructions(chunks: "list[TextChunk] | None") -> str:
    """给系统提示词追加 read_sections 的使用说明（有切片时才追加）。"""

    if not chunks:
        return ""
    return (
        "\n\n## 按需取原文\n"
        "本次会话你有一个工具可以调用：read_sections({\"keywords\": [\"关键词1\"], "
        "\"chunk_ids\": [\"chunk-x\"]})。当精读报告或目录不足以回答问题时，"
        "先用关键词或目录里的编号调用它取回相关原文片段，再基于片段内容回答；"
        "最多可以调用几轮。出处标注规则不变：来自取回片段的结论标"
        " [全文:章节/段落标识]（片段头部会写明所属章节）。"
    )


# ---------------------------------------------------------------------------
# read_sections 工具（只读，从切片缓存拉取原文）
# ---------------------------------------------------------------------------

# read_sections 工具的 LLM function schema（传给 provider.chat 的 tools 参数）。
_QA_READ_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_sections",
        "description": (
            "按关键词或片段编号从论文全文里取回相关片段（最多 6 段，每段最多 1200 字）。"
            "回答问题前，如果精读报告和全文目录不够用，先用它取回原文再回答。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要找的内容关键词（中英文均可），命中越多段的排序越靠前",
                },
                "chunk_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "全文目录里看到的片段编号，点名要某一段时使用",
                },
            },
        },
    },
}


def _handle_read_sections(arguments: JsonObject, chunks: "list[TextChunk]") -> str:
    """执行 read_sections：按关键词/编号从切片列表里挑片段，返回拼接文本。

   挑法（中文注释）：先按关键词计分（大小写不敏感、命中关键词数多者优先），
    没给关键词时按给出的 chunk_ids 直接取；两种都没给就返回未命中提示。
    取回的每段头部会写明编号和章节，方便模型标注出处。
    """

    keywords = [str(k or "").strip().lower() for k in (arguments.get("keywords") or []) if str(k or "").strip()]
    wanted_ids = {str(k or "").strip() for k in (arguments.get("chunk_ids") or []) if str(k or "").strip()}

    scored: "list[tuple[int, int, TextChunk]]" = []
    for index, chunk in enumerate(chunks):
        content = chunk.content or ""
        lowered = content.lower()
        score = sum(1 for keyword in keywords if keyword and keyword in lowered)
        if wanted_ids and chunk.chunk_id in wanted_ids:
            score += len(keywords) + 1  # 点名要的片段直接排最前
        if score > 0 or (not keywords and not wanted_ids):
            scored.append((score, -index, chunk))

    if not scored:
        return QA_READ_MISS_NOTE

    # 计分高在前，同分的按位置（老段优先）。
    scored.sort(key=lambda pair: (pair[0], pair[1]), reverse=True)
    picked = scored[:QA_READ_MAX_CHUNKS]

    lines: "list[str]" = []
    for score, _, chunk in picked:
        content = (chunk.content or "")[:QA_READ_CHUNK_MAX_CHARS]
        section_label = chunk.section or "（未标注章节）"
        lines.append(f"### 片段 {chunk.chunk_id}（章节：{section_label}）\n{content}")

    # 中文注释：按模型给的 chunk_ids 点名取却没找到的片段，如实提示。
    picked_ids = {chunk.chunk_id for _, _, chunk in picked}
    if wanted_ids - picked_ids:
        lines.append(f"[以下编号没有对应片段或没有命中内容：{', '.join(sorted(wanted_ids - picked_ids))}]")

    text = "\n\n".join(lines)
    logger.info(
        "问答 read_sections 取回片段",
        extra={"chunks": len(picked), "chars": len(text)},
    )
    return text


def _build_qa_tools(chunks: "list[TextChunk]") -> list[JsonObject]:
    """构建传给 provider.chat 的工具 schema 列表（有切片时只有一个 read_sections）。"""

    return [_QA_READ_TOOL_SCHEMA]


def _normalize_tool_calls(tool_calls: Any) -> list[JsonObject]:
    """把 provider 返回的 tool_calls 整理成 [{"id", "name", "arguments"(dict)}]。

    中文注释：arguments 上游可能返回原始 JSON 字符串，这里统一解析成字典，
    解析失败按空参数处理（read_sections 两个参数都是可选的）。
    """

    normalized: "list[JsonObject]" = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            # provider 的统一结构是 ToolCallRequest（dataclass），转字典处理。
            call = {
                "id": getattr(call, "id", None) or "",
                "name": getattr(call, "name", "") or "",
                "arguments": getattr(call, "arguments", None),
            }
        arguments = call.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        # 中文注释：有的供应商不返回 tool_call id，此时 assistant/tool 消息会
        # 拼出空 id 配对，部分上游会直接拒绝。没有 id 就按调用顺序补一个。
        call_id = str(call.get("id") or "").strip()
        if not call_id:
            call_id = f"qa_call_{len(normalized) + 1}"
        normalized.append(
            {
                "id": call_id,
                "name": str(call.get("name") or "read_sections"),
                "arguments": arguments,
            }
        )
    return normalized


# ---------------------------------------------------------------------------
# 上下文截断（全文直接放进提示词的降级路径用）
# ---------------------------------------------------------------------------

# 切片缓存不可用但有全文时，塞进提示词的全文上限。
QA_CONTEXT_MAX_CHARS = 30000

# 全文截断时夹在前后两段中间的省略提示语。
QA_TRUNCATION_MARKER = "[全文中间部分已省略]"


def _truncate_fulltext(text: str) -> str:
    """全文超长时保留前 2/3 和后 1/3，中间用一行省略提示标注。

    中文注释：这条路径只在"全文在但切片缓存丢了"时使用——正常路径下模型
    通过 read_sections 按需取原文，根本不需要全文进提示词。
    """

    if len(text) <= QA_CONTEXT_MAX_CHARS:
        return text
    # 前段预算取 2/3，后段取剩下的 1/3，加起来正好是上限字符数。
    front_size = QA_CONTEXT_MAX_CHARS * 2 // 3
    back_size = QA_CONTEXT_MAX_CHARS - front_size
    return (
        text[:front_size]
        + "\n"
        + QA_TRUNCATION_MARKER
        + "\n"
        + text[-back_size:]
    )


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------


def _check_cancellation(deps: PaperQaDeps) -> None:
    """检查用户是否点了停止，点了就抛出取消异常让当前流程尽快退出。"""

    if deps.cancellation is not None:
        deps.cancellation.raise_if_requested()


def _fail(deps: PaperQaDeps, reason: str) -> JsonObject:
    """统一处理问答失败：推 failed 事件并返回结构化失败结果。"""

    deps.reporter.failed(reason, stage=QA_STAGE, event_key=deps.event_key)
    logger.info("论文问答失败", extra={"reason": reason[:200]})
    return {"status": "failed", "reason": reason}


def _model_error_summary(response: "LLMResponse") -> str:
    """从模型失败响应里拼一句简短的错误摘要，写进失败原因里。"""

    # 优先用错误类别和状态码，都没有就退到"模型调用失败"。
    parts: list[str] = []
    if response.error_kind:
        parts.append(response.error_kind)
    if response.error_status_code is not None:
        parts.append(f"HTTP {response.error_status_code}")
    if not parts:
        parts.append("模型调用失败")
    summary = "，".join(parts)
    # 错误正文太长就截断，避免把整段错误体写进失败原因。
    detail = (response.content or "").strip()
    if detail:
        summary = f"{summary}：{detail[:QA_ERROR_DETAIL_CHARS]}"
    return summary


def _safe_string_list(value: Any) -> list[str]:
    """把任意输入整理成字符串列表（宽容解析）。

    单个字符串会被包装成只有一个元素的列表；列表里的非字符串元素会被转成字符串；
    空白元素直接丢弃。这样模型输出格式稍有偏差时数据也不会丢。
    """

    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result
