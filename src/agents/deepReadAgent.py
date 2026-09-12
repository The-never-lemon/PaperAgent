"""精读子 Agent（agent-as-tool 模式）。

这个模块是精读链路的执行核心。主 Agent 通过 deep_read_paper 工具把精读任务
委派到这里，本模块负责：下载全文（失败回退摘要）→ 转 Markdown → 分块 → map
逐块精读 → reduce 汇总生成 DeepReadReport → 报告与全文存 artifact → 写工作区
→ 推 deep_read_report 卡片 → 返回结构化摘要。

整个流程自包含，不依赖 research_tools / researchAgent，所有运行时依赖通过
DeepReadDeps 显式传入，避免 Agent 间横向依赖和循环导入。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any

from src.llm.base import normalize_token_usage
from src.llm.config import SystemConfig
from src.models.deep_read import (
    DEEP_READ_SOURCE_ABSTRACT,
    DEEP_READ_SOURCE_FULLTEXT,
    DeepReadReport,
    DimensionScore,
)
from src.models.sessions import utc_now
from src.models.workspace import sanitize_for_filename
from src.paper_retrieval.download import async_download_paper_fulltext
from src.paper_retrieval.models import PaperDocument
from src.utils import get_logger
from src.utils.llm_json import parse_llm_json
from src.utils.read_utils.chunkers import async_build_chunks_file
from src.utils.read_utils.read_fulltext import async_convert_fulltext_to_markdown

from .contracts import JsonObject
from .Prompts import (
    DEEP_READ_ABSTRACT_SYSTEM_PROMPT,
    DEEP_READ_MAP_SYSTEM_PROMPT,
    DEEP_READ_REDUCE_SYSTEM_PROMPT,
)


if TYPE_CHECKING:
    from src.graph.runtime import WorkflowCancellation, WorkflowNodeReporter
    from src.graph.runtime_resources import WorkflowRuntimeResources
    from src.llm import ProviderSnapshot
    from src.models.workspace import SessionWorkspace
    from src.repositories.sessions.base import SessionRepository
    from src.utils.read_utils.chunkers import TextChunk


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 模块常量（全部集中在顶部，禁止魔法数字散落函数体）
# ---------------------------------------------------------------------------

# map 阶段同时精读多少个正文片段（信号量并发上限）。
DEEP_READ_MAP_CONCURRENCY = 3

# 单块笔记最多保留多少字，超出截断。
MAP_NOTE_MAX_CHARS = 500

# reduce 输入（分段笔记拼成的总文本）最多保留多少字，超出按块序截断并标注省略。
REDUCE_INPUT_MAX_CHARS = 60000

# 返回给工具调用方的 report_summary 截断长度。
REPORT_SUMMARY_CHARS = 300

# 精读进度事件的 stage 名，对应 runtime.py 里 ("tool","deep_read_paper") 映射。
DEEP_READ_STAGE = "deep_read_paper"

# 产物类型常量，写 artifact 时使用。
ARTIFACT_TYPE_REPORT = "deep_read_report"
ARTIFACT_TYPE_FULLTEXT = "paper_fulltext"

# reduce 输入里论文摘要的截断长度。
REDUCE_ABSTRACT_CHARS = 400

# 推卡片时论文标题的截断长度。
CARD_TITLE_CHARS = 60


# ---------------------------------------------------------------------------
# 运行时依赖
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DeepReadDeps:
    """精读子 Agent 的运行时依赖（由工具 handler 组装传入）。

    本模块禁止 import research_tools / researchAgent（避免 Agent 间横向依赖与
    循环导入），需要的一切通过 Deps 显式传入。

    Attributes:
        session_key: 会话编号。
        workspace: 会话工作区（论文状态都在这里，改动会立即落盘）。
        repo: 会话仓储，写产物文件统一走它的 write_artifact。
        reporter: 绑定到"工具执行"节点的事件上报器，卡片消息和进度事件从这里发。
        event_key: 本次工具调用的事件键（进度/用量聚合到这张卡片）。
        cancellation: 用户停止请求的控制对象，长流程要定期检查。
        resources: 本次 run 共享的并发控制与 HTTP 客户端资源。
        llm: 精读使用的模型快照，map/reduce/降级都要调它的 provider.chat。
    """

    session_key: str
    workspace: "SessionWorkspace"
    repo: "SessionRepository"
    reporter: "WorkflowNodeReporter"
    event_key: str
    cancellation: "WorkflowCancellation | None" = None
    resources: "WorkflowRuntimeResources | None" = None
    llm: "ProviderSnapshot | None" = None


# ---------------------------------------------------------------------------
# 精读主入口
# ---------------------------------------------------------------------------


async def run_deep_read(*, paper_id: str, focus: str = "", deps: DeepReadDeps) -> JsonObject:
    """精读主入口。

    成功时返回：
        {"paper_id", "source": "fulltext"|"abstract_fallback",
         "report_summary"(short_summary 截 300 字), "artifact_id", "cached": bool}
    失败时返回：
        {"status": "failed", "reason": str}
        —— 内部任何业务异常都折叠成这个结构，绝不上抛；
          唯一例外 asyncio.CancelledError 原样上抛。
    """

    # 中文注释：用一层 try/except 把所有业务异常都折成结构化返回。
    # asyncio.CancelledError 继承自 BaseException，不会被这里捕获，会原样上抛。
    try:
        return await _run_deep_read_impl(paper_id=paper_id, focus=focus, deps=deps)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "精读过程出现意外错误",
            extra={"paper_id": paper_id, "error": str(exc)[:200]},
        )
        return _fail(deps, f"精读过程出现意外错误：{exc}")


async def _run_deep_read_impl(*, paper_id: str, focus: str, deps: DeepReadDeps) -> JsonObject:
    """精读主流程实现（由 run_deep_read 包裹异常折叠）。"""

    # 第一步：按编号取出论文，工作区里没有就直接报失败。
    _check_cancellation(deps)
    entry = deps.workspace.get_paper(paper_id)
    if entry is None:
        return _fail(deps, f"工作区里没有这篇论文：{paper_id}")

    # 第二步：已有精读报告就直接返回缓存，不调模型、不推卡片。
    if entry.deep_read is not None:
        deps.reporter.progress(
            "命中已有精读报告，直接返回",
            stage=DEEP_READ_STAGE,
            event_key=deps.event_key,
        )
        return {
            "paper_id": paper_id,
            "source": entry.deep_read.source,
            "report_summary": entry.deep_read.short_summary[:REPORT_SUMMARY_CHARS],
            "artifact_id": entry.deep_read.artifact_id,
            "cached": True,
        }

    # 第三步：模型没装配就没法精读。
    if deps.llm is None:
        return _fail(deps, "模型未装配，无法精读")

    # 第四步：把工作区里的论文元数据字典还原成 PaperDocument 对象。
    doc = _paper_document(entry.paper)

    # 第五步：下载全文。下载失败就记下原因，后面走摘要降级路径。
    _check_cancellation(deps)
    deps.reporter.progress("正在下载论文全文", stage=DEEP_READ_STAGE, event_key=deps.event_key)
    logger.info("精读开始下载全文", extra={"session_key": deps.session_key, "paper_id": paper_id})
    read_cfg = SystemConfig.load().read
    downloaded = await async_download_paper_fulltext(
        doc,
        cache_dir=read_cfg.paper_cache_dir,
        connect_timeout_seconds=read_cfg.connect_timeout_seconds,
        download_timeout_seconds=read_cfg.download_timeout_seconds,
        max_file_size_mb=read_cfg.max_file_size_mb,
        runtime_resources=deps.resources,
    )

    # token 用量累加器：map + reduce（或降级）所有响应的 usage 统一累加。
    total_input = 0
    total_output = 0

    # 尝试全文路径：下载成功 → 转换 → 分块 → map → reduce。
    # 任何一步失败就降级到摘要精读。map/reduce 失败属于硬失败，直接返回 failed。
    source = DEEP_READ_SOURCE_ABSTRACT
    markdown_text: str | None = None
    payload: JsonObject | None = None

    if downloaded.status == "downloaded":
        # 第六步：转 Markdown + 分块。
        _check_cancellation(deps)
        deps.reporter.progress("正在转换全文为 Markdown", stage=DEEP_READ_STAGE, event_key=deps.event_key)
        conversion = await async_convert_fulltext_to_markdown(
            doc,
            source_path=downloaded.file_path,
            source_url=downloaded.source_url,
        )
        if conversion.markdown_path is not None:
            _check_cancellation(deps)
            deps.reporter.progress("正在切分全文", stage=DEEP_READ_STAGE, event_key=deps.event_key)
            chunk_result = await async_build_chunks_file(doc, markdown_path=conversion.markdown_path)
            if chunk_result.chunks:
                # 第七步：map 逐块精读。
                _check_cancellation(deps)
                notes, map_input, map_output = await _map_chunks(
                    deps, doc, chunk_result.chunks, focus
                )
                total_input += map_input
                total_output += map_output
                # 全部片段都精读失败 → 硬失败，不降级。
                if not any(notes):
                    return _fail(deps, "全部正文片段精读失败，无法汇总报告")

                # 第八步：reduce 汇总。
                _check_cancellation(deps)
                deps.reporter.progress(
                    "正在汇总精读报告",
                    stage=DEEP_READ_STAGE,
                    event_key=deps.event_key,
                )
                reduce_content = _build_reduce_user_content(doc, notes, chunk_result.chunks)
                payload, reduce_input, reduce_output = await _run_reduce(deps, reduce_content)
                total_input += reduce_input
                total_output += reduce_output
                # reduce 解析失败 → 硬失败，不降级。
                if payload is None:
                    return _fail(deps, "精读报告解析失败，无法生成报告")

                source = DEEP_READ_SOURCE_FULLTEXT
                # 读取全文 Markdown 文本（用于写产物），放在线程里避免阻塞事件循环。
                markdown_text = await asyncio.to_thread(
                    conversion.markdown_path.read_text, encoding="utf-8"
                )
            else:
                # 分块为空，降级摘要。
                deps.reporter.progress(
                    "全文分块为空，改用摘要精读",
                    stage=DEEP_READ_STAGE,
                    event_key=deps.event_key,
                )
        else:
            # 转换失败，降级摘要。
            deps.reporter.progress(
                "全文转换失败，改用摘要精读",
                stage=DEEP_READ_STAGE,
                event_key=deps.event_key,
            )
    else:
        # 下载失败，降级摘要。
        deps.reporter.progress(
            f"全文获取失败（{downloaded.reason}），改用摘要精读",
            stage=DEEP_READ_STAGE,
            event_key=deps.event_key,
        )

    # 第十四步（摘要降级路径）：全文路径没走通时，仅凭标题+摘要生成报告。
    if payload is None:
        _check_cancellation(deps)
        deps.reporter.progress(
            "正在基于摘要生成精读报告",
            stage=DEEP_READ_STAGE,
            event_key=deps.event_key,
        )
        payload, abstract_input, abstract_output = await _run_abstract_fallback(deps, doc, focus)
        total_input += abstract_input
        total_output += abstract_output
        # 降级路径解析也失败 → 硬失败。
        if payload is None:
            return _fail(deps, "精读报告解析失败，无法生成报告")

    # 第九步：组装 DeepReadReport（各字段从 payload 宽容取出）。
    report = _assemble_report(paper_id, doc, source, payload)

    # 第十步：写产物（全文 Markdown + 报告 JSON），全部走 repo.write_artifact。
    _check_cancellation(deps)
    deps.reporter.progress("正在保存精读产物", stage=DEEP_READ_STAGE, event_key=deps.event_key)
    safe = sanitize_for_filename(paper_id)
    # 先写全文 Markdown（仅全文路径才有）。
    if markdown_text:
        fulltext_record = await asyncio.to_thread(
            deps.repo.write_artifact,
            deps.session_key,
            ARTIFACT_TYPE_FULLTEXT,
            f"paper_{safe}.md",
            markdown_text,
            relative_path=f"artifacts/paper_{safe}.md",
            metadata={"paper_id": paper_id},
        )
        report.fulltext_artifact_id = str(fulltext_record.get("id") or "")
    # 再写报告 JSON。
    report_record = await asyncio.to_thread(
        deps.repo.write_artifact,
        deps.session_key,
        ARTIFACT_TYPE_REPORT,
        f"deep_read_{safe}.json",
        json.dumps(report.to_dict(), ensure_ascii=False, indent=1),
        relative_path=f"artifacts/deep_read_{safe}.json",
        metadata={"paper_id": paper_id},
    )
    report.artifact_id = str(report_record.get("id") or "")

    # 第十一步：写工作区。
    await asyncio.to_thread(deps.workspace.set_deep_read, paper_id, report)
    if source == DEEP_READ_SOURCE_FULLTEXT:
        await asyncio.to_thread(deps.workspace.set_fulltext_cached, paper_id, True)

    # 第十二步：推 deep_read_report 卡片（role 用 system，不干扰助手消息缓冲区）。
    title_preview = (doc.title or "")[:CARD_TITLE_CHARS]
    deps.reporter.message(
        role="system",
        content=f"《{title_preview}》精读完成",
        metadata={
            "kind": "deep_read_report",
            "paper_id": paper_id,
            "source": source,
            "artifact_id": report.artifact_id,
            "report": report.to_dict(),
        },
    )

    # 第十三步：token 用量聚合，推一条带用量的完成进度事件。
    deps.reporter.progress(
        "精读完成",
        stage=DEEP_READ_STAGE,
        event_key=deps.event_key,
        input_tokens=total_input,
        output_tokens=total_output,
    )

    logger.info(
        "精读完成",
        extra={
            "session_key": deps.session_key,
            "paper_id": paper_id,
            "source": source,
            "input_tokens": total_input,
            "output_tokens": total_output,
        },
    )

    return {
        "paper_id": paper_id,
        "source": source,
        "report_summary": report.short_summary[:REPORT_SUMMARY_CHARS],
        "artifact_id": report.artifact_id,
        "cached": False,
    }


# ---------------------------------------------------------------------------
# map 阶段：并发逐块精读
# ---------------------------------------------------------------------------


async def _map_chunks(
    deps: DeepReadDeps,
    doc: PaperDocument,
    chunks: "list[TextChunk]",
    focus: str,
) -> tuple[list[str], int, int]:
    """并发精读每个正文片段，返回 (每块笔记列表, 输入token, 输出token)。

    用 asyncio.Semaphore 把并发压在 DEEP_READ_MAP_CONCURRENCY 内。
    单块失败（response.ok 为 False 或抛异常）→ 该块笔记记为空字符串，不中断整批。
    每完成一块更新进度"正在精读第 x/y 段"。
    """

    semaphore = asyncio.Semaphore(DEEP_READ_MAP_CONCURRENCY)
    total = len(chunks)
    completed = 0
    total_input = 0
    total_output = 0
    topic = deps.workspace.research_topic

    async def _map_one(chunk: "TextChunk") -> str:
        """精读单个正文片段，返回笔记文本（失败返回空字符串）。"""

        nonlocal completed, total_input, total_output
        async with semaphore:
            # 构造用户消息：研究主题 + 关注点 + 片段编号 + 正文片段。
            user_content = json.dumps(
                {
                    "研究主题": topic,
                    "用户关注点": focus,
                    "chunk_id": chunk.chunk_id,
                    "正文片段": chunk.content,
                },
                ensure_ascii=False,
            )
            messages = [
                {"role": "system", "content": DEEP_READ_MAP_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ]
            try:
                # 非流式调用模型，温度设 0 追求稳定输出。
                response = await deps.llm.provider.chat(messages, temperature=0)
            except Exception as exc:
                # 单块调用异常不中断整批，只记日志并把这块笔记留空。
                logger.warning(
                    "单块精读调用异常",
                    extra={"chunk_id": chunk.chunk_id, "error": str(exc)[:200]},
                )
                return ""
            # 累加这块的 token 用量。
            usage = normalize_token_usage(response.usage)
            total_input += usage["input_tokens"]
            total_output += usage["output_tokens"]
            # 每完成一块更新进度。
            completed += 1
            deps.reporter.progress(
                f"正在精读第 {completed}/{total} 段",
                stage=DEEP_READ_STAGE,
                event_key=deps.event_key,
            )
            # 模型返回不可用 → 这块笔记留空。
            if not response.ok:
                return ""
            # 取笔记文本，超长截断到 MAP_NOTE_MAX_CHARS。
            note = (response.content or "").strip()
            if len(note) > MAP_NOTE_MAX_CHARS:
                note = note[:MAP_NOTE_MAX_CHARS]
            return note

    # gather 保持顺序：返回的笔记列表和 chunks 一一对应。
    notes = await asyncio.gather(*(_map_one(chunk) for chunk in chunks))
    return list(notes), total_input, total_output


# ---------------------------------------------------------------------------
# reduce 阶段：汇总分段笔记生成报告
# ---------------------------------------------------------------------------


async def _run_reduce(
    deps: DeepReadDeps,
    reduce_user_content: str,
) -> tuple[JsonObject | None, int, int]:
    """调用模型汇总精读报告，返回 (解析后的payload, 输入token, 输出token)。

    payload 含 parse_error 时返回 (None, 输入token, 输出token)，表示解析失败。
    解析失败带错误信息重试 1 次（在 parse_llm_json 的 repair 闭包里发生）。
    """

    total_input = 0
    total_output = 0

    messages = [
        {"role": "system", "content": DEEP_READ_REDUCE_SYSTEM_PROMPT},
        {"role": "user", "content": reduce_user_content},
    ]
    response = await deps.llm.provider.chat(messages, temperature=0)
    usage = normalize_token_usage(response.usage)
    total_input += usage["input_tokens"]
    total_output += usage["output_tokens"]

    # repair 闭包：解析失败时把错误信息发给模型再调一次 chat，返回新文本。
    # 这就是"失败重试1次"，不许自己再写重试循环。
    async def _repair(error: str) -> str:
        """带着原始材料、上次的坏输出和解析错误信息让模型重新输出一次。

        中文注释：重试时必须把原始的笔记材料一起还给模型——只发一句错误提示的话，
        模型看不到材料，只能凭空编一份报告，重试就失去意义了。
        """

        nonlocal total_input, total_output
        repair_messages = [
            {"role": "system", "content": DEEP_READ_REDUCE_SYSTEM_PROMPT},
            {"role": "user", "content": reduce_user_content},
            {"role": "assistant", "content": response.content or ""},
            {"role": "user", "content": f"你上次的输出无法解析成要求的 JSON：{error}。请重新只输出一个符合要求的 JSON 对象。"},
        ]
        repair_response = await deps.llm.provider.chat(repair_messages, temperature=0)
        repair_usage = normalize_token_usage(repair_response.usage)
        total_input += repair_usage["input_tokens"]
        total_output += repair_usage["output_tokens"]
        return repair_response.content or ""

    # 统一走 parse_llm_json：先解析，失败走 repair 重试一次，再失败返回兜底 + parse_error。
    payload = await parse_llm_json(response.content or "", fallback={}, repair=_repair)
    if "parse_error" in payload:
        logger.warning(
            "精读报告解析失败",
            extra={"error": str(payload["parse_error"])[:200]},
        )
        return None, total_input, total_output
    return payload, total_input, total_output


def _build_reduce_user_content(
    doc: PaperDocument,
    notes: list[str],
    chunks: "list[TextChunk]",
) -> str:
    """组装 reduce 阶段的用户消息：论文元数据 + 分段笔记。

    论文元数据包含标题、摘要（截 400 字）、作者。
    分段笔记拼成"笔记 i (chunk_id): 笔记内容"列表。
    总长超 REDUCE_INPUT_MAX_CHARS 时按块序保留前面块并追加"[部分内容已省略]"。
    """

    metadata = {
        "标题": doc.title,
        "摘要": (doc.abstract or "")[:REDUCE_ABSTRACT_CHARS],
        "作者": list(doc.authors),
    }
    # 把每块笔记格式化成"笔记 i (chunk_id): 内容"。
    note_lines = [
        f"笔记 {i} ({chunk.chunk_id}): {note}"
        for i, (note, chunk) in enumerate(zip(notes, chunks), 1)
    ]

    def _serialize(lines: list[str]) -> str:
        """把元数据和笔记行列表序列化成 JSON 字符串。"""

        return json.dumps(
            {"论文元数据": metadata, "分段笔记": lines},
            ensure_ascii=False,
        )

    text = _serialize(note_lines)
    # 总长没超限制，直接返回。
    if len(text) <= REDUCE_INPUT_MAX_CHARS:
        return text

    # 总长超限：按块序从后往前删除，直到总长不超过限制。
    while note_lines and len(_serialize(note_lines)) > REDUCE_INPUT_MAX_CHARS:
        note_lines.pop()
    # 追加省略提示，让模型知道有内容被截断了。
    note_lines.append("[部分内容已省略]")
    return _serialize(note_lines)


# ---------------------------------------------------------------------------
# 摘要降级路径
# ---------------------------------------------------------------------------


async def _run_abstract_fallback(
    deps: DeepReadDeps,
    doc: PaperDocument,
    focus: str,
) -> tuple[JsonObject | None, int, int]:
    """摘要降级路径：仅凭标题+摘要+focus 生成精读报告。

    和 reduce 一样输出相同结构的 JSON，解析也走 parse_llm_json + repair。
    """

    total_input = 0
    total_output = 0

    user_content = json.dumps(
        {
            "标题": doc.title,
            "摘要": doc.abstract or "",
            "用户关注点": focus,
        },
        ensure_ascii=False,
    )
    messages = [
        {"role": "system", "content": DEEP_READ_ABSTRACT_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    response = await deps.llm.provider.chat(messages, temperature=0)
    usage = normalize_token_usage(response.usage)
    total_input += usage["input_tokens"]
    total_output += usage["output_tokens"]

    # repair 闭包：和 reduce 阶段同款逻辑，只是系统提示词换成降级版。
    async def _repair(error: str) -> str:
        """带着原始材料、上次的坏输出和解析错误信息让模型重新输出一次。"""

        nonlocal total_input, total_output
        repair_messages = [
            {"role": "system", "content": DEEP_READ_ABSTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": response.content or ""},
            {"role": "user", "content": f"你上次的输出无法解析成要求的 JSON：{error}。请重新只输出一个符合要求的 JSON 对象。"},
        ]
        repair_response = await deps.llm.provider.chat(repair_messages, temperature=0)
        repair_usage = normalize_token_usage(repair_response.usage)
        total_input += repair_usage["input_tokens"]
        total_output += repair_usage["output_tokens"]
        return repair_response.content or ""

    payload = await parse_llm_json(response.content or "", fallback={}, repair=_repair)
    if "parse_error" in payload:
        logger.warning(
            "精读报告解析失败（摘要降级）",
            extra={"error": str(payload["parse_error"])[:200]},
        )
        return None, total_input, total_output
    return payload, total_input, total_output


# ---------------------------------------------------------------------------
# 报告组装
# ---------------------------------------------------------------------------


def _assemble_report(
    paper_id: str,
    doc: PaperDocument,
    source: str,
    payload: JsonObject,
) -> DeepReadReport:
    """从模型输出的 payload 组装 DeepReadReport。

    各字段从 payload 宽容取出：字符串用 str()、列表逐项转 str、
    分数用 DimensionScore.from_dict（已做宽容解析）。
    overall_score 超范围截断到 0-100。
    """

    return DeepReadReport(
        paper_id=paper_id,
        title=doc.title,
        source=source,
        created_at=utc_now(),
        main_question=str(payload.get("main_question") or ""),
        methods=_safe_string_list(payload.get("methods")),
        datasets=_safe_string_list(payload.get("datasets")),
        contributions=_safe_string_list(payload.get("contributions")),
        limitations=_safe_string_list(payload.get("limitations")),
        main_results=_safe_string_list(payload.get("main_results")),
        short_summary=str(payload.get("short_summary") or ""),
        experimental_setup=str(payload.get("experimental_setup") or ""),
        conclusions=str(payload.get("conclusions") or ""),
        relevance=DimensionScore.from_dict(payload.get("relevance")),
        novelty=DimensionScore.from_dict(payload.get("novelty")),
        rigor=DimensionScore.from_dict(payload.get("rigor")),
        clarity=DimensionScore.from_dict(payload.get("clarity")),
        overall_score=_clamp_overall_score(payload.get("overall_score")),
        overall_comment=str(payload.get("overall_comment") or ""),
    )


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------


def _check_cancellation(deps: DeepReadDeps) -> None:
    """检查用户是否点了停止，点了就抛出取消异常让当前流程尽快退出。"""

    if deps.cancellation is not None:
        deps.cancellation.raise_if_requested()


def _fail(deps: DeepReadDeps, reason: str) -> JsonObject:
    """统一处理精读失败：推 failed 卡片并返回结构化失败结果。"""

    deps.reporter.failed(reason, stage=DEEP_READ_STAGE, event_key=deps.event_key)
    logger.info("精读失败", extra={"reason": reason[:200]})
    return {"status": "failed", "reason": reason}


def _paper_document(payload: JsonObject) -> PaperDocument:
    """把工作区里的论文元数据字典还原成 PaperDocument 对象。

    和 research_tools._paper_document_from_dict 同款逻辑，但本模块禁止 import
    research_tools，所以在这里自己实现一份。工作区里存的 paper 字典来自
    PaperDocument.to_dict()，它带了一个 "journal/conference" 键（中间有斜杠、
    不是 dataclass 字段），直接拿去构造 PaperDocument 会报错，必须先过滤掉。
    另外 to_dict 对 year=None 输出了空字符串，这里要转回 None。
    """

    # 只保留 PaperDocument 真正声明的字段名，把 "journal/conference" 这类额外键挡在外面。
    valid_names = {f.name for f in fields(PaperDocument)}
    kwargs = {key: value for key, value in payload.items() if key in valid_names}
    # year 在 to_dict 里被 None 写成了 ""，这里转回 None，保证类型和原对象一致。
    if kwargs.get("year") == "":
        kwargs["year"] = None
    return PaperDocument(**kwargs)


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


def _clamp_overall_score(value: Any) -> int:
    """把 overall_score 截断到 0-100 范围内的整数，转换不了返回 0。"""

    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0
