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
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

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
from src.services.paper_memory import record_fulltext_deep_read, resolve_paper_cache_dir
from src.utils import get_logger
from src.utils.llm_json import parse_llm_json
from src.utils.read_utils.chunkers import (
    CHUNKER_VERSION,
    PageChunker,
    async_build_chunks_file,
    load_chunks_file,
)
from src.utils.read_utils.figure_reader import collect_paper_figures, read_figure_notes
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
# 中文注释：替代按需拉取的更简方案——1M 窗口下直接放宽上限，绝大多数论文的
# 全部笔记都能给到汇总阶段，"[部分内容已省略]"只在极端长文的兜底里出现。
REDUCE_INPUT_MAX_CHARS = 150000

# 返回给工具调用方的 report_summary 截断长度。
# 中文注释：300 → 1500。主 Agent 的工具结果上限也已放宽到 20000 字符，
# 多给一些报告要点，主 Agent 转述/追问时不再只凭 300 字判断，减少
# "以为没读过又去 deep_read 一遍"的误判。
REPORT_SUMMARY_CHARS = 1500

# 精读进度事件的 stage 名，对应 runtime.py 里 ("tool","deep_read_paper") 映射。
DEEP_READ_STAGE = "deep_read_paper"

# 产物类型常量，写 artifact 时使用。
ARTIFACT_TYPE_REPORT = "deep_read_report"
ARTIFACT_TYPE_FULLTEXT = "paper_fulltext"
# 中文注释：论文配图也单独存成产物。这样全文 Markdown 里的图片引用才能指向一个
# 真能打开的地址（详见 _write_figure_artifacts）。
ARTIFACT_TYPE_FIGURE = "paper_figure"

# reduce 输入里论文摘要的截断长度。
REDUCE_ABSTRACT_CHARS = 400
# 中文注释：插图笔记整段最多这么多字符。正常情况下十几张图的解读加起来远不到，
# 设这条是为了防止某一篇图特别多时把提示词撑爆。
REDUCE_FIGURE_NOTES_MAX_CHARS = 6000

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


async def run_deep_read(*, paper_id: str, focus: str = "", force: bool = False, deps: DeepReadDeps) -> JsonObject:
    """精读主入口。

    成功时返回：
        {"paper_id", "source": "fulltext"|"abstract_fallback",
         "report_summary"(short_summary 截 300 字), "artifact_id", "cached": bool}
    失败时返回：
        {"status": "failed", "reason": str}
        —— 内部任何业务异常都折叠成这个结构，绝不上抛；
          唯一例外 asyncio.CancelledError 原样上抛。

    force=True 时会清掉已有报告，用本地已经切好的正文片段重新读一遍，
    不再要求用户先从工作区删掉这篇论文。
    """

    # 中文注释：用一层 try/except 把所有业务异常都折成结构化返回。
    # asyncio.CancelledError 继承自 BaseException，不会被这里捕获，会原样上抛。
    try:
        return await _run_deep_read_impl(paper_id=paper_id, focus=focus, force=force, deps=deps)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "精读过程出现意外错误",
            extra={"paper_id": paper_id, "error": str(exc)[:200]},
        )
        return _fail(deps, f"精读过程出现意外错误：{exc}")


async def _run_deep_read_impl(*, paper_id: str, focus: str, force: bool, deps: DeepReadDeps) -> JsonObject:
    """精读主流程实现（由 run_deep_read 包裹异常折叠）。"""

    # 第一步：按编号取出论文，工作区里没有就直接报失败。
    _check_cancellation(deps)
    entry = deps.workspace.get_paper(paper_id)
    if entry is None:
        return _fail(deps, f"工作区里没有这篇论文：{paper_id}")

    # 第二步：已有精读报告时，普通精读直接返回；强制重读则只清掉旧报告，论文留在工作区。
    if entry.deep_read is not None and not force:
        deps.reporter.progress(
            "命中已有精读报告，直接返回",
            stage=DEEP_READ_STAGE,
            event_key=deps.event_key,
        )
        cached_source = entry.deep_read.source
        cached_fulltext = cached_source == DEEP_READ_SOURCE_FULLTEXT
        return {
            "paper_id": paper_id,
            "source": cached_source,
            "fulltext_available": cached_fulltext,
            "notice": _fulltext_notice(cached_fulltext, ""),
            "report_summary": entry.deep_read.short_summary[:REPORT_SUMMARY_CHARS],
            "artifact_id": entry.deep_read.artifact_id,
            "cached": True,
        }
    if force and entry.deep_read is not None:
        await asyncio.to_thread(deps.workspace.clear_deep_read, paper_id)
        deps.reporter.progress(
            "已去掉旧报告，准备重新精读",
            stage=DEEP_READ_STAGE,
            event_key=deps.event_key,
        )

    # 第三步：模型没装配就没法精读。
    if deps.llm is None:
        return _fail(deps, "模型未装配，无法精读")

    # 第四步：把工作区里的论文元数据字典还原成 PaperDocument 对象。
    doc = _paper_document(entry.paper)
    read_cfg = SystemConfig.load().read

    # token 用量累加器：map + reduce（或降级）所有响应的 usage 统一累加。
    total_input = 0
    total_output = 0
    # 中文注释：全文没拿到时把原因记在这里，最后要如实告诉用户"这篇论文下载不了"，
    # 不能让用户以为报告是读了全文写出来的。
    fulltext_failure_reason = ""

    # 尝试全文路径：能复用本地切片就跳过下载和再切分；否则下载 → 转换 → 分块。
    # 任何一步失败就降级到摘要精读。map/reduce 失败属于硬失败，直接返回 failed。
    source = DEEP_READ_SOURCE_ABSTRACT
    markdown_text: str | None = None
    payload: JsonObject | None = None
    chunks: list[Any] | None = None
    assets_dir: Path | None = None

    reused = await asyncio.to_thread(_load_existing_slices, doc, read_cfg.paper_cache_dir)
    if reused is not None:
        markdown_text, chunks, assets_dir = reused
        deps.reporter.progress(
            "正在读取已切好的正文片段",
            stage=DEEP_READ_STAGE,
            event_key=deps.event_key,
        )
        logger.info(
            "精读复用本地已切好的正文片段",
            extra={"session_key": deps.session_key, "paper_id": paper_id, "chunk_count": len(chunks)},
        )
    else:
        # 第五步：本地没有可用切片，才去下载全文。
        _check_cancellation(deps)
        deps.reporter.progress("正在下载论文全文", stage=DEEP_READ_STAGE, event_key=deps.event_key)
        logger.info("精读开始下载全文", extra={"session_key": deps.session_key, "paper_id": paper_id})
        downloaded = await async_download_paper_fulltext(
            doc,
            cache_dir=read_cfg.paper_cache_dir,
            connect_timeout_seconds=read_cfg.connect_timeout_seconds,
            download_timeout_seconds=read_cfg.download_timeout_seconds,
            max_file_size_mb=read_cfg.max_file_size_mb,
            runtime_resources=deps.resources,
        )

        if downloaded.status == "downloaded":
            # 第六步：转 Markdown + 分块。
            _check_cancellation(deps)
            deps.reporter.progress("正在转换全文为 Markdown", stage=DEEP_READ_STAGE, event_key=deps.event_key)
            conversion = await async_convert_fulltext_to_markdown(
                doc,
                source_path=downloaded.file_path,
                source_url=downloaded.source_url,
                # 中文注释：把精读用的模型交给转换那一层，让它把公式截图转成 LaTeX。
                # 不传也能跑，只是公式会退回原来的展平写法。
                llm=deps.llm,
                on_progress=lambda message: deps.reporter.progress(
                    message, stage=DEEP_READ_STAGE, event_key=deps.event_key
                ),
                raise_if_cancelled=deps.cancellation.raise_if_requested if deps.cancellation else None,
            )
            # 中文注释：公式转写也是真金白银的模型调用，用掉的 token 要并进整篇精读的
            # 用量里一起上报，否则后台看到的用量会比实际花的少一截。
            total_input += conversion.input_tokens
            total_output += conversion.output_tokens
            if conversion.markdown_path is not None:
                _check_cancellation(deps)
                deps.reporter.progress("正在切分全文", stage=DEEP_READ_STAGE, event_key=deps.event_key)
                chunk_result = await async_build_chunks_file(doc, markdown_path=conversion.markdown_path)
                if chunk_result.chunks:
                    markdown_text = await asyncio.to_thread(
                        conversion.markdown_path.read_text, encoding="utf-8"
                    )
                    chunks = chunk_result.chunks
                    assets_dir = conversion.assets_dir
                else:
                    # 分块为空，降级摘要。
                    fulltext_failure_reason = "全文分块为空"
                    deps.reporter.progress(
                        "全文分块为空，改用摘要精读",
                        stage=DEEP_READ_STAGE,
                        event_key=deps.event_key,
                    )
            else:
                # 转换失败，降级摘要。
                fulltext_failure_reason = "全文转换失败"
                deps.reporter.progress(
                    "全文转换失败，改用摘要精读",
                    stage=DEEP_READ_STAGE,
                    event_key=deps.event_key,
                )
        else:
            # 下载失败，降级摘要。
            # 中文注释：这里的原因来自下载层（比如"未提供开放获取链接""下载全文超时"），
            # 会原样带给用户，让用户知道到底是没链接还是链接打不开。
            fulltext_failure_reason = downloaded.reason or "未能获取全文"
            deps.reporter.progress(
                f"全文获取失败（{downloaded.reason}），改用摘要精读",
                stage=DEEP_READ_STAGE,
                event_key=deps.event_key,
            )

    if chunks and markdown_text is not None:
        # 第七步：让模型把论文插图读一遍。
        # 中文注释：这一步和分段阅读是两条独立的线——插图不走切分片段，而是自己
        # 成批发给模型，读出来的笔记最后并在汇总阶段。这样图片内容既不受片段大小
        # 限制，也不会被"每段笔记最多 500 字"那条上限压掉。
        _check_cancellation(deps)
        figure_notes, figure_input, figure_output = await read_figure_notes(
            figures=collect_paper_figures(markdown_text, assets_dir),
            focus=focus,
            llm=deps.llm,
            on_progress=lambda message: deps.reporter.progress(
                message, stage=DEEP_READ_STAGE, event_key=deps.event_key
            ),
            raise_if_cancelled=deps.cancellation.raise_if_requested if deps.cancellation else None,
        )
        total_input += figure_input
        total_output += figure_output

        # 第八步：map 逐块精读。
        _check_cancellation(deps)
        notes, map_input, map_output = await _map_chunks(deps, doc, chunks, focus)
        total_input += map_input
        total_output += map_output
        # 全部片段都精读失败 → 硬失败，不降级。
        if not any(notes):
            return _fail(deps, "全部正文片段精读失败，无法汇总报告")

        # 第九步：reduce 汇总。
        _check_cancellation(deps)
        deps.reporter.progress(
            "正在汇总精读报告",
            stage=DEEP_READ_STAGE,
            event_key=deps.event_key,
        )
        reduce_content = _build_reduce_user_content(doc, notes, chunks, figure_notes)
        payload, reduce_input, reduce_output = await _run_reduce(deps, reduce_content)
        total_input += reduce_input
        total_output += reduce_output
        # reduce 解析失败 → 硬失败，不降级。
        if payload is None:
            return _fail(deps, "精读报告解析失败，无法生成报告")

        source = DEEP_READ_SOURCE_FULLTEXT

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
        # 中文注释：全文 Markdown 里的图片引用是 "assets/xxx.png" 这种相对地址，而图片文件
        # 躺在论文缓存目录里。产物只存 Markdown 文本，图片不跟着走，相对地址就会指向一个
        # 不存在的目录，用户打开产物看到满屏裂图。所以这里先把配图也存成产物，并把引用
        # 换成能打开的接口地址，再写全文产物。
        markdown_text = await _write_figure_artifacts(deps, paper_id, assets_dir, markdown_text)
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
        # 中文注释：全文精读成功后记入本机长期记忆。之后任意会话再检索到
        # 同一篇论文，就能把这份报告和已经切好的分片召回来，不必再精读一遍。
        await asyncio.to_thread(record_fulltext_deep_read, doc, report)

    # 第十二步：推 deep_read_report 卡片（role 用 system，不干扰助手消息缓冲区）。
    title_preview = (doc.title or "")[:CARD_TITLE_CHARS]
    # 中文注释：全文没拿到时，卡片标题必须如实写明"无法下载全文"。
    # 否则用户看到的报告和正常精读长得一模一样，会误以为全文读过。
    fulltext_available = source == DEEP_READ_SOURCE_FULLTEXT
    if fulltext_available:
        card_title = f"《{title_preview}》精读完成"
    else:
        card_title = f"《{title_preview}》无法下载全文，报告基于摘要生成"
    deps.reporter.message(
        role="system",
        content=card_title,
        metadata=report.to_card_payload(
            source=source,
            fulltext_available=fulltext_available,
            fulltext_failure_reason=fulltext_failure_reason,
            artifact_id=report.artifact_id,
        ),
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
        "fulltext_available": fulltext_available,
        "notice": _fulltext_notice(fulltext_available, fulltext_failure_reason),
        "report_summary": report.short_summary[:REPORT_SUMMARY_CHARS],
        "artifact_id": report.artifact_id,
        "cached": False,
    }


# ---------------------------------------------------------------------------
# map 阶段：并发逐块精读
# ---------------------------------------------------------------------------


async def _write_figure_artifacts(
    deps: DeepReadDeps,
    paper_id: str,
    assets_dir: "Path | None",
    markdown_text: str,
) -> str:
    """把论文配图也存成产物，并把正文里的图片引用改成能打开的地址。

    中文注释：转换出来的 paper.md 里，图片写的是 "![Figure 1](assets/fig_p1_1.png)"
    这种相对地址，图片文件本身躺在论文缓存目录里。产物只把 Markdown 文本单独存一份，
    图片不跟着走，相对地址就指向一个不存在的目录 —— 用户打开产物看到的是满屏裂图。
    改造前正文里根本没有图片引用，所以这个问题看不出来；现在有了图表提取就必须处理。

    做法是：每张图也当成一件产物存进会话目录（各拿到自己的编号），然后把正文里的相对
    地址换成"按编号取这张图"的接口地址。这样复用已有的产物下载接口，不必新增路由。
    同一张图可能被多页引用，但替换是按文件名做的，一次替换就能覆盖所有出现的位置。

    拿不到图片目录时（比如老缓存的目录被清理掉了），原样返回、不做任何替换。
    """

    if assets_dir is None or not assets_dir.is_dir():
        return markdown_text
    for figure_path in sorted(assets_dir.iterdir()):
        if not figure_path.is_file():
            continue
        try:
            payload = await asyncio.to_thread(figure_path.read_bytes)
            record = await asyncio.to_thread(
                deps.repo.write_artifact,
                deps.session_key,
                ARTIFACT_TYPE_FIGURE,
                figure_path.name,
                payload,
                relative_path=f"artifacts/assets/{figure_path.name}",
                metadata={"paper_id": paper_id},
            )
        except Exception as exc:
            # 中文注释：单张图写不进去，不该把整篇精读拖垮 —— 那张图的地址会保留原样、
            # 显示成裂图，但报告和其余图片都还在。这里故意捕得宽一些：写产物可能因为
            # 磁盘满、路径非法、会话目录不见了等各种原因失败，而它们都不值得让整份报告作废。
            # 记一条日志，方便事后发现。
            logger.warning(
                "论文配图写入产物失败，正文里会保留这张图的相对地址",
                extra={"paper_id": paper_id, "figure": figure_path.name, "reason": str(exc)},
            )
            continue
        # 中文注释：地址形状要跟前端取产物的方式对齐（见前端 DeepReadDrawer 里
        # /api/sessions/{会话}/artifacts/{产物编号}），否则拼出来的地址照样打不开。
        figure_url = f"/api/sessions/{quote(deps.session_key, safe='')}/artifacts/{record.get('id') or ''}"
        markdown_text = markdown_text.replace(f"](assets/{figure_path.name})", f"]({figure_url})")
    return markdown_text


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
                # 中文注释：这里的截断是按字数位置硬切的，模型自己看不到这把刀，所以
                # 提示词里"装不下时先保方法与实验数据"那句取舍根本无从执行——而表格片段
                # 恰恰是数值最密、被切掉最多的那种。所以切了必须留痕：补一句标记，
                # 让汇总阶段和读报告的人知道这段笔记不完整，而不是把半截笔记当完整结论用。
                # 写法和 _build_reduce_user_content 里已有的「[部分内容已省略]」保持一致。
                note = note[:MAP_NOTE_MAX_CHARS] + "\n[本段笔记超长已截断]"
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
    figure_notes: str = "",
) -> str:
    """组装 reduce 阶段的用户消息：论文元数据 + 分段笔记 + 插图笔记。

    论文元数据包含标题、摘要（截 400 字）、作者。
    分段笔记拼成"笔记 i (chunk_id): 笔记内容"列表。
    插图笔记是模型读图得到的一段文字，没有图时为空、那一项就不出现。
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
    figures = (figure_notes or "")[:REDUCE_FIGURE_NOTES_MAX_CHARS]

    def _serialize(lines: list[str], with_figures: bool = True) -> str:
        """把元数据、笔记行和插图笔记序列化成 JSON 字符串。"""

        payload: JsonObject = {"论文元数据": metadata, "分段笔记": lines}
        if with_figures and figures:
            payload["插图笔记"] = figures
        return json.dumps(payload, ensure_ascii=False)

    text = _serialize(note_lines)
    # 总长没超限制，直接返回。
    if len(text) <= REDUCE_INPUT_MAX_CHARS:
        return text

    # 总长超限：按块序从后往前删除，直到总长不超过限制。
    while note_lines and len(_serialize(note_lines)) > REDUCE_INPUT_MAX_CHARS:
        note_lines.pop()
    # 中文注释：正文笔记砍光了还是超，说明插图笔记本身就太长，那就把它也去掉——
    # 宁可让模型少看到图，也不能让整段提示词超出模型的上下文。
    if len(_serialize(note_lines)) > REDUCE_INPUT_MAX_CHARS:
        return _serialize(note_lines, with_figures=False)
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


def _load_existing_slices(
    doc: PaperDocument,
    cache_base: str | Path,
) -> tuple[str, list[Any], Path | None] | None:
    """如果本地已经有切好的正文片段，就直接读出来，不必再下载、再转换、再切片。

    中文注释：重新精读时用户要的是「旧报告丢掉、正文片段还在、再读一遍」。
    磁盘上同时有 paper.md 和 chunk.json、并且切片规则版本还对得上，才算能复用。
    缺任何一个，返回空，让主流程走原来的下载和切分。
    """

    cache_dir = resolve_paper_cache_dir(cache_base, doc)
    markdown_path = cache_dir / "paper.md"
    chunks_path = cache_dir / "chunk.json"
    if not markdown_path.is_file() or not chunks_path.is_file():
        return None
    try:
        payload = json.loads(chunks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != CHUNKER_VERSION:
        return None
    chunks = load_chunks_file(chunks_path)
    if not chunks:
        return None
    if any(len(chunk.content) > PageChunker.max_atomic_characters for chunk in chunks):
        return None
    try:
        markdown_text = markdown_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not markdown_text.strip():
        return None
    assets = cache_dir / "assets"
    assets_dir = assets if assets.is_dir() else None
    return markdown_text, chunks, assets_dir


def _fulltext_notice(fulltext_available: bool, reason: str) -> str:
    """生成"全文到底有没有拿到"的提示语，交给主 Agent 转告用户。

    中文注释：主 Agent 看到这句话就知道该怎么跟用户说。全文拿到了就返回空字符串，
    主 Agent 按正常流程汇报即可；没拿到时把原因一并写清楚，
    让用户能分辨是"这篇论文本来就没有开放全文"还是"有链接但打不开"。
    """

    if fulltext_available:
        return ""
    if reason:
        return f"该论文无法下载全文（{reason}），本报告基于标题和摘要生成，未通读全文。"
    return "该论文无法下载全文，本报告基于标题和摘要生成，未通读全文。"


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
