"""综述子 Agent（agent-as-tool 模式）。

这个模块是综述链路的执行核心。主 Agent 通过 generate_review 工具把综述任务
委派到这里，本模块负责编排已有的三个 Agent（AnalyseAgent→WritingOutlineAgent
→WritingAgent）：工作区论文 → 子主题分析 → 全局综合 → 大纲 → 逐节写作 → 摘要
→ 参考文献与引用替换 → 终稿 Markdown 存 artifact → 推 review 卡片 → 返回
{status, word_count, sections, artifact_id}。

整个流程自包含，不依赖 research_tools / researchAgent / deepReadAgent /
paperQaAgent，也不依赖 src.graph 下任何节点模块。所有从旧节点移植过来的逻辑
都改成对参数 / 工作区的依赖，避免和 State 耦合。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.llm.base import normalize_token_usage
from src.llm.config import SystemConfig
from src.models.sessions import utc_now
from src.utils import get_logger

from .analyseAgent import build_analyse_agent
from .contracts import JsonObject
from .writingAgent import build_writing_agent
from .writingOutlineAgent import OVERALL_ANALYSIS_FIELDS, build_writing_outline_agent


if TYPE_CHECKING:
    from src.graph.runtime import WorkflowCancellation, WorkflowNodeReporter
    from src.llm import ProviderSnapshot
    from src.models.workspace import SessionWorkspace, WorkspacePaperEntry
    from src.repositories.sessions.base import SessionRepository


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 模块常量（全部集中在顶部，禁止魔法数字散落函数体）
# ---------------------------------------------------------------------------

# 综述分析报告版本号（从 analyse_node 移植，保持一致）。
ANALYSIS_VERSION = "3.0"

# 全局综合分析"返回非 JSON"时的最大尝试次数（含首次），对齐旧
# OVERALL_ANALYSIS_MAX_ATTEMPTS=2：第一次失败后只重试 1 次。
OVERALL_ANALYSIS_MAX_ATTEMPTS = 2

# 综述进度事件的 stage 名，对应 runtime.py 里 ("tool","generate_review") 映射。
REVIEW_STAGE = "generate_review"

# 产物类型常量，写 artifact 时使用。
ARTIFACT_TYPE_REVIEW = "final_review"

# 摘要默认字数。
ABSTRACT_WORD_COUNT = 300

# 小节默认字数（大纲里没写 word-count 时兜底）。
DEFAULT_SECTION_WORD_COUNT = 800

# 分析输入里作者列表最多保留前几位。
ANALYSIS_AUTHORS_LIMIT = 8

# 分析输入里各文本字段的截断长度（对齐 analyse_node._paper_analysis_input）。
TITLE_TRUNCATE = 220
ABSTRACT_TRUNCATE = 900
NOTE_TEXT_TRUNCATE = 400
NOTE_LIST_MAX_ITEMS = 8
NOTE_LIST_SHORT_CHARS = 180
NOTE_LIST_LONG_CHARS = 220
SHORT_SUMMARY_TRUNCATE = 500

# 推卡片时综述主题的截断长度。
TOPIC_PREVIEW_CHARS = 60

# 一次取"工作区全部论文"时传给分页查询的数量上限（论文规模远小于它，等价于全取）。
WORKSPACE_ALL_PAPERS_LIMIT = 10_000

# 证据级别常量：有精读报告时用报告的 source，没有精读时标记为只看元数据。
EVIDENCE_LEVEL_METADATA = "metadata"

# relevance.status 在工作区有 / 没有评价时的取值。
RELEVANCE_STATUS_EVALUATED = "evaluated"
RELEVANCE_STATUS_NOT_EVALUATED = "not_evaluated"


# ---------------------------------------------------------------------------
# 运行时依赖
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReviewDeps:
    """综述子 Agent 的运行时依赖（由工具 handler 组装传入）。

    本模块禁止 import research_tools / researchAgent / deepReadAgent /
    paperQaAgent 及 src.graph 下任何节点模块（避免 Agent 间横向依赖与循环
    导入），需要的一切通过 Deps 显式传入。

    Attributes:
        session_key: 会话编号。
        turn_id: 当前对话回合编号（写产物路径时用）。
        workspace: 会话工作区（论文状态都在这里，改动会立即落盘）。
        repo: 会话仓储，写产物文件统一走它的 write_artifact。
        reporter: 绑定到"工具执行"节点的事件上报器，卡片消息和进度事件从这里发。
        event_key: 本次工具调用的事件键（进度/用量聚合到这张卡片）。
        cancellation: 用户停止请求的控制对象，长流程要定期检查。
        llm: 综述使用的模型快照，分析/大纲/写作都要调它的 provider.chat。
    """

    session_key: str
    turn_id: str
    workspace: "SessionWorkspace"
    repo: "SessionRepository"
    reporter: "WorkflowNodeReporter"
    event_key: str
    cancellation: "WorkflowCancellation | None" = None
    llm: "ProviderSnapshot | None" = None


# ---------------------------------------------------------------------------
# 综述主入口
# ---------------------------------------------------------------------------


async def run_review(*, topic: str, paper_ids: list[str] | None, deps: ReviewDeps) -> JsonObject:
    """综述主入口。

    成功时返回：
        {"status":"ok","word_count":int,
         "sections":[{"section_id","title"}],"artifact_id":str}
    失败时返回：
        {"status":"failed","reason":str}
        —— 内部任何业务异常都折成这个结构，绝不上抛；
          唯一例外 asyncio.CancelledError 原样上抛。
    """

    # 用一层 try/except 把所有业务异常都折成结构化返回。
    # asyncio.CancelledError 继承自 BaseException，不会被这里捕获，会原样上抛。
    try:
        return await _run_review_impl(topic=topic, paper_ids=paper_ids, deps=deps)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "综述过程出现意外错误",
            extra={"error": str(exc)[:200]},
        )
        return _fail(deps, f"综述过程出现意外错误：{exc}")


async def _run_review_impl(*, topic: str, paper_ids: list[str] | None, deps: ReviewDeps) -> JsonObject:
    """综述主流程实现（由 run_review 包裹异常折叠）。"""

    # token 用量累加器：分析 + 大纲 + 写作 + 摘要所有响应的 usage 统一累加。
    total_input = 0
    total_output = 0

    def collect_usage(usage: JsonObject) -> None:
        """把每次模型调用的 token 用量累加到总量里。"""

        nonlocal total_input, total_output
        total_input += int(usage.get("input_tokens") or 0)
        total_output += int(usage.get("output_tokens") or 0)

    # ------------------------------------------------------------------
    # 第一步：前置校验——模型、主题、论文数量。
    # ------------------------------------------------------------------
    _check_cancellation(deps)
    if deps.llm is None:
        return _fail(deps, "模型未装配，无法生成综述")
    topic_text = str(topic or "").strip()
    if not topic_text:
        return _fail(deps, "缺少综述主题，请先明确研究主题或传入 topic")

    # 筛选论文：paper_ids 非空只取这些（缺失的忽略并计数），否则全部。
    entries = _collect_paper_entries(deps.workspace, paper_ids)
    if not entries:
        return _fail(deps, "工作区里没有可用于综述的论文，请先检索或加入论文")

    deps.reporter.progress(
        f"准备综述 {len(entries)} 篇论文",
        stage=REVIEW_STAGE,
        event_key=deps.event_key,
    )

    # ------------------------------------------------------------------
    # 第二步：构造分析输入（形状对齐 analyse_node._paper_analysis_input）。
    # 每篇论文整理成分析模型需要的结构化摘要。
    # ------------------------------------------------------------------
    _check_cancellation(deps)
    analysis_inputs: list[JsonObject] = []
    for paper_id, entry in entries:
        analysis_inputs.append(_paper_analysis_input(paper_id, entry))
    # 单组：新架构没有检索子主题元数据，全量一组做横向比较。
    group: JsonObject = {
        "subtopic": topic_text,
        "search_keyword": "",
        "papers": analysis_inputs,
        "paper_count": len(analysis_inputs),
    }

    # ------------------------------------------------------------------
    # 第三步：分析——子主题分析 → 全局综合。
    # ------------------------------------------------------------------
    _check_cancellation(deps)
    deps.reporter.progress("正在分析论文", stage=REVIEW_STAGE, event_key=deps.event_key)
    analyse_agent = build_analyse_agent(deps.llm)

    # 子主题分析。
    subtopic_result = await analyse_agent.async_analyse_subtopic(
        topic=topic_text, group=group, usage_callback=collect_usage
    )
    if subtopic_result.parsed is None:
        return _fail(deps, f"子主题分析失败：{subtopic_result.reason}")
    subtopic_analysis = _normalize_subtopic_analysis(subtopic_result.parsed, group)

    # 全局综合分析：返回非 JSON 时重试 1 次（对齐旧 OVERALL_ANALYSIS_MAX_ATTEMPTS=2）。
    _check_cancellation(deps)
    deps.reporter.progress("正在综合分析", stage=REVIEW_STAGE, event_key=deps.event_key)
    overall_analysis = await _analyse_overall_with_retry(
        topic=topic_text,
        subtopic_analyses=[subtopic_analysis],
        agent=analyse_agent,
        usage_callback=collect_usage,
        deps=deps,
    )
    if overall_analysis is None:
        return _fail(deps, "全局综合分析未返回合法 JSON，已重试 1 次仍失败")

    # 组装分析报告（参照 analyse_node._build_final_report）。
    analysis_report = _build_analysis_report(
        topic=topic_text,
        subtopic_analyses=[subtopic_analysis],
        overall_analysis=overall_analysis,
        model_used=deps.llm.model if deps.llm else "unavailable",
    )

    # ------------------------------------------------------------------
    # 第四步：大纲。
    # ------------------------------------------------------------------
    _check_cancellation(deps)
    deps.reporter.progress("正在生成写作大纲", stage=REVIEW_STAGE, event_key=deps.event_key)
    outline_agent = build_writing_outline_agent(deps.llm)
    outline, _raw, reason = await outline_agent.async_generate_outline(
        topic=topic_text, analysis_report=analysis_report, usage_callback=collect_usage
    )
    if outline is None:
        return _fail(deps, f"写作大纲生成失败：{reason}")

    # ------------------------------------------------------------------
    # 第五步：逐节写作（顺序，不并发——previous_sections 依赖前文）。
    # ------------------------------------------------------------------
    _check_cancellation(deps)
    writing_agent = build_writing_agent(deps.llm)
    # 拍平大纲（移植 writing_node._flatten_outline）。
    section_tasks = _flatten_outline(outline)
    cache_dir = SystemConfig.load().read.paper_cache_dir
    available_paper_ids = [str(item["paperId"]) for item in analysis_inputs if str(item.get("paperId") or "").strip()]
    written_sections: list[JsonObject] = []

    for index, section_task in enumerate(section_tasks, start=1):
        _check_cancellation(deps)
        section_title = str(section_task.get("section_title") or section_task["section_id"])
        deps.reporter.progress(
            f"正在写作第 {index}/{len(section_tasks)} 节：{section_title}",
            stage=REVIEW_STAGE,
            event_key=deps.event_key,
        )
        # 按大纲 evidence-map 指定的字段从全局分析里取出证据。
        evidence = _resolve_section_evidence(
            evidence_fields=list(section_task.get("evidence_map") or []),
            overall_analysis=analysis_report["overall_analysis"],
        )
        # 已写小节作为前文参考（只取 section_id / content / cited_paper_ids）。
        previous_sections = [
            {
                "section_id": s["section_id"],
                "content": str(s.get("content") or ""),
                "cited_paper_ids": list(s.get("cited_paper_ids") or []),
            }
            for s in written_sections
        ]
        # 写作单节回调：收集用量，忽略内部进度消息（进度由上面的 progress 统一发）。
        def section_callback(message: str, usage: JsonObject | None = None) -> None:
            """写作小节内部回调：只收用量，内部进度消息不外发。"""

            nonlocal total_input, total_output
            if usage:
                total_input += int(usage.get("input_tokens") or 0)
                total_output += int(usage.get("output_tokens") or 0)

        section_result = await writing_agent.async_write_section(
            section_id=str(section_task["section_id"]),
            task=str(section_task.get("task") or ""),
            evidence_map=evidence,
            previous_sections=previous_sections,
            word_count=int(section_task.get("word_count") or DEFAULT_SECTION_WORD_COUNT),
            read_results=analysis_inputs,
            cache_dir=cache_dir,
            session_read_results=[],
            available_paper_ids=available_paper_ids,
            progress_callback=section_callback,
        )
        # 补上章节信息，方便后面拼 Markdown 和提取引用。
        section_result.update(
            chapter_key=section_task["chapter_key"],
            section_key=section_task["section_key"],
            chapter_title=str(section_task.get("chapter_title") or section_task["chapter_key"]),
            section_title=section_title,
        )
        written_sections.append(section_result)

    # ------------------------------------------------------------------
    # 第六步：摘要。
    # ------------------------------------------------------------------
    _check_cancellation(deps)
    deps.reporter.progress("正在生成摘要", stage=REVIEW_STAGE, event_key=deps.event_key)
    abstract, abstract_status = await writing_agent.async_write_abstract(
        topic=topic_text,
        sections=written_sections,
        word_count=ABSTRACT_WORD_COUNT,
        usage_callback=collect_usage,
    )

    # ------------------------------------------------------------------
    # 第七步：参考文献与引用替换（移植 writing_node 的引用整理逻辑）。
    # ------------------------------------------------------------------
    _check_cancellation(deps)
    deps.reporter.progress("正在整理参考文献", stage=REVIEW_STAGE, event_key=deps.event_key)
    # 引用顺序以正文里 paperId 第一次出现的位置为准。
    candidate_paper_ids = _extract_paper_ids_from_sections(written_sections)
    # metadata_by_id 从工作区 entry.paper 构造。
    metadata_by_id = _build_workspace_metadata(deps.workspace, entries)
    # 找出没有真实论文资料（缺题名）的引用，从正文和摘要里删掉。
    valid_keys = {
        paper_id
        for paper_id, metadata in metadata_by_id.items()
        if _has_reference_metadata(metadata)
    }
    unknown_paper_ids = [
        paper_id
        for paper_id in candidate_paper_ids
        if paper_id.lower() not in valid_keys
    ]
    written_sections = _remove_unknown_paper_citations(written_sections, unknown_paper_ids)
    abstract = _remove_unknown_citation_markers(abstract, unknown_paper_ids)
    # 中文注释：把没有真实论文资料支撑的引用从正文里删掉，属于"悄悄改文章"的动作。
    # 如果不留日志，事后谁也不知道综述里为什么少了引用，所以这里把删掉的编号
    # 完整记录下来，让"静默删引用"变成可以追查的行为。
    if unknown_paper_ids:
        logger.warning(
            "综述引用整理：剥离了没有真实论文资料支撑的引用编号",
            extra={
                "session_key": deps.session_key,
                "removed_paper_ids": unknown_paper_ids,
                "removed_count": len(unknown_paper_ids),
            },
        )
    # 重新提取引用并生成参考文献。
    cited_paper_ids = _extract_paper_ids_from_sections(written_sections)
    references = _build_references(cited_paper_ids, metadata_by_id)

    # 引用替换：把正文和摘要里的 [paperId] 换成 [序号]。
    citation_index_by_paper_id = {
        str(item.get("paperId") or "").strip().lower(): str(item.get("index"))
        for item in references
        if str(item.get("paperId") or "").strip() and item.get("index") is not None
    }
    written_sections = _replace_section_citations(written_sections, citation_index_by_paper_id)
    abstract = _replace_citation_numbers(abstract, citation_index_by_paper_id)

    # ------------------------------------------------------------------
    # 第八步：终稿 Markdown（移植 reply_node._build_final_markdown 的结构）。
    # ------------------------------------------------------------------
    markdown = _build_final_markdown(
        topic=topic_text,
        sections=written_sections,
        abstract=abstract,
        references=references,
    )

    # 字数 = 摘要字数 + 各节正文字数。
    word_count = len(abstract) + sum(len(str(s.get("content") or "")) for s in written_sections)
    # 给前端用的章节列表：只取 section_id 和标题。
    sections_view = [
        {
            "section_id": str(s.get("section_id") or ""),
            "title": str(s.get("section_title") or s.get("section_id") or ""),
        }
        for s in written_sections
    ]

    # ------------------------------------------------------------------
    # 第九步：存 artifact（一律走 repo.write_artifact，禁止直接写文件）。
    # ------------------------------------------------------------------
    _check_cancellation(deps)
    deps.reporter.progress("正在保存综述产物", stage=REVIEW_STAGE, event_key=deps.event_key)
    record = await asyncio.to_thread(
        deps.repo.write_artifact,
        deps.session_key,
        ARTIFACT_TYPE_REVIEW,
        "literature_review.md",
        markdown,
        relative_path=f"artifacts/review/{deps.turn_id}/literature_review.md",
        metadata={
            "topic": topic_text,
            "format": "markdown",
            "section_count": len(written_sections),
            "reference_count": len(references),
            "word_count": word_count,
            "abstract_status": abstract_status,
        },
    )
    artifact_id = str(record.get("id") or "")

    # ------------------------------------------------------------------
    # 第十步：推 review 卡片（role 用 system，不干扰助手消息缓冲区）。
    # ------------------------------------------------------------------
    deps.reporter.message(
        role="system",
        content=f"综述《{topic_text[:TOPIC_PREVIEW_CHARS]}》已生成（{word_count} 字，{len(sections_view)} 节）",
        metadata={
            "kind": "review",
            "artifact_id": artifact_id,
            "word_count": word_count,
            "sections": sections_view,
            "topic": topic_text,
        },
    )

    # ------------------------------------------------------------------
    # 第十一步：token 用量聚合，推一条带用量的完成进度事件。
    # ------------------------------------------------------------------
    deps.reporter.progress(
        "综述完成",
        stage=REVIEW_STAGE,
        event_key=deps.event_key,
        input_tokens=total_input,
        output_tokens=total_output,
    )
    logger.info(
        "综述完成",
        extra={
            "session_key": deps.session_key,
            "topic": topic_text[:TOPIC_PREVIEW_CHARS],
            "word_count": word_count,
            "section_count": len(sections_view),
            "input_tokens": total_input,
            "output_tokens": total_output,
        },
    )

    return {
        "status": "ok",
        "word_count": word_count,
        "sections": sections_view,
        "artifact_id": artifact_id,
    }


# ---------------------------------------------------------------------------
# 论文收集与分析输入构造（移植自 analyse_node._paper_analysis_input）
# ---------------------------------------------------------------------------


def _collect_paper_entries(
    workspace: "SessionWorkspace", paper_ids: list[str] | None
) -> list[tuple[str, "WorkspacePaperEntry"]]:
    """从工作区收集要综述的论文。

    paper_ids 非空时只取这些编号（缺失的忽略并计数），否则取全部。
    返回 (paper_id, entry) 列表，按论文进入工作区的顺序排列。
    """

    if paper_ids:
        # 指定了编号：逐个取，缺失的跳过（调用方已做过工作区校验）。
        result: list[tuple[str, "WorkspacePaperEntry"]] = []
        for paper_id in paper_ids:
            entry = workspace.get_paper(paper_id)
            if entry is not None:
                result.append((paper_id, entry))
        return result
    # 没指定编号：取全部论文（按进入工作区时间排序，结果稳定）。
    _, page = workspace.query_papers(limit=WORKSPACE_ALL_PAPERS_LIMIT, offset=0)
    return page


def _paper_analysis_input(paper_id: str, entry: "WorkspacePaperEntry") -> JsonObject:
    """把工作区里的单篇论文压成分析模型需要的结构化摘要。

    逻辑移植自 analyse_node._paper_analysis_input，但数据来源从 read_results
    的 item 改成工作区的 entry（entry.paper / entry.deep_read / entry.evaluation）。
    有精读报告时 structured_summary 用报告字段、evidence_level=报告 source；
    否则全空、evidence_level="metadata"。有评价时 relevance 用之，否则默认空。
    """

    paper = dict(entry.paper or {})
    report = entry.deep_read
    evaluation = entry.evaluation
    # structured_summary：有精读报告就用报告字段，没有就全空。
    if report is not None:
        structured_summary: JsonObject = {
            "main_question": _shorten(report.main_question, NOTE_TEXT_TRUNCATE),
            "methods": _shorten_list(report.methods, NOTE_LIST_MAX_ITEMS, NOTE_LIST_SHORT_CHARS),
            "datasets": _shorten_list(report.datasets, NOTE_LIST_MAX_ITEMS, NOTE_LIST_SHORT_CHARS),
            "contributions": _shorten_list(report.contributions, NOTE_LIST_MAX_ITEMS, NOTE_LIST_LONG_CHARS),
            "limitations": _shorten_list(report.limitations, NOTE_LIST_MAX_ITEMS, NOTE_LIST_LONG_CHARS),
            "main_results": _shorten_list(report.main_results, NOTE_LIST_MAX_ITEMS, NOTE_LIST_LONG_CHARS),
            "short_summary": _shorten(report.short_summary, SHORT_SUMMARY_TRUNCATE),
            # evidence_level 用报告来源（fulltext / abstract_fallback），没有报告时标记只看元数据。
            "evidence_level": str(report.source or EVIDENCE_LEVEL_METADATA),
        }
    else:
        structured_summary = {
            "main_question": "",
            "methods": [],
            "datasets": [],
            "contributions": [],
            "limitations": [],
            "main_results": [],
            "short_summary": "",
            "evidence_level": EVIDENCE_LEVEL_METADATA,
        }
    # relevance：有评价就用之，否则默认空 match_levels / 0 分 / 未评价。
    if evaluation is not None:
        relevance: JsonObject = {
            "match_levels": dict(evaluation.match_levels or {}),
            "score": evaluation.score,
            "status": RELEVANCE_STATUS_EVALUATED,
        }
    else:
        relevance = {
            "match_levels": {},
            "score": 0,
            "status": RELEVANCE_STATUS_NOT_EVALUATED,
        }
    return {
        "paperId": paper_id,
        "title": _shorten(paper.get("title"), TITLE_TRUNCATE),
        "year": paper.get("year") or "",
        "authors": list(paper.get("authors") or [])[:ANALYSIS_AUTHORS_LIMIT],
        "venue": paper.get("journal_conference") or paper.get("journal/conference") or paper.get("venue") or "",
        "abstract": _shorten(paper.get("abstract"), ABSTRACT_TRUNCATE),
        "structured_summary": structured_summary,
        "relevance": relevance,
        # 新架构不再从 read_results 读这些字段，统一留空。
        "full_text_status": {},
        "extraction": {},
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# 分析归一化与报告组装（移植自 analyse_node）
# ---------------------------------------------------------------------------


def _analyse_overall_with_retry(
    *,
    topic: str,
    subtopic_analyses: list[JsonObject],
    agent: Any,
    usage_callback: Any | None,
    deps: ReviewDeps,
) -> JsonObject | None:
    """全局综合分析：返回非 JSON 时重试 1 次（对齐旧 OVERALL_ANALYSIS_MAX_ATTEMPTS=2）。

    模型调用本身已经由 provider 负责网络重试；这里的重试只针对"请求成功但返回
    内容不是 JSON"这一业务层问题。两次都失败返回 None，由调用方决定报失败。
    """

    async def _run() -> JsonObject | None:
        last_reason = "模型没有返回可解析的 JSON"
        for attempt in range(1, OVERALL_ANALYSIS_MAX_ATTEMPTS + 1):
            _check_cancellation(deps)
            result = await agent.async_analyse_overall(
                topic=topic,
                subtopic_analyses=subtopic_analyses,
                usage_callback=usage_callback,
            )
            if result.parsed is not None:
                return _normalize_overall_analysis(result.parsed, subtopic_analyses)
            last_reason = str(result.reason or "").strip() or "模型没有返回可解析的 JSON"
            if attempt < OVERALL_ANALYSIS_MAX_ATTEMPTS:
                deps.reporter.progress(
                    "全局分析结果不可解析，正在重试",
                    stage=REVIEW_STAGE,
                    event_key=deps.event_key,
                )
                continue
        logger.warning("全局综合分析未返回合法 JSON", extra={"reason": last_reason[:200]})
        return None

    return _run()


def _normalize_subtopic_analysis(parsed: JsonObject, group: JsonObject) -> JsonObject:
    """整理模型返回的子主题分析 JSON，并由服务端补齐流程需要的论文信息。

    移植自 analyse_node._normalize_subtopic_analysis，不回读 State。
    """

    paper_ids = [
        str(paper.get("paperId") or "").strip()
        for paper in group.get("papers", [])
        if str(paper.get("paperId") or "").strip()
    ]
    if not paper_ids:
        paper_ids = _string_list(group.get("paperIds"))
    fallback = _fallback_subtopic_analysis(group, reason="")
    return {
        "subtopic": str(group.get("subtopic") or "综合分析"),
        "search_keyword": str(group.get("search_keyword") or ""),
        "paper_count": int(group.get("paper_count") or len(paper_ids)),
        "paperIds": paper_ids,
        "研究现状": _text_with_citation(parsed.get("研究现状") or fallback["研究现状"], paper_ids),
        "一致点": _text_list_with_citation(parsed.get("一致点"), paper_ids),
        "矛盾点": _text_with_citation(parsed.get("矛盾点") or fallback["矛盾点"], paper_ids),
        "研究空白": _text_with_citation(parsed.get("研究空白") or fallback["研究空白"], paper_ids),
        "时间线演化": _text_with_citation(parsed.get("时间线演化") or fallback["时间线演化"], paper_ids),
        "技术方法栈演变": _text_with_citation(parsed.get("技术方法栈演变") or fallback["技术方法栈演变"], paper_ids),
    }


def _fallback_subtopic_analysis(group: JsonObject, *, reason: str) -> JsonObject:
    """生成兜底子主题分析，保证字段齐全。

    移植自 analyse_node._fallback_subtopic_analysis。
    """

    # 中文注释：兜底文本不再自动贴论文引用。引用由模型自己写，程序不伪造归因。
    paper_ids = [
        str(paper.get("paperId") or "").strip()
        for paper in group.get("papers", [])
        if str(paper.get("paperId") or "").strip()
    ]
    if not paper_ids:
        paper_ids = _string_list(group.get("paperIds"))
    summary = f"当前子主题共有 {len(paper_ids)} 篇论文可用于分析，建议结合这些论文的结构化摘要继续判断。"
    if reason:
        summary = f"{summary} 说明：{reason}。"
    return {
        "subtopic": str(group.get("subtopic") or "综合分析"),
        "search_keyword": str(group.get("search_keyword") or ""),
        "paper_count": int(group.get("paper_count") or len(paper_ids)),
        "paperIds": paper_ids,
        "研究现状": summary,
        "一致点": [],
        "矛盾点": "暂未能稳定归纳论文之间的矛盾点，需要模型进一步分析。",
        "研究空白": "暂未能稳定归纳研究空白，需要模型进一步分析。",
        "时间线演化": _fallback_timeline(group),
        "技术方法栈演变": "暂未能稳定归纳方法栈演变，需要模型进一步分析。",
    }


def _normalize_overall_analysis(parsed: JsonObject, subtopic_analyses: list[JsonObject]) -> JsonObject:
    """整理模型返回的八部分综合结果，并保证每段文字带有论文引用。

    移植自 analyse_node._normalize_overall_analysis。
    """

    paper_ids = _paper_ids_from_analyses(subtopic_analyses)
    fallback = _fallback_overall_analysis("当前研究领域", subtopic_analyses, reason="")
    normalized: JsonObject = {
        field: _text_with_citation(parsed.get(field) or fallback[field], paper_ids)
        for field in OVERALL_ANALYSIS_FIELDS
    }
    return normalized


def _fallback_overall_analysis(topic: str, subtopic_analyses: list[JsonObject], *, reason: str) -> JsonObject:
    """生成全域八部分综合分析的兜底结果。

    移植自 analyse_node._fallback_overall_analysis。
    """

    # 中文注释：兜底文本不再自动贴论文引用，理由同 _fallback_subtopic_analysis。
    summary = (
        f"《{topic}》目前已完成 {len(subtopic_analyses)} 个子主题的分析，综合结果将从全域概况、"
        f"共识、争议、空白、演化、方法、横向差异和展望八个方面归纳。"
    )
    if reason:
        summary = f"{summary} 说明：{reason}。"
    return {
        "领域整体研究概况": summary,
        "领域全域共性研究共识": "当前暂未能稳定提炼跨子主题的共同结论，建议结合各子主题分析继续归纳。",
        "领域核心研究争议与矛盾体系": "当前暂未能稳定归纳贯穿全领域的争议及其适用边界，建议结合各子主题的分歧证据继续分析。",
        "领域系统性研究空白与局限": "当前暂未能稳定整合全域研究空白，建议从地域、视角、时长、方法、内容和场景六个维度继续核查。",
        "领域研究时序演化脉络": "当前暂未能稳定整理全领域的阶段性演化轨迹，建议依据论文发表时间继续归纳研究重心变化。",
        "领域技术与研究方法迭代脉络": "当前暂未能稳定归纳全领域技术和研究方法的迭代路径，建议结合各阶段论文方法继续分析。",
        "各子主题横向差异对比分析": "当前暂未能稳定比较各子主题的成熟度、共识度、争议度、空白体量和技术应用深度。",
        "领域整体总结与研究展望": f"《{topic}》的整体价值、实践启示和后续研究方向仍需在完成上述全域归纳后进一步凝练。",
    }


def _build_analysis_report(
    *,
    topic: str,
    subtopic_analyses: list[JsonObject],
    overall_analysis: JsonObject,
    model_used: str,
) -> JsonObject:
    """组装最终分析报告。

    移植自 analyse_node._build_final_report，analysis_version 常量 "3.0" 移植。
    """

    total_papers = len(set(_paper_ids_from_analyses(subtopic_analyses)))
    return {
        "analysis_version": ANALYSIS_VERSION,
        "topic": topic,
        "overall_framework": overall_analysis.get("领域整体研究概况") or "",
        "overall_analysis": overall_analysis,
        "subtopic_analyses": subtopic_analyses,
        "execution_metadata": {
            "total_papers_analyzed": total_papers,
            "subtopic_count": len(subtopic_analyses),
            "model_used": model_used,
            "created_at": utc_now(),
        },
    }


def _fallback_timeline(group: JsonObject) -> str:
    """根据年份生成一个很保守的兜底时间线。移植自 analyse_node._fallback_timeline。"""

    papers = list(group.get("papers") or [])
    years = [int(paper["year"]) for paper in papers if str(paper.get("year") or "").isdigit()]
    paper_ids = [str(paper.get("paperId") or "").strip() for paper in papers if str(paper.get("paperId") or "").strip()]
    # 中文注释：兜底文本不再自动贴论文引用，理由同 _fallback_subtopic_analysis。
    if not years:
        return "暂无足够的年份信息来归纳时间线演化。"
    return (
        f"现有论文的发表年份覆盖 {min(years)} 至 {max(years)} 年，"
        f"具体演化过程仍需要模型进一步归纳。"
    )


def _paper_ids_from_analyses(analyses: list[JsonObject]) -> list[str]:
    """从子主题分析里尽量收集 paperId。移植自 analyse_node._paper_ids_from_analyses。"""

    ids: list[str] = []
    pattern = re.compile(r"\[([^\[\]]+)\]")
    for analysis in analyses:
        for paper_id in _list_value(analysis.get("paperIds")):
            text_id = str(paper_id or "").strip()
            if text_id and text_id not in ids:
                ids.append(text_id)
        text = json.dumps(analysis, ensure_ascii=False)
        for match in pattern.findall(text):
            paper_id = match.strip()
            # JSON 数组会长得像 ["P1"]，它不是正文里的 [paperId] 引用。
            # 这里跳过带引号或逗号的内容，只保留真正的文本引用。
            if '"' in paper_id or "'" in paper_id or "," in paper_id:
                continue
            if paper_id and paper_id not in ids:
                ids.append(paper_id)
    return ids


def _text_with_citation(value: Any, paper_ids: list[str]) -> str:
    """整理关键文本字段，空文本填占位。

    中文注释：
    改造前这个函数会在文本缺少 [...] 引用时自动贴上 paper_ids 的编号尾巴。
    这导致程序系统性地伪造归因——任何模型没写引用的结论都被自动贴上三篇
    论文的编号，下游 _build_references 再据此生成正式的 GB/T 7714 参考文献。
    现在只保留「空文本填占位」，引用由模型自己写，不自动补。
    """

    text = str(value or "").strip()
    if not text:
        text = "暂无稳定结论。"
    return text


def _citation_tail(paper_ids: list[str]) -> str:
    """把 paperId 列表变成 [paperId] 引用尾巴。移植自 analyse_node._citation_tail。"""

    cleaned = [paper_id for paper_id in paper_ids if paper_id]
    return "".join(f"[{paper_id}]" for paper_id in cleaned[:6])


def _text_list_with_citation(value: Any, paper_ids: list[str]) -> list[str]:
    """整理一致点数组，并保证每一条都至少带一个论文引用。"""

    return [_text_with_citation(item, paper_ids) for item in _string_list(value)]


# ---------------------------------------------------------------------------
# 大纲拍平与证据解析（移植自 writing_node）
# ---------------------------------------------------------------------------


def _flatten_outline(outline: JsonObject) -> list[JsonObject]:
    """把章节大纲拍平成按顺序执行的小节任务列表。

    移植自 writing_node._flatten_outline。注意大纲键是 evidence-map / ref-sections /
    word-count 连字符格式。
    """

    tasks: list[JsonObject] = []
    for chapter_key, chapter in outline.items():
        if not isinstance(chapter, dict):
            continue
        sections = chapter.get("Sections")
        if not isinstance(sections, dict):
            continue
        for section_key, section in sections.items():
            if not isinstance(section, dict):
                continue
            section_id = f"{chapter_key}.{section_key}"
            tasks.append(
                {
                    "section_id": section_id,
                    "chapter_key": chapter_key,
                    "section_key": section_key,
                    "chapter_title": str(chapter.get("title") or chapter_key),
                    "section_title": str(section.get("title") or section_key),
                    "task": str(section.get("task") or ""),
                    "evidence_map": list(section.get("evidence-map") or []),
                    "ref_sections": list(section.get("ref-sections") or []),
                    "word_count": int(section.get("word-count") or DEFAULT_SECTION_WORD_COUNT),
                }
            )
    return tasks


def _resolve_section_evidence(*, evidence_fields: list[Any], overall_analysis: JsonObject) -> list[JsonObject]:
    """按 evidence-map 指定的字段，从全局分析中取出当前小节可用的证据。

    移植自 writing_node._resolve_section_evidence。
    """

    evidence: list[JsonObject] = []
    used_fields: set[str] = set()
    for item in evidence_fields:
        field = str(item or "").strip()
        # 大纲由模型生成，先核对字段名，避免把标题或其他说明误当成证据。
        if field not in OVERALL_ANALYSIS_FIELDS or field in used_fields:
            continue
        content = str(overall_analysis.get(field) or "").strip()
        # 空字段不能支撑正文，因此不传给写作 Agent。
        if not content:
            continue
        evidence.append({"全局分析字段": field, "内容": content})
        used_fields.add(field)
    return evidence


# ---------------------------------------------------------------------------
# 参考文献与引用替换（移植自 writing_node）
# ---------------------------------------------------------------------------


def _build_workspace_metadata(
    workspace: "SessionWorkspace", entries: list[tuple[str, "WorkspacePaperEntry"]]
) -> dict[str, JsonObject]:
    """从工作区 entry.paper 构造 paperId(小写) → 论文元数据字典 的映射。"""

    metadata_by_id: dict[str, JsonObject] = {}
    for paper_id, entry in entries:
        paper = dict(entry.paper or {})
        key = paper_id.strip().lower()
        if key and key not in metadata_by_id:
            metadata_by_id[key] = paper
    return metadata_by_id


def _has_reference_metadata(paper: JsonObject) -> bool:
    """判断论文资料是否至少包含可展示的题名。移植自 writing_node._has_reference_metadata。"""

    return bool(str(paper.get("title") or "").strip())


def _build_references(paper_ids: list[str], metadata_by_id: dict[str, JsonObject]) -> list[JsonObject]:
    """按正文首次引用顺序生成 GB/T 7714 参考文献条目。移植自 writing_node._build_references。"""

    references: list[JsonObject] = []
    for paper_id in paper_ids:
        metadata = dict(metadata_by_id.get(paper_id.lower()) or {})
        # 没有题名的资料不生成参考文献，不能用 paperId 代替论文题名。
        if not _has_reference_metadata(metadata):
            continue
        references.append(
            {
                "index": len(references) + 1,
                "paperId": paper_id,
                "citation": _format_gbt7714_reference(paper_id, metadata),
                "metadata": metadata,
            }
        )
    return references


def _format_gbt7714_reference(paper_id: str, paper: JsonObject) -> str:
    """使用论文元数据生成常见的 GB/T 7714 顺序编码制格式。

    移植自 writing_node._format_gbt7714_reference，格式保持不变。
    """

    title = str(paper.get("title") or paper_id).strip()
    extra_metadata = paper.get("metadata") if isinstance(paper.get("metadata"), dict) else {}
    authors = _format_reference_authors(paper.get("authors") or paper.get("author"))
    resource_type = _reference_resource_type(paper)
    container = str(
        paper.get("journal_conference")
        or paper.get("journal/conference")
        or paper.get("journal")
        or paper.get("venue")
        or extra_metadata.get("journal")
        or ""
    ).strip()
    year = str(paper.get("year") or paper.get("publication_date") or "").strip()[:4]
    volume = str(paper.get("volume") or "").strip()
    issue = str(paper.get("issue") or "").strip()
    pages = str(
        paper.get("pages")
        or paper.get("page_range")
        or extra_metadata.get("pages")
        or (
            f"{paper.get('page_start')}-{paper.get('page_end')}"
            if paper.get("page_start") is not None and paper.get("page_end") is not None
            else ""
        )
    ).strip()
    doi = str(paper.get("doi") or "").strip()
    url = str(paper.get("url") or "").strip()

    citation = f"{authors + '. ' if authors else ''}{title}[{resource_type}]"
    if container:
        citation += f". {container}"
    if year:
        citation += f", {year}"
    if volume:
        citation += f", {volume}"
        if issue:
            citation += f"({issue})"
    elif issue:
        citation += f", ({issue})"
    if pages:
        citation += f": {pages}"
    citation += "."
    if doi:
        citation += f" DOI: {doi}."
    elif url:
        citation += f" {url}."
    return citation


def _format_reference_authors(value: Any) -> str:
    """整理作者字段，超过三位时按 GB/T 7714 习惯使用 et al.。移植自 writing_node._format_reference_authors。"""

    if isinstance(value, str):
        authors = [item.strip() for item in re.split(r"[,;，；]", value) if item.strip()]
    elif isinstance(value, list):
        authors = []
        for item in value:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("author") or "").strip()
            else:
                name = str(item or "").strip()
            if name:
                authors.append(name)
    else:
        authors = []
    if len(authors) > 3:
        suffix = "等" if any("一" <= character <= "鿿" for character in authors[0]) else "et al"
        return ", ".join(authors[:3]) + ("，" if suffix == "等" else ", ") + suffix
    return ", ".join(authors)


def _reference_resource_type(paper: JsonObject) -> str:
    """根据元数据推断参考文献类型，缺少信息时按期刊论文处理。移植自 writing_node._reference_resource_type。"""

    type_text = " ".join(
        str(paper.get(key) or "")
        for key in ("type", "document_type", "publication_type", "source")
    ).lower()
    if "conference" in type_text or "proceedings" in type_text:
        return "C"
    if "thesis" in type_text or "dissertation" in type_text:
        return "D"
    if "book" in type_text:
        return "M"
    if "arxiv" in type_text or "preprint" in type_text:
        return "EB/OL"
    return "J"


def _extract_paper_ids_from_sections(sections: list[JsonObject]) -> list[str]:
    """从小节正文的方括号引用中提取 paperId，并按首次出现顺序去重。

    移植自 writing_node._extract_paper_ids_from_sections。
    """

    declared_ids = _collect_cited_paper_ids(sections)
    declared_by_key = {paper_id.lower(): paper_id for paper_id in declared_ids}
    found: list[str] = []
    seen: set[str] = set()
    citation_pattern = re.compile(r"\[([^\[\]\r\n]+)\]")
    for section in sections:
        content = str(section.get("content") or "")
        for match in citation_pattern.finditer(content):
            candidate = match.group(1).strip().strip('"').strip("'")
            if not candidate or any(character.isspace() for character in candidate):
                continue
            # 即使模型漏掉了前面的归一化，也不能把切片编号直接生成参考文献。
            if _is_chunk_id(candidate):
                continue
            paper_id = declared_by_key.get(candidate.lower(), candidate)
            if candidate.lower() not in declared_by_key and not re.search(r"\d|[:/.]", candidate):
                continue
            key = paper_id.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(paper_id)
        # 如果模型把引用列在结构化字段里但正文没有重复写出，仍保留该引用。
        for paper_id in list(section.get("cited_paper_ids") or []):
            text = str(paper_id or "").strip()
            if _is_chunk_id(text):
                continue
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                found.append(text)
    return found


def _is_chunk_id(value: str) -> bool:
    """判断一个候选编号是否符合全文切片的页码或分段编号格式。移植自 writing_node._is_chunk_id。"""

    return bool(re.search(r":(?:p|c)\d{4}(?::s\d{4})?$", str(value or "").strip(), flags=re.IGNORECASE))


def _collect_cited_paper_ids(sections: list[JsonObject]) -> list[str]:
    """汇总所有小节实际引用到的 paperId。移植自 writing_node._collect_cited_paper_ids。"""

    seen: set[str] = set()
    result: list[str] = []
    for section in sections:
        for paper_id in list(section.get("cited_paper_ids") or []):
            text = str(paper_id or "").strip()
            if _is_chunk_id(text):
                continue
            key = text.lower()
            if not text or key in seen:
                continue
            seen.add(key)
            result.append(text)
    return result


def _remove_unknown_paper_citations(
    sections: list[JsonObject],
    unknown_paper_ids: list[str],
) -> list[JsonObject]:
    """删除小节中没有真实论文资料支撑的引用标记。移植自 writing_node._remove_unknown_paper_citations。"""

    if not unknown_paper_ids:
        return [dict(section) for section in sections]
    unknown_keys = {paper_id.lower() for paper_id in unknown_paper_ids}
    cleaned_sections: list[JsonObject] = []
    for section in sections:
        cleaned = dict(section)
        cleaned["content"] = _remove_unknown_citation_markers(
            str(cleaned.get("content") or ""),
            unknown_paper_ids,
        )
        # 正文和 cited_paper_ids 必须同步清理。
        cleaned["cited_paper_ids"] = [
            paper_id
            for paper_id in list(cleaned.get("cited_paper_ids") or [])
            if str(paper_id or "").strip().lower() not in unknown_keys
        ]
        cleaned_sections.append(cleaned)
    return cleaned_sections


def _remove_unknown_citation_markers(content: str, unknown_paper_ids: list[str]) -> str:
    """从一段文字中删除形如 [P1] 的未知引用标记。移植自 writing_node._remove_unknown_citation_markers。"""

    if not content or not unknown_paper_ids:
        return content
    unknown_keys = {paper_id.lower() for paper_id in unknown_paper_ids}
    citation_pattern = re.compile(r"\[([^\[\]\r\n]+)\]")

    def replace(match: re.Match[str]) -> str:
        paper_id = match.group(1).strip().strip('"').strip("'")
        return "" if paper_id.lower() in unknown_keys else match.group(0)

    return citation_pattern.sub(replace, content)


def _replace_section_citations(
    sections: list[JsonObject],
    citation_index_by_paper_id: dict[str, str],
) -> list[JsonObject]:
    """把正文小节中的 [paperId] 替换为参考文献序号。移植自 writing_node._replace_section_citations。"""

    if not citation_index_by_paper_id:
        return [dict(section) for section in sections]
    replaced: list[JsonObject] = []
    for section in sections:
        item = dict(section)
        item["content"] = _replace_citation_numbers(
            str(item.get("content") or ""), citation_index_by_paper_id
        )
        replaced.append(item)
    return replaced


def _replace_citation_numbers(content: str, citation_index_by_paper_id: dict[str, str]) -> str:
    """替换一段文本中的论文编号，普通 Markdown 方括号保持不变。移植自 writing_node._replace_citation_numbers。"""

    citation_pattern = re.compile(r"\[([^\[\]\r\n]+)\]")

    def replace(match: re.Match[str]) -> str:
        candidate = match.group(1).strip().strip('"').strip("'").strip()
        index = citation_index_by_paper_id.get(candidate.lower())
        return f"[{index}]" if index else match.group(0)

    return citation_pattern.sub(replace, content)


# ---------------------------------------------------------------------------
# 终稿 Markdown（移植自 reply_node._build_final_markdown）
# ---------------------------------------------------------------------------


def _build_final_markdown(
    *,
    topic: str,
    sections: list[JsonObject],
    abstract: str,
    references: list[JsonObject],
) -> str:
    """把写作节点的全部已完成内容拼成一份可以直接保存的 Markdown 综述。

    结构移植自 reply_node._build_final_markdown：# 标题 / ## 摘要 / ## 章标题 /
    ### 节标题 / ## 参考文献。
    """

    blocks: list[str] = []
    title = topic.strip() or "文献综述"
    if title:
        blocks.append(f"# {title}")
    if abstract.strip():
        blocks.append(f"## 摘要\n\n{abstract.strip()}")

    current_chapter = ""
    for section in sections:
        if not isinstance(section, dict):
            continue
        chapter_key = str(section.get("chapter_key") or "").strip()
        chapter_title = str(section.get("chapter_title") or chapter_key).strip()
        if chapter_key and chapter_key != current_chapter:
            blocks.append(f"## {chapter_title or chapter_key}")
            current_chapter = chapter_key
        section_title = str(section.get("section_title") or section.get("section_id") or "小节").strip()
        content = str(section.get("content") or "").strip()
        if content:
            blocks.append(f"### {section_title}\n\n{content}")

    reference_lines = [
        f"[{item.get('index')}] {item.get('citation')}"
        for item in references
        if isinstance(item, dict) and str(item.get("citation") or "").strip()
    ]
    if reference_lines:
        blocks.append("## 参考文献\n\n" + "\n".join(reference_lines))
    return "\n\n".join(blocks).strip() + ("\n" if blocks else "")


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------


def _check_cancellation(deps: ReviewDeps) -> None:
    """检查用户是否点了停止，点了就抛出取消异常让当前流程尽快退出。"""

    if deps.cancellation is not None:
        deps.cancellation.raise_if_requested()


def _fail(deps: ReviewDeps, reason: str) -> JsonObject:
    """统一处理综述失败：推 failed 卡片并返回结构化失败结果。"""

    deps.reporter.failed(reason, stage=REVIEW_STAGE, event_key=deps.event_key)
    logger.info("综述失败", extra={"reason": reason[:200]})
    return {"status": "failed", "reason": reason}


def _shorten(value: Any, limit: int) -> str:
    """截短很长的文本，避免一次分析塞进过多无关内容。移植自 analyse_node._shorten。"""

    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _shorten_list(value: Any, max_items: int, item_limit: int) -> list[str]:
    """截短列表字段，保留最重要的前几条。移植自 analyse_node._shorten_list。"""

    if not isinstance(value, list):
        return []
    return [_shorten(item, item_limit) for item in value[:max_items] if str(item or "").strip()]


def _list_value(value: Any) -> list[Any]:
    """把模型返回的列表字段整理成列表。移植自 analyse_node._list_value。"""

    return value if isinstance(value, list) else []


def _string_list(value: Any) -> list[str]:
    """只保留列表中的非空文本。移植自 analyse_node._string_list。"""

    return [str(item).strip() for item in _list_value(value) if str(item).strip()]
