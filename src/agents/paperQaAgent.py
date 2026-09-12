"""论文问答子 Agent（agent-as-tool 模式）。

这个模块是对话式调研"追问已精读论文"链路的执行核心。主 Agent 通过
ask_paper 工具把用户关于某篇论文的细节问题委派到这里，本模块负责：从工作区
加载精读报告 → 从会话产物加载论文全文（拿不到全文时退回摘要）→ 组装上下文
（超长截断）→ 单次模型调用（标注出处）→ 解析返回 {answer, source_sections}。

整个流程自包含，不依赖 research_tools / researchAgent / deepReadAgent，所有
运行时依赖通过 PaperQaDeps 显式传入，避免 Agent 间横向依赖和循环导入。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.llm.base import normalize_token_usage
from src.utils import get_logger
from src.utils.llm_json import parse_llm_json

from .contracts import JsonObject
from .Prompts import PAPER_QA_SYSTEM_PROMPT


if TYPE_CHECKING:
    from src.graph.runtime import WorkflowCancellation, WorkflowNodeReporter
    from src.llm import ProviderSnapshot
    from src.llm.base import LLMResponse
    from src.models.workspace import SessionWorkspace, WorkspacePaperEntry
    from src.repositories.sessions.base import SessionRepository


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 模块常量（全部集中在顶部，禁止魔法数字散落函数体）
# ---------------------------------------------------------------------------

# 问答进度事件的 stage 名，对应 runtime.py 里 ("tool","ask_paper") 映射。
QA_STAGE = "ask_paper"

# 组装给模型的论文全文上下文最大字符数，超出按"前 2/3 + 后 1/3"截断。
QA_CONTEXT_MAX_CHARS = 30000

# 全文截断时夹在前后两段中间的省略提示语。
QA_TRUNCATION_MARKER = "[全文中间部分已省略]"

# 模型调用失败时，写进失败原因的错误正文截断长度。
QA_ERROR_DETAIL_CHARS = 120


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

    # 第四步：加载材料。报告直接从工作区取；全文优先从会话产物读，读不到就用摘要代替。
    _check_cancellation(deps)
    deps.reporter.progress("正在准备问答材料", stage=QA_STAGE, event_key=deps.event_key)
    report = entry.deep_read.to_dict()
    fulltext, used_abstract = await _load_fulltext(deps, entry, paper_id)
    if used_abstract:
        # 拿不到全文（摘要降级精读或读取失败），用报告加摘要回答，不报错。
        deps.reporter.progress(
            "未找到全文，使用报告与摘要回答",
            stage=QA_STAGE,
            event_key=deps.event_key,
        )

    # 第五步：组装上下文（超长截断），拼成 JSON 字符串作为 user 消息。
    _check_cancellation(deps)
    truncated = _truncate_fulltext(fulltext)
    user_content = json.dumps(
        {
            "精读报告": report,
            "论文全文": truncated,
            "论文标题": str(entry.paper.get("title") or ""),
            "用户问题": question,
        },
        ensure_ascii=False,
    )

    # 第六步：单次调用模型，温度设 0 追求稳定、可核对的回答。
    _check_cancellation(deps)
    deps.reporter.progress("正在生成回答", stage=QA_STAGE, event_key=deps.event_key)
    messages = [
        {"role": "system", "content": PAPER_QA_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    response = await deps.llm.provider.chat(messages, temperature=0)

    # token 用量累加器：主调用 + 可能的一次 repair 重试都累加到这里。
    total_input = 0
    total_output = 0
    usage = normalize_token_usage(response.usage)
    total_input += usage["input_tokens"]
    total_output += usage["output_tokens"]

    # 模型调用失败 → 折叠成 failed（用量不单独上报，和 deepRead 的失败处理一致）。
    if not response.ok:
        return _fail(deps, _model_error_summary(response))

    # 第七步：解析模型输出。解析失败会带错误信息重试一次（在 _repair 闭包里发生）。
    # 这就是"失败重试1次"，业务层不再自己套重试循环。
    async def _repair(error: str) -> str:
        """带着原始材料、上次的坏输出和解析错误信息让模型重新输出一次。

        中文注释：重试时必须把精读报告和论文材料一起还给模型——只发一句错误
        提示的话，模型看不到材料，只能编一个空洞回答，重试就失去意义了。
        """

        nonlocal total_input, total_output
        repair_messages = [
            {"role": "system", "content": PAPER_QA_SYSTEM_PROMPT},
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
            "used_abstract": used_abstract,
            "answer_chars": len(answer),
            "input_tokens": total_input,
            "output_tokens": total_output,
        },
    )

    return {"answer": answer, "source_sections": source_sections}


# ---------------------------------------------------------------------------
# 材料加载
# ---------------------------------------------------------------------------


async def _load_fulltext(
    deps: PaperQaDeps,
    entry: "WorkspacePaperEntry",
    paper_id: str,
) -> tuple[str, bool]:
    """加载论文全文，读不到就退回摘要。

    返回 (全文文本, 是否用了摘要代替)。优先用精读报告里记录的全文产物编号
    去会话产物里取文件路径，再读文本；任何一步失败或没有全文产物，就用论文
    摘要代替，不报错（摘要降级精读的论文本来就没有全文产物）。
    """

    # 调用方已保证 entry.deep_read 不为 None（在 _run_paper_qa_impl 第二步检查过）。
    report = entry.deep_read
    artifact_id = str(report.fulltext_artifact_id or "").strip() if report is not None else ""
    if artifact_id:
        try:
            # 走仓储的 read_artifact_path 拿路径（自带路径安全校验），放在线程里避免阻塞。
            path = await asyncio.to_thread(
                deps.repo.read_artifact_path, deps.session_key, artifact_id
            )
            if path is not None:
                # 路径拿到再在线程里读文本，避免同步 IO 卡住事件循环。
                text = await asyncio.to_thread(path.read_text, encoding="utf-8")
                if text:
                    logger.info(
                        "问答加载到论文全文",
                        extra={
                            "session_key": deps.session_key,
                            "paper_id": paper_id,
                            "chars": len(text),
                        },
                    )
                    return text, False
        except Exception as exc:
            # 读不到全文不报错，降级用摘要回答（不捕获 CancelledError，它属于 BaseException）。
            logger.warning(
                "读取论文全文产物失败，改用摘要",
                extra={
                    "session_key": deps.session_key,
                    "paper_id": paper_id,
                    "error": str(exc)[:200],
                },
            )

    # 走到这里说明没有全文产物，或者读取失败：用摘要代替。
    abstract = str(entry.paper.get("abstract") or "")
    return abstract, True


# ---------------------------------------------------------------------------
# 上下文截断
# ---------------------------------------------------------------------------


def _truncate_fulltext(text: str) -> str:
    """全文超长时保留前 2/3 和后 1/3，中间用一行省略提示标注。

    全文长度没超过 QA_CONTEXT_MAX_CHARS 时原样返回；超过时按预算的前 2/3 + 后 1/3
    截断，保证模型既能看到开头的方法/背景，也能看到结尾的结论。
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
