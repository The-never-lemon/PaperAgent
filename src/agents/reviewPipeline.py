"""综述子 Agent（作为主对话的一个工具来用）。

主 Agent 通过 generate_review 把写综述这件事交到这里。本模块按固定顺序往下走：
准备工作区论文 → 分析论文集 → 做领域综合 → 拟大纲 → 一节一节写正文 → 写摘要
→ 整理参考文献 → 拼成 Markdown 存下来 → 推出综述卡片。

分析、大纲、写作是本模块调用的普通函数，不是再嵌进去的子 Agent。
小节内部用普通循环取证和审查，不另存检查点。

流程用一张流程图串起来：每个阶段是图上的一个节点，节点与节点之间的边界
就是「这一步已经做完了」。接上检查点以后，可以从最后一个做完的阶段接着写，
不用从头重来。已经写好的小节不用重写，正在写的那一节要重做。

其中「逐节写作」用一条指回自己的边表示：写完一节就回到同一个节点写下一节，
直到大纲里的小节都写完，再去写摘要。

每做完一个阶段，图的状态会写进会话目录下的 checkpoints.db。用户点了停止，
或者进程中途停了，可以用 resume_review 接着写。

每个节点都要用的运行期对象（工作区、仓储、进度上报、模型、取消控制）
没法写进检查点，所以用 ReviewDeps 传进去。图状态里只放能存下来的
普通数据：文本、数字、字典和列表。

节点名字和状态字段不要改。进行中的综述是靠这些名字接着写的。
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import NodeCancelledError
from langgraph.graph import END, START, StateGraph

from src.llm.config import SystemConfig
from src.models.sessions import utc_now
from src.utils import get_logger

from .contracts import JsonObject
from .review_analyse import analyse_overall, analyse_subtopic
from .review_document import (
    _build_final_markdown,
    _build_references,
    _extract_paper_ids_from_sections,
    _has_reference_metadata,
    _remove_unknown_citation_markers,
    _remove_unknown_paper_citations,
    _replace_citation_numbers,
    _replace_section_citations,
)
from .review_outline import OVERALL_ANALYSIS_FIELDS, generate_outline
from .review_writing import write_abstract, write_section


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
# 图状态、内部异常与用量累加器
# ---------------------------------------------------------------------------


class ReviewState(TypedDict, total=False):
    """综述流程图的状态。

    中文说明：这里只放"能存下来"的普通数据（文本、数字、dict、list）。
    工作区、仓储、事件上报器、模型快照这些对象一律不放——它们没法保存，
    放进状态里以后接检查点会直接失败。它们改由闭包从 ReviewDeps 传入。

    另外，各个字段都是"全量覆盖"语义（total=False 表示都可以缺省），
    没有用 LangGraph 那种"自动累加"的写法。原因是节点万一重跑，自动累加
    会把同一份东西加两遍；而每个节点自己读旧值、算好新值再写回去，重复
    执行结果一样，是安全的。
    """

    # ---- 输入：进入图之前就定下来 ----
    topic: str
    paper_ids: list[str]

    # ---- prepare 阶段的产出 ----
    resolved_paper_ids: list[str]
    analysis_inputs: list[JsonObject]
    group: JsonObject
    available_paper_ids: list[str]
    cache_dir: str

    # ---- 各阶段产物 ----
    subtopic_analysis: JsonObject
    overall_analysis: JsonObject
    analysis_report: JsonObject
    outline: JsonObject
    section_tasks: list[JsonObject]
    written_sections: list[JsonObject]
    abstract: str
    abstract_status: str
    references: list[JsonObject]
    markdown: str
    word_count: int
    sections_view: list[JsonObject]
    artifact_id: str

    # ---- token 用量累计（整个综述过程所有模型调用的总和）----
    total_input_tokens: int
    total_output_tokens: int


class _ReviewFailed(Exception):
    """综述流程里"可以预料"的失败（例如模型没返回能解析的 JSON）。

    中文说明：节点里遇到这类问题就抛这个异常，由最外层的 run_review 统一
    折成 {status:"failed", reason} 返回。这样每个节点就不用写一套"失败了
    怎么跳到结尾"的边，图上的边全都保持简单。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _UsageCollector:
    """累计一次节点里所有模型调用的 token 用量。

    中文说明：模型调用完会把用量回调进来，先记在这个小盒子里，等节点返回时
    再一次性并进状态里的累计值。
    """

    __slots__ = ("input_tokens", "output_tokens")

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    def collect(self, usage: JsonObject | None) -> None:
        """收到一次模型调用的用量就记一笔。"""

        if not usage:
            return
        self.input_tokens += int(usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or 0)


def _usage_update(state: ReviewState, usage: _UsageCollector) -> JsonObject:
    """把本节点的用量并进状态里的累计值，返回要写回状态的字段。"""

    return {
        "total_input_tokens": int(state.get("total_input_tokens") or 0) + usage.input_tokens,
        "total_output_tokens": int(state.get("total_output_tokens") or 0) + usage.output_tokens,
    }


# ---------------------------------------------------------------------------
# 综述主入口
# ---------------------------------------------------------------------------


async def run_review(*, topic: str, paper_ids: list[str] | None, deps: ReviewDeps) -> JsonObject:
    """综述主入口：新开一篇综述。

    成功时返回：
        {"status":"ok","word_count":int,
         "sections":[{"section_id","title"}],"artifact_id":str}
    失败时返回：
        {"status":"failed","reason":str}
        —— 内部任何业务异常都折成这个结构，绝不上抛；
          唯一例外 asyncio.CancelledError 原样上抛。

    中文说明：每做完一个阶段都会往检查点库里记一笔。万一进程挂了、或者用户中途
    点了停止，可以用 resume_review 从最后一个做完的阶段接着写，不用从头重来。
    """

    return await _run_graph_safely(
        lambda: _start_review(topic=topic, paper_ids=paper_ids, deps=deps),
        deps=deps,
        thread_id=_review_thread_id(deps),
        failure_prefix="综述过程出现意外错误",
    )


async def resume_review(*, thread_id: str, deps: ReviewDeps) -> JsonObject:
    """接着写一篇没写完的综述：从检查点里最后一个做完的阶段继续。

    返回的结构和 run_review 完全一样，方便调用方走同一套处理。

    中文说明：thread_id 是这篇综述在检查点库里的编号（形如"回合编号:卡片编号"），
    可以从上一张失败的综述卡片上原样取回来。
    """

    return await _run_graph_safely(
        lambda: _continue_review(thread_id=thread_id, deps=deps),
        deps=deps,
        thread_id=thread_id,
        failure_prefix="继续综述失败",
    )


async def _run_graph_safely(
    run_graph: Any,
    *,
    deps: ReviewDeps,
    thread_id: str,
    failure_prefix: str,
) -> JsonObject:
    """跑一次图，把所有异常折成统一的 {status, reason} 返回。

    中文说明：新开一篇和接着写一篇这两个入口都走这里，保证它们对"用户取消"和
    "业务失败"的处理完全一致，不会一个漏了某种情况。
    """

    # 用一层 try/except 把所有业务异常都折成结构化返回。
    # asyncio.CancelledError 继承自 BaseException，不会被这里捕获，会原样上抛。
    try:
        return await run_graph()
    except asyncio.CancelledError:
        # 中文注释：用户点了停止。检查点里已经记着做到哪一步了，所以这一刻同样是
        # "可以接着往下写"的，补一条带编号的进度让前端那张卡片上有"继续"可点。
        _mark_resumable(deps, thread_id)
        raise
    except NodeCancelledError as exc:
        # 中文说明：用户点"停止"时，节点开头的取消检查会抛 asyncio.CancelledError。
        # LangGraph 会把节点里抛出的这个取消异常换成它自己的 NodeCancelledError，
        # 而后者是个普通 Exception——不转回来的话，会被下面的 except Exception 当成
        # "意外错误"，前端看到的就不是"已取消"，而是一句看不懂的报错。这里转回标准
        # 的取消异常，让取消一路传到最上层。
        _mark_resumable(deps, thread_id)
        raise asyncio.CancelledError() from exc
    except _ReviewFailed as exc:
        # 流程内部预料之中的失败（比如模型没返回能解析的 JSON），原因已经说清楚了。
        # 这时候图的检查点还在，所以把编号一起发出去，让前端给用户一个"继续"。
        return _fail(deps, exc.reason, resume_thread_id=thread_id)
    except Exception as exc:
        logger.warning(failure_prefix, extra={"error": str(exc)[:200]})
        return _fail(deps, f"{failure_prefix}：{exc}", resume_thread_id=thread_id)


def _mark_resumable(deps: ReviewDeps, thread_id: str) -> None:
    """把"这次停下来了，但还能接着写"这件事告诉前端。

    中文说明：用进度事件而不是失败事件，是因为用户点停止不是出错，卡片不该变成
    红色"失败"。状态写成 cancelled，卡片会显示"已停止"，同时把续跑编号带上，
    前端看到编号就知道可以在那张卡片上放一个"继续"。
    """

    deps.reporter.progress(
        "已停止，可以接着写",
        stage=REVIEW_STAGE,
        event_key=deps.event_key,
        runtime_status="cancelled",
        resume_thread_id=thread_id,
    )


# ---------------------------------------------------------------------------
# 检查点：这篇综述跑到哪一步了、崩了以后从哪接着跑
# ---------------------------------------------------------------------------


def _review_thread_id(deps: ReviewDeps) -> str:
    """这篇综述在检查点库里的编号。

    中文说明：同一个会话可能生成很多次综述，所以不能只拿会话编号当 key，否则
    第二次会续到第一次的旧图上去。这里用"回合编号 + 本次工具调用的卡片编号"
    拼出来——这两个值本来就已经存在数据里了，续跑时才拼得回同一个编号。
    """

    return f"{deps.turn_id}:{deps.event_key}"


def _review_checkpoint_path(deps: ReviewDeps) -> Path:
    """检查点数据库放在哪：会话自己目录下的 checkpoints.db。

    中文说明：故意放在会话目录里，这样删会话时文件跟着目录一起被删掉，不用另写
    清理逻辑；也避免和业务库 data/session_store.db 去抢同一把写锁。
    """

    sessions_dir = getattr(getattr(deps.repo, "backend", None), "sessions_dir", None)
    return Path(sessions_dir or "data/sessions") / deps.session_key / "checkpoints.db"


def _open_review_saver(deps: ReviewDeps):
    """打开这个会话的检查点数据库。

    中文说明：返回的是一个异步上下文管理器，要用 async with 包起来用，连接才会
    在用完之后关掉——直接返回一个已经打开的连接容易忘记关。
    """

    db_path = _review_checkpoint_path(deps)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return AsyncSqliteSaver.from_conn_string(str(db_path))


async def _start_review(*, topic: str, paper_ids: list[str] | None, deps: ReviewDeps) -> JsonObject:
    """新开一篇综述：先做几项毫秒级检查，然后带着检查点从第一步跑到最后一步。"""

    _check_cancellation(deps)
    if deps.llm is None:
        return _fail(deps, "模型未装配，无法生成综述")
    topic_text = str(topic or "").strip()
    if not topic_text:
        return _fail(deps, "缺少综述主题，请先明确研究主题或传入 topic")

    async with _open_review_saver(deps) as saver:
        compiled = _build_review_graph(deps).compile(checkpointer=saver, name="review_pipeline")
        final_state = await compiled.ainvoke(
            {"topic": topic_text, "paper_ids": list(paper_ids or [])},
            config={"configurable": {"thread_id": _review_thread_id(deps)}},
        )
    return _to_result(final_state)


async def _continue_review(*, thread_id: str, deps: ReviewDeps) -> JsonObject:
    """接着写：先确认检查点里确实还有没做完的活，再从那里往下跑。"""

    _check_cancellation(deps)
    if deps.llm is None:
        return _fail(deps, "模型未装配，无法生成综述")

    async with _open_review_saver(deps) as saver:
        compiled = _build_review_graph(deps).compile(checkpointer=saver, name="review_pipeline")
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = await compiled.aget_state(config)
        # 中文注释：编号拼错、或者记录被清掉了，读出来就是空的。这时候如果直接往下
        # 跑，图会当成全新的一篇从第一步开始，写出来的东西肯定不对，所以宁可明确报错。
        if not snapshot.values:
            return _fail(deps, "找不到可以继续的综述记录，可能已经完成或已被清理")
        if not snapshot.next:
            return _fail(deps, "这篇综述已经写完了，不需要继续")
        # 输入传 None 表示"不要从头开始，读检查点接着跑"。
        final_state = await compiled.ainvoke(None, config)
    return _to_result(final_state)


# ---------------------------------------------------------------------------
# 图结构：有哪些节点、节点之间怎么连
# ---------------------------------------------------------------------------


def _bind_node(node_fn: Any, deps: ReviewDeps):
    """把 deps 绑到节点函数上，包出一个只吃 state 的异步函数给图用。

    中文说明：这里不能图省事写 lambda。LangGraph 要靠"函数是不是 async def"
    来判断返回值到底该当成状态更新、还是当成一个还没跑完的协程；lambda 看不出
    是不是异步，它会把协程对象直接当成状态更新，然后报 "Expected dict, got
    coroutine"。所以老老实实定义一个 async def 再传进去。
    """

    async def runner(state: ReviewState) -> JsonObject:
        return await node_fn(state, deps)

    # 保留原函数名，出错时日志里看到的就是 prepare / write_section 这种可读名字。
    runner.__name__ = getattr(node_fn, "__name__", "review_node")
    return runner


def _build_review_graph(deps: ReviewDeps):
    """把综述的各个阶段拼成一张流程图。"""

    workflow = StateGraph(ReviewState)
    workflow.add_node("prepare", _bind_node(_node_prepare, deps))
    workflow.add_node("analyse_subtopic", _bind_node(_node_analyse_subtopic, deps))
    workflow.add_node("analyse_overall", _bind_node(_node_analyse_overall, deps))
    workflow.add_node("build_outline", _bind_node(_node_build_outline, deps))
    workflow.add_node("write_section", _bind_node(_node_write_section, deps))
    workflow.add_node("write_abstract", _bind_node(_node_write_abstract, deps))
    workflow.add_node("compile_document", _bind_node(_node_compile_document, deps))
    workflow.add_node("finalize", _bind_node(_node_finalize, deps))

    workflow.add_edge(START, "prepare")
    workflow.add_edge("prepare", "analyse_subtopic")
    workflow.add_edge("analyse_subtopic", "analyse_overall")
    workflow.add_edge("analyse_overall", "build_outline")
    workflow.add_edge("build_outline", "write_section")
    # 写完一节回到自己身上继续写下一节，全部写完就去写摘要。
    workflow.add_conditional_edges(
        "write_section",
        _route_next_section,
        {"write_section": "write_section", "write_abstract": "write_abstract"},
    )
    workflow.add_edge("write_abstract", "compile_document")
    workflow.add_edge("compile_document", "finalize")
    workflow.add_edge("finalize", END)
    # 中文说明：这里只把图的形状拼好，不编译。因为编译时要挂上检查点数据库，
    # 而检查点连接是有生命周期的（用完要关），由调用方在 async with 里编译更合适。
    return workflow


def _route_next_section(state: ReviewState) -> str:
    """写完一节以后去哪：还有没写的小节就继续写，都写完了就去写摘要。"""

    written = len(state.get("written_sections") or [])
    total = len(state.get("section_tasks") or [])
    return "write_section" if written < total else "write_abstract"


def _to_result(state: ReviewState) -> JsonObject:
    """把图跑完之后的最终状态，折成调用方（工具 handler）要的结果形状。"""

    return {
        "status": "ok",
        "word_count": int(state.get("word_count") or 0),
        "sections": list(state.get("sections_view") or []),
        "artifact_id": str(state.get("artifact_id") or ""),
    }


# ---------------------------------------------------------------------------
# 各节点实现（按流程图里的先后顺序排列）
# ---------------------------------------------------------------------------


async def _node_prepare(state: ReviewState, deps: ReviewDeps) -> JsonObject:
    """准备阶段：把工作区里的论文整理成分析模型需要的结构化摘要。"""

    _check_cancellation(deps)
    # 筛选论文：调用方指定了编号就只取这些（缺失的忽略并计数），否则取全部。
    entries = _collect_paper_entries(deps.workspace, list(state.get("paper_ids") or []))
    if not entries:
        raise _ReviewFailed("工作区里没有可用于综述的论文，请先检索或加入论文")

    deps.reporter.progress(
        f"准备综述 {len(entries)} 篇论文",
        stage=REVIEW_STAGE,
        event_key=deps.event_key,
    )

    # 每篇论文整理成分析模型需要的结构化摘要。
    analysis_inputs: list[JsonObject] = [_paper_analysis_input(paper_id, entry) for paper_id, entry in entries]
    # 单组：新架构没有检索子主题元数据，全量一组做横向比较。
    group: JsonObject = {
        "subtopic": str(state.get("topic") or ""),
        "search_keyword": "",
        "papers": analysis_inputs,
        "paper_count": len(analysis_inputs),
    }
    return {
        # 论文集合固定在准备阶段。综述跑的时候主 Agent 可能同时在精读别的论文，
        # 工作区随时会被改；把编号在这里定下来，后面的小节才不会看到不同的论文集。
        "resolved_paper_ids": [paper_id for paper_id, _entry in entries],
        "analysis_inputs": analysis_inputs,
        "group": group,
        "available_paper_ids": [
            str(item["paperId"]) for item in analysis_inputs if str(item.get("paperId") or "").strip()
        ],
        "cache_dir": SystemConfig.load().read.paper_cache_dir,
    }

async def _node_analyse_subtopic(state: ReviewState, deps: ReviewDeps) -> JsonObject:
    """子主题分析：让模型读一遍论文集，产出研究现状、共识、争议、空白等。"""

    _check_cancellation(deps)
    deps.reporter.progress("正在分析论文", stage=REVIEW_STAGE, event_key=deps.event_key)

    group = dict(state.get("group") or {})
    usage = _UsageCollector()
    subtopic_result = await analyse_subtopic(
        topic=str(state.get("topic") or ""),
        group=group,
        llm=deps.llm,
        usage_callback=usage.collect,
    )
    if subtopic_result.parsed is None:
        raise _ReviewFailed(f"子主题分析失败：{subtopic_result.reason}")
    return {
        "subtopic_analysis": _normalize_subtopic_analysis(subtopic_result.parsed, group),
        **_usage_update(state, usage),
    }


async def _node_analyse_overall(state: ReviewState, deps: ReviewDeps) -> JsonObject:
    """全局综合分析：在子主题分析的基础上，归纳整个研究领域的八个方面。"""

    _check_cancellation(deps)
    deps.reporter.progress("正在综合分析", stage=REVIEW_STAGE, event_key=deps.event_key)

    usage = _UsageCollector()
    # 中文说明：模型调用本身的重试（网络抖动、限流、输出被截断）已经在分析函数
    # 内部处理过了，这里不再套一层重试循环——套了就是同一件事做两遍。走到这一步还
    # 是没解析出来，说明真的写不动了，直接把模型自己给的原因报上去，别再用一句
    # 笼统的"未返回合法 JSON"盖住具体原因。
    overall_result = await analyse_overall(
        topic=str(state.get("topic") or ""),
        subtopic_analyses=[dict(state.get("subtopic_analysis") or {})],
        llm=deps.llm,
        usage_callback=usage.collect,
    )
    if overall_result.parsed is None:
        raise _ReviewFailed(f"全局综合分析失败：{overall_result.reason}")
    return {
        "overall_analysis": _normalize_overall_analysis(
            overall_result.parsed,
            [dict(state.get("subtopic_analysis") or {})],
        ),
        **_usage_update(state, usage),
    }


async def _node_build_outline(state: ReviewState, deps: ReviewDeps) -> JsonObject:
    """先组装分析报告，再据此生成写作大纲，最后把大纲拍平成一个个小节任务。"""

    _check_cancellation(deps)
    topic_text = str(state.get("topic") or "")
    subtopic_analysis = dict(state.get("subtopic_analysis") or {})
    # 组装分析报告（参照 analyse_node._build_final_report）。
    analysis_report = _build_analysis_report(
        topic=topic_text,
        subtopic_analyses=[subtopic_analysis],
        overall_analysis=dict(state.get("overall_analysis") or {}),
        model_used=deps.llm.model if deps.llm else "unavailable",
    )

    deps.reporter.progress("正在生成写作大纲", stage=REVIEW_STAGE, event_key=deps.event_key)
    usage = _UsageCollector()
    outline, _raw, reason = await generate_outline(
        topic=topic_text,
        analysis_report=analysis_report,
        llm=deps.llm,
        usage_callback=usage.collect,
    )
    if outline is None:
        raise _ReviewFailed(f"写作大纲生成失败：{reason}")
    return {
        "analysis_report": analysis_report,
        "outline": outline,
        "section_tasks": _flatten_outline(outline),
        **_usage_update(state, usage),
    }

async def _node_write_section(state: ReviewState, deps: ReviewDeps) -> JsonObject:
    """写一个小节。

    中文说明：这个节点每次只写一节，写完由条件边决定是回到这里写下一节，
    还是去写摘要。之所以不写成 for 循环，是因为"写一节"就是一个步骤，
    步骤边界清楚，以后接上检查点就能精确知道写到了第几节。
    """

    _check_cancellation(deps)
    section_tasks = list(state.get("section_tasks") or [])
    written_sections = list(state.get("written_sections") or [])
    # 边界保护：大纲里一个小节都没有时，条件边仍会送进来一次，这里直接跳过。
    if len(written_sections) >= len(section_tasks):
        return {}

    index = len(written_sections) + 1
    section_task = section_tasks[index - 1]
    section_title = str(section_task.get("section_title") or section_task["section_id"])
    deps.reporter.progress(
        f"正在写作第 {index}/{len(section_tasks)} 节：{section_title}",
        stage=REVIEW_STAGE,
        event_key=deps.event_key,
    )
    # 按大纲 evidence-map 指定的字段从全局分析里取出证据。
    evidence = _resolve_section_evidence(
        evidence_fields=list(section_task.get("evidence_map") or []),
        overall_analysis=dict((state.get("analysis_report") or {}).get("overall_analysis") or {}),
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

    usage = _UsageCollector()

    def on_section_progress(message: str, section_usage: JsonObject | None = None) -> None:
        """写作小节内部回调：只收用量，内部进度消息不外发（进度由上面统一发）。"""

        usage.collect(section_usage)

    writing_result = await write_section(
        section_id=str(section_task["section_id"]),
        task=str(section_task.get("task") or ""),
        evidence_map=evidence,
        previous_sections=previous_sections,
        word_count=int(section_task.get("word_count") or DEFAULT_SECTION_WORD_COUNT),
        read_results=list(state.get("analysis_inputs") or []),
        cache_dir=str(state.get("cache_dir") or ""),
        available_paper_ids=list(state.get("available_paper_ids") or []),
        progress_callback=on_section_progress,
        llm=deps.llm,
    )
    # 补上章节信息，方便后面拼 Markdown 和提取引用。
    writing_result.update(
        chapter_key=section_task["chapter_key"],
        section_key=section_task["section_key"],
        chapter_title=str(section_task.get("chapter_title") or section_task["chapter_key"]),
        section_title=section_title,
    )
    return {
        "written_sections": [*written_sections, writing_result],
        **_usage_update(state, usage),
    }


async def _node_write_abstract(state: ReviewState, deps: ReviewDeps) -> JsonObject:
    """所有小节写完以后，根据正文生成摘要。"""

    _check_cancellation(deps)
    deps.reporter.progress("正在生成摘要", stage=REVIEW_STAGE, event_key=deps.event_key)

    usage = _UsageCollector()
    abstract, abstract_status = await write_abstract(
        topic=str(state.get("topic") or ""),
        sections=list(state.get("written_sections") or []),
        word_count=ABSTRACT_WORD_COUNT,
        usage_callback=usage.collect,
        llm=deps.llm,
    )
    return {
        "abstract": abstract,
        "abstract_status": abstract_status,
        **_usage_update(state, usage),
    }


async def _node_compile_document(state: ReviewState, deps: ReviewDeps) -> JsonObject:
    """整理参考文献、把正文里的 [paperId] 换成序号，并拼出终稿 Markdown。"""

    _check_cancellation(deps)
    deps.reporter.progress("正在整理参考文献", stage=REVIEW_STAGE, event_key=deps.event_key)

    written_sections = list(state.get("written_sections") or [])
    abstract = str(state.get("abstract") or "")
    topic_text = str(state.get("topic") or "")

    # 引用顺序以正文里 paperId 第一次出现的位置为准。
    candidate_paper_ids = _extract_paper_ids_from_sections(written_sections)
    # 中文注释：这里按准备阶段定下来的编号重新去工作区读一次论文资料。论文集合不变，
    # 但能拿到最新的题名等信息——综述跑的期间，别的工具可能刚好把元数据补全了。
    metadata_by_id = _build_workspace_metadata(
        deps.workspace,
        _collect_paper_entries(deps.workspace, list(state.get("resolved_paper_ids") or [])),
    )
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

    # 终稿 Markdown（移植 reply_node._build_final_markdown 的结构）。
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
    return {
        "written_sections": written_sections,
        "abstract": abstract,
        "references": references,
        "markdown": markdown,
        "word_count": word_count,
        "sections_view": sections_view,
    }


async def _node_finalize(state: ReviewState, deps: ReviewDeps) -> JsonObject:
    """收尾：保存产物、推 review 卡片、报出整个综述的 token 用量。

    中文说明：这三件事放在同一个节点里是有意的——只存了产物但没推卡片，前端
    就永远看不到这份综述；只推了卡片但没存产物，点下载会拿到空气。它们必须
    一起成功，或者一起失败。
    """

    _check_cancellation(deps)
    topic_text = str(state.get("topic") or "")
    written_sections = list(state.get("written_sections") or [])
    references = list(state.get("references") or [])
    sections_view = list(state.get("sections_view") or [])
    word_count = int(state.get("word_count") or 0)

    # 存 artifact（一律走 repo.write_artifact，禁止直接写文件）。
    deps.reporter.progress("正在保存综述产物", stage=REVIEW_STAGE, event_key=deps.event_key)
    record = await asyncio.to_thread(
        deps.repo.write_artifact,
        deps.session_key,
        ARTIFACT_TYPE_REVIEW,
        "literature_review.md",
        str(state.get("markdown") or ""),
        relative_path=f"artifacts/review/{deps.turn_id}/literature_review.md",
        metadata={
            "topic": topic_text,
            "format": "markdown",
            "section_count": len(written_sections),
            "reference_count": len(references),
            "word_count": word_count,
            "abstract_status": str(state.get("abstract_status") or ""),
        },
    )
    artifact_id = str(record.get("id") or "")

    # 推 review 卡片（role 用 system，不干扰助手消息缓冲区）。
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

    # token 用量聚合，推一条带用量的完成进度事件。
    total_input = int(state.get("total_input_tokens") or 0)
    total_output = int(state.get("total_output_tokens") or 0)
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
    return {"artifact_id": artifact_id}


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
        "研究现状": _text_with_citation(parsed.get("研究现状") or fallback["研究现状"]),
        "一致点": _text_list_with_citation(parsed.get("一致点")),
        "矛盾点": _text_with_citation(parsed.get("矛盾点") or fallback["矛盾点"]),
        "研究空白": _text_with_citation(parsed.get("研究空白") or fallback["研究空白"]),
        "时间线演化": _text_with_citation(parsed.get("时间线演化") or fallback["时间线演化"]),
        "技术方法栈演变": _text_with_citation(parsed.get("技术方法栈演变") or fallback["技术方法栈演变"]),
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
    """整理模型返回的八部分综合结果。空字段用兜底句子填上，不自动补引用。"""

    fallback = _fallback_overall_analysis("当前研究领域", subtopic_analyses, reason="")
    normalized: JsonObject = {
        field: _text_with_citation(parsed.get(field) or fallback[field])
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


def _text_with_citation(value: Any) -> str:
    """整理关键文本字段。空文本填一句占位，不自动贴论文编号。

    中文说明：引用必须由模型自己写。程序如果在缺引用时自动补上论文编号，
    后面生成参考文献时就会把没被论述过的论文写进去。
    """

    text = str(value or "").strip()
    if not text:
        text = "暂无稳定结论。"
    return text


def _text_list_with_citation(value: Any) -> list[str]:
    """整理一致点数组。每一条空文本同样只填占位，不自动补引用。"""

    return [_text_with_citation(item) for item in _string_list(value)]


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
        # 空字段不能支撑正文，因此不传给写作步骤。
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


# ---------------------------------------------------------------------------
# 内部小工具
# ---------------------------------------------------------------------------


def _check_cancellation(deps: ReviewDeps) -> None:
    """检查用户是否点了停止，点了就抛出取消异常让当前流程尽快退出。"""

    if deps.cancellation is not None:
        deps.cancellation.raise_if_requested()


def _fail(deps: ReviewDeps, reason: str, *, resume_thread_id: str = "") -> JsonObject:
    """统一处理综述失败：推 failed 卡片并返回结构化失败结果。

    中文说明：resume_thread_id 不为空，说明这次失败是在流程中间发生的——检查点里
    还留着"做到哪一步"的记录，前端可以据此在失败卡片上放一个"继续"按钮。开跑之前
    的检查没过（比如主题为空）就报失败的话，这里留空，因为确实没什么可继续的。
    """

    extra: JsonObject = {"resume_thread_id": resume_thread_id} if resume_thread_id else {}
    deps.reporter.failed(reason, stage=REVIEW_STAGE, event_key=deps.event_key, **extra)
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
